# -*- coding: utf-8 -*-

"""Maps list page: every map the signed-in user is a member of, with role
badge, tairudb file count and (best-effort) record count."""

from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QCheckBox,
)

try:
    from ..compat import _USER_ROLE
    from .style import (
        ERROR, ON_PRIMARY, SECONDARY_CONTAINER, ON_SECONDARY_CONTAINER,
        SURFACE_CONTAINER, WARNING, WARNING_CONTAINER,
        apply_tairu_style, badge_style, set_primary_button, set_title,
        status_style,
    )
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _USER_ROLE
    from tairu_ui.style import (
        ERROR, ON_PRIMARY, SECONDARY_CONTAINER, ON_SECONDARY_CONTAINER,
        SURFACE_CONTAINER, WARNING, WARNING_CONTAINER,
        apply_tairu_style, badge_style, set_primary_button, set_title,
        status_style,
    )

try:
    _TRANSPARENT_MOUSE = Qt.WidgetAttribute.WA_TransparentForMouseEvents
except AttributeError:
    _TRANSPARENT_MOUSE = Qt.WA_TransparentForMouseEvents

try:
    _STYLED_BACKGROUND = Qt.WidgetAttribute.WA_StyledBackground
except AttributeError:
    _STYLED_BACKGROUND = Qt.WA_StyledBackground

_MAP_LIST_STYLE = """
QListWidget#TairuMapsList {
    background: transparent;
    border: none;
    outline: none;
}

QListWidget#TairuMapsList::item {
    background: transparent;
    border: 0px;
    margin: 0px;
    padding: 0px;
}

QListWidget#TairuMapsList::item:hover,
QListWidget#TairuMapsList::item:selected {
    background: transparent;
    border: 0px;
}
"""


class MapsPage(QWidget):

    mapOpened = pyqtSignal(str)      # map_id
    refreshRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._maps = {}        # map_id -> TairuMap
        self._counts = {}      # map_id -> record count
        self._uid = None

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        title = set_title(QLabel('Meus mapas'))
        header.addWidget(title)
        header.addStretch(1)
        self.refresh_btn = set_primary_button(QPushButton('Atualizar'))
        self.refresh_btn.clicked.connect(self.refreshRequested.emit)
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        self.archived_check = QCheckBox('Mostrar mapas arquivados')
        self.archived_check.toggled.connect(lambda _: self._rebuild())
        layout.addWidget(self.archived_check)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName('TairuMapsList')
        self.list_widget.setSpacing(8)
        self.list_widget.setStyleSheet(_MAP_LIST_STYLE)
        self.list_widget.setWordWrap(False)
        self.list_widget.itemClicked.connect(self._on_item_activated)
        layout.addWidget(self.list_widget, 1)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        layout.addWidget(self.status_label)
        apply_tairu_style(self)

    # ------------------------------------------------------------------ api

    def set_maps(self, maps, uid):
        """maps: list of TairuMap models."""
        self._maps = {m.map_id: m for m in maps}
        self._uid = uid
        self._rebuild()

    def set_record_count(self, map_id, count):
        self._counts[map_id] = count
        self._rebuild()

    def set_busy(self, busy):
        self.refresh_btn.setEnabled(not busy)
        self.list_widget.setEnabled(not busy)

    def set_status(self, message, error=False):
        self.status_label.setStyleSheet(status_style(error))
        self.status_label.setText(message or '')
        self.status_label.setVisible(bool(message))

    # ------------------------------------------------------------- internal

    def _rebuild(self):
        self.list_widget.clear()
        show_archived = self.archived_check.isChecked()
        visible = 0
        for tmap in sorted(self._maps.values(), key=lambda m: (m.nome or '').lower()):
            if tmap.is_deleted:
                continue
            if tmap.status == 'archived' and not show_archived:
                continue
            visible += 1
            count = self._counts.get(tmap.map_id)
            files = len(tmap.tairudb_remote_files)
            widget = MapListItemWidget(tmap, self._uid, files, count)
            item = QListWidgetItem()
            item.setData(_USER_ROLE, tmap.map_id)
            item.setToolTip('Clique para abrir este mapa.')
            item.setSizeHint(QSize(0, 128))
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, widget)
        if visible == 0:
            self.set_status('Nenhum mapa encontrado. Crie um mapa no aplicativo Tairu Maps.')
        else:
            self.set_status('')

    def _on_item_activated(self, item):
        map_id = item.data(_USER_ROLE)
        if map_id:
            self.mapOpened.emit(map_id)


