# -*- coding: utf-8 -*-

"""
Firebase Storage REST client (firebasestorage.googleapis.com/v0).

Uploads use the X-Goog resumable protocol in 8MB chunks so big .tairudb
files get real progress reporting and survive a token refresh mid-transfer
(the upload URL is independent of auth once granted). Auth uses the
'Authorization: Firebase <idToken>' scheme required for Firebase ID tokens.
"""

import json as jsonlib
import os
import urllib.error
import urllib.parse
import urllib.request

try:
    from .http import AuthorizedSession, FirebaseError, USER_AGENT
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_firebase.http import AuthorizedSession, FirebaseError, USER_AGENT

_STORAGE_HOST = 'https://firebasestorage.googleapis.com/v0'

UPLOAD_CHUNK = 8 * 1024 * 1024     # resumable upload chunk
DOWNLOAD_CHUNK = 1024 * 1024       # streamed download read size


class CanceledError(Exception):
    """Raised when a progress callback's cancel check asks to abort."""


class StorageClient:

    def __init__(self, env, token_manager, app_check_manager=None):
        self.env = env
        self._session = AuthorizedSession(token_manager, scheme='Firebase',
                                          app_check_manager=app_check_manager)
        self._base = f'{_STORAGE_HOST}/b/{env.bucket}/o'

    def _object_url(self, object_path):
        return f'{self._base}/{urllib.parse.quote(object_path, safe="")}'

    # ------------------------------------------------------------ metadata

    def get_metadata(self, object_path):
        """Object metadata dict; raises FirebaseError(404) when missing."""
        return self._session.request_json('GET', self._object_url(object_path))

    def exists(self, object_path):
        try:
            self.get_metadata(object_path)
            return True
        except FirebaseError as e:
            if e.http_status == 404:
                return False
            raise

    # ------------------------------------------------------------ download

    def download(self, object_path, dest_path, progress_cb=None, cancel_cb=None):
        """Stream the object to dest_path. progress_cb(bytes_done, bytes_total)."""
        url = f'{self._object_url(object_path)}?alt=media'
        headers = {'User-Agent': USER_AGENT}
        headers.update(self._session.auth_header())
        req = urllib.request.Request(url, headers=headers)

        tmp_path = dest_path + '.part'
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get('Content-Length') or 0)
                done = 0
                with open(tmp_path, 'wb') as out:
                    while True:
                        if cancel_cb and cancel_cb():
                            raise CanceledError()
                        chunk = resp.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress_cb:
                            progress_cb(done, total)
            os.replace(tmp_path, dest_path)
            return dest_path
        except urllib.error.HTTPError as e:
            body = e.read()
            raise _storage_error(e.code, body) from e
        except urllib.error.URLError as e:
            raise FirebaseError('NETWORK', str(e.reason)) from e
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # -------------------------------------------------------------- upload

    def upload_resumable(self, local_path, object_path,
                         content_type='application/x-sqlite3',
                         progress_cb=None, cancel_cb=None):
        """Upload a file with the X-Goog resumable protocol. Returns metadata."""
        size = os.path.getsize(local_path)

        # 1) Start: obtain the session upload URL
        start_url = (f'{self._base}?name={urllib.parse.quote(object_path, safe="")}'
                     f'&uploadType=resumable')
        metadata = jsonlib.dumps({'name': object_path, 'contentType': content_type}).encode('utf-8')
        headers = {
            'User-Agent': USER_AGENT,
            'Content-Type': 'application/json; charset=UTF-8',
            'X-Goog-Upload-Protocol': 'resumable',
            'X-Goog-Upload-Command': 'start',
            'X-Goog-Upload-Header-Content-Length': str(size),
            'X-Goog-Upload-Header-Content-Type': content_type,
        }
        headers.update(self._session.auth_header())

        req = urllib.request.Request(start_url, data=metadata, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                upload_url = resp.headers.get('X-Goog-Upload-URL')
        except urllib.error.HTTPError as e:
            raise _storage_error(e.code, e.read()) from e
        except urllib.error.URLError as e:
            raise FirebaseError('NETWORK', str(e.reason)) from e

        if not upload_url:
            raise FirebaseError('UPLOAD', 'Servidor não retornou URL de upload')

        # 2) Send chunks
        offset = 0
        final_response = None
        with open(local_path, 'rb') as src:
            while True:
                if cancel_cb and cancel_cb():
                    self._cancel_upload(upload_url)
                    raise CanceledError()

                chunk = src.read(UPLOAD_CHUNK)
                is_last = offset + len(chunk) >= size
                command = 'upload, finalize' if is_last else 'upload'
                chunk_headers = {
                    'User-Agent': USER_AGENT,
                    'X-Goog-Upload-Command': command,
                    'X-Goog-Upload-Offset': str(offset),
                }
                req = urllib.request.Request(upload_url, data=chunk, headers=chunk_headers, method='POST')
                try:
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        body = resp.read()
                        if is_last and body:
                            final_response = jsonlib.loads(body.decode('utf-8'))
                except urllib.error.HTTPError as e:
                    raise _storage_error(e.code, e.read()) from e
                except urllib.error.URLError as e:
                    raise FirebaseError('NETWORK', str(e.reason)) from e

                offset += len(chunk)
                if progress_cb:
                    progress_cb(min(offset, size), size)
                if is_last:
                    break

        return final_response or {}

    def _cancel_upload(self, upload_url):
        try:
            req = urllib.request.Request(
                upload_url,
                headers={'User-Agent': USER_AGENT, 'X-Goog-Upload-Command': 'cancel'},
                method='POST',
            )
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            pass  # best effort


def _storage_error(status, body_bytes):
    message = ''
    code = str(status)
    try:
        payload = jsonlib.loads(body_bytes.decode('utf-8'))
        err = payload.get('error', {})
        message = err.get('message', '')
        code = err.get('status') or code
    except Exception:
        message = body_bytes[:300].decode('utf-8', errors='replace') if body_bytes else ''
    return FirebaseError(code, message, http_status=status)
