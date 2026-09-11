# -*- coding: utf-8 -*-

"""
TairuDB generation wizard: extent → parameters → estimate → generate.

The same generation flow is used before login (save a local .tairudb file)
and after login (generate into the map workspace, then upload/register the
file on the selected Tairu Maps map). Supports both raster tiles and optional
vector layer export.

Generation runs on the GUI thread (same TileRenderEngine constraint as the
Processing algorithm and the raster cloud wizard).
"""

import contextlib
import datetime
import math
import os
import traceback

from qgis.PyQt.QtCore import QTimer, QCoreApplication, QSettings
from qgis.PyQt.QtWidgets import (
    QWizard, QWizardPage, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QRadioButton, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit,
    QPlainTextEdit, QProgressBar, QFileDialog, QScrollArea,
    QCheckBox, QWidget, QColorDialog, QSlider, QMessageBox,
    QTableWidget, QTableWidgetItem, QButtonGroup,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis, QgsMessageLog,
    QgsCoordinateReferenceSystem, QgsGeometry, QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsMapLayerComboBox

try:
    from ..compat import (
        _POLYGON_LAYER_FILTER, _RASTER_LAYER_TYPE, _VECTOR_TILE_LAYER_TYPE,
    )
    from ..compat import _CHECKED, _ITEM_IS_CHECKABLE, _ITEM_IS_EDITABLE, _ITEM_IS_ENABLED, _UNCHECKED
    from ..tairu_core.contour_generator import (
        ContourError, SOURCE_INPE, SOURCE_COPERNICUS,
        SMOOTHING_NONE, generate_contours,
    )
    from ..tairu_core.feedback import FeedbackAdapter
    from ..tairu_core.layer_tree import layer_is_visible
    from ..tairu_core.raster_footprint import coverage_ratio, data_footprint
    from ..tairu_core.generator import GenerationSpec, TileRenderEngine, estimate, format_estimate_report
    from ..tairu_core.tile_math import compute_region_tiles, to_wgs84
    from ..tairu_core.tile_prefetch import prefetch_basemap_tiles
    from ..tairu_core.reentrancy_guard import enter as gen_enter, leave as gen_leave, run_or_defer
    from ..tairu_core.vector_export import export_vector_layers
    from ..tairu_core.elevation_tiles import (
        write_elevation_tiles, elevation_tiles_for_extent,
        estimate_bytes as elevation_estimate_bytes)
    from ..tairu_core.workspace import map_workspace, slugify_filename
    from .extent_tool import ExtentPicker
    from .style import (
        apply_combo_popup_style, apply_table_style, apply_tairu_style,
        set_control_enabled, set_muted, set_plain_button, set_primary_button,
        set_secondary_button, set_warning_banner, status_style, SCROLLBAR_STYLE,
    )
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import (
        _POLYGON_LAYER_FILTER, _RASTER_LAYER_TYPE, _VECTOR_TILE_LAYER_TYPE,
    )
    from compat import _CHECKED, _ITEM_IS_CHECKABLE, _ITEM_IS_EDITABLE, _ITEM_IS_ENABLED, _UNCHECKED
    from tairu_core.contour_generator import (
        ContourError, SOURCE_INPE, SOURCE_COPERNICUS,
        SMOOTHING_NONE, generate_contours,
    )
    from tairu_core.feedback import FeedbackAdapter
    from tairu_core.layer_tree import layer_is_visible
    from tairu_core.raster_footprint import coverage_ratio, data_footprint
    from tairu_core.generator import GenerationSpec, TileRenderEngine, estimate, format_estimate_report
    from tairu_core.tile_math import compute_region_tiles, to_wgs84
    from tairu_core.tile_prefetch import prefetch_basemap_tiles
    from tairu_core.reentrancy_guard import enter as gen_enter, leave as gen_leave, run_or_defer
    from tairu_core.vector_export import export_vector_layers
    from tairu_core.elevation_tiles import (
        write_elevation_tiles, elevation_tiles_for_extent,
        estimate_bytes as elevation_estimate_bytes)
    from tairu_core.workspace import map_workspace, slugify_filename
    from tairu_ui.extent_tool import ExtentPicker
    from tairu_ui.style import (
        apply_combo_popup_style, apply_table_style, apply_tairu_style,
        set_control_enabled, set_muted, set_plain_button, set_primary_button,
        set_secondary_button, set_warning_banner, status_style, SCROLLBAR_STYLE,
    )

_VECTOR_LIST_STYLE = """
QScrollArea {
    border: none;
    background: transparent;
}
QWidget#VectorScrollContent {
    background: transparent;
}
QCheckBox {
    padding: 6px 8px;
    spacing: 8px;
    border: 1px solid transparent;
    border-radius: 6px;
}
QCheckBox:hover {
    background: rgba(0, 106, 67, 0.08);
    border-color: rgba(0, 106, 67, 0.2);
}
"""

# Rótulo e valor da grade GRG numa fonte só: o log imprimia o valor cru do enum.
_GRG_TYPES = [('Alfanumérica', 'alphanumeric'), ('Coordenada Geográfica', 'geographic')]
_GRG_TYPE_LABELS = {valor: rotulo for rotulo, valor in _GRG_TYPES}

_RESOLUTIONS = [
    ('Máxima (0,25 m/px)', 19),
    ('Altíssima (0,5 m/px)', 18), ('Alta (1 m/px)', 17), ('Médio Alta (2 m/px)', 16),
    ('Média (4 m/px)', 15), ('Médio Baixa (8 m/px)', 14), ('Baixa (16 m/px)', 13),
    ('Muito Baixa (32 m/px)', 12),
]
_FORMATS = ['PNG', 'JPG', 'WEBP']

# Informational (non-blocking) warning threshold for locally generated files:
# above this size the .tairudb is heavy on mobile and can't be uploaded to a
# cloud map (server hard-cap is 100 MB). Local generation is never blocked.
_LARGE_FILE_WARN_MB = 100
_UPLOAD_SOFT_LIMIT_MB = 90
_UPLOAD_HARD_LIMIT_BYTES = 100 * 1024 * 1024


# Wizards are shown non-modally: an application-modal exec() floats the window above
# every other window (on macOS even above other apps) and blocks the map canvas that
# extent picking needs. Kept alive here until the wizard closes.
_open_wizards = []


def _show_wizard(wizard):
    wizard.setModal(False)
    _open_wizards.append(wizard)

    # A closed wizard MUST be destroyed. It is parented to the dock/main window, so
    # dropping the last Python reference does NOT delete it: the C++ widget survives
    # hidden, forever, and its QgsMapLayerComboBox keeps firing layerChanged on every
    # QgsProject.addMapLayer — i.e. every "Receber Registros" runs the slots of every
    # wizard ever opened, inside QgsMapLayerModel's endInsertRows. That is the SIGSEGV
    # reported on 2026-08-20 (and, after a plugin reload, those slots point into the
    # unloaded module's code). deleteLater is deferred while a generation is pumping
    # its nested event loop, which would otherwise process the delete mid-generation.
    def _forget(_result=0, w=wizard):
        if w in _open_wizards:
            _open_wizards.remove(w)
        run_or_defer(w.deleteLater)
    wizard.finished.connect(_forget)
    wizard.show()
    wizard.raise_()
    wizard.activateWindow()


def close_open_wizards():
    """Close every open wizard (plugin unload/reload).

    A wizard parented to the QGIS main window outlives the plugin module; after a
    reload its layerChanged slots would point into the unloaded module's code.
    """
    for wizard in list(_open_wizards):
        with contextlib.suppress(Exception):
            wizard.close()


def open_local_generate_wizard(iface):
    _show_wizard(LocalGenerateWizard(iface))


def open_raster_wizard(dock, tmap):
    _show_wizard(TairuDBGenerateWizard(dock.iface, dock=dock, tmap=tmap))


class WizardFeedback(FeedbackAdapter):
    """Routes engine feedback into the run page; guards against a closed wizard."""

    def __init__(self, progress_bar, log_fn):
        self._bar = progress_bar
        self._log = log_fn
        self.canceled = False
        self._last_progress = 0

    def set_progress(self, value):
        # Progress only moves forward WITHIN a phase — prevents backward jitter.
        # reset_progress() starts a fresh phase so the next one grows from 0.
        v = int(value)
        if v < self._last_progress:
            return
        self._last_progress = v
        with contextlib.suppress(RuntimeError):
            self._bar.setValue(v)

    def reset_progress(self):
        self._last_progress = 0
        with contextlib.suppress(RuntimeError):
            self._bar.setValue(0)

    def set_progress_text(self, text):
        self.push_info(text)

    def heartbeat(self, text):
        # Live, in-place status on the progress bar itself (no log spam). If this
        # keeps updating during a long render, the UI is alive — and the user sees it.
        with contextlib.suppress(RuntimeError):
            self._bar.setFormat(text)

    def push_info(self, text):
        with contextlib.suppress(RuntimeError):
            if text:
                self._log(text)

    def report_error(self, text, fatal=False):
        self.push_info(f'ERRO: {text}')

    def is_canceled(self):
        return self.canceled


class TairuDBGenerateWizard(QWizard):

    def __init__(self, iface, dock=None, tmap=None):
        super().__init__(dock if dock is not None else iface.mainWindow())
        self.iface = iface
        self.dock = dock
        self.tmap = tmap
        self.is_upload_mode = dock is not None and tmap is not None
        if self.is_upload_mode:
            self.setWindowTitle(f'Gerar e enviar TairuDB · {tmap.nome}')
        else:
            self.setWindowTitle('Gerar arquivo TairuDB')
        self.resize(680, 560)

        # Cross-page state
        self.polygons_wgs84 = []
        self.region_result = None
        self.estimate_result = None
        self.feedback = None

        self.extent_page = ExtentPage(self)
        self.params_page = ParamsPage(self)
        self.vector_page = VectorLayersPage(self)
        self.contour_page = ContourPage(self)
        self.grg_page = GrgPage(self)
        self.destination_page = DestinationPage(self)
        self.estimate_page = EstimatePage(self)
        self.run_page = RunPage(self)
        self.addPage(self.extent_page)
        self.addPage(self.params_page)
        self.addPage(self.vector_page)
        self.addPage(self.contour_page)
        self.addPage(self.grg_page)
        self.addPage(self.destination_page)
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
            with contextlib.suppress(Exception):
                button_id = self._wizard_button_id(name)
                self.setButtonText(button_id, label)
                button = self.button(button_id)
                if button is not None and name in primary:
                    set_primary_button(button)
                elif button is not None:
                    set_plain_button(button)

    def visible_basemap_layers(self):
        """O que desenha o mapa: as imagens marcadas na primeira tela mais o mapa
        de fundo do projeto, se marcado em Parâmetros — na ordem de desenho.

        As duas escolhas são explícitas, de propósito: uma camada online ligada
        no QGIS já entrou sozinha na geração e baixou milhares de tiles sem
        aviso. Deixá-la de fora SEM ter como entrar, porém, travava na Estimativa
        todo projeto que usa só um satélite online — que é a maioria deles.
        """
        chosen = {lyr.id() for lyr in self.extent_page.checked_raster_layers()}
        chosen |= {lyr.id() for lyr in self.params_page.extra_basemap_layers()}
        return [lyr for lyr in QgsProject.instance().layerTreeRoot().layerOrder()
                if lyr.id() in chosen]

    def _on_rejected(self):
        if self.feedback is not None:
            self.feedback.canceled = True
        self.extent_page.stop_picker()


class LocalGenerateWizard(TairuDBGenerateWizard):

    def __init__(self, iface):
        super().__init__(iface)


# ------------------------------------------------------------------ page 1


def _km_size(bb):
    """"4,2 × 3,1 km" a partir de uma envoltória em graus (EPSG:4326)."""
    clat = math.radians((bb.yMinimum() + bb.yMaximum()) / 2)
    w_km = abs(bb.xMaximum() - bb.xMinimum()) * 111.32 * math.cos(clat)
    h_km = abs(bb.yMaximum() - bb.yMinimum()) * 110.574
    return f'{w_km:.1f} × {h_km:.1f} km'.replace('.', ',')


_ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft
_SOURCE_LABELS = {
    'canvas': 'A área visível do mapa',
    'draw': 'Um retângulo desenhado no mapa',
    'layer': 'Cada polígono de uma camada',
    'raster': 'Cada imagem carregada no projeto',
}
_IMAGE_HEADERS = ['Usar', 'Imagem', 'Área', 'Resolução', 'SRC']


def _resolution_label(layer, bb_wgs84):
    """Metros por pixel no chão, medidos da extensão em graus e do nº de pixels.

    Não usa `rasterUnitsPerPixel`: numa camada geográfica ele vem em GRAUS, e
    "8e-05 m/px" não diz nada a ninguém.
    """
    width = layer.width()
    if not width:
        return '—'
    clat = math.radians((bb_wgs84.yMinimum() + bb_wgs84.yMaximum()) / 2)
    metres = abs(bb_wgs84.xMaximum() - bb_wgs84.xMinimum()) * 111320.0 * math.cos(clat)
    res = metres / width
    if res < 10:
        return f'{res:.2f} m/px'.replace('.', ',')
    return f'{res:.0f} m/px'


def _is_online(layer):
    """A fonte busca pela rede — o que transforma "gerar" em "baixar".

    Testa a URL na fonte, não o nome do provedor: XYZ, WMTS e tiles vetoriais
    chegam todos pelo provedor `wms`/`vectortile`, e o mesmo provedor também
    serve um .mbtiles local, que não baixa nada.
    """
    return 'http' in (layer.source() or '').lower()


def _layer_origin(layer):
    return 'internet' if _is_online(layer) else 'arquivo local'


def _hidden_basemap_names(project):
    """Nomes de imagens/mapas de fundo que só não entram por estarem OCULTOS.

    Sem isto, um projeto com a ortofoto desmarcada no painel de camadas diz
    "nenhum arquivo de imagem neste projeto" — que é falso, e manda o usuário
    procurar o defeito no plugin em vez de na caixinha do painel.
    """
    nomes = []
    for layer in project.layerTreeRoot().layerOrder():
        if not layer.isValid() or layer_is_visible(layer, project):
            continue
        if layer.type() in (_RASTER_LAYER_TYPE, _VECTOR_TILE_LAYER_TYPE):
            nomes.append(layer.name())
    return nomes


def _project_basemap_layers(project):
    """O mapa de fundo do projeto: o que desenha fundo e NÃO está na tabela de
    imagens da primeira tela (XYZ, WMTS, WMS, mbtiles, tiles vetoriais).

    A tabela da primeira tela lista só imagens de arquivo (provider `gdal`),
    porque lá cada uma também define uma região; estas aqui não definem região
    nenhuma (a extensão é mundial), então são uma escolha à parte.
    """
    layers = []
    for layer in project.layerTreeRoot().layerOrder():
        if not layer.isValid() or not layer_is_visible(layer, project):
            continue
        tipo = layer.type()
        if tipo == _VECTOR_TILE_LAYER_TYPE:
            layers.append(layer)
        elif tipo == _RASTER_LAYER_TYPE and layer.providerType() != 'gdal':
            layers.append(layer)
    return layers


def _safe_measure(fn, *args):
    """Uma dica que não pôde ser medida vira texto, não uma exceção no assistente."""
    try:
        return fn(*args)
    except Exception as exc:
        return f'Não foi possível medir a área: {exc}'


def _regions_text(count, bb):
    size = _km_size(bb)
    # featureCount() responde -1 quando o provedor não sabe contar sem varrer.
    if count is None or count < 0:
        return f'Área total: {size}'
    label = '1 região' if count == 1 else f'{count} regiões'
    return f'{label} · {size}'


class ExtentPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Área de interesse')
        self.setSubTitle('Escolha a área que vai virar mapa.')
        self.drawn_rect = None
        self._picker = None
        self.hidden_images = []
        self._raster_rows = []          # [(layer_id, utilizável)] na ordem da tabela

        layout = QVBoxLayout(self)
        # 12 entre OPÇÕES; dentro de cada opção, 2 entre o rádio e a caixa dele.
        # Com um espaçamento só para os dois casos, a caixa da opção marcada
        # ficava à mesma distância do próprio rádio e do rádio seguinte, e lia
        # como um espaço solto entre as duas opções.
        layout.setSpacing(12)
        # Cada opção fica num recipiente próprio para colar a caixa no rádio, e
        # isso tira os quatro do mesmo pai — sem grupo explícito eles deixam de
        # ser exclusivos e dá para marcar dois ao mesmo tempo.
        self._source_group = QButtonGroup(self)

        # A área visível vem marcada por ser a única que funciona em QUALQUER
        # projeto. Com "camada de polígonos" no lugar dela — o padrão anterior —
        # o assistente abria com o Avançar desligado para quem não tem uma
        # camada de polígonos, que é a maioria dos projetos.
        self.canvas_radio = QRadioButton(_SOURCE_LABELS['canvas'])
        self.canvas_radio.setChecked(True)
        self._add_option(layout, self.canvas_radio)

        self.draw_radio = QRadioButton(_SOURCE_LABELS['draw'])
        self._draw_box = self._add_option(layout, self.draw_radio)
        self.draw_btn = set_secondary_button(QPushButton('Desenhar no mapa'))
        self.draw_btn.clicked.connect(self._start_picker)
        self._draw_box.layout().addWidget(self.draw_btn, 0, _ALIGN_LEFT)

        self.layer_radio = QRadioButton(_SOURCE_LABELS['layer'])
        self._layer_box = self._add_option(layout, self.layer_radio)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(_POLYGON_LAYER_FILTER)
        apply_combo_popup_style(self.layer_combo)
        self._layer_box.layout().addWidget(self.layer_combo)

        self.raster_radio = QRadioButton(_SOURCE_LABELS['raster'])
        self._raster_box = self._add_option(layout, self.raster_radio, stretch=1)
        self._raster_holder = self._raster_box.parentWidget()
        self._raster_box.layout().addWidget(set_muted(QLabel(
            'As imagens marcadas são desenhadas no mapa. Nesta opção, cada uma '
            'também define uma região.')))
        self.raster_table = QTableWidget(0, len(_IMAGE_HEADERS))
        self.raster_table.setHorizontalHeaderLabels(_IMAGE_HEADERS)
        self.raster_table.setAlternatingRowColors(True)
        self.raster_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.raster_table.verticalHeader().setVisible(False)
        self.raster_table.horizontalHeader().setStretchLastSection(True)
        # Sem altura mínima a tabela divide a sobra com o addStretch do fim do
        # layout e abre com uma linha e meia à vista.
        self.raster_table.setMinimumHeight(160)
        apply_table_style(self.raster_table)
        self.raster_table.itemChanged.connect(lambda _: self._sync_controls())
        self._raster_box.layout().addWidget(self.raster_table, 1)

        # Fora da caixa da opção, de propósito: a caixa fica escondida enquanto a
        # opção não está marcada — e ela nem pode ser marcada quando TODAS as
        # imagens estão ocultas, que é exatamente quando este aviso importa.
        self.hidden_images_label = set_muted(QLabel(''))
        self.hidden_images_label.setWordWrap(True)
        self.hidden_images_label.hide()
        layout.addWidget(self.hidden_images_label)

        layout.addStretch(1)

        self.layer_radio.toggled.connect(self._sync_controls)
        self.canvas_radio.toggled.connect(self._sync_controls)
        self.raster_radio.toggled.connect(self._sync_controls)
        self.draw_radio.toggled.connect(self._sync_controls)
        # Deferred on purpose: layerChanged also fires from inside QgsMapLayerModel's
        # endInsertRows when a layer is added to the project (a records pull), and
        # emitting completeChanged there re-enters QWizard mid-model-mutation.
        self.layer_combo.layerChanged.connect(lambda _: QTimer.singleShot(0, self._sync_controls))
        self._sync_controls()

    # --------------------------------------------------------------- layout

    def _add_option(self, layout, radio, stretch=0):
        """Rádio + a caixa dele num só bloco, para que a caixa fique colada no rádio."""
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self._source_group.addButton(radio)
        outer.addWidget(radio)
        box = QWidget()
        inner = QVBoxLayout(box)
        # Folga em cima e embaixo: escondida a caixa, nada disto conta, então o
        # espaçamento entre opções não marcadas continua sendo só o do layout.
        inner.setContentsMargins(22, 2, 0, 6)
        inner.setSpacing(6)
        box.setVisible(False)
        outer.addWidget(box, stretch)
        # Sem esticar aqui: um recipiente com fator de esticamento absorve a
        # sobra vertical mesmo com a caixa escondida, e o rádio dele descola dos
        # de cima. Quem estica é o _sync_controls, e só na opção marcada.
        layout.addWidget(holder)
        self._page_layout = layout
        return box

    def _sync_controls(self):
        """Só a origem marcada aparece — nem controles, nem medida das outras."""
        self._layer_box.setVisible(self.layer_radio.isChecked())
        self._draw_box.setVisible(self.draw_radio.isChecked())
        self._raster_box.setVisible(self.raster_radio.isChecked())
        # A tabela cresce com a janela só quando está à vista.
        self._page_layout.setStretchFactor(
            self._raster_holder, 1 if self.raster_radio.isChecked() else 0)
        self._refresh_labels()
        self.completeChanged.emit()

    def _refresh_labels(self):
        """Estado e medida no PRÓPRIO rótulo de cada opção.

        Uma linha de texto sob a opção marcada abria um vão que lia como espaço
        vazio entre ela e a seguinte — e a medida ficava fraca demais para
        parecer conteúdo. No rótulo, o espaçamento entre opções é sempre o mesmo
        e só um controle de verdade (botão, lista, tabela) ocupa altura.

        Origem impossível fica DESABILITADA com o motivo no rótulo: sem ele, uma
        opção que não responde ao clique parece defeito do plugin.
        """
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        ctx = QgsProject.instance().transformContext()
        has_polygons = self.layer_combo.count() > 0
        has_images = self.has_usable_images()
        set_control_enabled(self.layer_radio, has_polygons)
        set_control_enabled(self.raster_radio, has_images)

        # Uma origem que não existe mais neste projeto não pode seguir marcada.
        if ((self.layer_radio.isChecked() and not has_polygons)
                or (self.raster_radio.isChecked() and not has_images)):
            self.canvas_radio.setChecked(True)   # reentra por toggled
            return

        self._set_label(self.canvas_radio, 'canvas',
                        _safe_measure(self._canvas_hint_text, wgs84, ctx))
        self._set_label(self.draw_radio, 'draw',
                        _safe_measure(self._draw_hint_text, wgs84, ctx))
        self._set_label(
            self.layer_radio, 'layer',
            _safe_measure(self._layer_hint_text, wgs84, ctx) if has_polygons
            else 'nenhuma camada de polígonos neste projeto')
        self._set_label(
            self.raster_radio, 'raster',
            _safe_measure(self._raster_hint_text, wgs84, ctx) if has_images
            else ('nenhuma imagem visível — as do projeto estão ocultas no painel'
                  if self.hidden_images
                  else 'nenhum arquivo de imagem neste projeto'))

    def _set_label(self, radio, key, suffix):
        """O sufixo só entra na opção marcada — ou quando ela não pode ser marcada."""
        base = _SOURCE_LABELS[key]
        show = radio.isChecked() or not radio.isEnabled()
        radio.setText(f'{base} — {suffix}' if show and suffix else base)

    def _canvas_hint_text(self, wgs84, ctx):
        canvas = self._wizard.iface.mapCanvas()
        return _regions_text(1, to_wgs84(QgsGeometry.fromRect(canvas.extent()),
                                         self._canvas_crs(), wgs84, ctx).boundingBox())

    def _draw_hint_text(self, wgs84, ctx):
        if self.drawn_rect is None or self.drawn_rect.isEmpty():
            return 'nenhum definido ainda'
        crs = QgsProject.instance().crs()
        return _regions_text(1, to_wgs84(
            QgsGeometry.fromRect(self.drawn_rect),
            crs if crs.isValid() else self._canvas_crs(), wgs84, ctx).boundingBox())

    def _layer_hint_text(self, wgs84, ctx):
        layer = self.layer_combo.currentLayer()
        if layer is None:
            return 'nenhuma camada selecionada'
        return _regions_text(layer.featureCount(), to_wgs84(
            QgsGeometry.fromRect(layer.extent()), layer.crs(), wgs84, ctx).boundingBox())

    def _raster_hint_text(self, wgs84, ctx):
        layers = self.checked_raster_layers()
        if not layers:
            return 'nenhuma imagem marcada'
        union = None
        for layer in layers:
            bb = to_wgs84(QgsGeometry.fromRect(layer.extent()),
                          layer.crs(), wgs84, ctx).boundingBox()
            if union is None:
                union = bb
            else:
                union.combineExtentWith(bb)
        return _regions_text(len(layers), union)

    # ---------------------------------------------------- tabela de imagens

    def initializePage(self):
        """(Re)monta a tabela de imagens a cada visita — o projeto pode ter mudado.

        Vêm marcadas só as camadas de arquivo (provider `gdal`): um XYZ/WMS
        visível tem extensão mundial e viraria uma área do tamanho do planeta.
        Numa revisita vale o que o usuário tinha marcado, não o padrão.
        """
        first_visit = not self._raster_rows
        previously_checked = {lid for lid, _u in self._raster_rows
                              if lid in self._checked_ids()}

        project = QgsProject.instance()
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        ctx = project.transformContext()
        # Só imagens de ARQUIVO (provider `gdal`). Um XYZ/WMS tem extensão
        # mundial e não define região nenhuma; listá-lo aqui com uma caixinha
        # fazia o usuário concluir que a caixinha decidia se o mapa de fundo
        # entrava no arquivo — não decide (isso é a página Parâmetros), e o
        # download acontecia mesmo com ela desmarcada.
        layers = [layer for layer in project.layerTreeRoot().layerOrder()
                  if layer.type() == _RASTER_LAYER_TYPE and layer.isValid()
                  and layer.providerType() == 'gdal'
                  # Camada oculta não é renderizada: a extensão dela só geraria tiles vazios.
                  and layer_is_visible(layer, project)]

        # Guardado em atributo, não lido de volta do rótulo: `isVisible()` é
        # False enquanto a página não foi mostrada, e initializePage roda antes.
        self.hidden_images = _hidden_basemap_names(project)
        if self.hidden_images:
            uma = len(self.hidden_images) == 1
            self.hidden_images_label.setText(
                ('1 imagem oculta no painel de camadas não entra no mapa'
                 if uma else
                 f'{len(self.hidden_images)} imagens ocultas no painel de camadas '
                 f'não entram no mapa')
                + f' ({", ".join(self.hidden_images)}). '
                + 'Marque a camada no painel do QGIS para poder usá-la aqui.')
        self.hidden_images_label.setVisible(bool(self.hidden_images))

        self._raster_rows = []
        self.raster_table.blockSignals(True)
        self.raster_table.setRowCount(len(layers))
        for row, layer in enumerate(layers):
            try:
                values = self._image_row_values(layer, wgs84, ctx)
                usable, reason = True, None
            except ValueError as exc:
                # Sem reprojeção não há área. Dizer o motivo AQUI, e não deixar
                # falhar como "nenhum tile intersecta a área" três telas adiante.
                values = [layer.name(), 'sem área utilizável', '—', layer.crs().authid() or '—']
                usable, reason = False, str(exc)
            use = QTableWidgetItem('')
            flags = use.flags() & ~_ITEM_IS_EDITABLE
            if usable:
                use.setFlags(flags | _ITEM_IS_CHECKABLE)
                use.setCheckState(_CHECKED if (
                    first_visit or layer.id() in previously_checked) else _UNCHECKED)
            else:
                use.setFlags(flags & ~_ITEM_IS_ENABLED)
                use.setToolTip(reason)
            self.raster_table.setItem(row, 0, use)
            for col, text in enumerate(values, start=1):
                cell = QTableWidgetItem(text)
                cell.setFlags(cell.flags() & ~_ITEM_IS_EDITABLE)
                if reason:
                    cell.setToolTip(reason)
                self.raster_table.setItem(row, col, cell)
            self._raster_rows.append((layer.id(), usable))
        self.raster_table.blockSignals(False)
        self.raster_table.resizeColumnsToContents()
        self._sync_controls()

    def _image_row_values(self, layer, wgs84, ctx):
        """Nome, área, resolução no chão e SRC — o que decide se a imagem entra."""
        if layer.extent().isEmpty():
            raise ValueError('A camada não informa uma extensão.')
        bb = to_wgs84(QgsGeometry.fromRect(layer.extent()), layer.crs(), wgs84, ctx).boundingBox()
        area = _km_size(bb)
        # Numa imagem recortada a medida da caixa mente por larga margem: o
        # mosaico de um corredor fluvial anunciava "50,6 × 47,3 km" com 4% de
        # pixel. Quem lê a tabela é quem decide marcar a imagem — o número tem
        # que estar aqui, não três telas adiante.
        ratio = coverage_ratio(layer, data_footprint(layer))
        if ratio is not None and ratio < 0.9:
            area = f'{area} ({ratio * 100:.0f}% com imagem)'.replace('.', ',')
        return [layer.name(), area, _resolution_label(layer, bb), layer.crs().authid() or '—']

    def has_usable_images(self):
        """Existe imagem de arquivo que possa desenhar o mapa neste projeto."""
        return any(usable for _lid, usable in self._raster_rows)

    def _checked_ids(self):
        checked = set()
        for row, (layer_id, usable) in enumerate(self._raster_rows):
            item = self.raster_table.item(row, 0)
            if usable and item is not None and item.checkState() == _CHECKED:
                checked.add(layer_id)
        return checked

    def checked_raster_layers(self):
        """Marcadas aqui E ainda válidas e visíveis: o assistente não é modal,
        dá tempo de o usuário apagar ou ocultar a camada depois de marcá-la."""
        project = QgsProject.instance()
        checked = self._checked_ids()
        layers = []
        # Na ordem das linhas, que é a ordem de desenho do projeto: é ela que
        # decide o que compõe por cima de quê nos tiles.
        for layer_id in [lid for lid, _u in self._raster_rows if lid in checked]:
            layer = project.mapLayer(layer_id)
            if layer is None or not layer.isValid():
                continue
            if not layer_is_visible(layer, project):
                continue
            layers.append(layer)
        return layers

    # ------------------------------------------------- retângulo no canvas

    def _start_picker(self):
        canvas = self._wizard.iface.mapCanvas()
        self.stop_picker()
        self._picker = ExtentPicker(canvas, self)
        self._picker.extentPicked.connect(self._on_extent_picked)
        self._picker.canceled.connect(self._on_pick_canceled)
        self._wizard.hide()
        self._picker.start()

    def _on_extent_picked(self, rect):
        self.drawn_rect = rect
        self.draw_btn.setText('Desenhar outro retângulo')
        self.draw_btn.setToolTip(
            f'{rect.xMinimum():.5f}, {rect.yMinimum():.5f} — '
            f'{rect.xMaximum():.5f}, {rect.yMaximum():.5f} (CRS do projeto)')
        self._restore_wizard()

    def _on_pick_canceled(self):
        self._restore_wizard()

    def _restore_wizard(self):
        self._picker = None
        self._wizard.show()
        self._wizard.raise_()
        self._sync_controls()

    def stop_picker(self):
        if self._picker is not None:
            self._picker.stop()
            self._picker = None

    def isComplete(self):
        if self.draw_radio.isChecked():
            return self.drawn_rect is not None and not self.drawn_rect.isEmpty()
        if self.canvas_radio.isChecked():
            return True
        if self.raster_radio.isChecked():
            return bool(self.checked_raster_layers())
        return self.layer_combo.currentLayer() is not None

    def source_description(self):
        """Qual opção de área está marcada e com que CRS — para o log."""
        try:
            if self.draw_radio.isChecked():
                crs = QgsProject.instance().crs()
                return f'retângulo desenhado (CRS do projeto {crs.authid() or "?"})'
            if self.canvas_radio.isChecked():
                bruta = self._wizard.iface.mapCanvas().mapSettings().destinationCrs()
                usada = self._canvas_crs()
                origem = 'canvas' if bruta.isValid() else 'projeto (canvas sem SRC)'
                return f'área visível do mapa (CRS do {origem}: {usada.authid() or "?"})'
            if self.raster_radio.isChecked():
                imagens = self.checked_raster_layers()
                nomes = ', '.join(f'"{lyr.name()}" (CRS {lyr.crs().authid() or "?"})'
                                  for lyr in imagens)
                return f'extensão de {len(imagens)} imagem(ns): {nomes}'
            layer = self.layer_combo.currentLayer()
            if layer is None:
                return 'camada de polígonos (nenhuma selecionada)'
            return f'camada "{layer.name()}" (CRS {layer.crs().authid() or "?"})'
        except Exception as exc:
            return f'indeterminada ({exc})'

    def _canvas_crs(self):
        """CRS em que `canvas.extent()` está expresso.

        O canvas desenha NA CRS DO PROJETO — são o mesmo ajuste sob dois nomes.
        Visto em campo: um canvas em Web Mercator (extent em metros) cujo
        `mapSettings().destinationCrs()` respondia INVÁLIDO. O código antigo
        montava um transform inválido com isso, a reprojeção virava no-op sem
        erro, e os metros seguiam para uma matemática que espera graus. Perguntar
        ao canvas primeiro e cair para o projeto cobre os dois lados.
        """
        crs = self._wizard.iface.mapCanvas().mapSettings().destinationCrs()
        return crs if crs.isValid() else QgsProject.instance().crs()

    def polygons_wgs84(self):
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        ctx = QgsProject.instance().transformContext()
        polygons = []
        if self.draw_radio.isChecked():
            # O retângulo é desenhado SOBRE o canvas, então vem na CRS dele.
            crs = QgsProject.instance().crs()
            polygons.append(to_wgs84(
                QgsGeometry.fromRect(self.drawn_rect),
                crs if crs.isValid() else self._canvas_crs(), wgs84, ctx))
        elif self.canvas_radio.isChecked():
            canvas = self._wizard.iface.mapCanvas()
            polygons.append(to_wgs84(
                QgsGeometry.fromRect(canvas.extent()),
                self._canvas_crs(), wgs84, ctx))
        elif self.raster_radio.isChecked():
            # Uma região por imagem. Tiles repetidos entre imagens que se
            # sobrepõem são unificados em `filtered_tiles`, não renderizados 2x.
            #
            # O contorno do dado válido, e não `extent()`: a caixa de uma imagem
            # RECORTADA cobre muito mais chão do que ela tem pixel — num mosaico
            # de corredor fluvial foram 106.392 tiles pela caixa contra 5.949
            # pelo contorno. E como a região virava um retângulo, o estêncil de
            # borda não recortava nada e o mapa de fundo do projeto preenchia
            # todo o vazio com conteúdo: o arquivo saía 18x maior, não só mais
            # lento. Imagem sem alfa nem nodata devolve None e segue na caixa.
            for layer in self.checked_raster_layers():
                geom = data_footprint(layer) or QgsGeometry.fromRect(layer.extent())
                polygons.append(to_wgs84(geom, layer.crs(), wgs84, ctx))
        else:
            layer = self.layer_combo.currentLayer()
            for feature in layer.getFeatures():
                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    continue
                polygons.append(to_wgs84(QgsGeometry(geom), layer.crs(), wgs84, ctx))
        return polygons


# ------------------------------------------------------------------ page 2

class ParamsPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Parâmetros')
        self.setSubTitle('Resolução e formato dos tiles do mapa.')

        self._basemap_ids = []
        self._first_visit = True

        layout = QVBoxLayout(self)
        # O mapa de fundo do projeto entra por escolha explícita — e só aqui
        # existe essa escolha. Um XYZ ligado no QGIS já entrou sozinho na
        # geração e baixou milhares de tiles sem aviso; tirá-lo sem oferecer a
        # caixa deixou sem saída quem não tem nenhuma imagem em disco.
        self.basemap_check = QCheckBox('Incluir o mapa de fundo do projeto')
        self.basemap_check.hide()
        layout.addWidget(self.basemap_check)
        self.online_note = set_muted(QLabel(''))
        self.online_note.setWordWrap(True)
        self.online_note.hide()
        layout.addWidget(self.online_note)
        form = QFormLayout()

        self.resolution_combo = QComboBox()
        for label, zoom in _RESOLUTIONS:
            self.resolution_combo.addItem(label, zoom)
        # 0,25 m/px quadruplica tiles, tamanho e tempo de renderização: entra na
        # lista, mas não como padrão. Mesma razão do índice fixo do combo de formato.
        self.resolution_combo.setCurrentIndex(1)  # Altíssima (0,5 m/px)
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

        layout.addLayout(form)

        # On by default, and it belongs here rather than under Curvas de Nível:
        # the two come from different sources and either is useful without the
        # other. Cheap enough that asking would be the bigger imposition — one
        # ~36 KB tile covers 82 km², so a 30x30 km map gains under 1 MB.
        self.elevation_check = QCheckBox('Incluir dados de altitude do terreno')
        self.elevation_check.setChecked(True)
        self.elevation_check.setToolTip(
            'Permite ao app mostrar a altitude de pontos e o perfil de elevação '
            'de linhas sem internet. Baixa tiles do modelo de terreno (USGS) '
            'para a área do mapa.')
        layout.addWidget(self.elevation_check)

        _elev_hint = QLabel(
            'ℹ️  Requer Tairu Maps versão 1.0.66 ou superior. '
            'Versões anteriores ignoram estes dados e abrem o arquivo normalmente.')
        _elev_hint.setWordWrap(True)
        _elev_hint.setStyleSheet('color: #666; font-style: italic;')
        layout.addWidget(_elev_hint)

        layout.addStretch(1)

    def initializePage(self):
        camadas = _project_basemap_layers(QgsProject.instance())
        # Guardadas por id, nunca por objeto: o assistente não é modal e uma
        # camada removida no meio deixa um ponteiro morto que derruba o QGIS.
        self._basemap_ids = [layer.id() for layer in camadas]
        self.basemap_check.setVisible(bool(camadas))
        self.online_note.setVisible(bool(camadas))
        if not camadas:
            return
        nomes = ', '.join(layer.name() for layer in camadas)
        online = [layer.name() for layer in camadas if _is_online(layer)]
        self.basemap_check.setText(f'Incluir o mapa de fundo do projeto ({nomes})')
        self.online_note.setText(
            'Marcar isto BAIXA os tiles de ' + ', '.join(online) + ' durante a geração; '
            'a Estimativa mostra quantos antes de começar.' if online else
            'O restante do mapa vem das imagens marcadas na primeira tela.')
        if self._first_visit:
            self._first_visit = False
            # Marcado só quando não há imagem de arquivo: aí ele é a ÚNICA coisa
            # capaz de desenhar o mapa, e deixá-lo desmarcado é um beco sem saída.
            # Havendo imagens em disco, quem manda são elas — foi por entrar por
            # cima delas que o fundo online virou download surpresa.
            self.basemap_check.setChecked(
                not self._wizard.extent_page.has_usable_images())

    def extra_basemap_layers(self):
        """O mapa de fundo marcado aqui, ainda válido e visível no projeto."""
        if not self.basemap_check.isChecked():
            return []
        escolhidos = set(self._basemap_ids)
        return [lyr for lyr in _project_basemap_layers(QgsProject.instance())
                if lyr.id() in escolhidos]

    def isComplete(self):
        return True

    def max_zoom(self):
        return self.resolution_combo.currentData()

    def elevation_enabled(self):
        return self.elevation_check.isChecked()

    def tile_format(self):
        return self.format_combo.currentText()

    def _sync_quality_state(self):
        set_control_enabled(self.quality_spin, self.tile_format() in ('JPG', 'WEBP'))


# ------------------------------------------------------------------ page 3 — camadas vetoriais

class VectorLayersPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Camadas Vetoriais')
        self.setSubTitle(
            'Selecione camadas QGIS a incluir no .tairudb (opcional).\n'
            'As camadas vetoriais incluídas serão somente leitura no app '
            '(não editáveis no Tairu Maps).')

        layout = QVBoxLayout(self)
        self._vector_checkboxes = {}
        self.dropped_hidden = []
        self._scroll_content = QWidget()
        self._scroll_content.setObjectName('VectorScrollContent')
        self._scroll_inner = QVBoxLayout(self._scroll_content)
        self._scroll_inner.setContentsMargins(0, 0, 0, 0)
        self._scroll_inner.setSpacing(2)

        self._vector_scroll = QScrollArea()
        self._vector_scroll.setWidgetResizable(True)
        self._vector_scroll.setWidget(self._scroll_content)
        self._vector_scroll.setStyleSheet(_VECTOR_LIST_STYLE)
        self._vector_scroll.verticalScrollBar().setStyleSheet(SCROLLBAR_STYLE)
        layout.addWidget(self._vector_scroll, 1)

        self.hidden_label = set_muted(QLabel(''))
        self.hidden_label.setWordWrap(True)
        self.hidden_label.hide()
        layout.addWidget(self.hidden_label)

    def initializePage(self):
        # initializePage runs on EVERY visit (QWizard re-inits when the user goes back
        # then forward), and it rebuilds the checkbox list from scratch. Remember which
        # layers were checked and restore them after rebuilding, so the selection
        # survives navigating back to the quality/params pages and forward again.
        # (Rebuilding — rather than skipping — keeps the list in sync if project layers
        # changed; a layer removed meanwhile simply drops, a new one appears unchecked.)
        previously_checked = {
            layer_id for layer_id, cb in self._vector_checkboxes.items() if cb.isChecked()
        }
        while self._scroll_inner.count():
            item = self._scroll_inner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._vector_checkboxes.clear()

        project = QgsProject.instance()
        hidden = 0
        for layer in project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            # Camada desmarcada no painel de camadas não é desenhada no mapa e não entra
            # no .tairudb — não faz sentido oferecê-la aqui.
            if not layer_is_visible(layer, project):
                hidden += 1
                continue
            cb = QCheckBox(layer.name())
            if layer.id() in previously_checked:
                cb.setChecked(True)
            self._vector_checkboxes[layer.id()] = cb
            self._scroll_inner.addWidget(cb)
        self._scroll_inner.addStretch(1)
        # Sem esta linha, "cadê minha camada?" vira chamado de suporte.
        if hidden:
            plural = 's' if hidden > 1 else ''
            self.hidden_label.setText(
                f'{hidden} camada{plural} oculta{plural} no painel de camadas '
                f'não {"são" if hidden > 1 else "é"} listada{plural}.')
            self.hidden_label.show()
        else:
            self.hidden_label.hide()

    def selected_vector_layers(self):
        """Camadas marcadas aqui E ainda visíveis no painel de camadas.

        A visibilidade é re-checada porque o assistente não é modal: dá tempo de o usuário
        desmarcar a camada no painel depois de tê-la marcado aqui. O que cai fica nomeado em
        `dropped_hidden` — descartar em silêncio uma camada que o usuário marcou é o tipo de
        coisa que vira "a exportação não funcionou".
        """
        project = QgsProject.instance()
        layers = []
        self.dropped_hidden = []
        for layer_id, cb in self._vector_checkboxes.items():
            if not cb.isChecked():
                continue
            layer = project.mapLayer(layer_id)
            if layer is None or not layer.isValid():
                continue
            if not layer_is_visible(layer, project):
                self.dropped_hidden.append(layer.name())
                continue
            layers.append(layer)
        return layers


# ------------------------------------------------------------------ page 4 — Curvas de Nível

class ContourPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Curvas de Nível')
        self.setSubTitle(
            'Gere curvas de nível automaticamente a partir de dados de elevação (opcional).\n'
            'Requer conexão com a internet na etapa de geração.')

        layout = QVBoxLayout(self)

        self._enable_check = QCheckBox('Gerar Curvas de Nível')
        layout.addWidget(self._enable_check)

        _compat_label = QLabel('ℹ️  Requer Tairu Maps versão 1.0.38 ou superior.')
        _compat_label.setStyleSheet('color: #666; font-style: italic; margin-bottom: 4px;')
        layout.addWidget(_compat_label)

        self._options_widget = QWidget()
        form = QFormLayout(self._options_widget)

        self._source_combo = QComboBox()
        self._source_combo.addItem('Copernicus GLO-30 (Mundial)', SOURCE_COPERNICUS)
        self._source_combo.addItem('INPE TOPODATA (Brasil)', SOURCE_INPE)
        apply_combo_popup_style(self._source_combo)
        form.addRow('Fonte de dados:', self._source_combo)

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 1000)
        self._interval_spin.setValue(10)
        self._interval_spin.setSuffix(' m')
        self._interval_spin.setSingleStep(5)
        self._interval_spin.setAccelerated(True)
        self._interval_spin.setMaximumWidth(110)
        form.addRow('Intervalo:', self._interval_spin)

        self._smoothing_combo = QComboBox()
        for lvl in [SMOOTHING_NONE, 'Baixo', 'Médio', 'Alto']:
            self._smoothing_combo.addItem(lvl)
        self._smoothing_combo.setCurrentIndex(2)  # Médio
        apply_combo_popup_style(self._smoothing_combo)
        form.addRow('Suavização:', self._smoothing_combo)

        self._color = QColor(204, 119, 0, 204)  # brownish, ~80% opacity
        self._color_btn = QPushButton('  ')
        self._color_btn.setFixedWidth(48)
        self._update_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        form.addRow('Cor das curvas:', self._color_btn)

        layout.addWidget(self._options_widget)
        layout.addStretch(1)

        self._options_widget.setVisible(False)
        self._enable_check.toggled.connect(self._options_widget.setVisible)

    def _update_color_btn(self):
        r, g, b, a = (self._color.red(), self._color.green(),
                      self._color.blue(), self._color.alpha())
        self._color_btn.setStyleSheet(
            f'background-color: rgba({r},{g},{b},{a}); border: 1px solid #666;')

    def _pick_color(self):
        opt = QColorDialog.ColorDialogOption.ShowAlphaChannel
        color = QColorDialog.getColor(
            self._color, self, 'Cor das curvas de nível', options=opt)
        if color.isValid():
            self._color = color
            self._update_color_btn()

    def contour_enabled(self):
        return self._enable_check.isChecked()

    def dem_source(self):
        return self._source_combo.currentData()

    def source_label(self):
        return self._source_combo.currentText()

    def interval(self):
        return self._interval_spin.value()

    def smoothing(self):
        return self._smoothing_combo.currentText()

    def color(self):
        return QColor(self._color)


