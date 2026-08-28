# -*- coding: utf-8 -*-

"""Canvas rectangle picker wrapping QgsMapToolExtent: activates the tool,
emits the drawn rectangle (project CRS) once, then restores the previous
map tool.

`canceled` existe porque quem chama ESCONDE a janela enquanto o usuário
desenha: sem um aviso de desistência (Esc, ou trocar de ferramenta no QGIS),
a janela ficaria escondida para sempre e o assistente pareceria ter sumido."""

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsRectangle
from qgis.gui import QgsMapToolExtent


class ExtentPicker(QObject):

    extentPicked = pyqtSignal(object)   # QgsRectangle in the project CRS
    canceled = pyqtSignal()             # ferramenta abandonada sem desenhar

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.tool = QgsMapToolExtent(canvas)
        self.tool.extentChanged.connect(self._on_extent_changed)
        self.tool.deactivated.connect(self._on_deactivated)
        self._previous_tool = None
        self._active = False

    def start(self):
        self._previous_tool = self.canvas.mapTool()
        self._active = True
        self.canvas.setMapTool(self.tool)

    def stop(self):
        if not self._active:
            return
        self._active = False
        if self._previous_tool is not None:
            self.canvas.setMapTool(self._previous_tool)
        else:
            self.canvas.unsetMapTool(self.tool)
        self._previous_tool = None

    def _on_deactivated(self):
        """Só dispara em desistência: stop() zera _active ANTES de trocar a ferramenta."""
        if not self._active:
            return
        self._active = False
        self._previous_tool = None
        self.canceled.emit()

    def _on_extent_changed(self, rect):
        if not self._active or rect is None or rect.isEmpty():
            return
        picked = QgsRectangle(rect)
        self.stop()
        self.extentPicked.emit(picked)
