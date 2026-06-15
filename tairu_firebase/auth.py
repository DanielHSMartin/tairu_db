# -*- coding: utf-8 -*-

"""
Firebase Authentication via the Identity Toolkit / Secure Token REST APIs,
plus session persistence in the QGIS auth database (encrypted) with a
QgsSettings fallback.

Custom claims (appVersion / versionExpiresAt) ride inside the ID token, so
the same plan gate enforced by the Firestore rules can be checked locally.
"""

import base64
import json
import threading
import time

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsSettings

try:
    from .config import ALLOWED_APP_VERSIONS
    from .http import request_json, FirebaseError
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_firebase.config import ALLOWED_APP_VERSIONS
    from tairu_firebase.http import request_json, FirebaseError

_IDENTITY_BASE = 'https://identitytoolkit.googleapis.com/v1'
_SECURE_TOKEN_URL = 'https://securetoken.googleapis.com/v1/token'

# Refresh the ID token when it has less than this many seconds left
_REFRESH_MARGIN_SECONDS = 300


def decode_jwt_claims(id_token):
    """Decode the payload segment of a JWT without verifying the signature.

    Signature verification is unnecessary client-side: the token is only
    presented back to Google services, which verify it themselves.
    """
    try:
        payload = id_token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode('utf-8'))
    except Exception:
        return {}


def plan_status(claims, now=None):
    """Replicates the rules' hasValidVersion(): returns (effective_version, allowed)."""
    now = now if now is not None else time.time()
    version = claims.get('appVersion')
    expires_at = claims.get('versionExpiresAt')
    if version is None:
        effective = 'offline'
    elif expires_at is not None and float(expires_at) < now:
        effective = 'offline'
    else:
        effective = version
    return effective, effective in ALLOWED_APP_VERSIONS


class AuthClient:
    """Stateless REST calls against the Identity Toolkit API for one environment."""

    def __init__(self, env):
        self.env = env

    def sign_in_password(self, email, password):
        """Returns the raw response: idToken, refreshToken, expiresIn, localId, email."""
        return request_json(
            'POST',
            f'{_IDENTITY_BASE}/accounts:signInWithPassword?key={self.env.api_key}',
            json_body={'email': email, 'password': password, 'returnSecureToken': True},
        )

    def refresh(self, refresh_token):
        """Returns: id_token, refresh_token, expires_in, user_id (snake_case keys)."""
        return request_json(
            'POST',
            f'{_SECURE_TOKEN_URL}?key={self.env.api_key}',
            data=f'grant_type=refresh_token&refresh_token={refresh_token}'.encode('ascii'),
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )

    def lookup(self, id_token):
        """Account info for the signed-in user (email, providers, display name)."""
        return request_json(
            'POST',
            f'{_IDENTITY_BASE}/accounts:lookup?key={self.env.api_key}',
            json_body={'idToken': id_token},
        )


