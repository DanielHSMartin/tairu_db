# -*- coding: utf-8 -*-

"""Registros organizados por grupo, como no aplicativo.

Cada teste aqui existe por causa de uma falha que NAO aparece na tela: ou apaga
registro em silencio, ou esconde registro, ou mostra a arvore vazia.

1) Uma camada FILTRADA (a pasta de um grupo, ou o Filtrar... do QGIS) nao pode
   propagar exclusoes. `getFeatures()` honra o filtro e o snapshot de sincronizacao
   e gravado a partir dele, entao "estava no snapshot e sumiu da camada" passa a
   significar "esta em outro grupo". Sem a guarda, mover um registro de grupo no
   aplicativo fazia o envio seguinte gravar isDeleted=True nele.
2) A exclusao de verdade (camada sem filtro, feicao removida) tem de continuar valendo
   — uma guarda que apaga o recurso nao e conserto.
3) O pull INCREMENTAL tem de aplicar mudanca de grupo. O groupId nao entra no
   tairuSyncHash de proposito (mexer nele reclassificaria a expedicao inteira como
   alterada no proximo envio), entao a comparacao do grupo e separada; sem ela o
   registro ficava para sempre na pasta antiga.
4) Nenhum registro pode ficar invisivel: um groupId que aponta para grupo apagado ou
   inexistente cai em "Sem grupo", exatamente como no aplicativo.
5) A hierarquia e a ordem sao as do aplicativo (porte de effectiveParentId e
   foldForMatching), incluindo ciclo, orfao e acento.

Precisa do Python do QGIS (qgis.core); pulado em outros interpretadores.
"""

import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import QgsApplication, QgsLayerTree, QgsProject
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None
_MAP_ID = 'map-de-teste'
_MAPPING = {'tipo': 'local', 'sub_tipo': 'outroLocal', 'situation': 'Ativo'}


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], False)
    _APP.initQgis()


class _Mapa(object):
    map_id = _MAP_ID
    nome = 'Expedição de teste'

    def role_for(self, _uid):
        return 'owner'


def _record(index, group_id, nome=None):
    from tairu_firebase.models import TairuRecord, points_to_json

    return TairuRecord(
        record_id='r%d' % index,
        nome=nome or 'Registro %d' % index,
        tipo_registro='local',
        geometry_type='point',
        geometry_points_json=points_to_json([(-20.0 - index * 0.01, -44.0)], ts=1),
        group_id=group_id,
        created_by='u1',
        created_at=1,
        last_modified=1000 + index,
    )


def _group(group_id, name, parent='', deleted=False):
    from tairu_firebase.models import TairuRecordGroup

    return TairuRecordGroup(group_id=group_id, name=name,
                            parent_group_id=parent, is_deleted=deleted)


def _fresh_gpkg(records):
    from tairu_sync.record_convert import apply_pull

    path = os.path.join(tempfile.mkdtemp(), 'records.gpkg')
    apply_pull(path, records)
    return path


def _record_symbol_layer(layer, record_id):
    for categoria in layer.renderer().categories():
        if categoria.value() == record_id:
            return categoria.symbol().symbolLayer(0)
    return None


def _leaf(bucket, spec_key='point'):
    key = '%s|%s|%s' % (_MAP_ID, spec_key, bucket)
    for layer in QgsProject.instance().mapLayers().values():
        if layer.customProperty('tairu/folder', '') == key:
            return layer
    return None


