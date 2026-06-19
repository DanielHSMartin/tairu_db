# -*- coding: utf-8 -*-

"""
Raster push wizard: extent → parameters → estimate (size gate) → generate +
upload + register on the map document.

Generation runs on the GUI thread (TileRenderEngine pumps the event loop —
same constraint as the Processing algorithm's FlagNoThreading); only the
Storage upload and the Firestore commit run as a background task.
"""

import datetime
import os

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QFormLayout, QLabel, QRadioButton,
    QPushButton, QComboBox, QSpinBox, QLineEdit, QPlainTextEdit, QProgressBar,
    QGroupBox, QScrollArea, QCheckBox, QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsMapLayerComboBox

try:
    from ..compat import _POLYGON_LAYER_FILTER, _RASTER_LAYER_TYPE, _VECTOR_TILE_LAYER_TYPE, _exec_dialog
    from ..tairu_core.feedback import FeedbackAdapter
    from ..tairu_core.generator import GenerationSpec, TileRenderEngine, estimate, format_estimate_report
    from ..tairu_core.tile_math import compute_region_tiles
    from ..tairu_core.vector_export import export_vector_layers
    from ..tairu_core.workspace import map_workspace, slugify_filename
    from ..tairu_firebase.config import TAIRUDB_OBJECT_PATH
    from ..tairu_firebase.http import FirebaseError
    from ..tairu_firebase.models import now_millis
    from ..tairu_sync.tasks import run_task
    from .extent_tool import ExtentPicker
    from .style import (
        apply_combo_popup_style, apply_tairu_style, set_action_button,
        set_control_enabled, set_muted, set_plain_button, set_primary_button,
        status_style, SCROLLBAR_STYLE,
    )
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _POLYGON_LAYER_FILTER, _RASTER_LAYER_TYPE, _VECTOR_TILE_LAYER_TYPE, _exec_dialog
    from tairu_core.feedback import FeedbackAdapter
    from tairu_core.generator import GenerationSpec, TileRenderEngine, estimate, format_estimate_report
    from tairu_core.tile_math import compute_region_tiles
    from tairu_core.vector_export import export_vector_layers
    from tairu_core.workspace import map_workspace, slugify_filename
    from tairu_firebase.config import TAIRUDB_OBJECT_PATH
    from tairu_firebase.http import FirebaseError
    from tairu_firebase.models import now_millis
    from tairu_sync.tasks import run_task
    from tairu_ui.extent_tool import ExtentPicker
    from tairu_ui.style import (
        apply_combo_popup_style, apply_tairu_style, set_action_button,
        set_control_enabled, set_muted, set_plain_button, set_primary_button,
        status_style, SCROLLBAR_STYLE,
    )

_VECTOR_LIST_STYLE = f"""
QScrollArea {{
    border: none;
    background: transparent;
}}
QWidget#VectorScrollContent {{
    background: transparent;
}}
QCheckBox {{
    padding: 6px 8px;
    spacing: 8px;
    border: 1px solid transparent;
    border-radius: 6px;
}}
QCheckBox:hover {{
    background: rgba(0, 106, 67, 0.08);
    border-color: rgba(0, 106, 67, 0.2);
}}
"""

_RESOLUTIONS = [
    ('Altíssima (0,5 m/px)', 18), ('Alta (1 m/px)', 17), ('Médio Alta (2 m/px)', 16),
    ('Média (4 m/px)', 15), ('Médio Baixa (8 m/px)', 14), ('Baixa (16 m/px)', 13),
    ('Muito Baixa (32 m/px)', 12),
]
_FORMATS = ['PNG', 'JPG', 'WEBP']

_SOFT_LIMIT_MB = 90    # estimate gate (Storage rules hard-cap at 100MB)
_HARD_LIMIT_BYTES = 100 * 1024 * 1024


def open_raster_wizard(dock, tmap):
    wizard = RasterWizard(dock, tmap)
    _exec_dialog(wizard)


class WizardFeedback(FeedbackAdapter):
    """Routes engine feedback into the run page; widget calls are guarded so a
    closed wizard can't crash an unwinding generation."""

    def __init__(self, progress_bar, log_fn):
        self._bar = progress_bar
        self._log = log_fn
        self.canceled = False

    def set_progress(self, value):
        try:
            self._bar.setValue(int(value))
        except RuntimeError:
            pass

    def set_progress_text(self, text):
        self.push_info(text)

    def push_info(self, text):
        try:
            if text:
                self._log(text)
        except RuntimeError:
            pass

    def report_error(self, text, fatal=False):
        self.push_info(f'ERRO: {text}')

    def is_canceled(self):
        return self.canceled


