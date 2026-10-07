# -*- coding: utf-8 -*-

"""Canvas rectangle picker wrapping QgsMapToolExtent: activates the tool,
emits the drawn rectangle (project CRS) once, then restores the previous
map tool.

`canceled` existe porque quem chama ESCONDE a janela enquanto o usuário
desenha: sem um aviso de desistência (Esc, ou trocar de ferramenta no QGIS),
a janela ficaria escondida para sempre e o assistente pareceria ter sumido.

Visto em campo (QGIS 4.2, Windows, vários plugins): a ferramenta era trocada
logo depois de ativada, antes de qualquer clique no mapa, e o assistente
fechava e reabria sem deixar desenhar. Não reproduz sem o QGIS completo do
usuário — quem troca é o QGIS dele ou outro plugin, nunca este. Por isso uma
desativação nos primeiros instantes retoma a ferramenta UMA vez e registra
quem a tomou, no log TairuDB; a segunda vale como desistência."""

import time

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.core import Qgis, QgsMessageLog, QgsRectangle
from qgis.gui import QgsMapToolExtent

# ponytail: janela fixa; se o ladrão agir no 1º clique do mapa, trocar por "antes do 1º press".
_GRACE_S = 1.0


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
        self._started_at = 0.0
        self._reclaimed = False

    def start(self):
        self._previous_tool = self.canvas.mapTool()
        self._active = True
        self._started_at = time.monotonic()
        self._reclaimed = False
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
        if not self._reclaimed and time.monotonic() - self._started_at < _GRACE_S:
            # Adiado: o deactivated sai de DENTRO do setMapTool de quem tomou a
            # ferramenta, que ainda vai gravar a dele por cima da nossa.
            self._reclaimed = True
            QTimer.singleShot(0, self._reclaim)
            return
        self._active = False
        self._previous_tool = None
        self.canceled.emit()

    def _reclaim(self):
        if not self._active:
            return
        thief = self.canvas.mapTool()
        action = thief.action() if thief is not None else None
        QgsMessageLog.logMessage(
            'Desenhar retângulo: a ferramenta foi trocada logo após ativar, por '
            f'{type(thief).__name__ if thief is not None else "nenhuma"}'
            f'{" (" + action.text() + ")" if action is not None else ""}; retomando.',
            'TairuDB', Qgis.MessageLevel.Warning)
        self.canvas.setMapTool(self.tool)

    def _on_extent_changed(self, rect):
        if not self._active or rect is None or rect.isEmpty():
            return
        picked = QgsRectangle(rect)
        self.stop()
        self.extentPicked.emit(picked)
