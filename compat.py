# -*- coding: utf-8 -*-

"""
QGIS 4 (PyQt6) / QGIS 3 (PyQt5) compatibility constants shared by the plugin.

All version-dependent enum lookups live here so the rest of the codebase can
import a single stable name regardless of the QGIS/PyQt generation.
"""

from qgis.PyQt.QtCore import Qt, QBuffer
from qgis.PyQt.QtGui import QImage
from qgis.core import Qgis, QgsProcessingAlgorithm

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

try:
    from qgis.PyQt.QtCore import QIODeviceBase
    _OPEN_WRITE_ONLY = QIODeviceBase.WriteOnly         # PyQt6
    _OPEN_READ_ONLY = QIODeviceBase.ReadOnly
except ImportError:
    _OPEN_WRITE_ONLY = QBuffer.WriteOnly               # PyQt5
    _OPEN_READ_ONLY = QBuffer.ReadOnly

try:
    _FMT_ARGB32 = QImage.Format.Format_ARGB32          # PyQt6
except AttributeError:
    _FMT_ARGB32 = QImage.Format_ARGB32                 # PyQt5

try:
    _FLAG_NO_THREADING = QgsProcessingAlgorithm.Flag.FlagNoThreading
except AttributeError:
    _FLAG_NO_THREADING = QgsProcessingAlgorithm.FlagNoThreading

try:
    _DOCK_RIGHT_AREA = Qt.DockWidgetArea.RightDockWidgetArea  # PyQt6
except AttributeError:
    _DOCK_RIGHT_AREA = Qt.RightDockWidgetArea                  # PyQt5

try:
    _USER_ROLE = Qt.ItemDataRole.UserRole                      # PyQt6
except AttributeError:
    _USER_ROLE = Qt.UserRole                                   # PyQt5

from qgis.PyQt.QtWidgets import QLineEdit as _QLineEdit
try:
    _ECHO_PASSWORD = _QLineEdit.EchoMode.Password              # PyQt6
except AttributeError:
    _ECHO_PASSWORD = _QLineEdit.Password                       # PyQt5

from qgis.core import QgsVectorFileWriter as _QgsVectorFileWriter
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

from qgis.core import QgsSymbolLayer as _QgsSymbolLayer
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

try:
    _MSG_WARNING = Qgis.MessageLevel.Warning                                              # QGIS 4
except AttributeError:
    _MSG_WARNING = Qgis.Warning                                                           # QGIS 3

from qgis.core import QgsMapLayerProxyModel as _QgsMapLayerProxyModel
try:
    _VECTOR_LAYER_FILTER = _QgsMapLayerProxyModel.Filter.VectorLayer                      # QGIS 4
    _POLYGON_LAYER_FILTER = _QgsMapLayerProxyModel.Filter.PolygonLayer
except AttributeError:
    _VECTOR_LAYER_FILTER = _QgsMapLayerProxyModel.VectorLayer                             # QGIS 3
    _POLYGON_LAYER_FILTER = _QgsMapLayerProxyModel.PolygonLayer


def _exec_dialog(dialog):
    """QDialog.exec() (PyQt6) / exec_() (older PyQt5)."""
    try:
        return dialog.exec()
    except AttributeError:
        return dialog.exec_()
