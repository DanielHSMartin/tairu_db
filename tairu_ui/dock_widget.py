# -*- coding: utf-8 -*-

"""
Tairu Maps dock widget: session controller and page host.

Owns the TokenManager/Firestore/Storage clients for the production environment
and routes between LoginPage, MapsPage and MapDetailPage. All network work
happens in FirebaseTask background tasks; this class only touches widgets.
"""

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
    QMessageBox,
)
from qgis.gui import QgsDockWidget

try:
    from ..tairu_firebase import auth as auth_store
    from ..tairu_firebase.appcheck import AppCheckManager
    from ..tairu_firebase.auth import TokenManager
    from ..tairu_firebase.config import ENVIRONMENTS, DEFAULT_ENVIRONMENT_KEY
    from ..tairu_firebase.firestore import FirestoreClient
    from ..tairu_firebase.models import TairuMap
    from ..tairu_firebase.oauth_loopback import LoopbackServer
    from ..tairu_firebase.storage import StorageClient
    from ..tairu_sync.tasks import run_task, cancel_all_tasks
    from .login_page import LoginPage
    from .maps_page import MapsPage
    from .map_detail_page import MapDetailPage
    from .style import apply_tairu_style, set_link_button, set_muted
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_firebase import auth as auth_store
    from tairu_firebase.appcheck import AppCheckManager
    from tairu_firebase.auth import TokenManager
    from tairu_firebase.config import ENVIRONMENTS, DEFAULT_ENVIRONMENT_KEY
    from tairu_firebase.firestore import FirestoreClient
    from tairu_firebase.models import TairuMap
    from tairu_firebase.oauth_loopback import LoopbackServer
    from tairu_firebase.storage import StorageClient
    from tairu_sync.tasks import run_task, cancel_all_tasks
    from tairu_ui.login_page import LoginPage
    from tairu_ui.maps_page import MapsPage
    from tairu_ui.map_detail_page import MapDetailPage
    from tairu_ui.style import apply_tairu_style, set_link_button, set_muted

_VERSION_LABELS = {'offline': 'Offline', 'online': 'Online', 'realtime': 'Tempo Real'}