# ------------------------------------------------------------------ page 5 — Grade GRG

class GrgPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Grade GRG')
        self.setSubTitle('Adicione uma grade de referência geográfica ao arquivo (opcional).')

        layout = QVBoxLayout(self)

        self._grg_check = QCheckBox('Incluir grade GRG')
        layout.addWidget(self._grg_check)

        _compat_label = QLabel(
            'ℹ️  Requer Tairu Maps versão 1.0.38 ou superior.'
        )
        _compat_label.setStyleSheet('color: #666; font-style: italic; margin-bottom: 4px;')
        layout.addWidget(_compat_label)

        self._grg_options_widget = QWidget()
        form = QFormLayout(self._grg_options_widget)

        self._grg_type_combo = QComboBox()
        for rotulo, valor in _GRG_TYPES:
            self._grg_type_combo.addItem(rotulo, valor)
        apply_combo_popup_style(self._grg_type_combo)
        form.addRow('Tipo:', self._grg_type_combo)

        self._grg_spacing_spin = QDoubleSpinBox()
        self._grg_spacing_spin.setRange(50, 200000)
        self._grg_spacing_spin.setValue(500)
        self._grg_spacing_spin.setSuffix(' m')
        self._grg_spacing_spin.setDecimals(0)
        self._grg_spacing_spin.setSingleStep(100)
        form.addRow('Espaçamento:', self._grg_spacing_spin)

        # Line style
        self._grg_style_combo = QComboBox()
        for lbl, val in [('Sólido', 'solid'), ('Tracejado', 'dashed'),
                         ('Pontilhado', 'dotted'), ('Traço-ponto', 'dotdash')]:
            self._grg_style_combo.addItem(lbl, val)
        apply_combo_popup_style(self._grg_style_combo)
        form.addRow('Estilo:', self._grg_style_combo)

        # Thickness + opacity
        width_row = QWidget()
        width_lay = QHBoxLayout(width_row)
        width_lay.setContentsMargins(0, 0, 0, 0)
        self._grg_width_spin = QSpinBox()
        self._grg_width_spin.setRange(1, 10)
        self._grg_width_spin.setValue(2)
        self._grg_width_spin.setSuffix(' px')
        self._grg_width_spin.setMaximumWidth(80)
        width_lay.addWidget(self._grg_width_spin)
        width_lay.addWidget(QLabel('Opacidade:'))
        self._grg_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._grg_opacity_slider.setRange(0, 100)
        self._grg_opacity_slider.setValue(80)
        self._grg_opacity_label = QLabel('80%')
        self._grg_opacity_slider.valueChanged.connect(
            lambda v: self._grg_opacity_label.setText(f'{v}%'))
        width_lay.addWidget(self._grg_opacity_slider, 1)
        width_lay.addWidget(self._grg_opacity_label)
        form.addRow('Espessura:', width_row)

        # Line color
        self._grg_line_color = QColor('#000000')
        self._grg_color_btn = QPushButton('  ')
        self._grg_color_btn.setFixedWidth(48)
        self._grg_color_btn.setStyleSheet(
            f'background-color: {self._grg_line_color.name()}; border: 1px solid #666;')
        self._grg_color_btn.clicked.connect(self._pick_line_color)
        form.addRow('Cor da linha:', self._grg_color_btn)

        # Font color + size
        font_row = QWidget()
        font_lay = QHBoxLayout(font_row)
        font_lay.setContentsMargins(0, 0, 0, 0)
        self._grg_font_color = QColor('#FFFFFF')
        self._grg_font_color_btn = QPushButton('  ')
        self._grg_font_color_btn.setFixedWidth(48)
        self._grg_font_color_btn.setStyleSheet(
            f'background-color: {self._grg_font_color.name()}; border: 1px solid #666;')
        self._grg_font_color_btn.clicked.connect(self._pick_font_color)
        font_lay.addWidget(self._grg_font_color_btn)
        font_lay.addWidget(QLabel('Tamanho:'))
        self._grg_font_spin = QSpinBox()
        self._grg_font_spin.setRange(8, 48)
        self._grg_font_spin.setValue(14)
        self._grg_font_spin.setSuffix(' pt')
        self._grg_font_spin.setMaximumWidth(80)
        font_lay.addWidget(self._grg_font_spin)
        font_lay.addStretch()
        form.addRow('Cor do texto:', font_row)

        layout.addWidget(self._grg_options_widget)
        layout.addStretch(1)

        self._grg_options_widget.setVisible(False)
        self._grg_check.toggled.connect(self._grg_options_widget.setVisible)

    def _pick_line_color(self):
        color = QColorDialog.getColor(self._grg_line_color, self, 'Cor da linha GRG')
        if color.isValid():
            self._grg_line_color = color
            self._grg_color_btn.setStyleSheet(
                f'background-color: {color.name()}; border: 1px solid #666;')

    def _pick_font_color(self):
        color = QColorDialog.getColor(self._grg_font_color, self, 'Cor do texto GRG')
        if color.isValid():
            self._grg_font_color = color
            self._grg_font_color_btn.setStyleSheet(
                f'background-color: {color.name()}; border: 1px solid #666;')

    def grg_enabled(self):
        return self._grg_check.isChecked()

    def grg_type_label(self):
        return self._grg_type_combo.currentText()

    def grg_options(self):
        grid_type = self._grg_type_combo.currentData()
        opts = {
            'line_color': self._grg_line_color.name(),
            'line_opacity': self._grg_opacity_slider.value() / 100.0,
            'line_width': self._grg_width_spin.value(),
            'line_style': self._grg_style_combo.currentData(),
            'font_color': self._grg_font_color.name(),
            'font_size': self._grg_font_spin.value(),
        }
        spacing_m = self._grg_spacing_spin.value()
        if grid_type == 'alphanumeric':
            opts['spacing_m'] = spacing_m
        elif grid_type == 'geographic':
            opts['spacing_deg'] = spacing_m / 111320.0
        return grid_type, opts


