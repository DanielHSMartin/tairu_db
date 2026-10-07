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
        from qgis.PyQt.QtCore import Qt
        from tairu_ui.layer_table import CHECK_COL
        from tairu_ui.local_generate_wizard import VectorLayersPage

        visivel, oculta, no_grupo = _build_project()
        page = VectorLayersPage(None)
        page.initializePage()

        self.assertEqual(page._layer_ids, [visivel.id()])
        self.assertTrue(page.hidden_label.text())
        # nada vem marcado: as camadas vetoriais são opcionais no arquivo
        self.assertEqual(page.selected_vector_layers(), [])

        page.layer_table.item(0, CHECK_COL).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(page.selected_vector_layers(), [visivel])

        # o assistente não é modal: esconder DEPOIS de marcar também tem de valer
        QgsProject.instance().layerTreeRoot().findLayer(
            visivel.id()).setItemVisibilityChecked(False)
        self.assertEqual(page.selected_vector_layers(), [])
        # e o que caiu tem de ser dito, não descartado em silêncio
        self.assertEqual(page.dropped_hidden, [visivel.name()])

    def test_same_table_as_the_push_dialog(self):
        """Colunas, nome com a pasta e ordem do painel, como no envio de camadas; a
        camada de registros do Tairu aparece desligada (a exportação a descarta) e a
        aberta de um .tairudb fica fora (é resultado, não fonte)."""
        from qgis.PyQt.QtCore import Qt
        from tairu_core.layer_tree import TAIRUDB_VIEW_PROPERTY
        from tairu_ui.layer_table import CHECK_COL, NAME_COL
        from tairu_ui.local_generate_wizard import VectorLayersPage

        project = QgsProject.instance()
        project.clear()
        root = project.layerTreeRoot()
        avulsa = _layer('avulsa')
        pontos = _layer('Pontos')
        registros = QgsVectorLayer(
            'Point?crs=EPSG:4326&field=recordId:string&field=tairuSyncHash:string', 'registros', 'memory')
        project.addMapLayer(registros, False)
        aberta = _layer('aberta')
        aberta.setCustomProperty(TAIRUDB_VIEW_PROPERTY, '/tmp/x.tairudb')
        root.addLayer(avulsa)
        root.addGroup('Expedição').addLayer(pontos)
        root.addLayer(registros)
        root.addLayer(aberta)

        page = VectorLayersPage(None)
        page.initializePage()
        table = page.layer_table

        self.assertEqual([table.horizontalHeaderItem(c).text() for c in range(table.columnCount())],
                         ['Incluir', 'Camada', 'Geometria', 'Feições', 'SRC', 'Origem'])
        self.assertEqual(page._layer_ids, [avulsa.id(), pontos.id(), registros.id()])
        self.assertEqual([table.item(r, NAME_COL).text() for r in range(table.rowCount())],
                         ['avulsa', 'Expedição / Pontos', 'registros'])
        self.assertEqual([table.item(r, table.columnCount() - 1).text() for r in range(table.rowCount())],
                         ['QGIS', 'QGIS', 'Tairu Maps'])

        caixa = table.item(2, CHECK_COL)
        self.assertFalse(caixa.flags() & Qt.ItemFlag.ItemIsEnabled, 'registros do Tairu não se marcam')
        self.assertTrue(caixa.toolTip())
        self.assertFalse(page.records_label.isHidden())
        caixa.setCheckState(Qt.CheckState.Checked)  # nem à força
        self.assertEqual(page.selected_vector_layers(), [])

        # a escolha sobrevive a voltar e avançar (initializePage roda a cada visita)
        table.item(1, CHECK_COL).setCheckState(Qt.CheckState.Checked)
        page.initializePage()
        self.assertEqual(page.selected_vector_layers(), [pontos])


