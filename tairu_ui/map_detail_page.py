# -*- coding: utf-8 -*-

"""Per-map page: Pull (records → layers, tairudb files → raster) and
Push (layers → records, raster area → tairudb upload)."""

from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QProgressBar,
)

try:
    from ..compat import _USER_ROLE, _exec_dialog
    from .style import (
        INFO, INFO_CONTAINER, ON_SURFACE, ON_SURFACE_VARIANT, apply_tairu_style,
        badge_style, set_action_button, set_muted, set_plain_button,
        set_primary_button, set_section_title, set_title, status_style,
    )
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _USER_ROLE, _exec_dialog
    from tairu_ui.style import (
        INFO, INFO_CONTAINER, ON_SURFACE, ON_SURFACE_VARIANT, apply_tairu_style,
        badge_style, set_action_button, set_muted, set_plain_button,
        set_primary_button, set_section_title, set_title, status_style,
    )

try:
    _TRANSPARENT_MOUSE = Qt.WidgetAttribute.WA_TransparentForMouseEvents
except AttributeError:
    _TRANSPARENT_MOUSE = Qt.WA_TransparentForMouseEvents


class MapDetailPage(QWidget):

    backRequested = pyqtSignal()
    pullRecordsRequested = pyqtSignal(str)        # map_id
    downloadFileRequested = pyqtSignal(str, str)  # map_id, file_name
    pushRecordsRequested = pyqtSignal(str)        # map_id
    pushRasterRequested = pyqtSignal(str)         # map_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._map = None
        self._uid = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self.back_btn = QPushButton('← Mapas')
        self.back_btn.clicked.connect(self.backRequested.emit)
        header.addWidget(self.back_btn)
        self.title_label = set_title(QLabel(''))
        self.title_label.setWordWrap(True)
        header.addWidget(self.title_label, 1)
        layout.addLayout(header)

        # self.role_label = set_muted(QLabel(''))
        # layout.addWidget(self.role_label)

        # ---------------- Pull actions
        layout.addSpacing(8)
        pull_title = set_section_title(QLabel('Receber do Tairu Maps'))
        layout.addWidget(pull_title)

        pull_layout = QVBoxLayout()
        pull_layout.setContentsMargins(0, 0, 0, 0)
        pull_layout.setSpacing(8)

        self.pull_records_btn = set_action_button(QPushButton('Receber Registros'))
        self.pull_records_btn.setToolTip(
            'Baixa os registros do mapa e os carrega como camadas (GeoPackage) no projeto.')
        self.pull_records_btn.clicked.connect(
            lambda: self._map and self.pullRecordsRequested.emit(self._map.map_id))
        pull_layout.addWidget(self.pull_records_btn)

        self.files_btn = set_action_button(QPushButton('Receber arquivos TairuDB'))
        self.files_btn.setToolTip(
            'Abre a lista de arquivos TairuDB disponíveis para baixar e adicionar ao projeto.')
        self.files_btn.clicked.connect(self._open_files_dialog)
        pull_layout.addWidget(self.files_btn)
        layout.addLayout(pull_layout)
        layout.addSpacing(18)

        # ---------------- Push actions
        push_title = set_section_title(QLabel('Enviar para o Tairu Maps'))
        layout.addWidget(push_title)

        push_layout = QVBoxLayout()
        push_layout.setContentsMargins(0, 0, 0, 0)
        push_layout.setSpacing(8)

        self.push_records_btn = set_action_button(QPushButton('Enviar camada vetorial'))
        self.push_records_btn.setToolTip(
            'Converte feições de uma camada vetorial em registros do mapa (com prévia).')
        self.push_records_btn.clicked.connect(
            lambda: self._map and self.pushRecordsRequested.emit(self._map.map_id))
        push_layout.addWidget(self.push_records_btn)

        self.push_raster_btn = set_action_button(QPushButton('Enviar camada raster (.tairudb)'))
        self.push_raster_btn.clicked.connect(
            lambda: self._map and self.pushRasterRequested.emit(self._map.map_id))
        push_layout.addWidget(self.push_raster_btn)
        layout.addLayout(push_layout)

        # ---------------- status
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)
        apply_tairu_style(self)

    # ------------------------------------------------------------------ api

    def set_map(self, tmap, uid):
        self._map = tmap
        self._uid = uid
        self.title_label.setText(tmap.nome or '(sem nome)')
        # role = tmap.role_label(uid)
        # self.role_label.setText(f'Seu papel: {role}')
        # self.role_label.setStyleSheet(badge_style(INFO, INFO_CONTAINER))
        can_edit_files = tmap.can_edit_files(uid)
        self.push_raster_btn.setEnabled(can_edit_files)
        self.push_raster_btn.setToolTip(
            'Gera um .tairudb da área escolhida e envia para o mapa.' if can_edit_files
            else 'Requer papel de proprietário ou administrador do mapa.')
        self.set_busy(False)
        self.set_status('')
        self.update_files(tmap)

    def update_files(self, tmap):
        self._map = tmap
        count = len(tmap.tairudb_remote_files)
        self.files_btn.setText(f'Arquivos TairuDB ({count})')
        self.files_btn.setEnabled(count > 0)

    def set_busy(self, busy, message=None):
        for w in (self.pull_records_btn, self.files_btn, self.push_records_btn,
                  self.back_btn):
            w.setEnabled(not busy)
        if not busy and self._map:
            self.push_raster_btn.setEnabled(self._map.can_edit_files(self._uid))
            self.files_btn.setEnabled(bool(self._map.tairudb_remote_files))
        else:
            self.push_raster_btn.setEnabled(False)
        self.progress.setVisible(busy)
        if not busy:
            self.progress.setValue(0)
        if message is not None:
            self.set_status(message)

    def set_progress(self, fraction, text=''):
        self.progress.setVisible(True)
        self.progress.setValue(int(fraction * 100))
        if text:
            self.set_status(text)

    def set_status(self, message, error=False):
        self.status_label.setStyleSheet(status_style(error))
        self.status_label.setText(message or '')

    # ------------------------------------------------------------- internal

    def _open_files_dialog(self):
        if not self._map:
            return
        dialog = TairuDbFilesDialog(self._map, self)
        name = dialog.selected_file()
        if name:
            self.downloadFileRequested.emit(self._map.map_id, name)