class TokenManager(QObject):
    """Holds the active session and transparently refreshes the ID token.

    get_token()/force_refresh() are thread-safe and are called from QgsTask
    worker threads; signals are delivered queued to the GUI thread.
    """

    tokenRefreshed = pyqtSignal()
    sessionExpired = pyqtSignal(str)  # reason

    def __init__(self, env, parent=None):
        super().__init__(parent)
        self.env = env
        self._auth = AuthClient(env)
        self._lock = threading.Lock()
        self._id_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self.uid = None
        self.email = None
        self.claims = {}

    # ------------------------------------------------------------- session

    @property
    def is_signed_in(self):
        return bool(self._refresh_token)

    @property
    def refresh_token(self):
        return self._refresh_token

    def set_session(self, id_token, refresh_token, expires_in=None):
        """Install a session obtained from any sign-in flow (password, browser)."""
        with self._lock:
            self._install(id_token, refresh_token, expires_in)

    def sign_in_password(self, email, password):
        data = self._auth.sign_in_password(email, password)
        with self._lock:
            self._install(data['idToken'], data['refreshToken'], data.get('expiresIn'))
        return data

    def resume_from_refresh_token(self, refresh_token):
        """Restore a persisted session; raises FirebaseError if revoked/invalid."""
        data = self._auth.refresh(refresh_token)
        with self._lock:
            self._install(data['id_token'], data['refresh_token'], data.get('expires_in'))

    def sign_out(self):
        with self._lock:
            self._id_token = None
            self._refresh_token = None
            self._expires_at = 0.0
            self.uid = None
            self.email = None
            self.claims = {}

    def _install(self, id_token, refresh_token, expires_in):
        self._id_token = id_token
        self._refresh_token = refresh_token
        try:
            ttl = float(expires_in) if expires_in else 3600.0
        except (TypeError, ValueError):
            ttl = 3600.0
        self._expires_at = time.time() + ttl
        self.claims = decode_jwt_claims(id_token)
        self.uid = self.claims.get('user_id') or self.claims.get('sub')
        self.email = self.claims.get('email') or self.email

    # -------------------------------------------------------------- tokens

    def get_token(self):
        """Current ID token, refreshing first when close to expiry."""
        with self._lock:
            if not self._refresh_token:
                raise FirebaseError('UNAUTHENTICATED', 'não autenticado', http_status=401)
            if time.time() > self._expires_at - _REFRESH_MARGIN_SECONDS:
                self._refresh_locked()
            return self._id_token

    def force_refresh(self):
        """Refresh now regardless of expiry (used after a 401)."""
        with self._lock:
            if not self._refresh_token:
                raise FirebaseError('UNAUTHENTICATED', 'não autenticado', http_status=401)
            self._refresh_locked()
            return self._id_token

    def _refresh_locked(self):
        try:
            data = self._auth.refresh(self._refresh_token)
        except FirebaseError as e:
            # Refresh token revoked/expired: the session is over.
            self.sign_out_locked_safe()
            self.sessionExpired.emit(e.user_message())
            raise
        self._install(data['id_token'], data['refresh_token'], data.get('expires_in'))
        self.tokenRefreshed.emit()

    def sign_out_locked_safe(self):
        self._id_token = None
        self._refresh_token = None
        self._expires_at = 0.0

    # ---------------------------------------------------------------- plan

    def plan_status(self):
        """(effective_version, allowed) from the current token's custom claims."""
        return plan_status(self.claims)


# --------------------------------------------------------------- persistence

_AUTHDB_KEY = 'tairu_db/{env}/refreshToken'
_SETTINGS_TOKEN_KEY = 'tairu_db/{env}/refresh_token'
_SETTINGS_EMAIL_KEY = 'tairu_db/{env}/email'
_SETTINGS_ENV_KEY = 'tairu_db/environment'


def save_refresh_token(env_key, refresh_token, email=None):
    """Persist the refresh token, preferring the encrypted QGIS auth database."""
    stored_encrypted = False
    try:
        mgr = QgsApplication.authManager()
        if mgr and not mgr.isDisabled():
            stored_encrypted = bool(mgr.storeAuthSetting(_AUTHDB_KEY.format(env=env_key), refresh_token, True))
    except Exception:
        stored_encrypted = False

    settings = QgsSettings()
    if stored_encrypted:
        settings.remove(_SETTINGS_TOKEN_KEY.format(env=env_key))
    else:
        # Plaintext fallback when the auth database is unavailable
        settings.setValue(_SETTINGS_TOKEN_KEY.format(env=env_key), refresh_token)
    if email:
        settings.setValue(_SETTINGS_EMAIL_KEY.format(env=env_key), email)
    return stored_encrypted


def load_refresh_token(env_key):
    """Returns (refresh_token or None, email or None)."""
    token = None
    try:
        mgr = QgsApplication.authManager()
        if mgr and not mgr.isDisabled():
            value = mgr.authSetting(_AUTHDB_KEY.format(env=env_key), '', True)
            token = value or None
    except Exception:
        token = None

    settings = QgsSettings()
    if not token:
        token = settings.value(_SETTINGS_TOKEN_KEY.format(env=env_key)) or None
    email = settings.value(_SETTINGS_EMAIL_KEY.format(env=env_key)) or None
    return token, email


def clear_refresh_token(env_key):
    try:
        mgr = QgsApplication.authManager()
        if mgr and not mgr.isDisabled():
            mgr.removeAuthSetting(_AUTHDB_KEY.format(env=env_key))
    except Exception:
        pass
    settings = QgsSettings()
    settings.remove(_SETTINGS_TOKEN_KEY.format(env=env_key))
    settings.remove(_SETTINGS_EMAIL_KEY.format(env=env_key))


def save_environment_key(env_key):
    QgsSettings().setValue(_SETTINGS_ENV_KEY, env_key)


def load_environment_key(default='prod'):
    return QgsSettings().value(_SETTINGS_ENV_KEY, default) or default