class TestWizardVectorPageEdges(unittest.TestCase):
    """Casos achados na revisão de 07/10/2026."""

    def test_one_row_per_layer_records_dropped_and_honest_empty_message(self):
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import Qt, QVariant
        from tairu_core.layer_tree import TAIRUDB_VIEW_PROPERTY
        from tairu_ui.layer_table import CHECK_COL
        from tairu_ui.local_generate_wizard import VectorLayersPage

        project = QgsProject.instance()
        project.clear()
        root = project.layerTreeRoot()
        # a mesma camada em dois nós do painel: uma linha só (senão entraria duas vezes)
        dupla = _layer('dupla')
        root.addLayer(dupla)
        root.addGroup('outro grupo').addLayer(dupla)
        page = VectorLayersPage(None)
        page.initializePage()
        self.assertEqual(page._layer_ids, [dupla.id()])

        # marcada e DEPOIS virou camada de registros (enviada com o assistente aberto): cai
        page.layer_table.item(0, CHECK_COL).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(page.selected_vector_layers(), [dupla])
        dupla.dataProvider().addAttributes([QgsField('recordId', QVariant.String),
                                            QgsField('tairuSyncHash', QVariant.String)])
        dupla.updateFields()
        self.assertEqual(page.selected_vector_layers(), [])
        self.assertEqual(page.dropped_records, ['dupla'])   # e a geração diz por quê

        # só camadas abertas de um .tairudb: nada listado, e não é "nenhuma neste projeto"
        project.clear()
        aberta = _layer('aberta')
        aberta.setCustomProperty(TAIRUDB_VIEW_PROPERTY, '/tmp/x.tairudb')
        project.layerTreeRoot().addLayer(aberta)
        page.initializePage()
        self.assertEqual(page._layer_ids, [])
        self.assertTrue(page.hidden_label.isHidden())

        # e o projeto vazio de verdade diz isso
        project.clear()
        page.initializePage()
        self.assertFalse(page.hidden_label.isHidden())
        self.assertTrue(page.hidden_label.text())


    def test_layer_inside_a_group_rendered_as_a_layer_is_listed(self):
        # layerOrder() é a ordem de DESENHO: devolve o grupo "renderizado como camada" no
        # lugar das camadas de dentro dele, e elas sumiam da lista sem aviso.
        from qgis.core import QgsGroupLayer
        from tairu_ui.local_generate_wizard import VectorLayersPage

        project = QgsProject.instance()
        project.clear()
        root = project.layerTreeRoot()
        solta, no_grupo = _layer('solta'), _layer('no_grupo')
        root.addLayer(solta)
        grupo = root.addGroup('Grupo')
        grupo.addLayer(no_grupo)
        # o que o QGIS faz em "Renderizar camadas como grupo": converte e registra a camada
        project.addMapLayer(grupo.convertToGroupLayer(
            QgsGroupLayer.LayerOptions(project.transformContext())), False)
        self.assertNotIn(no_grupo, root.layerOrder())
        # tabela sem geometria (um CSV de apoio) não entra: no envio viria marcada
        tabela = QgsVectorLayer('None?field=id:integer', 'tabela', 'memory')
        project.addMapLayer(tabela)
        page = VectorLayersPage(None)
        page.initializePage()
        self.assertEqual(page._layer_ids, [solta.id(), no_grupo.id()])

    def test_disabled_row_still_names_its_layer(self):
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QVariant
        from tairu_ui.layer_table import NAME_COL
        from tairu_ui.local_generate_wizard import VectorLayersPage

        project = QgsProject.instance()
        project.clear()
        registros = _layer('registros_ponto')
        registros.dataProvider().addAttributes([QgsField('recordId', QVariant.String),
                                                QgsField('tairuSyncHash', QVariant.String)])
        registros.updateFields()
        project.layerTreeRoot().addLayer(registros)
        page = VectorLayersPage(None)
        page.initializePage()
        dica = page.layer_table.item(0, NAME_COL).toolTip()
        self.assertIn('registros_ponto', dica)   # o nome com a pasta sai cortado na tela
        self.assertIn('registros do Tairu', dica)


class TestPushDialogLayerStep(unittest.TestCase):

    def test_only_tairudb_views_is_not_called_an_empty_project(self):
        from tairu_core.layer_tree import TAIRUDB_VIEW_PROPERTY
        from tairu_ui.push_dialog import PushDialog

        project = QgsProject.instance()
        project.clear()
        aberta = _layer('aberta')
        aberta.setCustomProperty(TAIRUDB_VIEW_PROPERTY, '/tmp/x.tairudb')
        project.layerTreeRoot().addLayer(aberta)

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
            self.assertEqual(dialog._layer_ids, [])
            texto = dialog.hidden_label.text()
            self.assertNotIn('neste projeto', texto)   # o painel mostra a camada
            self.assertIn('.tairudb', texto)
        finally:
            dialog.deleteLater()

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

        # camada aberta de um .tairudb: é resultado de geração, não fonte — marcada como
        # tudo aqui, subiria de volta como registros novos
        from tairu_core.layer_tree import TAIRUDB_VIEW_PROPERTY
        aberta = _layer('aberta')
        aberta.setCustomProperty(TAIRUDB_VIEW_PROPERTY, '/tmp/x.tairudb')
        QgsProject.instance().layerTreeRoot().addLayer(aberta)

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