# ------------------------------------------------------------------ page 6 — arquivo de destino

# Where the last destination is remembered, so a RE-EXPORT defaults to the same
# file name instead of a fresh timestamp. Overwriting the previous file is what
# lets the app replace the map in place on re-import (it evicts the stale
# instance and reloads); a new name every time makes the user accumulate files
# and, for anyone who incorporated features, duplicates them against the records.
# Scoped per expedition in upload mode — two expeditions do not share a name.
_LAST_OUTPUT_SETTINGS_KEY = 'tairu_db/last_output'


def _remember_output(scope, value):
    """Best-effort: a settings failure must never block an export."""
    with contextlib.suppress(Exception):
        QSettings().setValue(f'{_LAST_OUTPUT_SETTINGS_KEY}/{scope}', value)


def _recall_output(scope):
    try:
        value = QSettings().value(f'{_LAST_OUTPUT_SETTINGS_KEY}/{scope}')
        return value if isinstance(value, str) and value.strip() else None
    except Exception:
        return None


class DestinationPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Nome do Arquivo' if wizard.is_upload_mode else 'Arquivo de Destino')
        self.output_edit = None
        self.name_edit = None

        layout = QVBoxLayout(self)

        if wizard.is_upload_mode:
            self.setSubTitle(
                'Escolha apenas o nome do arquivo que será enviado para a expedição.')
            self.name_edit = QLineEdit()
            self.name_edit.setPlaceholderText('Ex.: minha-expedicao.tairudb')
            self.name_edit.textChanged.connect(lambda _: self.completeChanged.emit())
            form = QFormLayout()
            form.addRow('Nome do arquivo:', self.name_edit)
            layout.addLayout(form)
            note = set_muted(QLabel(
                'O plugin salva o arquivo temporariamente no workspace local e envia para a '
                'expedição usando apenas este nome.'))
            note.setWordWrap(True)
            layout.addWidget(note)
        else:
            self.setSubTitle('Escolha onde salvar o arquivo .tairudb gerado.')
            output_layout = QHBoxLayout()
            self.output_edit = QLineEdit()
            self.output_edit.setPlaceholderText('Escolha onde salvar o arquivo .tairudb…')
            self.output_edit.textChanged.connect(lambda _: self.completeChanged.emit())
            output_layout.addWidget(self.output_edit, 1)
            browse_btn = QPushButton('Procurar…')
            browse_btn.clicked.connect(self._browse_output)
            output_layout.addWidget(browse_btn)
            layout.addLayout(output_layout)

        layout.addStretch(1)

    def _settings_scope(self):
        if self._wizard.is_upload_mode and self._wizard.tmap is not None:
            return f'map_{self._wizard.tmap.map_id}'
        return 'local'

    def initializePage(self):
        # Reuse the previous destination when there is one: re-exporting a layer
        # is the normal update path for users who receive no cloud sync, and
        # overwriting the same file is what makes the app replace the map instead
        # of stacking another copy of it.
        remembered = _recall_output(self._settings_scope())
        if self._wizard.is_upload_mode and self.name_edit is not None:
            if not self.name_edit.text().strip():
                if remembered:
                    self.name_edit.setText(remembered)
                else:
                    date_str = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
                    base = slugify_filename(self._wizard.tmap.nome or 'expedicao')
                    self.name_edit.setText(f'{base}-{date_str}.tairudb')
        elif self.output_edit is not None:
            if not self.output_edit.text().strip():
                # Only reuse a path whose folder still exists — a remembered file
                # on an unplugged drive would strand the user on an unwritable
                # destination with no explanation.
                if remembered and os.path.isdir(os.path.dirname(remembered)):
                    self.output_edit.setText(remembered)
                else:
                    date_str = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
                    docs = os.path.expanduser('~/Documents')
                    self.output_edit.setText(os.path.join(docs, f'mapa-{date_str}.tairudb'))

    def validatePage(self):
        # Remembered on the way forward, not at generation time: the user may
        # cancel later, and the destination they chose is still the one they mean
        # next time.
        if self._wizard.is_upload_mode:
            value = self.file_name()
        else:
            value = self.output_path()
        if value:
            _remember_output(self._settings_scope(), value)
        return True

    def _browse_output(self):
        current = self.output_edit.text().strip()
        start_dir = os.path.dirname(current) if current else os.path.expanduser('~/Documents')
        path, _ = QFileDialog.getSaveFileName(
            self, 'Salvar arquivo TairuDB', start_dir, 'TairuDB (*.tairudb)')
        if path:
            if not path.lower().endswith('.tairudb'):
                path += '.tairudb'
            self.output_edit.setText(path)

    def isComplete(self):
        if self._wizard.is_upload_mode:
            return bool(self.file_name())
        path = self.output_edit.text().strip() if self.output_edit else ''
        return bool(path)

    def output_path(self):
        if self._wizard.is_upload_mode:
            file_name = self.file_name()
            if not file_name:
                return ''
            paths = map_workspace(self._wizard.dock.env.key, self._wizard.tmap.map_id)
            return os.path.join(paths['out'], file_name)
        path = self.output_edit.text().strip() if self.output_edit else ''
        if not path:
            return ''
        if not path.lower().endswith('.tairudb'):
            path += '.tairudb'
        return path

    def file_name(self):
        if self._wizard.is_upload_mode:
            if self.name_edit is None:
                return ''
            name = slugify_filename(self.name_edit.text().strip(), fallback='')
        else:
            output_path = self.output_path()
            name = os.path.basename(output_path) if output_path else ''
        if not name:
            return ''
        if not name.lower().endswith('.tairudb'):
            name += '.tairudb'
        return name


