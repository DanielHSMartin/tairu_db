# -*- coding: utf-8 -*-

"""
Minimal urllib-based HTTP helper for the Google/Firebase REST APIs.

Worker-thread safe (no Qt networking); used from QgsTask.run() bodies.
"""

import json as jsonlib
import urllib.error
import urllib.request

USER_AGENT = 'TairuDB-QGIS-Plugin'


class FirebaseError(Exception):
    """A non-2xx response from a Firebase/Google REST endpoint."""

    def __init__(self, code, message, http_status=None):
        self.code = code                # e.g. 'PERMISSION_DENIED', 'EMAIL_NOT_FOUND'
        self.message = message
        self.http_status = http_status
        super().__init__(f"{code}: {message}")

    @property
    def is_auth_error(self):
        return self.http_status in (401,) or self.code in ('UNAUTHENTICATED', 'TOKEN_EXPIRED', 'INVALID_ID_TOKEN')

    @property
    def is_permission_denied(self):
        return self.http_status == 403 or self.code == 'PERMISSION_DENIED'

    def user_message(self):
        """Readable Portuguese message for the most common failure modes."""
        if self.is_permission_denied:
            return ("Permissão negada pelo servidor. Verifique se sua conta possui plano "
                    "Online ou Tempo Real e se você tem o papel necessário nesta expedição.")
        if 'App Check' in (self.message or ''):
            return ("Verificação de segurança (App Check) falhou. "
                    "Tente entrar novamente pelo navegador.")
        if self.is_auth_error:
            return "Sessão expirada. Entre novamente."
        if self.code == 'ALREADY_EXISTS' or self.http_status == 409:
            return self.message or 'Já existe um arquivo com esse nome nesta expedição.'
        translations = {
            'EMAIL_NOT_FOUND': 'E-mail não cadastrado.',
            'INVALID_PASSWORD': 'Senha incorreta.',  # pragma: allowlist secret
            'INVALID_LOGIN_CREDENTIALS': 'E-mail ou senha incorretos.',
            'USER_DISABLED': 'Esta conta foi desativada.',
            'TOO_MANY_ATTEMPTS_TRY_LATER': 'Muitas tentativas. Tente novamente mais tarde.',
            'NETWORK': 'Falha de rede. Verifique sua conexão com a internet.',
        }
        for key, msg in translations.items():
            if key in (self.code or ''):
                return msg
        return f"Erro do servidor: {self.message or self.code}"


def _parse_error(status, body_bytes):
    code = str(status)
    message = ''
    try:
        payload = jsonlib.loads(body_bytes.decode('utf-8'))
        err = payload.get('error', {})
        if isinstance(err, dict):
            message = err.get('message', '') or ''
            code = err.get('status') or message.split(' ')[0] or code
        else:
            message = str(err)
    except Exception:
        message = body_bytes[:300].decode('utf-8', errors='replace') if body_bytes else ''
    return FirebaseError(code, message, http_status=status)


def request_json(method, url, headers=None, json_body=None, data=None, timeout=30):
    """Perform an HTTP request and return the parsed JSON response.

    json_body: dict serialized as the request body (sets Content-Type).
    data: raw bytes body (caller sets Content-Type via headers).
    Raises FirebaseError on non-2xx responses or network failures.
    """
    if not url.startswith('https://'):
        raise ValueError(f'Only HTTPS URLs are permitted: {url!r}')
    all_headers = {'User-Agent': USER_AGENT}
    if headers:
        all_headers.update(headers)

    body = data
    if json_body is not None:
        body = jsonlib.dumps(json_body).encode('utf-8')
        all_headers.setdefault('Content-Type', 'application/json')

    req = urllib.request.Request(url, data=body, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            raw = resp.read()
            if not raw:
                return {}
            return jsonlib.loads(raw.decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise _parse_error(e.code, e.read()) from e
    except urllib.error.URLError as e:
        raise FirebaseError('NETWORK', str(e.reason), http_status=None) from e
    except TimeoutError as e:
        raise FirebaseError('NETWORK', 'timeout', http_status=None) from e


class AuthorizedSession:
    """Injects the Firebase ID token (and App Check token when available).

    app_check_manager is optional; when supplied its get_token() is called on
    every request and the result is sent as X-Firebase-AppCheck.  A failure
    in get_token() is re-raised so the caller sees a meaningful error instead
    of a silent 401 from Firebase.
    """

    def __init__(self, token_manager, scheme='Bearer', app_check_manager=None):
        self._tokens = token_manager
        self._scheme = scheme  # 'Bearer' for Firestore, 'Firebase' for Storage
        self._appcheck = app_check_manager

    def auth_header(self):
        headers = {'Authorization': f'{self._scheme} {self._tokens.get_token()}'}
        if self._appcheck is not None:
            ac_token = self._appcheck.get_token()
            if ac_token:
                headers['X-Firebase-AppCheck'] = ac_token
        return headers

    def request_json(self, method, url, headers=None, json_body=None, data=None, timeout=30):
        merged = dict(headers or {})
        merged.update(self.auth_header())
        try:
            return request_json(method, url, headers=merged, json_body=json_body, data=data, timeout=timeout)
        except FirebaseError as e:
            if not e.is_auth_error:
                raise
            # Token may have just expired: force one refresh and retry once.
            self._tokens.force_refresh()
            merged.update(self.auth_header())
            return request_json(method, url, headers=merged, json_body=json_body, data=data, timeout=timeout)
