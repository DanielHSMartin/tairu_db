# -*- coding: utf-8 -*-

"""Desenhar retângulo: ferramenta tomada logo após ativar não fecha o desenho.

Em campo (QGIS 4.2, Windows) algo trocava a ferramenta assim que ela era
ativada e o assistente reabria sem deixar desenhar. Aqui o "ladrão" é simulado
com um setMapTool qualquer.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QGIS_CUSTOM_CONFIG_PATH', tempfile.mkdtemp(prefix='qgis-test-'))

try:
    from qgis.core import QgsApplication
    from qgis.gui import QgsMapCanvas, QgsMapToolPan
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], True)
    _APP.initQgis()


class ExtentPickerStolenToolTest(unittest.TestCase):

    def setUp(self):
        from tairu_ui.extent_tool import ExtentPicker
        self.canvas = QgsMapCanvas()
        self.pan = QgsMapToolPan(self.canvas)
        self.other = QgsMapToolPan(self.canvas)
        self.canvas.setMapTool(self.pan)
        self.picker = ExtentPicker(self.canvas)
        self.canceled = []
        self.picker.canceled.connect(lambda: self.canceled.append(True))
        self.picker.start()

    def _steal(self):
        self.canvas.setMapTool(self.other)
        _APP.processEvents()

    def test_tomada_logo_apos_ativar_e_retomada(self):
        self._steal()
        self.assertIs(self.canvas.mapTool(), self.picker.tool)
        self.assertEqual(self.canceled, [])

    def test_tomada_de_novo_vale_como_desistencia(self):
        self._steal()
        self._steal()
        self.assertEqual(self.canceled, [True])
        self.assertIs(self.canvas.mapTool(), self.other)

    def test_tomada_depois_da_janela_vale_como_desistencia(self):
        self.picker._started_at -= 10
        self._steal()
        self.assertEqual(self.canceled, [True])

    def test_stop_nao_e_desistencia(self):
        self.picker.stop()
        _APP.processEvents()
        self.assertEqual(self.canceled, [])
        self.assertIs(self.canvas.mapTool(), self.pan)


if __name__ == '__main__':
    unittest.main()