class RasterWizard(QWizard):

    def __init__(self, dock, tmap):
        super().__init__(dock)
        self.dock = dock
        self.tmap = tmap
        self.setWindowTitle(f'Gerar e enviar raster · {tmap.nome}')
        self.resize(640, 560)

        # Cross-page state
        self.polygons_wgs84 = []
        self.region_result = None
        self.estimate_result = None
        self.feedback = None
        self.upload_done = False

        self.extent_page = ExtentPage(self)
        self.params_page = ParamsPage(self)
        self.estimate_page = EstimatePage(self)
        self.run_page = RunPage(self)
        self.addPage(self.extent_page)
        self.addPage(self.params_page)
        self.addPage(self.estimate_page)
        self.addPage(self.run_page)

        self.rejected.connect(self._on_rejected)
        apply_tairu_style(self)
        self._style_wizard_buttons()

    def _wizard_button_id(self, name):
        enum = getattr(QWizard, 'WizardButton', None)
        return getattr(enum, name) if enum is not None else getattr(QWizard, name)

    def _style_wizard_buttons(self):
        labels = {
            'BackButton': 'Voltar',
            'NextButton': 'Avançar',
            'CancelButton': 'Cancelar',
            'FinishButton': 'Concluir',
            'CommitButton': 'Enviar',
        }
        primary = {'NextButton', 'FinishButton', 'CommitButton'}
        for name, label in labels.items():
            try:
                button_id = self._wizard_button_id(name)
                self.setButtonText(button_id, label)
                button = self.button(button_id)
                if button is not None and name in primary:
                    set_primary_button(button)
                elif button is not None:
                    set_plain_button(button)
            except Exception:
                pass

    def visible_basemap_layers(self):
        """Visible raster and vector-tile layers, in the project's draw order.

        Vector-tile basemaps are rendered into the raster tiles just like raster
        layers (regular vector layers are exported as features instead). Using
        layerOrder() preserves the project hierarchy so overlays composite on top
        of the basemap correctly.
        """
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        layers = []
        for layer in root.layerOrder():
            node = root.findLayer(layer.id())
            if node is None or not node.isVisible():
                continue
            if layer.type() in (_RASTER_LAYER_TYPE, _VECTOR_TILE_LAYER_TYPE):
                layers.append(layer)
        return layers

    def _on_rejected(self):
        if self.feedback is not None:
            self.feedback.canceled = True
        self.extent_page.stop_picker()


# ------------------------------------------------------------------ page 1

class ExtentPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Área de interesse')
        self.setSubTitle('Escolha a área que será convertida em tiles raster.')
        self.drawn_rect = None   # QgsRectangle in project CRS
        self._picker = None

        layout = QVBoxLayout(self)
        self.layer_radio = QRadioButton('Usar polígono(s) de uma camada (uma região por feição)')
        self.layer_radio.setChecked(True)
        layout.addWidget(self.layer_radio)

        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(_POLYGON_LAYER_FILTER)
        apply_combo_popup_style(self.layer_combo)
        layout.addWidget(self.layer_combo)

        self.draw_radio = QRadioButton('Desenhar um retângulo no mapa')
        layout.addWidget(self.draw_radio)

        self.draw_btn = set_action_button(QPushButton('Desenhar no mapa…'))
        self.draw_btn.setEnabled(False)
        self.draw_btn.clicked.connect(self._start_picker)
        layout.addWidget(self.draw_btn)

        self.drawn_label = set_muted(QLabel(''))
        layout.addWidget(self.drawn_label)

        self.canvas_radio = QRadioButton('Usar área visível do mapa')
        layout.addWidget(self.canvas_radio)

        layout.addStretch(1)

        self.layer_radio.toggled.connect(self._sync_controls)
        self.draw_radio.toggled.connect(self._sync_controls)
        self.canvas_radio.toggled.connect(self._sync_controls)
        self.layer_combo.layerChanged.connect(lambda _: self.completeChanged.emit())

    def _sync_controls(self):
        use_layer = self.layer_radio.isChecked()
        use_draw = self.draw_radio.isChecked()
        self.layer_combo.setEnabled(use_layer)
        self.draw_btn.setEnabled(use_draw)
        self.completeChanged.emit()

    def _start_picker(self):
        canvas = self._wizard.dock.iface.mapCanvas()
        self._picker = ExtentPicker(canvas, self)
        self._picker.extentPicked.connect(self._on_extent_picked)
        self._wizard.hide()
        self._picker.start()

    def _on_extent_picked(self, rect):
        self.drawn_rect = rect
        self.drawn_label.setText(
            f'Retângulo: {rect.xMinimum():.5f}, {rect.yMinimum():.5f} — '
            f'{rect.xMaximum():.5f}, {rect.yMaximum():.5f} (CRS do projeto)')
        self._wizard.show()
        self._wizard.raise_()
        self.completeChanged.emit()

    def stop_picker(self):
        if self._picker is not None:
            self._picker.stop()
            self._picker = None

    def isComplete(self):
        if self.draw_radio.isChecked():
            return self.drawn_rect is not None and not self.drawn_rect.isEmpty()
        if self.canvas_radio.isChecked():
            return True
        return self.layer_combo.currentLayer() is not None

    def polygons_wgs84(self):
        """Selected area as WGS84 polygons (one per region)."""
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        ctx = QgsProject.instance().transformContext()
        polygons = []
        if self.draw_radio.isChecked():
            transform = QgsCoordinateTransform(QgsProject.instance().crs(), wgs84, ctx)
            geom = QgsGeometry.fromRect(self.drawn_rect)
            geom.transform(transform)
            polygons.append(geom)
        elif self.canvas_radio.isChecked():
            canvas = self._wizard.dock.iface.mapCanvas()
            transform = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), wgs84, ctx)
            geom = QgsGeometry.fromRect(canvas.extent())
            geom.transform(transform)
            polygons.append(geom)
        else:
            layer = self.layer_combo.currentLayer()
            transform = QgsCoordinateTransform(layer.crs(), wgs84, ctx)
            for feature in layer.getFeatures():
                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    continue
                geom = QgsGeometry(geom)
                geom.transform(transform)
                polygons.append(geom)
        return polygons