class TestFilteredViewNeverDeletes(unittest.TestCase):
    """A guarda que separa "a contagem mente" de "o registro sumiu da nuvem"."""

    def setUp(self):
        QgsProject.instance().clear()

    def test_registro_reagrupado_no_app_nao_vira_exclusao(self):
        from tairu_sync.push import build_push_plan
        from tairu_sync.record_convert import apply_pull, sync_record_layers

        grupos = [_group('gA', 'Alfa'), _group('gB', 'Beta')]
        gpkg = _fresh_gpkg([_record(i, 'gA') for i in range(3)]
                           + [_record(i, 'gB') for i in range(3, 6)])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)

        # O usuario move r0 de Alfa para Beta NO APLICATIVO; o pull traz a mudanca.
        apply_pull(gpkg, [_record(0, 'gB')], remove_missing=False)
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)

        vista = _leaf('gA')
        self.assertIsNotNone(vista)
        self.assertEqual(vista.featureCount(), 2, 'o pull incremental nao re-homou r0')

        plano = build_push_plan(vista, _MAPPING, _Mapa(), 'u1', propagate_deletions=True)
        apagados = [i.record.record_id for i in plano.items if i.action == 'delete']
        self.assertEqual(apagados, [], 'o envio da pasta antiga apagaria %s' % apagados)

    def test_filtro_manual_do_qgis_tambem_nao_apaga(self):
        from tairu_sync.push import build_push_plan
        from tairu_sync.record_convert import configure_record_layer_fields, open_gpkg_layer

        gpkg = _fresh_gpkg([_record(i, '') for i in range(4)])
        camada = open_gpkg_layer(gpkg, 'point')
        configure_record_layer_fields(camada)          # snapshot da tabela inteira
        camada.setSubsetString('"recordId" = \'r1\'')  # Construtor de Consultas

        plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1', propagate_deletions=True)
        self.assertEqual([i for i in plano.items if i.action == 'delete'], [])

    def test_exclusao_real_a_partir_de_uma_pasta_de_grupo(self):
        from tairu_sync.push import build_push_plan
        from tairu_sync.record_convert import sync_record_layers

        # Numa expedicao com grupos NAO existe mais camada sem filtro. Se a guarda fosse
        # "camada filtrada nunca propaga exclusao", nao sobraria caminho nenhum para
        # apagar um registro pelo QGIS — o envio diria "nada a excluir" e o recebimento
        # seguinte recriaria o registro. A pergunta e feita a TABELA, nao a vista.
        gpkg = _fresh_gpkg([_record(0, 'gA'), _record(1, 'gA')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        vista = _leaf('gA')
        alvo = [f.id() for f in vista.getFeatures()
                if f.attribute(vista.fields().indexOf('recordId')) == 'r1']
        vista.startEditing()
        vista.deleteFeature(alvo[0])

        # Ainda no buffer: nao e exclusao. "Descartar edicoes" devolveria a feicao aqui e
        # deixaria o registro apagado na nuvem.
        plano = build_push_plan(vista, _MAPPING, _Mapa(), 'u1', propagate_deletions=True)
        self.assertEqual([i for i in plano.items if i.action == 'delete'], [])

        vista.commitChanges()
        vista.reload()
        plano = build_push_plan(vista, _MAPPING, _Mapa(), 'u1', propagate_deletions=True)
        self.assertEqual([i.record.record_id for i in plano.items if i.action == 'delete'],
                         ['r1'])

    def test_exclusao_real_continua_funcionando(self):
        from tairu_sync.push import build_push_plan
        from tairu_sync.record_convert import configure_record_layer_fields, open_gpkg_layer

        gpkg = _fresh_gpkg([_record(i, '') for i in range(3)])
        camada = open_gpkg_layer(gpkg, 'point')
        configure_record_layer_fields(camada)
        alvo = [f.id() for f in camada.getFeatures()
                if f.attribute(camada.fields().indexOf('recordId')) == 'r2']
        camada.dataProvider().deleteFeatures(alvo)
        camada.reload()

        plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1', propagate_deletions=True)
        self.assertEqual([i.record.record_id for i in plano.items if i.action == 'delete'],
                         ['r2'])


class TestArvoreDeGrupos(unittest.TestCase):

    def setUp(self):
        QgsProject.instance().clear()

    def test_nenhum_registro_fica_invisivel(self):
        from tairu_sync.record_convert import sync_record_layers

        grupos = [_group('gA', 'Alfa'), _group('gApagado', 'Apagado', deleted=True)]
        gpkg = _fresh_gpkg([
            _record(0, 'gA'),
            _record(1, ''),           # sem grupo
            _record(2, 'gApagado'),   # orfao: grupo com lapide
            _record(3, 'inexistente'),  # orfao: grupo que nunca existiu
        ])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)

        vistos = set()
        for layer in QgsProject.instance().mapLayers().values():
            idx = layer.fields().indexOf('recordId')
            if idx < 0:
                continue
            for feature in layer.getFeatures():
                vistos.add(feature.attribute(idx))
        self.assertEqual(vistos, {'r0', 'r1', 'r2', 'r3'})

    def test_repetir_o_pull_nao_duplica_camada(self):
        from tairu_sync.record_convert import sync_record_layers

        grupos = [_group('gA', 'Alfa')]
        gpkg = _fresh_gpkg([_record(0, 'gA'), _record(1, '')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)
        antes = len(QgsProject.instance().mapLayers())
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)
        self.assertEqual(len(QgsProject.instance().mapLayers()), antes)

    def test_expedicao_sem_grupo_nenhum_nao_filtra(self):
        from tairu_sync.record_convert import sync_record_layers

        gpkg = _fresh_gpkg([_record(0, ''), _record(1, '')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [])
        camada = _leaf('')
        self.assertIsNotNone(camada)
        # Sem grupo nenhum, "NOT IN ()" seria SQL invalido: o filtro certo e nenhum.
        self.assertEqual(camada.subsetString(), '')

    def test_acima_do_teto_volta_ao_simbolo_unico(self):
        from tairu_sync.record_convert import MAX_RECORD_CATEGORIES, sync_record_layers

        grupos = [_group('gA', 'Alfa')]
        gpkg = _fresh_gpkg([_record(i, 'gA') for i in range(MAX_RECORD_CATEGORIES + 2)])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)
        # Abrir o no da legenda e quadratico nas categorias: acima do teto o QGIS
        # congelaria por dezenas de segundos ao expandir a pasta.
        self.assertEqual(type(_leaf('gA').renderer()).__name__, 'QgsSingleSymbolRenderer')

    def test_registro_desenhado_na_pasta_nasce_no_grupo(self):
        from qgis.core import QgsGeometry, QgsPointXY, QgsVectorLayerUtils
        from tairu_sync.record_convert import sync_record_layers

        grupos = [_group('gA', 'Alfa'), _group('gB', 'Beta')]
        gpkg = _fresh_gpkg([_record(0, 'gA')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)
        vista = _leaf('gA')

        # createFeature e o caminho que a ferramenta de digitalizacao e o formulario do
        # QGIS usam: e ele que aplica os valores padrao do campo. Sem o padrao, groupId
        # nasce nulo, a feicao nao casa com o filtro da pasta e SOME ao salvar a edicao.
        vista.startEditing()
        nova = QgsVectorLayerUtils.createFeature(
            vista, QgsGeometry.fromPointXY(QgsPointXY(-44.5, -20.5)),
            {vista.fields().indexOf('nome'): 'Desenhado no QGIS'})
        self.assertEqual(nova.attribute(vista.fields().indexOf('groupId')), 'gA')
        vista.addFeature(nova)
        vista.commitChanges()
        vista.reload()
        self.assertEqual(vista.featureCount(), 2)

    def test_uma_entrada_de_legenda_por_registro(self):
        from tairu_sync.record_convert import sync_record_layers

        grupos = [_group('gA', 'Alfa')]
        gpkg = _fresh_gpkg([_record(0, 'gA', 'Ponte velha'), _record(1, 'gA', 'Trilha')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)
        renderizador = _leaf('gA').renderer()
        self.assertEqual(type(renderizador).__name__, 'QgsCategorizedSymbolRenderer')
        rotulos = [c.label() for c in renderizador.categories() if c.value() is not None]
        self.assertEqual(sorted(rotulos), ['Ponte velha', 'Trilha'])
        # A categoria coringa e obrigatoria: sem ela a feicao recem-digitalizada, cujo
        # recordId so nasce no envio, nao casa com categoria nenhuma e NAO e pintada.
        self.assertTrue(any(c.value() is None for c in renderizador.categories()))

    def test_coringa_pinta_mas_nao_polui_a_lista_de_camadas(self):
        from qgis.core import QgsLayerTreeModel, QgsMapLayerLegendUtils
        from tairu_sync.record_convert import sync_record_layers

        # Ela existe no renderizador (senao a feicao nova fica invisivel) e NAO aparece na
        # lista de camadas, onde so confundiria quem le. E o quadradinho da lista tem
        # tamanho proprio: o simbolo do mapa segue o do aplicativo, que e grande demais
        # para virar linha de painel.
        gpkg = _fresh_gpkg([_record(0, 'gA', 'Ponte'), _record(1, 'gA', 'Trilha')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        camada = _leaf('gA')
        no = QgsProject.instance().layerTreeRoot().findLayer(camada.id())
        modelo = QgsLayerTreeModel(QgsProject.instance().layerTreeRoot())
        rotulos = [n.data(0) for n in modelo.layerLegendNodes(no)]

        self.assertEqual(sorted(rotulos), ['Ponte', 'Trilha'])
        self.assertTrue(any(c.value() is None for c in camada.renderer().categories()))
        self.assertIsNotNone(QgsMapLayerLegendUtils.legendNodeSymbolSize(no, 0))

    def test_feicao_recem_desenhada_e_desenhada_no_mapa(self):
        from qgis.core import QgsGeometry, QgsPointXY, QgsRenderContext, QgsVectorLayerUtils
        from tairu_sync.record_convert import sync_record_layers

        gpkg = _fresh_gpkg([_record(0, 'gA')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        vista = _leaf('gA')
        vista.startEditing()
        vista.addFeature(QgsVectorLayerUtils.createFeature(
            vista, QgsGeometry.fromPointXY(QgsPointXY(-44.5, -20.5)),
            {vista.fields().indexOf('nome'): 'Desenhado'}))
        vista.commitChanges()
        vista.reload()

        renderizador = vista.renderer()
        contexto = QgsRenderContext()
        renderizador.startRender(contexto, vista.fields())
        try:
            sem_simbolo = [f.id() for f in vista.getFeatures()
                           if renderizador.symbolForFeature(f, contexto) is None]
        finally:
            renderizador.stopRender(contexto)
        self.assertEqual(sem_simbolo, [], 'feicao sem simbolo nao aparece no mapa')

    def test_apagar_grupo_com_neto_nao_derruba_a_reconciliacao(self):
        from tairu_sync.record_convert import sync_record_layers

        # Alfa > Beta > Gama. Mover ou apagar Alfa clona a subarvore e destroi a
        # original: um no guardado dentro do galho vira ponteiro para objeto C++ morto e
        # o toque seguinte levanta RuntimeError, abortando o pull no meio e prendendo o
        # painel em "Baixando registros...".
        vivos = [_group('gA', 'Alfa'), _group('gB', 'Beta', parent='gA'),
                 _group('gC', 'Gama', parent='gB')]
        gpkg = _fresh_gpkg([_record(0, 'gA'), _record(1, 'gB'), _record(2, 'gC')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, vivos)

        sobreviventes = [_group('gB', 'Beta'), _group('gC', 'Gama', parent='gB')]
        sync_record_layers(gpkg, 'Exp', _MAP_ID, sobreviventes)   # nao pode levantar
        sync_record_layers(gpkg, 'Exp', _MAP_ID, sobreviventes)

        vistos = set()
        for layer in QgsProject.instance().mapLayers().values():
            idx = layer.fields().indexOf('recordId')
            if idx >= 0:
                vistos.update(f.attribute(idx) for f in layer.getFeatures())
        self.assertEqual(vistos, {'r0', 'r1', 'r2'})

    def test_duas_expedicoes_de_mesmo_nome_nao_colapsam(self):
        from tairu_sync.record_convert import ROOT_GROUP_NAME, sync_record_layers

        um = _fresh_gpkg([_record(0, '')])
        outro = _fresh_gpkg([_record(1, '')])
        sync_record_layers(um, 'Levantamento', 'mapa-um', [])
        sync_record_layers(outro, 'Levantamento', 'mapa-dois', [])
        sync_record_layers(um, 'Levantamento', 'mapa-um', [])

        raiz = QgsProject.instance().layerTreeRoot().findGroup(ROOT_GROUP_NAME)
        chaves = sorted(n.customProperty('tairu/nodeKey', '') for n in raiz.findGroups(True))
        self.assertEqual(chaves, ['map:mapa-dois', 'map:mapa-um'])

    def test_raiz_antiga_e_renomeada_em_vez_de_duplicada(self):
        """Projeto salvo com o raiz chamado 'Tairu' nao pode ganhar um segundo raiz.

        Dois nos deixariam as camadas da expedicao divididas entre eles, e a metade
        antiga nunca mais seria tocada por nenhuma sincronizacao.
        """
        from tairu_sync.record_convert import ROOT_GROUP_NAME, sync_record_layers

        raiz = QgsProject.instance().layerTreeRoot()
        antigo = raiz.addGroup('Tairu')
        gpkg = _fresh_gpkg([_record(0, '')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [])

        self.assertIsNone(raiz.findGroup('Tairu'))
        self.assertEqual(ROOT_GROUP_NAME, antigo.name())
        self.assertEqual(1, sum(1 for n in raiz.children()
                                if QgsLayerTree.isGroup(n) and n.name() == ROOT_GROUP_NAME))
        self.assertTrue(raiz.findGroup(ROOT_GROUP_NAME).findGroups(True))

    def test_pasta_sem_grupo_existe_mesmo_vazia(self):
        from tairu_sync.record_convert import ROOT_GROUP_NAME, sync_record_layers

        # E para onde vai o registro que o usuario tira de um grupo pelo campo Grupo. Sem
        # a pasta, a feicao some do painel inteiro e a escolha nunca chega a ser enviada.
        gpkg = _fresh_gpkg([_record(0, 'gA')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        raiz = QgsProject.instance().layerTreeRoot().findGroup(ROOT_GROUP_NAME)
        nomes = [n.name() for n in raiz.findGroups(True)]
        self.assertIn('Sem grupo', nomes)


class TestSimbologia(unittest.TestCase):
    """A simbologia tem de andar nos DOIS sentidos.

    O defeito relatado: mudar a cor no aplicativo nao mudava nada no QGIS. A legenda so
    era reconstruida quando o CONJUNTO de registros mudava, e trocar uma cor nao muda o
    conjunto — entao a camada ficava com a cor antiga indefinidamente.
    """

    def setUp(self):
        QgsProject.instance().clear()

    def _com_cor(self, argb, lm):
        rec = _record(0, 'gA')
        rec.geometry_color_value = argb
        rec.last_modified = lm
        return rec

    def _cor_da_categoria(self):
        for categoria in _leaf('gA').renderer().categories():
            if categoria.value() == 'r0':
                return categoria.symbol().color().name()
        return None

    def test_cor_trocada_no_app_chega_ao_qgis(self):
        from tairu_sync.record_convert import apply_pull, sync_record_layers

        grupos = [_group('gA', 'Alfa')]
        gpkg = _fresh_gpkg([self._com_cor(0xFF2196F3, 1)])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)
        self.assertEqual(self._cor_da_categoria(), '#2196f3')

        apply_pull(gpkg, [self._com_cor(0xFFF44336, 2)], remove_missing=False)
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)
        self.assertEqual(self._cor_da_categoria(), '#f44336')

    def test_cor_escolhida_no_qgis_vai_para_o_app(self):
        from qgis.PyQt.QtGui import QColor
        from qgis.core import QgsMarkerSymbol
        from tairu_sync.push import build_push_plan
        from tairu_sync.record_convert import argb_to_hex, sync_record_layers

        gpkg = _fresh_gpkg([self._com_cor(0xFF2196F3, 1)])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        vista = _leaf('gA')

        renderizador = vista.renderer().clone()
        indice = [i for i, c in enumerate(renderizador.categories()) if c.value() == 'r0'][0]
        simbolo = QgsMarkerSymbol.createSimple({'size': '3'})
        simbolo.setColor(QColor('#00ff00'))
        renderizador.updateCategorySymbol(indice, simbolo)
        vista.setRenderer(renderizador)

        plano = build_push_plan(vista, _MAPPING, _Mapa(), 'u1')
        item = [i for i in plano.items if i.record.record_id == 'r0'][0]
        self.assertEqual(argb_to_hex(item.record.geometry_color_value), '#FF00FF00')

    def test_tamanho_do_app_e_respeitado(self):
        from tairu_sync.record_convert import sync_record_layers

        # Antes, todo ponto saia do mesmo tamanho no QGIS. O padrao do aplicativo (40)
        # continua desenhando como sempre; o dobro tem de sair o dobro.
        from tairu_firebase.models import points_to_json

        padrao, dobro = _record(0, 'gA'), _record(1, 'gA')
        for rec in (padrao, dobro):
            rec.geometry_type = 'line'
            rec.geometry_points_json = points_to_json([(-20.0, -44.0), (-20.1, -44.1)], ts=1)
        padrao.geometry_size, dobro.geometry_size = 3.0, 6.0
        gpkg = _fresh_gpkg([padrao, dobro])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        tamanhos = {}
        for categoria in _leaf('gA', spec_key='line').renderer().categories():
            if categoria.value() is not None:
                tamanhos[categoria.value()] = round(categoria.symbol().width(), 3)
        self.assertGreater(tamanhos['r0'], 0)
        self.assertAlmostEqual(tamanhos['r1'], tamanhos['r0'] * 2, places=2)

    def test_tamanho_do_icone_e_estatico_para_a_simbologia_funcionar(self):
        from qgis.core import Qgis, QgsSymbolLayer
        from tairu_sync.record_convert import (icon_font_family, icon_map_size_px,
                                               sync_record_layers)

        # NADA de tamanho definido por dado aqui. A expressao ganha do campo Tamanho da
        # simbologia do QGIS, em silencio: a pessoa digita um valor, nada acontece, e
        # conclui que a simbologia nao funciona nesta camada. Um simbolo que o usuario
        # nao consegue ajustar e pior do que uma linha alta na lista de camadas.
        if not icon_font_family():
            self.skipTest('fonte de icones indisponivel neste ambiente')
        rec = _record(0, 'gA')
        rec.geometry_size = 40.0
        sync_record_layers(_fresh_gpkg([rec]), 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        # Ler DENTRO do laco: guardar o simbolo de uma categoria e usa-lo depois deixa um
        # ponteiro para objeto ja liberado, e o processo cai sem aviso.
        estatico, unidade, tem_expressao = None, None, None
        for categoria in _leaf('gA').renderer().categories():
            if categoria.value() != 'r0':
                continue
            camada = categoria.symbol().symbolLayer(0)
            estatico, unidade = camada.size(), camada.sizeUnit()
            tem_expressao = camada.dataDefinedProperties().property(
                QgsSymbolLayer.Property.PropertySize).isActive()
            break
        self.assertAlmostEqual(estatico, icon_map_size_px(), places=2)
        # PIXELS, a unidade do aplicativo: "40" aqui e o mesmo "40" de la, e o tamanho
        # aparente deixa de depender da resolucao do canvas, que muda por maquina.
        self.assertEqual(unidade, Qgis.RenderUnit.Pixels)
        self.assertFalse(tem_expressao, 'expressao de tamanho anula o campo da simbologia')


class TestFidelidadeVisual(unittest.TestCase):
    """Icone e padrao de traco, para o QGIS mostrar o que o aplicativo mostra."""

    def setUp(self):
        QgsProject.instance().clear()

    def _com_estilo(self, indice, tipo, subtipo, base=None, gtype='point'):
        import json as _json
        rec = _record(indice, 'gA')
        rec.tipo_registro, rec.sub_tipo = tipo, subtipo
        if gtype == 'line':
            from tairu_firebase.models import points_to_json
            rec.geometry_type = 'line'
            rec.geometry_points_json = points_to_json([(-20.0, -44.0), (-20.1, -44.1)], ts=1)
        if base is not None:
            rec.style = _json.dumps({'v': 1, 'base': base})
        return rec

    def test_icone_segue_a_cadeia_do_app(self):
        from tairu_sync.record_convert import sync_record_layers

        gpkg = _fresh_gpkg([
            self._com_estilo(0, 'local', 'comercio'),                        # subtipo
            self._com_estilo(1, 'trilha', ''),                               # tipo
            self._com_estilo(2, 'local', 'residencia', {'icon': 'factory'}),  # styleJson
        ])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        camada = _leaf('gA')
        indice = camada.fields().indexOf('recordIcon')
        resolvidos = {f.attribute(camada.fields().indexOf('recordId')): f.attribute(indice)
                      for f in camada.getFeatures()}
        self.assertEqual(resolvidos, {'r0': 'store', 'r1': 'hiking', 'r2': 'factory'})

    def test_ponto_desenha_com_o_glifo_do_icone(self):
        from tairu_sync.record_convert import icon_font_family, sync_record_layers
        from tairu_core.record_icons import ICON_CODEPOINTS

        if not icon_font_family():
            self.skipTest('fonte de icones indisponivel neste ambiente')
        gpkg = _fresh_gpkg([self._com_estilo(0, 'local', 'comercio')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        camada = _record_symbol_layer(_leaf('gA'), 'r0')
        self.assertEqual(type(camada).__name__, 'QgsFontMarkerSymbolLayer')
        self.assertEqual(ord(camada.character()), ICON_CODEPOINTS['store'])

    def test_ponto_nunca_fica_invisivel(self):
        """Um ponto sem tinta e pior do que um ponto sem icone.

        Se o sistema ja tiver uma fonte chamada "Material Icons" com outros pontos de
        codigo, o Qt pode resolver para ela e o marcador desenha VAZIO — o ponto some do
        mapa sem erro nenhum. QFontMetrics.inFont() nao serve de guarda: ele devolve True
        ate para um ponto de codigo inexistente. A checagem e DESENHAR.
        """
        import tairu_sync.record_convert as rc

        familia = rc.icon_font_family()
        if familia:
            referencia = rc.ICON_CODEPOINTS[rc.FALLBACK_ICON]
            self.assertTrue(rc._glifo_pinta(familia, referencia))
            self.assertFalse(rc._glifo_pinta(familia, 0x0001))

        gpkg = _fresh_gpkg([self._com_estilo(0, 'local', 'comercio')])
        anterior = rc._familia_icones
        rc._familia_icones = ''          # a fonte nao serve
        try:
            rc.sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
            camada = _record_symbol_layer(_leaf('gA'), 'r0')
            self.assertEqual(type(camada).__name__, 'QgsSimpleMarkerSymbolLayer')
        finally:
            rc._familia_icones = anterior

    def test_tamanho_escolhido_pelo_usuario_sobrevive_ao_recebimento(self):
        from qgis.core import QgsFontMarkerSymbolLayer, QgsMarkerSymbol
        from qgis.PyQt.QtGui import QColor
        from tairu_sync.record_convert import (ICON_CODEPOINTS, apply_pull,
                                               icon_font_family, sync_record_layers)

        # E isto que responde "como o usuario muda o tamanho": ele muda pela simbologia do
        # QGIS, como em qualquer camada, e a escolha fica de pe. O renderizador so e
        # reconstruido quando os DADOS do registro mudam — ai a aparencia volta a seguir o
        # aplicativo, junto com a cor, que e o comportamento esperado de uma sincronizacao.
        if not icon_font_family():
            self.skipTest('fonte de icones indisponivel neste ambiente')
        grupos = [_group('gA', 'Alfa')]
        gpkg = _fresh_gpkg([_record(0, 'gA', 'Ponto')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)

        def tamanho():
            for categoria in _leaf('gA').renderer().categories():
                if categoria.value() == 'r0':
                    return round(categoria.symbol().size(), 2)
            return None

        padrao = tamanho()
        camada = _leaf('gA')
        renderizador = camada.renderer().clone()
        indice = [i for i, c in enumerate(renderizador.categories()) if c.value() == 'r0'][0]
        escolhido = QgsFontMarkerSymbolLayer(
            icon_font_family(), chr(ICON_CODEPOINTS['store']), 9.0)
        escolhido.setColor(QColor('#4caf50'))
        simbolo = QgsMarkerSymbol()
        simbolo.changeSymbolLayer(0, escolhido)
        renderizador.updateCategorySymbol(indice, simbolo)
        camada.setRenderer(renderizador)
        del renderizador
        self.assertAlmostEqual(tamanho(), 9.0, places=2)

        apply_pull(gpkg, [_record(0, 'gA', 'Ponto')], remove_missing=False)
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)
        self.assertAlmostEqual(tamanho(), 9.0, places=2)

        apply_pull(gpkg, [_record(0, 'gA', 'Outro nome')], remove_missing=False)
        sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)
        self.assertAlmostEqual(tamanho(), padrao, places=2)

    def test_traco_tracejado_e_pontilhado_chegam_a_linha(self):
        from qgis.PyQt.QtCore import Qt
        from tairu_sync.record_convert import sync_record_layers

        gpkg = _fresh_gpkg([
            self._com_estilo(0, 'local', '', {'stroke': 'dashed'}, gtype='line'),
            self._com_estilo(1, 'local', '', {'stroke': 'dotted'}, gtype='line'),
            self._com_estilo(2, 'local', '', None, gtype='line'),
        ])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        camada = _leaf('gA', spec_key='line')
        estilos = {}
        for categoria in camada.renderer().categories():
            if categoria.value() is not None:
                estilos[categoria.value()] = categoria.symbol().symbolLayer(0).penStyle()
        self.assertEqual(estilos['r0'], Qt.PenStyle.DashLine)
        self.assertEqual(estilos['r1'], Qt.PenStyle.DotLine)
        self.assertEqual(estilos['r2'], Qt.PenStyle.SolidLine)


class TestDicaDoMapa(unittest.TestCase):
    """A ponte entre o mapa e a lista de camadas.

    Na lista o registro aparece pelo NOME; olhando a forma no mapa nao da para saber qual
    e, e numa pasta com dezenas de poligonos achar a linha correspondente vira tentativa e
    erro. A dica ao apontar resolve, e carrega tambem o GRUPO, que era a outra pergunta
    sem resposta no mapa. Nao e rotulo de proposito: rotular tudo sujaria o mapa e o
    aplicativo mantem rotulo desligado por padrao.
    """

    def setUp(self):
        QgsProject.instance().clear()

    def _dica(self, camada):
        from qgis.core import (QgsExpression, QgsExpressionContext,
                               QgsExpressionContextUtils)
        import re
        modelo = camada.mapTipTemplate()
        saidas = []
        for feicao in camada.getFeatures():
            contexto = QgsExpressionContext(
                QgsExpressionContextUtils.globalProjectLayerScopes(camada))
            contexto.setFeature(feicao)
            saidas.append(re.sub(
                r'\[%(.*?)%\]',
                lambda m: str(QgsExpression(m.group(1).strip()).evaluate(contexto) or ''),
                modelo))
        return saidas

    def test_dica_traz_nome_tipo_e_grupo(self):
        from tairu_sync.record_convert import sync_record_layers

        com_nome = _record(0, 'gA', 'Talhão Norte')
        sem_nome = _record(1, 'gA')
        sem_nome.nome = ''          # o helper troca '' pelo nome padrao; queremos vazio
        solto = _record(2, '', 'Reserva')
        sync_record_layers(_fresh_gpkg([com_nome, sem_nome, solto]), 'Exp', _MAP_ID,
                           [_group('gA', 'Área Sul')])

        dicas = self._dica(_leaf('gA'))
        self.assertTrue(any('Talhão Norte' in d and 'Área Sul' in d for d in dicas))
        # Nome vazio e string '', nao nulo: sem tratar, a dica sairia so com o negrito
        # em branco e o usuario nao saberia o que apontou.
        self.assertTrue(any('(sem nome)' in d for d in dicas))

        from tairu_sync.record_convert import _NO_GROUP_LABEL
        self.assertTrue(any(_NO_GROUP_LABEL in d for d in self._dica(_leaf(''))))


class TestAvisoDeIdentidade(unittest.TestCase):
    """O aviso do envio tem de nomear a causa REAL.

    A camada aberta de dentro de um .zip (os pacotes de feicoes do CAR, o caso que
    apareceu em uso) e somente leitura pelo sistema virtual do GDAL, entao o
    identificador do registro nao tem onde ficar. O texto antigo dizia "formato somente
    leitura, como KML" e citava um exemplo que nao era o caso, o que so atrapalhava o
    diagnostico — e escondia a saida boa, que e extrair o arquivo.
    """

    def _camada(self, fonte):
        class Falsa(object):
            def __init__(self, s):
                self._s = s

            def source(self):
                return self._s
        return Falsa(fonte)

    def test_reconhece_camada_dentro_de_zip(self):
        from tairu_sync.push import _dentro_de_arquivo_compactado

        self.assertTrue(_dentro_de_arquivo_compactado(
            self._camada('/vsizip//Users/eu/Feicoes/Reserva_Legal.zip/Reserva_Legal.shp')))
        self.assertFalse(_dentro_de_arquivo_compactado(
            self._camada('/Users/eu/Feicoes/Reserva_Legal.shp')))

    def test_aviso_manda_extrair_quando_e_zip(self):
        from tairu_sync.push import _aviso_identidade_no_projeto

        zipada = self._camada('/vsizip//x/Reserva_Legal.zip/Reserva_Legal.shp')
        texto = _aviso_identidade_no_projeto([('Reserva_Legal.shp', zipada)])
        self.assertIn('ZIP', texto)
        self.assertIn('extraia o .zip', texto)
        self.assertIn('SALVE o projeto', texto)
        self.assertNotIn('KML', texto)

    def test_aviso_generico_quando_nao_e_zip(self):
        from tairu_sync.push import _aviso_identidade_no_projeto

        comum = self._camada('/x/rota.kml|layername=rota')
        texto = _aviso_identidade_no_projeto([('rota.kml', comum)])
        self.assertIn('rota.kml', texto)
        self.assertIn('SALVE o projeto', texto)
        self.assertNotIn('.zip', texto)


class TestRegistroDesenhadoNoQgis(unittest.TestCase):
    """Um registro criado no QGIS tem de nascer no aplicativo como se criado por la."""

    def setUp(self):
        QgsProject.instance().clear()

    def test_nao_herda_a_cor_de_espera_da_categoria_coringa(self):
        from qgis.core import QgsGeometry, QgsPointXY, QgsVectorLayerUtils
        from tairu_sync.push import build_push_plan
        from tairu_sync.record_convert import sync_record_layers

        # A feicao recem-desenhada nao tem recordId, entao casa com a categoria coringa —
        # um cinza de ESPERA, que existe so para o ponto aparecer no mapa antes do envio.
        # Gravar essa cor fazia o registro nascer cinza no aplicativo, como se o usuario a
        # tivesse escolhido. Sem cor, o aplicativo aplica a do TIPO, que e o que acontece
        # quando o registro e criado por la. Pego numa bateria de ida e volta contra o
        # servidor de desenvolvimento.
        gpkg = _fresh_gpkg([_record(0, 'gA')])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        camada = _leaf('gA')
        camada.startEditing()
        camada.addFeature(QgsVectorLayerUtils.createFeature(
            camada, QgsGeometry.fromPointXY(QgsPointXY(-44.5, -20.5)),
            {camada.fields().indexOf('nome'): 'Desenhado no QGIS'}))
        camada.commitChanges()
        camada.reload()

        plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1')
        novos = [i for i in plano.items if i.action == 'new']
        self.assertEqual(len(novos), 1)
        self.assertIsNone(novos[0].record.geometry_color_value,
                          'registro novo nao pode levar a cor de espera do coringa')

    def test_camada_do_usuario_continua_mandando_a_cor(self):
        from qgis.core import (QgsFeature, QgsGeometry, QgsMarkerSymbol, QgsPointXY,
                               QgsSingleSymbolRenderer, QgsVectorLayer)
        from qgis.PyQt.QtGui import QColor
        from tairu_sync.push import build_push_plan

        # A regra acima vale SO para as camadas de registro. Numa camada do proprio
        # usuario (shapefile, KML, memoria) a cor do simbolo e escolha dele e tem de ir.
        camada = QgsVectorLayer('Point?crs=EPSG:4326&field=nome:string', 'minha', 'memory')
        feicao = QgsFeature(camada.fields())
        feicao.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(-44.0, -20.0)))
        feicao.setAttribute(0, 'Minha feicao')
        camada.dataProvider().addFeatures([feicao])
        camada.updateExtents()
        simbolo = QgsMarkerSymbol.createSimple({'size': '3'})
        simbolo.setColor(QColor('#123456'))
        camada.setRenderer(QgsSingleSymbolRenderer(simbolo))
        QgsProject.instance().addMapLayer(camada, False)

        plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1')
        novos = [i for i in plano.items if i.action == 'new']
        self.assertEqual(len(novos), 1)
        self.assertEqual(novos[0].record.geometry_color_value & 0xFFFFFFFF, 0xFF123456)


class TestEstiloNoEnvio(unittest.TestCase):
    """O envio tem de MESCLAR o styleJson, nunca ignora-lo nem substitui-lo.

    O aplicativo resolve a cor pelo styleJson ANTES do campo simples, entao um envio que
    so atualize geometryColorValue nao muda nada na tela de um registro estilizado — foi
    exatamente assim que "o envio da simbologia nao funcionou". E gravar um styleJson novo
    em folha apagaria o icone e o rotulo que o usuario escolheu no aplicativo.
    """

    def setUp(self):
        QgsProject.instance().clear()

    def test_cor_do_qgis_entra_no_stylejson_sem_perder_icone_nem_rotulo(self):
        import json as _json
        from qgis.PyQt.QtGui import QColor
        from qgis.core import QgsMarkerSymbol
        from tairu_sync.push import build_push_plan, build_writes
        from tairu_sync.record_convert import sync_record_layers

        rec = _record(0, 'gA')
        rec.style = _json.dumps({'v': 1, 'base': {'color': 4284955319, 'markerSize': 40.0,
                                                  'icon': 'store'},
                                 'label': {'field': 'name', 'show': False}})
        gpkg = _fresh_gpkg([rec])
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [_group('gA', 'Alfa')])
        vista = _leaf('gA')

        renderizador = vista.renderer().clone()
        indice = [i for i, c in enumerate(renderizador.categories()) if c.value() == 'r0'][0]
        simbolo = QgsMarkerSymbol.createSimple({'size': '3'})
        simbolo.setColor(QColor('#ff6d00'))
        renderizador.updateCategorySymbol(indice, simbolo)
        vista.setRenderer(renderizador)

        plano = build_push_plan(vista, _MAPPING, _Mapa(), 'u1')
        item = [i for i in plano.items if i.record.record_id == 'r0'][0]
        estilo = _json.loads(item.record.style)
        self.assertEqual(estilo['base']['color'] & 0xFFFFFFFF, 0xFFFF6D00)
        self.assertEqual(estilo['base']['icon'], 'store')
        self.assertEqual(estilo['base']['markerSize'], 40.0)
        self.assertEqual(estilo['label'], {'field': 'name', 'show': False})

        # e o campo TEM de entrar na mascara, senao nada disso e gravado
        escritas = build_writes(_FsFalso(), plano, 'u1')
        self.assertIn('style', escritas[0]['mascara'])


class _FsFalso(object):
    """Só o suficiente para inspecionar a máscara que build_writes monta."""

    def build_create_write(self, path, fields, server_timestamp_field='serverTimestamp'):
        return {'caminho': path, 'mascara': list(fields), 'campos': fields}

    def build_update_write(self, path, fields, mask_fields, require_existing=True,
                           server_timestamp_field='serverTimestamp'):
        return {'caminho': path, 'mascara': list(mask_fields), 'campos': fields}


class TestMigracaoDeEsquema(unittest.TestCase):
    """Coluna nova tem de vir PREENCHIDA, senao o estrago e silencioso.

    O recebimento normal e incremental e nao traz registro inalterado. Uma coluna recem
    criada nascendo vazia poe toda a arvore em "Sem grupo", apaga os icones e — o pior —
    faz o envio mesclar a simbologia num estilo vazio, destruindo o icone e o rotulo que
    o usuario escolheu no aplicativo. Por isso a marca e um NUMERO: quem ja rodou a
    geracao anterior ainda recebe um recebimento completo.
    """

    def test_quem_rodou_a_geracao_anterior_ainda_precisa_de_recebimento_completo(self):
        from tairu_core.workspace import (mark_record_schema_generation,
                                          record_schema_generation)
        from tairu_sync.record_convert import RECORD_SCHEMA_GENERATION

        mark_record_schema_generation('teste', 'mapa-antigo', RECORD_SCHEMA_GENERATION - 1)
        self.assertLess(record_schema_generation('teste', 'mapa-antigo'),
                        RECORD_SCHEMA_GENERATION)

        mark_record_schema_generation('teste', 'mapa-antigo', RECORD_SCHEMA_GENERATION)
        self.assertGreaterEqual(record_schema_generation('teste', 'mapa-antigo'),
                                RECORD_SCHEMA_GENERATION)

    def test_expedicao_nunca_preenchida_conta_como_atrasada(self):
        from tairu_core.workspace import record_schema_generation
        from tairu_sync.record_convert import RECORD_SCHEMA_GENERATION

        self.assertLess(record_schema_generation('teste', 'mapa-novo-em-folha'),
                        RECORD_SCHEMA_GENERATION)


class TestRegrasPortadasDoApp(unittest.TestCase):
    """Paridade com record_group_model.dart e text_fold.dart (sem QGIS).

    Os imports do plugin ficam DENTRO dos testes, como nos demais modulos daqui: com
    eles no topo, o carregador do unittest derruba o modulo inteiro quando o sys.path
    da sessao nao resolve o pacote, e a checagem que impede apagar registro deixa de
    rodar sem ninguem notar.
    """

    def setUp(self):
        from tairu_core import record_groups
        self.rg = record_groups

    def test_vinculo_podre_sobe_ao_nivel_superior(self):
        alfa, beta = _group('a', 'A'), _group('b', 'B', parent='a')
        indice = {'a': alfa, 'b': beta}
        self.assertEqual(self.rg.effective_parent_id(beta, indice), 'a')
        self.assertEqual(self.rg.effective_parent_id(_group('x', 'X', parent='sumiu'), {}), '')
        auto = _group('s', 'S', parent='s')
        self.assertEqual(self.rg.effective_parent_id(auto, {'s': auto}), '')

    def test_ciclo_manda_os_dois_lados_para_o_topo_e_termina(self):
        um, dois = _group('1', 'Um', parent='2'), _group('2', 'Dois', parent='1')
        indice = {'1': um, '2': dois}
        self.assertEqual(self.rg.effective_parent_id(um, indice), '')
        self.assertEqual(self.rg.effective_parent_id(dois, indice), '')

    def test_galho_pendurado_num_ciclo_mantem_o_proprio_pai(self):
        um, dois = _group('1', 'Um', parent='2'), _group('2', 'Dois', parent='1')
        galho = _group('g', 'Galho', parent='1')
        indice = {'1': um, '2': dois, 'g': galho}
        self.assertEqual(self.rg.effective_parent_id(galho, indice), '1')

    def test_filtro_da_pasta(self):
        # Lista vazia NAO pode virar NOT IN (): o SQLite aceita e devolve tudo, o
        # analisador de expressao do QGIS rejeita e devolve ZERO feicoes.
        self.assertEqual(self.rg.folder_filter([]), '')
        # Aspa simples sem dobrar nao levanta erro: a pasta so aparece vazia.
        self.assertEqual(self.rg.folder_filter([], group_id="ap'os"), '"groupId" = \'ap\'\'os\'')
        # A expressao entra em source(): tem de ser estavel entre chamadas.
        self.assertEqual(self.rg.folder_filter(['b', 'a']), self.rg.folder_filter(['a', 'b']))

    def test_dobra_de_nome_ignora_acento_caixa_e_espaco(self):
        self.assertEqual(self.rg.fold_for_matching('  Setor   NORTE '), 'setor norte')
        self.assertEqual(self.rg.fold_for_matching('Ação'), self.rg.fold_for_matching('acao'))


if __name__ == '__main__':
    unittest.main()