# ------------------------------------------------------------------ page 7

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
        self.warn_label = set_warning_banner(QLabel(''))
        self.warn_label.setWordWrap(True)
        self.warn_label.hide()
        layout.addWidget(self.warn_label)
        self.gate_label = QLabel('')
        self.gate_label.setWordWrap(True)
        self.gate_label.setStyleSheet(status_style(True))
        layout.addWidget(self.gate_label)

    def initializePage(self):
        self._ok = False
        self.report.setPlainText('Calculando tiles da área selecionada…')
        self.gate_label.setText('')
        self.warn_label.setText('')   # senão o aviso da simulação anterior ressuscita
        self.warn_label.hide()
        QTimer.singleShot(50, self._compute)

    def _compute(self):
        wizard = self._wizard
        if not wizard.visible_basemap_layers():
            self.report.setPlainText('')
            ocultas = _hidden_basemap_names(QgsProject.instance())
            extra = (' Há camada de imagem oculta no painel do QGIS ('
                     + ', '.join(ocultas) + '): marque-a lá para poder usá-la.'
                     ) if ocultas else ''
            self.gate_label.setText(
                'Nenhuma camada marcada para desenhar o mapa. Marque uma imagem em '
                '"Área de interesse" ou o mapa de fundo do projeto em "Parâmetros".'
                + extra)
            self.completeChanged.emit()
            return

        QgsMessageLog.logMessage(
            'Estimativa: origem=' + wizard.extent_page.source_description()
            + f', zoom={wizard.params_page.max_zoom()}', 'TairuDB', Qgis.MessageLevel.Info)
        try:
            wizard.polygons_wgs84 = wizard.extent_page.polygons_wgs84()
            wizard.region_result = compute_region_tiles(
                wizard.polygons_wgs84, wizard.params_page.max_zoom(), FeedbackAdapter())
        except Exception as e:
            # Log com traceback: este caminho era mudo, e "nada nos logs" virou o
            # sintoma mais caro de diagnosticar deste assistente.
            detalhe = traceback.format_exc()
            texto = f'Falha ao calcular a área: {e}'
            self.report.setPlainText(texto + '\n\n' + detalhe)
            self.gate_label.setText(texto)
            QgsMessageLog.logMessage(texto + '\n' + detalhe, 'TairuDB', Qgis.MessageLevel.Critical)
            self.completeChanged.emit()
            return

        if wizard.region_result is None or not wizard.region_result.filtered_tiles:
            self.report.setPlainText('')
            # Dizer O QUE foi encontrado, nao so a conclusao. Sem isto a mensagem
            # e indiagnosticavel: nao distingue "nenhuma area escolhida" de
            # "a area nao virou tiles", e nao deixa rastro nenhum no log.
            n_poly = len(wizard.polygons_wgs84 or [])
            if wizard.region_result is None:
                motivo = 'cálculo interrompido'
            elif n_poly == 0:
                motivo = ('nenhum polígono de área foi produzido — verifique a '
                          'opção escolhida na etapa "Área de interesse"')
            else:
                bb = wizard.region_result.wgs84_extent
                motivo = (f'{n_poly} polígono(s), extensão WGS84 '
                          f'{bb.xMinimum():.5f},{bb.yMinimum():.5f} → '
                          f'{bb.xMaximum():.5f},{bb.yMaximum():.5f}')
            texto = (f'Nenhum tile intersecta a área selecionada '
                     f'(zoom {wizard.params_page.max_zoom()}; {motivo}).')
            self.gate_label.setText(texto)
            self.report.setPlainText(texto)
            QgsMessageLog.logMessage(texto, 'TairuDB', Qgis.MessageLevel.Warning)
            self.completeChanged.emit()
            return

        vector_layers = wizard.vector_page.selected_vector_layers()
        vector_feature_count = sum(
            lyr.featureCount() for lyr in vector_layers if lyr.isValid())

        # Com as camadas, a estimativa RENDERIZA alguns tiles para medir peso e
        # tempo em vez de multiplicar por uma tabela fixa: são poucos segundos
        # aqui contra um erro de 80% no tamanho anunciado.
        wizard.estimate_result = estimate(
            wizard.region_result, wizard.params_page.max_zoom(),
            wizard.params_page.tile_format(), wizard.params_page.quality_spin.value(),
            threads_number=min(os.cpu_count() or 4, 4),
            layers=wizard.visible_basemap_layers(),
            transform_context=QgsProject.instance().transformContext())

        lines = []

        class _Collector(FeedbackAdapter):
            def push_info(self, text):
                lines.append(text)

            def report_error(self, text, fatal=False):
                lines.append(text)

        cp = wizard.contour_page
        gp = wizard.grg_page
        elev_enabled = wizard.params_page.elevation_enabled()
        # Counted from the same extent the download will use, so the estimate
        # and the file agree instead of being two guesses.
        elev_tiles = (len(elevation_tiles_for_extent(
            wizard.region_result.wgs84_extent)) if elev_enabled else 0)
        elev_bytes = elevation_estimate_bytes(elev_tiles)
        format_estimate_report(
            wizard.estimate_result, _Collector(),
            num_vector_layers=len(vector_layers),
            vector_feature_count=vector_feature_count,
            dry_run_footer=False,
            contour_enabled=cp.contour_enabled(),
            contour_source_label=cp.source_label(),
            contour_interval=cp.interval(),
            contour_smoothing=cp.smoothing(),
            grg_enabled=gp.grg_enabled(),
            grg_type_label=gp.grg_type_label(),
            elevation_enabled=elev_enabled,
            elevation_tiles=elev_tiles,
            elevation_mb=elev_bytes / (1024 * 1024))
        self.report.setPlainText('\n'.join(lines))

        # The size gates weigh EVERYTHING that lands in the file, elevation
        # included: the server's 100 MB cap is checked against the finished
        # file, so a pre-flight that left a component out would wave through a
        # generation that then fails on upload — after the whole render.
        total_mb = wizard.estimate_result.avg_mb + elev_bytes / (1024 * 1024)

        if wizard.is_upload_mode and total_mb > _UPLOAD_SOFT_LIMIT_MB:
            self.gate_label.setText(
                f'Estimativa de {total_mb:.0f} MB excede o limite de '
                f'{_UPLOAD_SOFT_LIMIT_MB} MB para envio (máximo do servidor: 100 MB). '
                'Reduza a área, a resolução ou a qualidade.')
        elif total_mb > _LARGE_FILE_WARN_MB:
            self.warn_label.setText(
                f'⚠ Estimativa de {total_mb:.0f} MB. Arquivos grandes '
                'demoram para gerar e consomem bastante memória ao abrir no Tairu Maps mobile, '
                'e ultrapassam o limite de 100 MB para envio a uma expedição na nuvem. '
                'Você ainda pode gerar e usar o arquivo localmente.')
            self.warn_label.show()
            self._ok = True
        else:
            self._ok = True

        # A amostragem renderizou tiles e TODOS saíram vazios: a camada não cobre
        # a área. Sem isto o relatório mostrava um tamanho plausível (tabela por
        # formato) para um arquivo que sairia sem mapa nenhum — e o usuário só
        # descobria depois de esperar a geração inteira.
        est = wizard.estimate_result
        if est.blank_samples and not est.measured_from:
            self.warn_label.setText(
                ('⚠ Os tiles de amostra saíram sem imagem nenhuma: a camada marcada não '
                 'cobre esta área (ou não chegou a baixar). Gerar agora produz um arquivo '
                 'sem mapa.\n' + self.warn_label.text()).strip())
            self.warn_label.show()
        self.completeChanged.emit()

    def isComplete(self):
        return self._ok


