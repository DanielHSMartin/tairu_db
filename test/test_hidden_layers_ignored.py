# -*- coding: utf-8 -*-

"""Camada desmarcada no painel de camadas é ignorada pelo plugin.

Cobre a armadilha da API: em QGIS 3 quem responde "aparece no mapa?" é
QgsLayerTreeNode.isVisible() (sobe pelos grupos pai). O
isItemVisibilityCheckedRecursive(), de nome tentador, olha para os FILHOS e devolve
True para uma camada marcada dentro de um grupo desmarcado — trocar um pelo outro
faria camadas ocultas voltarem a ser assadas no .tairudb sem erro nenhum.

Precisa do Python do QGIS (qgis.core/qgis.gui); pulado em outros interpretadores.
"""

import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import QgsApplication, QgsProject, QgsVectorLayer
    from qgis.PyQt.QtWidgets import QWidget
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], True)
    _APP.initQgis()


def _layer(name):
    layer = QgsVectorLayer('Point?crs=EPSG:4326&field=id:integer', name, 'memory')
    QgsProject.instance().addMapLayer(layer, False)
    return layer


def _build_project():
    """visível / desmarcada / marcada-dentro-de-grupo-desmarcado. Devolve as três."""
    project = QgsProject.instance()
    project.clear()
    root = project.layerTreeRoot()

    visivel, oculta, no_grupo = _layer('visivel'), _layer('oculta'), _layer('no_grupo')
    root.addLayer(visivel)
    root.addLayer(oculta)
    root.findLayer(oculta.id()).setItemVisibilityChecked(False)
    grupo = root.addGroup('grupo desmarcado')
    grupo.addLayer(no_grupo)
    grupo.setItemVisibilityChecked(False)
    return visivel, oculta, no_grupo


class TestLayerIsVisible(unittest.TestCase):

    def test_unchecked_layer_and_unchecked_group_both_count_as_hidden(self):
        from tairu_core.layer_tree import layer_is_visible, visible_layers

        visivel, oculta, no_grupo = _build_project()
        self.assertTrue(layer_is_visible(visivel))
        self.assertFalse(layer_is_visible(oculta))
        self.assertFalse(layer_is_visible(no_grupo), 'grupo desmarcado deve ocultar')
        self.assertEqual(visible_layers([visivel, oculta, no_grupo]), [visivel])

    def test_layer_outside_the_tree_is_not_visible(self):
        from tairu_core.layer_tree import layer_is_visible

        _build_project()
        solta = _layer('fora da arvore')  # addMapLayer(layer, False)
        self.assertFalse(layer_is_visible(solta))
        self.assertFalse(layer_is_visible(None))


class TestWizardVectorPage(unittest.TestCase):

    def test_hidden_layers_are_not_listed_nor_exported(self):
        from tairu_ui.local_generate_wizard import VectorLayersPage

        visivel, oculta, no_grupo = _build_project()
        page = VectorLayersPage(None)
        page.initializePage()

        listed = set(page._vector_checkboxes)
        self.assertEqual(listed, {visivel.id()})
        self.assertTrue(page.hidden_label.text())

        for checkbox in page._vector_checkboxes.values():
            checkbox.setChecked(True)
        self.assertEqual(page.selected_vector_layers(), [visivel])

        # o assistente não é modal: esconder DEPOIS de marcar também tem de valer
        QgsProject.instance().layerTreeRoot().findLayer(
            visivel.id()).setItemVisibilityChecked(False)
        self.assertEqual(page.selected_vector_layers(), [])
        # e o que caiu tem de ser dito, não descartado em silêncio
        self.assertEqual(page.dropped_hidden, [visivel.name()])


class TestPushDialogCombo(unittest.TestCase):

    def test_combo_only_offers_visible_layers(self):
        from tairu_ui.push_dialog import PushDialog

        visivel, oculta, no_grupo = _build_project()

        class _Tokens:
            uid = 'uid1'

        class _Dock(QWidget):
            tokens = _Tokens()

        class _Map:
            map_id = 'm1'
            nome = 'mapa'

            def role_for(self, _uid):
                return 'owner'

        dialog = PushDialog(_Dock(), _Map())
        try:
            offered = {dialog.layer_combo.layer(i).id()
                       for i in range(dialog.layer_combo.count())}
            self.assertEqual(offered, {visivel.id()})
            self.assertEqual(dialog.layer_combo.currentLayer().id(), visivel.id())
        finally:
            dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