class MapListItemWidget(QWidget):

    def __init__(self, tmap, uid, file_count, record_count, parent=None):
        super().__init__(parent)
        self.setAttribute(_TRANSPARENT_MOUSE, True)
        self.setAttribute(_STYLED_BACKGROUND, True)
        self.setObjectName('TairuCardWidget')
        self.setMinimumHeight(128)
        self.setStyleSheet("""
            QWidget#TairuCardWidget {
                background-color: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #006A43,
                    stop:1 #004D2E
                );
                border-radius: 8px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self._badge(self._sharing_text(tmap), ON_PRIMARY, 'rgba(0, 0, 0, 90)'))
        if tmap.active_alert_count:
            top.addWidget(self._badge(
                self._alerts_text(tmap), ON_PRIMARY,
                ERROR if tmap.has_emergency_alert else '#F57C00'))
        top.addStretch(1)
        top.addWidget(self._badge(tmap.role_label(uid), ON_PRIMARY, 'rgba(0, 0, 0, 90)'))
        if tmap.status == 'archived':
            top.addWidget(self._badge('Arquivado', '#4B5563', SURFACE_CONTAINER))
        layout.addLayout(top)

        title_area = QVBoxLayout()
        title_area.setSpacing(3)
        title = QLabel(tmap.nome or '(sem nome)')
        title.setWordWrap(True)
        title.setStyleSheet(
            f'color: {ON_PRIMARY}; font-weight: 800; font-size: 16px;'
            'background: transparent;'
        )
        title_area.addWidget(title)
        plan = QLabel(self._plan_text(tmap))
        plan.setStyleSheet(
            'color: rgba(255, 255, 255, 210); font-size: 12px;'
            'font-weight: 500; background: transparent;'
        )
        title_area.addWidget(plan)
        layout.addLayout(title_area)

        details = QHBoxLayout()
        details.setSpacing(6)
        details.addWidget(self._badge(
            self._files_text(file_count), ON_SECONDARY_CONTAINER, SECONDARY_CONTAINER))
        details.addWidget(self._badge(self._records_text(record_count), WARNING, WARNING_CONTAINER))
        details.addStretch(1)
        layout.addLayout(details)

    def _badge(self, text, color, background):
        label = QLabel(text)
        label.setStyleSheet(badge_style(color, background))
        return label

    def _sharing_text(self, tmap):
        member_count = tmap.member_count()
        limit = self._member_limit(tmap.plan_version)
        if member_count > 1 and limit is not None:
            return f'Compartilhado com {member_count}/{limit}'
        if member_count > 1:
            return f'Compartilhado com {member_count}'
        return 'Privado'

    def _member_limit(self, plan_version):
        return {'online': 20, 'realtime': 50}.get((plan_version or '').lower())

    def _alerts_text(self, tmap):
        count = int(tmap.active_alert_count or 0)
        if count == 1:
            return '1 alerta ativo'
        return f'{count} alertas ativos'

    def _plan_text(self, tmap):
        label = {
            'offline': 'Offline',
            'online': 'Online',
            'realtime': 'Realtime',
        }.get((tmap.plan_version or '').lower(), tmap.plan_version or 'Online')
        return f'Plano: {label}'

    def _files_text(self, count):
        return f'{count} arquivo TairuDB' if count == 1 else f'{count} arquivos TairuDB'

    def _records_text(self, count):
        if count is None:
            return 'Registros: —'
        return f'{count} registro' if count == 1 else f'{count} registros'