# ------------------------------------------------------------------ page 8

class RunPage(QWizardPage):

    def __init__(self, wizard):
        super().__init__()
        self._wizard = wizard
        self.setTitle('Geração e envio' if wizard.is_upload_mode else 'Geração')
        self._running = False
        self._done = False

        layout = QVBoxLayout(self)
        self.notice = QLabel(
            '⏳ Em áreas grandes a geração pode levar vários minutos. Mantenha o QGIS '
            'aberto — a janela pode parecer congelada durante a finalização; é normal.')
        self.notice.setWordWrap(True)
        try:
            set_warning_banner(self.notice)
        except Exception:
            set_muted(self.notice)
        layout.addWidget(self.notice)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        self.output_label = set_muted(QLabel(''))
        self.output_label.setWordWrap(True)
        layout.addWidget(self.output_label)

    def _append(self, text):
        self.log.appendPlainText(text)

    def initializePage(self):
        if not self._running and not self._done:
            self._running = True
            QTimer.singleShot(100, self._start)

    def isComplete(self):
        return self._done

    def _set_back_enabled(self, enabled):
        with contextlib.suppress(Exception):
            back = QWizard.WizardButton.BackButton if hasattr(QWizard, 'WizardButton') \
                else QWizard.WizardButton.BackButton
            self._wizard.button(back).setEnabled(enabled)

    def _start(self):
        # Generation pumps the event loop (nested QEventLoop for prefetch/render,
        # processEvents during DEM downloads). That dispatches unrelated QgsTask
        # completions — a background records pull's on_success calls addMapLayer, and
        # doing that re-entrantly here crashes QGIS via the layer combo. The guard
        # defers such project mutations until this generation finishes.
        gen_enter()
        try:
            self._run_generation()
        finally:
            gen_leave()

    def _run_generation(self):
        wizard = self._wizard
        self._set_back_enabled(False)

        dest = wizard.destination_page
        params = wizard.params_page
        output_file = dest.output_path()
        file_name = dest.file_name()
        vector_layers = wizard.vector_page.selected_vector_layers()
        for nome in wizard.vector_page.dropped_hidden:
            self._append(f'Camada "{nome}" foi desmarcada no painel de camadas '
                         f'depois de escolhida — não será incluída.')

        os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)

        wizard.feedback = WizardFeedback(self.progress, self._append)
        spec = GenerationSpec(
            output_file=output_file,
            layers=wizard.visible_basemap_layers(),
            region_tiles=wizard.region_result.region_tiles,
            region_edge_tiles=wizard.region_result.region_edge_tiles,
            region_rings=wizard.region_result.region_rings,
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
        # Qual camada desenhou o mapa é a primeira pergunta quando o arquivo sai
        # branco ou sem o fundo esperado — e não estava em lugar nenhum do log.
        self._append('Mapa desenhado com: ' + ', '.join(
            f'{lyr.name()} ({_layer_origin(lyr)})' for lyr in spec.layers))

        # Off-ramp for genuinely large jobs: they hold the GUI thread for minutes and
        # the window can look frozen, so let the user opt in knowingly.
        n_tiles = len(spec.filtered_tiles)
        est_mb = getattr(getattr(wizard, 'estimate_result', None), 'avg_mb', 0) or 0
        if n_tiles > 10000 or est_mb > 300:
            proceed = QMessageBox.question(
                self, 'Geração de arquivo grande',
                f'Este arquivo é grande (~{est_mb:.0f} MB, {n_tiles} tiles). A geração pode '
                'levar vários minutos e a janela do QGIS pode parecer travada durante o '
                'processo — isso é normal. Não feche o QGIS.\n\nDeseja continuar?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if proceed != QMessageBox.StandardButton.Yes:
                self._append('Geração cancelada pelo usuário.')
                self._running = False
                self._set_back_enabled(True)
                return

        # Warm the HTTP cache for online (XYZ) basemaps FIRST, responsively. The render
        # itself downloads tiles synchronously on the GUI thread and would otherwise
        # freeze the window with no feedback; pre-fetching makes the download a live,
        # cancellable step and leaves the render instant (cache hit). Best-effort no-op
        # for offline/local basemaps.
        self._append('Preparando o mapa base…')
        prefetched = prefetch_basemap_tiles(
            spec.layers, spec.filtered_tiles, spec.max_zoom, wizard.feedback)
        if prefetched:
            self._append(f'Mapa base pré-carregado ({prefetched} tiles).')
        if wizard.feedback.canceled:
            self._append('Geração cancelada.')
            self._running = False
            self._set_back_enabled(True)
            return

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

        if wizard.contour_page.contour_enabled():
            self._append('Gerando curvas de nível…')
            # Update the bar off the stale "Renderizando…" text and repaint before the
            # (blocking) DEM download starts, so the user sees the stage change.
            wizard.feedback.heartbeat('Gerando curvas de nível — baixando elevação…')
            QCoreApplication.processEvents()
            try:
                contour_layer = generate_contours(
                    wizard.region_result.wgs84_extent,
                    wizard.contour_page.dem_source(),
                    wizard.contour_page.interval(),
                    wizard.contour_page.smoothing(),
                    wizard.contour_page.color(),
                    wizard.feedback,
                    clip_polygons=wizard.polygons_wgs84,
                )
                if wizard.feedback.canceled:
                    engine.cleanup_resources()
                    self._append('Geração cancelada.')
                    self._running = False
                    self._set_back_enabled(True)
                    return
                self._append(f'{contour_layer.featureCount()} curvas de nível geradas.')
                self._append('Exportando curvas de nível…')
                export_vector_layers(
                    engine.writer, [contour_layer],
                    QgsProject.instance().transformContext(), wizard.feedback,
                    progress_start=85, progress_span=5)
                if wizard.feedback.canceled:
                    engine.cleanup_resources()
                    self._append('Geração cancelada.')
                    self._running = False
                    self._set_back_enabled(True)
                    return
            except ContourError as exc:
                self._append(f'Aviso: curvas de nível não incluídas — {exc}')
            except Exception as exc:
                self._append(f'Aviso: erro ao gerar curvas de nível — {exc}')

        if wizard.grg_page.grg_enabled():
            grid_type, grg_opts = wizard.grg_page.grg_options()
            self._append(f'Gerando grade GRG ({_GRG_TYPE_LABELS.get(grid_type, grid_type)})…')
            bounds = wizard.region_result.wgs84_extent
            ok = engine.writer.writeGrg(bounds, grid_type, grg_opts)
            if not ok:
                self._append('Aviso: falha ao gerar grade GRG (grade não incluída).')

        if wizard.params_page.elevation_enabled():
            self._append('Baixando dados de altitude do terreno…')
            wizard.feedback.heartbeat('Baixando dados de altitude…')
            QCoreApplication.processEvents()
            try:
                stored = write_elevation_tiles(
                    engine.writer,
                    wizard.region_result.wgs84_extent,
                    wizard.feedback,
                )
                if stored:
                    self._append(f'{stored} tile(s) de altitude incluídos.')
                else:
                    # Never fatal: a map without terrain is still a map, and the
                    # app falls back to fetching altitude itself when online.
                    self._append('Aviso: nenhum tile de altitude baixado — '
                                 'o app buscará a altitude quando houver internet.')
            except Exception as exc:
                self._append(f'Aviso: altitude não incluída — {exc}')

        # Repaint before the (main-thread) commit so the window shows the stage and
        # doesn't read as frozen while the file is written out.
        self._append('Finalizando o arquivo…')
        wizard.feedback.heartbeat('Finalizando o arquivo…')
        QCoreApplication.processEvents()
        if not engine.finalize() or not os.path.exists(output_file):
            self._append('ERRO: não foi possível finalizar/publicar o arquivo gerado.')
            self._running = False
            self._set_back_enabled(True)
            return

        size_mb = os.path.getsize(output_file) / (1024 * 1024)
        if wizard.is_upload_mode:
            self._append(f'Arquivo gerado: {size_mb:.1f} MB')
            if os.path.getsize(output_file) > _UPLOAD_HARD_LIMIT_BYTES:
                self._append('ERRO: o arquivo excede o limite de 100 MB do servidor. '
                             'Reduza a área, a resolução ou a qualidade.')
                self._running = False
                self._set_back_enabled(True)
                return
            self._upload(output_file, file_name)
        else:
            self.progress.setValue(100)
            self._append(f'Concluído! Arquivo gerado: {size_mb:.1f} MB')
            self.output_label.setText(f'Salvo em: {output_file}')
            self._done = True
            self._running = False
            self.completeChanged.emit()

    def _upload(self, output_file, file_name):
        try:
            from ..tairu_firebase.config import TAIRUDB_OBJECT_PATH
            from ..tairu_firebase.http import FirebaseError
            from ..tairu_firebase.models import now_millis
            from ..tairu_sync.tasks import run_task
        except ImportError:
            from tairu_firebase.config import TAIRUDB_OBJECT_PATH
            from tairu_firebase.http import FirebaseError
            from tairu_firebase.models import now_millis
            from tairu_sync.tasks import run_task

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
                    f'Já existe um arquivo chamado "{file_name}" nesta expedição. '
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
            self._append('Concluído! O arquivo já aparece na expedição do Tairu Maps.')
            self.output_label.setText(f'Enviado para: {tmap.nome}')
            self._done = True
            self._running = False
            self.completeChanged.emit()
            dock.notify(f'{file_name} enviado para {tmap.nome}.')

        def on_error(message):
            self._append(f'Falha no envio: {message}')
            self._running = False
            self._set_back_enabled(True)

        def on_progress(fraction, message):
            with contextlib.suppress(RuntimeError):
                self.progress.setValue(int(fraction * 100))
                if message:
                    self._append(message)

        run_task(f'Tairu Maps: upload {file_name}', send,
                 on_success=on_success, on_error=on_error, on_progress=on_progress)
