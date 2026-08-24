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


class TestPushDialogLayerStep(unittest.TestCase):

    def test_step_one_only_offers_visible_layers_and_pre_checks_them(self):
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
            from tairu_ui.push_dialog import _CHECKED, _LAYER_NAME_COL, _LAYER_SEND_COL, _UNCHECKED

            self.assertEqual(dialog._layer_ids, [visivel.id()])
            self.assertEqual(dialog.layer_table.rowCount(), 1)
            self.assertEqual(dialog.layer_table.item(0, _LAYER_NAME_COL).text(), 'visivel')
            # dados da camada, não só o nome: geometria / feições / SRC / origem
            self.assertEqual(
                [dialog.layer_table.item(0, c).text()
                 for c in range(_LAYER_NAME_COL, dialog.layer_table.columnCount())],
                ['visivel', 'Ponto', '0', visivel.crs().authid() or '—', 'QGIS'])
            # "todas já vêm selecionadas"
            self.assertEqual(dialog.layer_table.item(0, _LAYER_SEND_COL).checkState(), _CHECKED)
            self.assertEqual(dialog.selected_layers(), [visivel])
            self.assertTrue(dialog.hidden_label.text(), 'camada oculta tem de ser dita')

            # desmarcar a última camada desliga o Avançar
            dialog.layer_table.item(0, _LAYER_SEND_COL).setCheckState(_UNCHECKED)
            self.assertEqual(dialog.selected_layers(), [])
            self.assertFalse(dialog.next_btn.isEnabled())

            # etapa 3: grupo já marcado, e desmarcar desliga o campo de nome
            self.assertTrue(dialog.group_check.isChecked())
            self.assertTrue(dialog.group_name_edit.isEnabled())
            self.assertIsNone(dialog.group_name_edit.graphicsEffect())

            dialog.group_check.setChecked(False)
            self.assertFalse(dialog.group_name_edit.isEnabled())
            self.assertFalse(dialog.group_name_label.isEnabled())
            # E TEM de parecer desligado: sem regra `:disabled` para QLineEdit no
            # TAIRU_STYLE_SHEET, um setEnabled(False) sozinho fica idêntico a um
            # campo ativo — foi exatamente o que o usuário viu.
            self.assertIsNotNone(dialog.group_name_edit.graphicsEffect())
            self.assertIsNotNone(dialog.group_name_label.graphicsEffect())

            dialog.group_check.setChecked(True)
            self.assertTrue(dialog.group_name_edit.isEnabled())
            self.assertIsNone(dialog.group_name_edit.graphicsEffect())
        finally:
            dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
