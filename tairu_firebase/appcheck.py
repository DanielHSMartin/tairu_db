# -*- coding: utf-8 -*-

"""
Firebase App Check token manager for the QGIS plugin.

The plugin can't use platform-based attestation (reCAPTCHA, DeviceCheck),
so tokens are minted server-side by the getQgisAppCheckToken Cloud Function,
which the user's Firebase ID token is sufficient to call.

First-sign-in flow (browser page):
  The qgis-auth page initialises App Check with reCAPTCHA before calling
  signInWithPopup, obtains a token after sign-in, and delivers it to QGIS
  via the loopback server.  set_initial_token() installs that token so the
  very first Firestore/Storage calls succeed without an extra round-trip.

Subsequent calls / token refresh:
  get_token() returns the cached token when it has more than MARGIN seconds
  left, otherwise calls the Cloud Function transparently (worker-thread safe).
"""

import json as jsonlib
import threading
import time
import urllib.error
import urllib.request

try:
    from .http import FirebaseError, USER_AGENT
except ImportError:
    from tairu_firebase.http import FirebaseError, USER_AGENT

# Refresh the App Check token this many seconds before it would expire.
_MARGIN_SECONDS = 300

_FUNCTION_NAME = 'getQgisAppCheckToken'


class AppCheckManager:
    """Thread-safe App Check token cache backed by the QGIS Cloud Function."""

    def __init__(self, env, token_manager):
        self._env = env
        self._tokens = token_manager
        self._lock = threading.Lock()
        self._token = None
        self._expires_at = 0.0   # epoch seconds

    # ------------------------------------------------------------------ api

    def set_initial_token(self, token, expire_time_millis=None):
        """Install a token delivered by the browser login page."""
        with self._lock:
            self._token = token or None
            if expire_time_millis:
                self._expires_at = float(expire_time_millis) / 1000.0
            elif token:
                self._expires_at = time.time() + 3600.0   # assume 1 h if unknown

    def clear(self):
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def get_token(self):
        """Return a valid App Check token, fetching one from the Cloud Function if needed.

        Must be called from a worker thread (makes blocking HTTP requests).
        Raises FirebaseError when the Cloud Function call fails.
        """
        with self._lock:
            if self._token and time.time() < self._expires_at - _MARGIN_SECONDS:
                return self._token

        result = self._fetch_from_function()
        token = result.get('token')
        expire_ms = result.get('expireTimeMillis')
        expires_at = float(expire_ms) / 1000.0 if expire_ms else time.time() + 3600.0

        with self._lock:
            self._token = token
            self._expires_at = expires_at

        return token

    # ----------------------------------------------------------- internals

    def _fetch_from_function(self):
        id_token = self._tokens.get_token()
        region = self._env.functions_region
        project_id = self._env.project_id
        url = (f'https://{region}-{project_id}.cloudfunctions.net'
               f'/{_FUNCTION_NAME}')

        body = jsonlib.dumps({'data': {}}).encode('utf-8')
        headers = {
            'User-Agent': USER_AGENT,
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {id_token}',
        }
        if not url.startswith('https://'):
            raise ValueError(f'Only HTTPS URLs are permitted: {url!r}')
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            try:
                payload = jsonlib.loads(body_bytes.decode('utf-8'))
                err = payload.get('error', {})
                code = err.get('status') or str(e.code)
                msg = err.get('message') or ''
            except Exception:
                code, msg = str(e.code), ''
            raise FirebaseError(code, msg, http_status=e.code) from e
        except urllib.error.URLError as e:
            raise FirebaseError('NETWORK', str(e.reason)) from e
        except TimeoutError as e:
            raise FirebaseError('NETWORK', 'timeout') from e

        # Callable functions wrap the return value in {"result": {...}}
        payload = jsonlib.loads(raw.decode('utf-8'))
        result = payload.get('result') or payload
        if not result.get('token'):
            raise FirebaseError('APP_CHECK_ERROR',
                                'getQgisAppCheckToken não retornou um token')
        return result