class TairuDbFilesDialog(QDialog):

    def __init__(self, tmap, parent=None):
        super().__init__(parent)
        self._selected_name = None
        self.setWindowTitle(f'Arquivos TairuDB · {tmap.nome or "(sem nome)"}')
        self.resize(520, 420)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(set_title(QLabel('Arquivos TairuDB')))
        subtitle = set_muted(QLabel(tmap.nome or '(sem nome)'))
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.files_list = QListWidget()
        self.files_list.setSpacing(8)
        self.files_list.setToolTip('Clique duas vezes para baixar e adicionar ao projeto.')
        self.files_list.itemDoubleClicked.connect(self._on_file_activated)
        layout.addWidget(self.files_list, 1)

        for name in sorted(tmap.tairudb_remote_files):
            item = QListWidgetItem()
            item.setData(_USER_ROLE, name)
            item.setToolTip(name)
            item.setSizeHint(QSize(0, 62))
            self.files_list.addItem(item)
            self.files_list.setItemWidget(item, TairuDbFileItemWidget(name))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.download_btn = set_primary_button(QPushButton('Receber e adicionar ao projeto'))
        self.download_btn.clicked.connect(self._on_download_clicked)
        buttons.addWidget(self.download_btn)
        cancel_btn = set_plain_button(QPushButton('Cancelar'))
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        self.download_btn.setEnabled(self.files_list.count() > 0)
        if self.files_list.count() > 0:
            self.files_list.setCurrentRow(0)
        apply_tairu_style(self)

    def selected_file(self):
        if _exec_dialog(self):
            return self._selected_name
        return None

    def _current_file(self):
        item = self.files_list.currentItem()
        return item.data(_USER_ROLE) if item else None

    def _on_file_activated(self, item):
        self._selected_name = item.data(_USER_ROLE)
        if self._selected_name:
            self.accept()

    def _on_download_clicked(self):
        self._selected_name = self._current_file()
        if self._selected_name:
            self.accept()


class TairuDbFileItemWidget(QWidget):

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.setAttribute(_TRANSPARENT_MOUSE, True)
        self.setStyleSheet('background: transparent;')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)

        text_area = QVBoxLayout()
        text_area.setSpacing(2)
        title = QLabel(name)
        title.setWordWrap(True)
        title.setStyleSheet(
            f'color: {ON_SURFACE}; font-size: 13px; font-weight: 700;'
            'background: transparent;'
        )
        text_area.addWidget(title)

        subtitle = QLabel('Arquivo TairuDB pronto para adicionar ao projeto')
        subtitle.setStyleSheet(
            f'color: {ON_SURFACE_VARIANT}; font-size: 11px; background: transparent;'
        )
        text_area.addWidget(subtitle)

        layout.addLayout(text_area, 1)