class TairuDockWidget(QgsDockWidget):

    def __init__(self, iface, parent=None):
        super().__init__('Tairu Maps', parent)
        self.setObjectName('TairuMapsDock')
        self.iface = iface

        # Session state
        self.env = None
        self.tokens = None
        self.appcheck = None
        self.fs = None
        self.storage = None
        self.maps = {}            # map_id -> TairuMap
        self._loopback = None

        # ----- UI scaffold
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        self.stack = QStackedWidget()
        self.login_page = LoginPage()
        self.maps_page = MapsPage()
        self.detail_page = MapDetailPage()
        self.stack.addWidget(self.login_page)   # index 0
        self.stack.addWidget(self.maps_page)    # index 1
        self.stack.addWidget(self.detail_page)  # index 2
        outer.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        self.account_label = set_muted(QLabel(''))
        footer.addWidget(self.account_label, 1)
        self.signout_btn = set_link_button(QPushButton('Sair'))
        self.signout_btn.clicked.connect(self.sign_out)
        self.signout_btn.hide()
        footer.addWidget(self.signout_btn)
        outer.addLayout(footer)
        apply_tairu_style(container)

        self.setWidget(container)

        # ----- wiring
        self.login_page.browserLogin.connect(self._on_browser_login)
        self.login_page.pasteCodeLogin.connect(self._on_paste_code)
        self.login_page.generateLocalRequested.connect(self._open_local_generate)

        self.maps_page.refreshRequested.connect(self.refresh_maps)
        self.maps_page.mapOpened.connect(self.open_map)

        self.detail_page.backRequested.connect(self.show_maps_page)
        self.detail_page.pullRecordsRequested.connect(self._pull_records)
        self.detail_page.downloadFileRequested.connect(self._download_tairudb)
        self.detail_page.pushRecordsRequested.connect(self._push_records)
        self.detail_page.pushRasterRequested.connect(self._push_raster)

        # ----- restore production session
        self._setup_session()
        self._try_resume()

    # ============================================================== session

    def _setup_session(self):
        if self.tokens:
            self.tokens.sign_out()
        self.env = ENVIRONMENTS[DEFAULT_ENVIRONMENT_KEY]
        self.tokens = TokenManager(self.env, parent=self)
        self.tokens.sessionExpired.connect(self._on_session_expired)
        self.appcheck = AppCheckManager(self.env, self.tokens)
        self.fs = FirestoreClient(self.env, self.tokens, app_check_manager=self.appcheck)
        self.storage = StorageClient(self.env, self.tokens, app_check_manager=self.appcheck)
        self.maps = {}

    def _ensure_session(self):
        if any(item is None for item in (self.env, self.tokens, self.appcheck, self.fs, self.storage)):
            self._setup_session()

    def _try_resume(self):
        self._ensure_session()
        token, _ = auth_store.load_refresh_token(self.env.key)
        if not token:
            self.show_login_page()
            return
        tokens = self.tokens
        self.login_page.set_busy(True, 'Restaurando sessão…')
        run_task(
            'Tairu Maps: restaurando sessão',
            lambda task: tokens.resume_from_refresh_token(token),
            on_success=lambda _: self._post_signin(remember=True),
            on_error=self._on_login_failed,
        )

    def _on_browser_login(self, remember):
        self._ensure_session()
        self._stop_loopback()
        self._loopback = LoopbackServer(self)
        self._remember_browser_login = remember
        self._loopback.credentialsReceived.connect(self._on_loopback_credentials)
        self._loopback.failed.connect(self._on_browser_login_failed)
        port, state = self._loopback.start()
        url = f'{self.env.auth_page}?port={port}&state={state}'
        self.login_page.set_busy(True, 'Conclua o login no navegador…')
        QDesktopServices.openUrl(QUrl(url))

    def _on_loopback_credentials(self, data):
        self._stop_loopback()
        self.tokens.set_session(data.get('idToken') or '', data.get('refreshToken'))
        # Install the App Check token delivered by the browser page (when present).
        ac_token = data.get('appCheckToken')
        if ac_token and self.appcheck:
            self.appcheck.set_initial_token(ac_token, data.get('appCheckExpireTimeMillis'))
        # idToken may be absent in the form-POST fallback: refresh to obtain one
        if not data.get('idToken'):
            self._resume_with_token(data.get('refreshToken'), self._remember_browser_login)
            return
        self._post_signin(self._remember_browser_login)

    def _on_browser_login_failed(self, reason):
        self._stop_loopback()
        self.login_page.set_busy(False)
        self.login_page.set_status(reason)

    def _on_paste_code(self, refresh_token, remember):
        self._resume_with_token(refresh_token, remember)

    def _resume_with_token(self, refresh_token, remember):
        tokens = self.tokens
        self.login_page.set_busy(True, 'Validando credenciais…')
        run_task(
            'Tairu Maps: validando credenciais',
            lambda task: tokens.resume_from_refresh_token(refresh_token),
            on_success=lambda _: self._post_signin(remember),
            on_error=self._on_login_failed,
        )

    def _on_login_failed(self, message):
        self.login_page.set_busy(False)
        self.login_page.clear_password()
        self.login_page.set_status(message)
        self.show_login_page()

    def _post_signin(self, remember):
        """Plan gate + persistence + page switch (GUI thread)."""
        self.login_page.set_busy(False)
        version, allowed = self.tokens.plan_status()
        if not allowed:
            label = _VERSION_LABELS.get(version, version)
            self.login_page.set_status(
                f'Sua conta está no plano {label}. O acesso pelo QGIS requer plano '
                f'Online ou Tempo Real.')
            self.tokens.sign_out()
            auth_store.clear_refresh_token(self.env.key)
            self.show_login_page()
            return

        if remember and self.tokens.refresh_token:
            encrypted = auth_store.save_refresh_token(
                self.env.key, self.tokens.refresh_token, email=self.tokens.email)
            if not encrypted:
                self.iface.messageBar().pushWarning(
                    'Tairu Maps',
                    'Banco de autenticação do QGIS indisponível — credenciais salvas sem criptografia.')
        auth_store.save_environment_key(self.env.key)

        self.account_label.setText(self.tokens.email or self.tokens.uid)
        self.signout_btn.show()
        self.login_page.clear_password()
        self.login_page.set_status('')
        self.show_maps_page()
        self.refresh_maps()

    def _on_session_expired(self, reason):
        self.account_label.setText('')
        self.signout_btn.hide()
        self.login_page.set_status(reason or 'Sessão expirada. Entre novamente.')
        self.show_login_page()

    def sign_out(self):
        self._stop_loopback()
        cancel_all_tasks()
        if self.tokens:
            self.tokens.sign_out()
        if self.appcheck:
            self.appcheck.clear()
        auth_store.clear_refresh_token(self.env.key)
        self.maps = {}
        self.account_label.setText('')
        self.signout_btn.hide()
        self.show_login_page()

    def _stop_loopback(self):
        if self._loopback is not None:
            try:
                self._loopback.credentialsReceived.disconnect()
                self._loopback.failed.disconnect()
            except Exception:
                pass
            self._loopback.stop()
            self._loopback = None

    def shutdown(self):
        """Called by the plugin's unload()."""
        self._stop_loopback()
        cancel_all_tasks()

    # ================================================================= maps

    def show_login_page(self):
        self.stack.setCurrentWidget(self.login_page)

    def show_maps_page(self):
        self.stack.setCurrentWidget(self.maps_page)

    def refresh_maps(self):
        fs, uid = self.fs, self.tokens.uid
        self.maps_page.set_busy(True)
        self.maps_page.set_status('Carregando mapas…')
        run_task(
            'Tairu Maps: carregando mapas',
            lambda task: fs.list_user_maps(uid),
            on_success=self._on_maps_loaded,
            on_error=self._on_maps_failed,
        )

    def _on_maps_loaded(self, rows):
        self.maps = {}
        for map_id, fields in rows:
            self.maps[map_id] = TairuMap.from_fields(map_id, fields)
        self.maps_page.set_busy(False)
        self.maps_page.set_maps(list(self.maps.values()), self.tokens.uid)
        self._load_record_counts()

    def _on_maps_failed(self, message):
        self.maps_page.set_busy(False)
        self.maps_page.set_status(message, error=True)

    def _load_record_counts(self):
        """Best-effort COUNT per map, all in one background task."""
        fs = self.fs
        map_ids = list(self.maps.keys())

        def fetch(task):
            counts = {}
            for i, map_id in enumerate(map_ids):
                if task.isCanceled():
                    break
                try:
                    counts[map_id] = fs.count_records(map_id)
                except Exception:
                    counts[map_id] = None
                task.report((i + 1) / max(1, len(map_ids)))
            return counts

        def apply(counts):
            for map_id, count in counts.items():
                if count is not None:
                    self.maps_page.set_record_count(map_id, count)

        if map_ids:
            run_task('Tairu Maps: contando registros', fetch,
                     on_success=apply, on_error=lambda _msg: None)

    def open_map(self, map_id):
        tmap = self.maps.get(map_id)
        if not tmap:
            return
        self.detail_page.set_map(tmap, self.tokens.uid)
        self.stack.setCurrentWidget(self.detail_page)

    # ====================================================== map actions
    # (implementations live in the pull/push phases; wired here)

    def _pull_records(self, map_id):
        try:
            from ..tairu_sync.pull import start_pull
        except ImportError:
            from tairu_sync.pull import start_pull
        tmap = self.maps.get(map_id)
        if tmap:
            start_pull(self, tmap)

    def _download_tairudb(self, map_id, file_name):
        try:
            from ..tairu_sync.pull import start_tairudb_download
        except ImportError:
            from tairu_sync.pull import start_tairudb_download
        tmap = self.maps.get(map_id)
        if tmap:
            start_tairudb_download(self, tmap, file_name)

    def _push_records(self, map_id):
        try:
            from .push_dialog import open_push_dialog
        except ImportError:
            from tairu_ui.push_dialog import open_push_dialog
        tmap = self.maps.get(map_id)
        if tmap:
            open_push_dialog(self, tmap)

    def _push_raster(self, map_id):
        try:
            from .raster_wizard import open_raster_wizard
        except ImportError:
            from tairu_ui.raster_wizard import open_raster_wizard
        tmap = self.maps.get(map_id)
        if tmap:
            open_raster_wizard(self, tmap)

    def _open_local_generate(self):
        try:
            from .local_generate_wizard import open_local_generate_wizard
        except ImportError:
            from tairu_ui.local_generate_wizard import open_local_generate_wizard
        open_local_generate_wizard(self.iface)

    # ------------------------------------------------------------- helpers

    def notify(self, message, error=False):
        if error:
            self.iface.messageBar().pushCritical('Tairu Maps', message)
        else:
            self.iface.messageBar().pushSuccess('Tairu Maps', message)

    def confirm(self, title, message):
        return QMessageBox.question(self, title, message) == (
            QMessageBox.StandardButton.Yes if hasattr(QMessageBox, 'StandardButton') else QMessageBox.Yes)