# ------------------------------------------------------------------ page 2

class ParamsPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Parâmetros')

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.resolution_combo = QComboBox()
        for label, zoom in _RESOLUTIONS:
            self.resolution_combo.addItem(label, zoom)
        apply_combo_popup_style(self.resolution_combo)
        form.addRow('Resolução:', self.resolution_combo)

        self.format_combo = QComboBox()
        for fmt in _FORMATS:
            self.format_combo.addItem(fmt)
        self.format_combo.setCurrentIndex(1)  # JPG
        apply_combo_popup_style(self.format_combo)
        form.addRow('Formato:', self.format_combo)

        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setSingleStep(5)
        self.quality_spin.setValue(90)
        self.quality_spin.setSuffix('%')
        self.quality_spin.setAccelerated(True)
        self.quality_spin.setMinimumWidth(84)
        self.quality_spin.setMaximumWidth(110)
        form.addRow('Qualidade (JPG/WebP):', self.quality_spin)
        self.format_combo.currentTextChanged.connect(self._sync_quality_state)
        self._sync_quality_state()

        self.name_edit = QLineEdit()
        form.addRow('Nome do arquivo:', self.name_edit)
        self.name_edit.textChanged.connect(lambda _: self.completeChanged.emit())
        layout.addLayout(form)

        vector_group = QGroupBox('Camadas vetoriais (opcional)')
        vector_layout = QVBoxLayout(vector_group)
        hint = set_muted(QLabel(
            'Selecione as camadas a incluir. '
            'Camadas vetoriais incluídas no .tairudb não são editáveis no Tairu Maps mobile.'))
        hint.setWordWrap(True)
        vector_layout.addWidget(hint)

        self._vector_checkboxes = {}
        self._scroll_content = QWidget()
        self._scroll_content.setObjectName('VectorScrollContent')
        self._scroll_inner = QVBoxLayout(self._scroll_content)
        self._scroll_inner.setContentsMargins(0, 0, 0, 0)
        self._scroll_inner.setSpacing(2)

        self._vector_scroll = QScrollArea()
        self._vector_scroll.setWidgetResizable(True)
        self._vector_scroll.setWidget(self._scroll_content)
        self._vector_scroll.setMaximumHeight(140)
        self._vector_scroll.setStyleSheet(_VECTOR_LIST_STYLE)
        self._vector_scroll.verticalScrollBar().setStyleSheet(SCROLLBAR_STYLE)
        vector_layout.addWidget(self._vector_scroll)
        layout.addWidget(vector_group)

    def initializePage(self):
        self._populate_vector_layers()
        if not self.name_edit.text().strip():
            # Include the time so regenerating for the same map on the same day
            # doesn't collide with an existing remote file name.
            date_str = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            base = slugify_filename(self._wizard.tmap.nome or 'mapa')
            self.name_edit.setText(f'{base}-{date_str}.tairudb')

    def _populate_vector_layers(self):
        while self._scroll_inner.count():
            item = self._scroll_inner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._vector_checkboxes.clear()

        project = QgsProject.instance()
        for layer in project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            cb = QCheckBox(layer.name())
            self._vector_checkboxes[layer.id()] = cb
            self._scroll_inner.addWidget(cb)
        self._scroll_inner.addStretch(1)

    def selected_vector_layers(self):
        project = QgsProject.instance()
        layers = []
        for layer_id, cb in self._vector_checkboxes.items():
            if cb.isChecked():
                layer = project.mapLayer(layer_id)
                if layer and layer.isValid():
                    layers.append(layer)
        return layers

    def isComplete(self):
        return bool(self.file_name())

    def file_name(self):
        name = slugify_filename(self.name_edit.text().strip(), fallback='')
        if not name:
            return ''
        if not name.lower().endswith('.tairudb'):
            name += '.tairudb'
        return name

    def max_zoom(self):
        return self.resolution_combo.currentData()

    def tile_format(self):
        return self.format_combo.currentText()

    def _sync_quality_state(self):
        set_control_enabled(self.quality_spin, self.tile_format() in ('JPG', 'WEBP'))


