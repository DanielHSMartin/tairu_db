# -*- coding: utf-8 -*-

"""Self-check for the closed-dialog leak that crashed QGIS on "Receber Registros".

A QDialog/QWizard parented to the dock or to the QGIS main window is owned by C++:
dropping the last Python reference does NOT destroy it. Every closed one stayed alive
hidden, and its QgsMapLayerComboBox kept answering layerChanged from inside
QgsMapLayerModel::endInsertRows on every QgsProject.addMapLayer — N dead slots per
pull (SIGSEGV after a plugin reload, GUI freeze from N re-runs of build_push_plan).

Needs the QGIS Python (qgis.core/qgis.gui); skipped elsewhere.
"""

import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import QgsApplication, QgsProject, QgsVectorLayer
    from qgis.gui import QgsMapLayerComboBox
    from qgis.PyQt.QtCore import QCoreApplication, QEvent
    from qgis.PyQt.QtWidgets import QDialog, QWidget
except ImportError:  # pragma: no cover - no QGIS on this interpreter
    QgsApplication = None

_APP = None


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], False)
    _APP.initQgis()


class _Dialog(QDialog):
    def __init__(self, parent, seen):
        super().__init__(parent)
        self._seen = seen
        self.combo = QgsMapLayerComboBox(self)
        self.combo.layerChanged.connect(lambda _layer: self._seen.append(id(self)))


class TestClosedDialogsStopListening(unittest.TestCase):
    def test_deleted_dialog_does_not_react_to_added_layers(self):
        seen = []
        parent = QWidget()  # stands in for the dock / QGIS main window

        for _ in range(3):
            dialog = _Dialog(parent, seen)
            dialog.close()
            dialog.deleteLater()  # the fix; without it the C++ widget survives
            del dialog
        # deleteLater posts a DeferredDelete event; outside an event loop it has to
        # be drained explicitly (QGIS's running main loop does this on its own).
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        self.assertEqual(parent.findChildren(QDialog), [])

        layer = QgsVectorLayer('Point?crs=EPSG:4326&field=id:integer', 'novo', 'memory')
        QgsProject.instance().addMapLayer(layer, False)
        self.assertEqual(seen, [])


if __name__ == '__main__':
    unittest.main()
