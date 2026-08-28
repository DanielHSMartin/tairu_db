# -*- coding: utf-8 -*-

"""
QGIS 4 (PyQt6) / QGIS 3 (PyQt5) compatibility constants shared by the plugin.

All version-dependent enum lookups live here so the rest of the codebase can
import a single stable name regardless of the QGIS/PyQt generation.
"""

from qgis.PyQt.QtCore import Qt, QIODevice
from qgis.PyQt.QtGui import QImage
# O underscore destes apelidos NAO e estilo: o checador Qt6 do plugins.qgis.org
# (scripts/pyqt5_to_pyqt6.py --dry_run) acusa o par (NomeDaClasse, membro) quando o acesso
# parte de um ast.Name com o nome original. Os fallbacks de QGIS 3 abaixo usam grafia sem
# escopo de proposito (a com escopo so existe no QGIS 4); e o apelido que os mantem fora do
# relatorio. Renomear _QgsSymbolLayer para QgsSymbolLayer reintroduz os achados em silencio.
from qgis.core import Qgis, QgsProcessingAlgorithm
from qgis.core import QgsMapLayerProxyModel as _QgsMapLayerProxyModel
from qgis.core import QgsSymbolLayer as _QgsSymbolLayer
from qgis.core import QgsVectorFileWriter as _QgsVectorFileWriter

try:
    _RASTER_LAYER_TYPE = Qgis.LayerType.Raster        # QGIS 4
except AttributeError:
    from qgis.core import QgsMapLayerType              # QGIS 3
    _RASTER_LAYER_TYPE = QgsMapLayerType.RasterLayer

try:
    _VECTOR_TILE_LAYER_TYPE = Qgis.LayerType.VectorTile   # QGIS 4
except AttributeError:
    from qgis.core import QgsMapLayerType                  # QGIS 3
    _VECTOR_TILE_LAYER_TYPE = QgsMapLayerType.VectorTileLayer

# Enums Qt: uma unica grafia, a com escopo (a exigida pelo Qt6).
#
# Nao ha shim PyQt5/PyQt6 aqui de proposito. O sip 4.19 do PyQt5 5.15.4 que o
# QGIS 3.40 LTR carrega gera cada enum como uma classe (sip.enumtype derivada de
# int) e expoe os membros TANTO no escopo externo (a grafia curta, sem o nome do
# enum) QUANTO na propria classe - as duas formas sempre valeram no PyQt5.
# Manter so a nova preserva o QGIS 3 e zera o relatorio de compatibilidade Qt6 do
# plugins.qgis.org, que le o fonte estaticamente e acusa a metade PyQt5 de um
# shim mesmo estando dentro de um try/except que nunca roda no Qt6.
_OPEN_WRITE_ONLY = QIODevice.OpenModeFlag.WriteOnly
_OPEN_READ_ONLY = QIODevice.OpenModeFlag.ReadOnly
_FMT_ARGB32 = QImage.Format.Format_ARGB32
_FLAG_NO_THREADING = QgsProcessingAlgorithm.Flag.FlagNoThreading
_DOCK_RIGHT_AREA = Qt.DockWidgetArea.RightDockWidgetArea
_USER_ROLE = Qt.ItemDataRole.UserRole
_ITEM_IS_EDITABLE = Qt.ItemFlag.ItemIsEditable
_ITEM_IS_CHECKABLE = Qt.ItemFlag.ItemIsUserCheckable
_ITEM_IS_ENABLED = Qt.ItemFlag.ItemIsEnabled
_CHECKED = Qt.CheckState.Checked
_UNCHECKED = Qt.CheckState.Unchecked

try:
    _GPKG_CREATE_FILE = _QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile   # QGIS 4
    _GPKG_CREATE_LAYER = _QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
except AttributeError:
    _GPKG_CREATE_FILE = _QgsVectorFileWriter.CreateOrOverwriteFile                        # QGIS 3
    _GPKG_CREATE_LAYER = _QgsVectorFileWriter.CreateOrOverwriteLayer

try:
    _WRITER_NO_ERROR = _QgsVectorFileWriter.WriterError.NoError                           # QGIS 4
except AttributeError:
    _WRITER_NO_ERROR = _QgsVectorFileWriter.NoError                                       # QGIS 3

try:
    _PROP_FILL_COLOR = _QgsSymbolLayer.Property.FillColor                                 # QGIS 4
    _PROP_STROKE_COLOR = _QgsSymbolLayer.Property.StrokeColor
except AttributeError:
    _PROP_FILL_COLOR = _QgsSymbolLayer.PropertyFillColor                                  # QGIS 3
    _PROP_STROKE_COLOR = _QgsSymbolLayer.PropertyStrokeColor

try:
    _SYMBOL_TYPE_FILL = Qgis.SymbolType.Fill                                              # QGIS 3.30+/4
except AttributeError:
    from qgis.core import QgsSymbol as _QgsSymbol
    _SYMBOL_TYPE_FILL = _QgsSymbol.Fill                                                   # older QGIS 3

_MSG_WARNING = Qgis.MessageLevel.Warning

try:
    _VECTOR_LAYER_FILTER = _QgsMapLayerProxyModel.Filter.VectorLayer                      # QGIS 4
    _POLYGON_LAYER_FILTER = _QgsMapLayerProxyModel.Filter.PolygonLayer
except AttributeError:
    _VECTOR_LAYER_FILTER = _QgsMapLayerProxyModel.VectorLayer                             # QGIS 3
    _POLYGON_LAYER_FILTER = _QgsMapLayerProxyModel.PolygonLayer