# ------------------------------------------------------------------ page 3

class EstimatePage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Estimativa')
        self.setSubTitle('Confira o tamanho estimado antes de gerar.')
        self._ok = False

        layout = QVBoxLayout(self)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        layout.addWidget(self.report, 1)
        self.gate_label = QLabel('')
        self.gate_label.setWordWrap(True)
        self.gate_label.setStyleSheet(status_style(True))
        layout.addWidget(self.gate_label)

    def initializePage(self):
        wizard = self._wizard
        self._ok = False
        self.report.setPlainText('Calculando tiles da área selecionada…')
        self.gate_label.setText('')
        QTimer.singleShot(50, self._compute)

    def _compute(self):
        wizard = self._wizard
        if not wizard.visible_basemap_layers():
            self.report.setPlainText('')
            self.gate_label.setText(
                'Nenhuma camada raster ou de tiles vetoriais visível no projeto — '
                'adicione/habilite o mapa base que deseja exportar antes de continuar.')
            self.completeChanged.emit()
            return

        try:
            wizard.polygons_wgs84 = wizard.extent_page.polygons_wgs84()
            wizard.region_result = compute_region_tiles(
                wizard.polygons_wgs84, wizard.params_page.max_zoom(), FeedbackAdapter())
        except Exception as e:
            self.report.setPlainText('')
            self.gate_label.setText(f'Falha ao calcular a área: {e}')
            self.completeChanged.emit()
            return

        if wizard.region_result is None or not wizard.region_result.filtered_tiles:
            self.report.setPlainText('')
            self.gate_label.setText('Nenhum tile intersecta a área selecionada.')
            self.completeChanged.emit()
            return

        wizard.estimate_result = estimate(
            wizard.region_result, wizard.params_page.max_zoom(),
            wizard.params_page.tile_format(), wizard.params_page.quality_spin.value(),
            threads_number=min(os.cpu_count() or 4, 4))

        lines = []

        class _Collector(FeedbackAdapter):
            def push_info(self, text):
                lines.append(text)

            def report_error(self, text, fatal=False):
                lines.append(text)

        vector_layers = wizard.params_page.selected_vector_layers()
        vector_feature_count = sum(lyr.featureCount() for lyr in vector_layers if lyr.isValid())
        format_estimate_report(
            wizard.estimate_result, _Collector(),
            num_vector_layers=len(vector_layers),
            vector_feature_count=vector_feature_count,
            dry_run_footer=False)
        self.report.setPlainText('\n'.join(lines))

        if wizard.estimate_result.avg_mb > _SOFT_LIMIT_MB:
            self.gate_label.setText(
                f'Estimativa de {wizard.estimate_result.avg_mb:.0f} MB excede o limite de '
                f'{_SOFT_LIMIT_MB} MB para envio (máximo do servidor: 100 MB). '
                'Reduza a área, a resolução ou a qualidade.')
        else:
            self._ok = True
        self.completeChanged.emit()

    def isComplete(self):
        return self._ok


# ------------------------------------------------------------------ page 4

class RunPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Geração e envio')
        self._running = False
        self._done = False

        layout = QVBoxLayout(self)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

    def _append(self, text):
        self.log.appendPlainText(text)

    def initializePage(self):
        if not self._running and not self._done:
            self._running = True
            QTimer.singleShot(100, self._start)

    def isComplete(self):
        return self._done

    def _set_back_enabled(self, enabled):
        try:
            back = QWizard.WizardButton.BackButton if hasattr(QWizard, 'WizardButton') \
                else QWizard.BackButton
            self._wizard.button(back).setEnabled(enabled)
        except Exception:
            pass

    def _start(self):
        wizard = self._wizard
        self._set_back_enabled(False)

        params = wizard.params_page
        file_name = params.file_name()
        paths = map_workspace(wizard.dock.env.key, wizard.tmap.map_id)
        output_file = os.path.join(paths['out'], file_name)

        vector_layers = params.selected_vector_layers()

        wizard.feedback = WizardFeedback(self.progress, self._append)
        spec = GenerationSpec(
            output_file=output_file,
            layers=wizard.visible_basemap_layers(),
            region_tiles=wizard.region_result.region_tiles,
            filtered_tiles=wizard.region_result.filtered_tiles,
            bounds_list=wizard.region_result.bounds_list,
            wgs84_extent=wizard.region_result.wgs84_extent,
            max_zoom=params.max_zoom(),
            tile_format=params.tile_format(),
            jpg_quality=params.quality_spin.value(),
            transform_context=QgsProject.instance().transformContext(),
            threads_number=min(os.cpu_count() or 4, 4),
            name=os.path.splitext(file_name)[0],
        )

        self._append(f'Gerando {file_name} '
                     f'({len(spec.filtered_tiles)} tiles, zoom {spec.max_zoom})…')

        engine = TileRenderEngine(spec, wizard.feedback)
        ok = engine.run()
        if not ok:
            engine.cleanup_resources()
            self._append('Geração cancelada.' if engine.canceled
                         else f'Falha na geração: {engine.error_message}')
            self._running = False
            self._set_back_enabled(True)
            return

        if vector_layers:
            self._append(f'Exportando {len(vector_layers)} camada(s) vetorial(is)…')
            export_vector_layers(
                engine.writer, vector_layers,
                QgsProject.instance().transformContext(), wizard.feedback)
            if wizard.feedback.canceled:
                engine.cleanup_resources()
                self._append('Geração cancelada.')
                self._running = False
                self._set_back_enabled(True)
                return

        engine.finalize()

        size = os.path.getsize(output_file)
        size_mb = size / (1024 * 1024)
        self._append(f'Arquivo gerado: {size_mb:.1f} MB')
        if size > _HARD_LIMIT_BYTES:
            self._append('ERRO: o arquivo excede o limite de 100 MB do servidor. '
                         'Reduza a área, a resolução ou a qualidade.')
            self._running = False
            self._set_back_enabled(True)
            return

        self._upload(output_file, file_name)

    def _upload(self, output_file, file_name):
        wizard = self._wizard
        dock = wizard.dock
        tmap = wizard.tmap
        storage, fs = dock.storage, dock.fs
        object_path = TAIRUDB_OBJECT_PATH.format(map_id=tmap.map_id, file_name=file_name)

        self._append('Enviando para o Tairu Maps…')

        def send(task):
            # A new file uses the Storage 'create' rule; overwriting an existing
            # object falls under 'update' and is rejected, surfacing as a generic
            # 403. Detect the collision up front and report it clearly instead.
            if storage.exists(object_path):
                raise FirebaseError(
                    'ALREADY_EXISTS',
                    f'Já existe um arquivo chamado "{file_name}" neste mapa. '
                    'Escolha outro nome.',
                    http_status=409)

            def up_progress(done, total):
                if total:
                    task.report(done / total,
                                f'Enviando… {done // (1024*1024)} de {total // (1024*1024)} MB')

            storage.upload_resumable(output_file, object_path,
                                     progress_cb=up_progress, cancel_cb=task.isCanceled)
            write = fs.build_array_append_write(
                f'maps/{tmap.map_id}', 'tairuDBRemoteFiles', [file_name],
                extra_py_fields={'lastModified': now_millis()})
            fs.commit([write])
            return file_name

        def on_success(_name):
            if file_name not in tmap.tairudb_remote_files:
                tmap.tairudb_remote_files.append(file_name)
            dock.detail_page.update_files(tmap)
            self._append('Concluído! O arquivo já aparece no mapa do Tairu Maps.')
            self._done = True
            self._running = False
            self.completeChanged.emit()
            dock.notify(f'{file_name} enviado para {tmap.nome}.')

        def on_error(message):
            self._append(f'Falha no envio: {message}')
            self._running = False
            self._set_back_enabled(True)

        def on_progress(fraction, message):
            try:
                self.progress.setValue(int(fraction * 100))
                if message:
                    self._append(message)
            except RuntimeError:
                pass

        run_task(f'Tairu Maps: upload {file_name}', send,
                 on_success=on_success, on_error=on_error, on_progress=on_progress)
