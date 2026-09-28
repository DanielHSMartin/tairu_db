# -*- coding: utf-8 -*-

"""O envio só grava o que mudou NO QGIS desde o último recebimento/envio.

Todo 'update' mandava todos os campos da camada. Qualquer motivo para reenviar — a
operação de datum nova mudando o hash de toda camada legada, a cor que o QGIS sorteia
ao reabrir a camada num projeto novo — desfazia em massa o que o app tinha editado
(nome, situação, descrição, cor) e recoloria os registros.

Precisa do Python do QGIS (qgis.core, pyproj); pulado em outros interpretadores.
"""

import base64
import copy
import json
import math
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import (
        QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
        QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
        QgsVectorFileWriter, QgsVectorLayer)
    from qgis.PyQt.QtGui import QColor
    from pyproj import Transformer
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None
_MAP_ID = 'map-de-teste'
_MAPPING = {'nome_field': 'nome', 'descricao_field': None, 'tipo': 'local',
            'sub_tipo': 'outroLocal', 'situation': 'Ativo'}
SANTIAGO = (-70.65, -33.45)    # PSAD56: a operação do app (6972) fica 57 m da do QGIS
_APP_EDITS = {'nome': 'Ponto A (conferido)', 'situation': 'Concluído',
              'descricao': 'visto em campo pelo app', 'geometryColorValue': 0xFFFF0000}


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


def _blob(value):
    return base64.b64encode(value).decode('ascii') if isinstance(value, bytes) else value


class _Nuvem(object):
    """Firestore em memória + o cache local do plugin (o documento do último recebimento)."""

    def __init__(self):
        self.docs = {}
        self.cache = {}
        self.bases = {}     # linha de base: (camada, recordId) -> campos
        self.mascaras = {}

    def build_create_write(self, path, fields):
        self.docs[path.rsplit('/', 1)[1]] = {k: _blob(v) for k, v in fields.items()}

    def build_update_write(self, path, fields, mask, require_existing=True):
        record_id = path.rsplit('/', 1)[1]
        for key in mask:
            self.docs[record_id][key] = _blob(fields.get(key))
        self.mascaras[record_id] = list(mask)

    def load_records_by_id(self, _map_id, ids):
        return {i: copy.deepcopy(self.cache[i]) for i in ids if i in self.cache}

    def store_records(self, _map_id, rows, _fetched_at_ms):
        self.cache.update(copy.deepcopy(dict(rows)))

    def load_baselines(self, _map_id, camada, ids):
        return {i: copy.deepcopy(self.bases[camada, i]) for i in ids if (camada, i) in self.bases}

    def store_baselines(self, _map_id, camada, rows):
        self.bases.update({(camada, i): copy.deepcopy(campos) for i, campos in rows})

    def commit(self, _writes):
        """As escritas já foram aplicadas por build_*_write."""

    def receber(self):
        """'Receber Registros': o cache passa a ser a nuvem."""
        self.cache = copy.deepcopy(self.docs)

    def plano(self, camada):
        from tairu_sync.push import build_push_plan
        return build_push_plan(camada, _MAPPING, _Mapa(), 'u1', cache=self)

    def gravar(self, plano, camada):
        """O que execute_push faz com o plano aprovado (e o recebimento que vem depois)."""
        from tairu_sync.push import _write_back_records_to_source_layer, build_writes
        self.mascaras = {}
        build_writes(self, plano, 'u1')
        _write_back_records_to_source_layer(plano, camada, self)
        self.receber()

    def enviar(self, camada):
        plano = self.plano(camada)
        self.gravar(plano, camada)
        return plano.items[0]


def _ref(code, lon, lat):
    la, lo = Transformer.from_pipeline(
        f'urn:ogc:def:coordinateOperation:EPSG::{code}').transform(lat, lon)
    return lo, la


def _metros(a, b):
    dx = (a[0] - b[0]) * 111320.0 * math.cos(math.radians(a[1]))
    return math.hypot(dx, (a[1] - b[1]) * 110574.0)


def _fonte(epsg, geog, lon, lat, geometria='Point', arquivo='fonte.gpkg'):
    """GeoPackage (ou Shapefile, por `arquivo`) do usuário com uma feição 'Ponto A' em (lon, lat)
    do datum `geog`."""
    caminho = os.path.join(tempfile.mkdtemp(), arquivo)
    ponto = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem(f'EPSG:{geog}'), QgsCoordinateReferenceSystem(f'EPSG:{epsg}'),
        QgsCoordinateTransformContext()).transform(QgsPointXY(lon, lat))
    memoria = QgsVectorLayer(f'{geometria}?crs=EPSG:{epsg}&field=nome:string', 'fonte', 'memory')
    feicao = QgsFeature(memoria.fields())
    feicao.setAttributes(['Ponto A'])
    geom = QgsGeometry.fromPointXY(ponto)
    feicao.setGeometry(geom.buffer(0.01, 4) if geometria == 'Polygon' else geom)
    memoria.dataProvider().addFeatures([feicao])
    opcoes = QgsVectorFileWriter.SaveVectorOptions()
    opcoes.driverName = 'ESRI Shapefile' if arquivo.endswith('.shp') else 'GPKG'
    QgsVectorFileWriter.writeAsVectorFormatV3(memoria, caminho, QgsCoordinateTransformContext(), opcoes)
    return caminho


def _abrir(caminho, cor):
    """A camada aberta num projeto NOVO, com a cor que o QGIS sorteou (aqui, fixada)."""
    QgsProject.instance().clear()
    camada = QgsVectorLayer(caminho, 'fonte', 'ogr')
    camada.renderer().symbol().setColor(QColor(cor))
    QgsProject.instance().addMapLayer(camada)
    return camada


def _editar(camada, campo, valor):
    feicao = next(camada.getFeatures())
    camada.startEditing()
    camada.changeAttributeValue(feicao.id(), camada.fields().indexOf(campo), valor)
    assert camada.commitChanges(), camada.commitErrors()


def _do_app(doc):
    return {k: (doc[k] & 0xFFFFFFFF if k == 'geometryColorValue' else doc[k]) for k in _APP_EDITS}


class TestEnvioNaoDesfazOApp(unittest.TestCase):

    def test_camada_legada_reenviada_sem_edicao_nao_desfaz_o_app(self):
        # Enviada por um plugin anterior (a operação de datum que o QGIS escolhe) e editada
        # no app: o hash de TODA feição muda com a operação nova e todas viram 'update'.
        from tairu_sync import push
        for receber_antes in (False, True):
            with self.subTest(receber_antes=receber_antes):
                nuvem = _Nuvem()
                caminho = _fonte(24879, 4248, *SANTIAGO)
                with mock.patch.object(push, 'datum_context', lambda contexto, *_a, **_k: contexto):
                    self.assertEqual(nuvem.enviar(_abrir(caminho, '#cc3300')).action, 'new')
                record_id, doc = next(iter(nuvem.docs.items()))
                doc.update(_APP_EDITS)
                if receber_antes:
                    nuvem.receber()

                item = nuvem.enviar(_abrir(caminho, '#cc3300'))
                doc = nuvem.docs[record_id]
                self.assertEqual(_do_app(doc), _APP_EDITS)
                # E a correção de datum sobe: o registro vai para onde a EPSG:6972 o põe.
                self.assertEqual(item.action, 'update')
                ponto = json.loads(doc['geometryPoints'])[0]
                self.assertLess(_metros((ponto['lo'], ponto['la']), _ref(6972, *SANTIAGO)), 0.05)

    def test_camada_reaberta_com_outra_cor_sorteada_nao_recolore(self):
        for geometria in ('Point', 'Polygon'):
            with self.subTest(geometria=geometria):
                nuvem = _Nuvem()
                caminho = _fonte(4326, 4326, -47.9, -15.8, geometria)
                self.assertEqual(nuvem.enviar(_abrir(caminho, '#309e3b')).action, 'new')
                record_id, doc = next(iter(nuvem.docs.items()))
                enviado = copy.deepcopy(doc)

                # Projeto novo: o QGIS sorteia outra cor para o MESMO símbolo padrão.
                self.assertEqual(nuvem.enviar(_abrir(caminho, '#bcbe57')).action, 'unchanged')
                for campo in ('geometryColorValue', 'geometryBackgroundColorValue', 'style'):
                    self.assertEqual(nuvem.docs[record_id].get(campo), enviado.get(campo), campo)

                # O app edita; reabrir de novo, com mais outra cor, não desfaz nem recolore.
                nuvem.docs[record_id].update(_APP_EDITS)
                nuvem.receber()
                nuvem.enviar(_abrir(caminho, '#123abc'))
                self.assertEqual(_do_app(nuvem.docs[record_id]), _APP_EDITS)

    def test_simbolo_escolhido_no_qgis_continua_recolorindo(self):
        nuvem = _Nuvem()
        caminho = _fonte(4326, 4326, -47.9, -15.8)
        nuvem.enviar(_abrir(caminho, '#309e3b'))
        camada = _abrir(caminho, '#aa0000')
        camada.renderer().symbol().setSize(4)   # símbolo do usuário, não o padrão
        item = nuvem.enviar(camada)
        self.assertEqual(item.action, 'update')
        self.assertEqual(item.changed_fields, ['geometryColorValue'])
        self.assertEqual(next(iter(nuvem.docs.values()))['geometryColorValue'] & 0xFFFFFFFF, 0xFFAA0000)

    def test_coluna_propria_mudada_por_outro_escritor_nao_e_desfeita(self):
        # build_writes punha `attributes` na máscara de todo 'update': renomear aqui devolvia à
        # nuvem a coluna que outra cópia da camada tinha corrigido depois do último envio.
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QVariant
        caminho = _fonte(4326, 4326, -47.9, -15.8)
        fonte = QgsVectorLayer(caminho, 'fonte', 'ogr')
        fonte.dataProvider().addAttributes([QgsField('obs', QVariant.String)])
        fonte.updateFields()
        fonte.dataProvider().changeAttributeValues({next(fonte.getFeatures()).id(): {
            fonte.fields().indexOf('obs'): 'original'}})
        del fonte

        nuvem = _Nuvem()
        camada = _abrir(caminho, '#309e3b')
        nuvem.enviar(camada)
        record_id = next(iter(nuvem.docs))
        _editar(camada, 'obs', 'do QGIS')   # a do QGIS sobe
        self.assertEqual(nuvem.enviar(camada).changed_fields, ['attributes'])
        self.assertEqual(json.loads(nuvem.docs[record_id]['attributes']), {'obs': 'do QGIS'})

        nuvem.docs[record_id].update(attributes='{"obs":"corrigido"}', lastModified=2)
        nuvem.receber()
        _editar(camada, 'nome', 'Renomeado no QGIS')
        self.assertEqual(nuvem.enviar(camada).changed_fields, ['nome'])
        self.assertEqual(nuvem.docs[record_id]['attributes'], '{"obs":"corrigido"}')

    def test_edicao_de_um_atributo_no_qgis_sobe_so_esse_campo(self):
        from tairu_firebase.models import TairuRecord
        from tairu_sync.record_convert import apply_pull, sync_record_layers

        nuvem = _Nuvem()
        nuvem.docs['R1'] = {
            'recordId': 'R1', 'nome': 'Ponto', 'tipoRegistro': 'local', 'subTipo': 'outroLocal',
            'situation': 'Ativo', 'descricao': '', 'geometryType': 'point',
            'geometryPoints': json.dumps([{'la': -15.8, 'lo': -47.9, 'ts': 1}]),
            'createdBy': 'u1', 'createdAt': 1, 'lastModified': 1}
        nuvem.receber()
        gpkg = os.path.join(tempfile.mkdtemp(), 'records.gpkg')
        apply_pull(gpkg, [TairuRecord.from_fields('R1', copy.deepcopy(nuvem.docs['R1']))])
        QgsProject.instance().clear()
        sync_record_layers(gpkg, 'Exp', _MAP_ID, [])
        camada = [c for c in QgsProject.instance().mapLayers().values()
                  if c.customProperty('tairu/folder', '') == '%s|point|' % _MAP_ID][0]

        nuvem.docs['R1']['nome'] = 'Renomeado no app'    # depois do último recebimento
        _editar(camada, 'situation', 'Concluído')
        plano = nuvem.plano(camada)
        self.assertEqual(plano.items[0].changed_fields, ['situation'])
        from tairu_sync.push import build_writes
        build_writes(nuvem, plano, 'u1')
        self.assertEqual(set(nuvem.mascaras['R1']),
                         {'situation', 'lastModified', 'lastModifiedBy', 'isDeleted'})
        self.assertEqual((nuvem.docs['R1']['nome'], nuvem.docs['R1']['situation']),
                         ('Renomeado no app', 'Concluído'))


class TestCamadaEnviadaPelaVersaoAnterior(unittest.TestCase):
    """O hash da 2.0.26 não vê anéis. Com a nuvem intocada desde o envio (o lastModified
    gravado junto com o hash), toda diferença é do QGIS: a correção de datum ou um vértice do
    contorno eram conflito, e o último furo (ou a parte menor) apagado era 'inalterado'."""

    _FURO = ('Polygon((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.7, -47.9 -15.8),'
             '(-47.86 -15.76, -47.84 -15.76, -47.84 -15.74, -47.86 -15.76))')
    _PARTES = ('MultiPolygon(((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.8)),'
               '((-47.5 -15.5, -47.45 -15.5, -47.45 -15.45, -47.5 -15.5)))')

    def _enviada_pela_2_0_26(self, wkt):
        # push ANTES do patch: importado dentro dele, guardaria o payload antigo para sempre.
        from tairu_sync import push, record_convert  # noqa: F401
        caminho = os.path.join(tempfile.mkdtemp(), 'fonte.gpkg')
        memoria = QgsVectorLayer('MultiPolygon?crs=EPSG:4326&field=nome:string', 'fonte', 'memory')
        feicao = QgsFeature(memoria.fields())
        feicao.setAttributes(['P'])
        feicao.setGeometry(QgsGeometry.fromWkt(wkt))
        memoria.dataProvider().addFeatures([feicao])
        opcoes = QgsVectorFileWriter.SaveVectorOptions()
        opcoes.driverName = 'GPKG'
        QgsVectorFileWriter.writeAsVectorFormatV3(memoria, caminho, QgsCoordinateTransformContext(), opcoes)
        atual = record_convert.sync_record_payload
        nuvem = _Nuvem()
        camada = _abrir(caminho, '#309e3b')
        with mock.patch.object(record_convert, 'sync_record_payload',
                               lambda rec: {k: v for k, v in atual(rec).items() if k != 'geometryRings'}):
            self.assertEqual(nuvem.enviar(camada).action, 'new')
        nuvem.bases.clear()    # a 2.0.26 não guardava a linha de base: só o carimbo
        self.assertEqual(nuvem.plano(camada).items[0].action, 'unchanged')
        return nuvem, camada

    def _mudar(self, camada, editar):
        feicao = next(camada.getFeatures())
        geometria = QgsGeometry(feicao.geometry())
        self.assertTrue(editar(geometria))
        camada.startEditing()
        camada.changeGeometry(feicao.id(), geometria)
        self.assertTrue(camada.commitChanges())
        return geometria.asWkt(6)

    def test_so_o_qgis_mudou_a_geometria_e_ela_sobe(self):
        casos = (('vértice do contorno', self._FURO, lambda g: g.moveVertex(-47.91, -15.81, 0)),
                 ('último furo apagado', self._FURO, lambda g: g.deleteRing(1)),
                 ('parte menor apagada', self._PARTES, lambda g: g.deletePart(1)))
        for nome, wkt, editar in casos:
            with self.subTest(nome):
                nuvem, camada = self._enviada_pela_2_0_26(wkt)
                editada = self._mudar(camada, editar)
                item = nuvem.enviar(camada)
                self.assertEqual((item.action, item.send), ('update', True))
                self.assertIn('geometryWkb', item.changed_fields)
                na_nuvem = QgsGeometry()
                na_nuvem.fromWkb(base64.b64decode(next(iter(nuvem.docs.values()))['geometryWkb']))
                self.assertEqual(na_nuvem.asWkt(6), QgsGeometry.fromWkt(editada).asWkt(6))
                self.assertEqual(nuvem.plano(camada).items[0].action, 'unchanged')

    def test_com_o_app_mexendo_depois_e_conflito(self):
        # O hash antigo não diz se o furo sumiu aqui ou lá: com a nuvem gravada depois do envio,
        # "o QGIS apagou o furo" some sem aviso nenhum se virar 'inalterado'.
        nuvem, camada = self._enviada_pela_2_0_26(self._FURO)
        next(iter(nuvem.docs.values())).update(situation='Concluído', lastModified=2)
        nuvem.receber()
        self._mudar(camada, lambda g: g.deleteRing(1))
        item = nuvem.plano(camada).items[0]
        self.assertEqual((item.action, item.send), ('conflict', False))


class TestGrupoTrocadoNaCamadaDoRecebimento(unittest.TestCase):

    def test_grupo_trocado_pela_coluna_sobe_sem_recolorir(self):
        # A camada do recebimento não tem as colunas próprias: os `attributes` da nuvem entravam
        # como "mudança do app", o groupId saía da máscara, e o recebimento seguinte devolvia o
        # grupo antigo. E a pasta de destino, sem categoria para o registro, o pintava com o
        # cinza de espera — que subia como a cor escolhida no QGIS.
        from tairu_firebase.models import TairuRecord, TairuRecordGroup
        from tairu_sync.push import build_push_plan, build_writes
        from tairu_sync.record_convert import apply_pull, sync_record_layers

        def doc(record_id, grupo, **extra):
            d = {'recordId': record_id, 'nome': 'R ' + record_id, 'tipoRegistro': 'local',
                 'subTipo': 'outroLocal', 'situation': 'Ativo', 'descricao': '', 'geometryType': 'point',
                 'geometryPoints': json.dumps([{'la': -15.8, 'lo': -47.9, 'ts': 1}]), 'groupId': grupo,
                 'createdBy': 'u1', 'createdAt': 1, 'lastModified': 1}
            d.update(extra)
            return d

        grupos = [TairuRecordGroup(group_id='gA', name='Alfa'), TairuRecordGroup(group_id='gB', name='Beta')]
        for extra in ({}, {'attributes': '{"cod":"X1"}'}):
            with self.subTest(**extra):
                nuvem = _Nuvem()
                nuvem.docs = {'R1': doc('R1', 'gA', **extra), 'R2': doc('R2', 'gB')}
                nuvem.receber()
                gpkg = os.path.join(tempfile.mkdtemp(), 'records.gpkg')
                apply_pull(gpkg, [TairuRecord.from_fields(i, copy.deepcopy(d)) for i, d in nuvem.docs.items()])
                QgsProject.instance().clear()
                sync_record_layers(gpkg, 'Exp', _MAP_ID, grupos)

                def pasta(grupo):
                    return [c for c in QgsProject.instance().mapLayers().values()
                            if c.customProperty('tairu/folder', '') == '%s|point|%s' % (_MAP_ID, grupo)][0]

                camada = pasta('gA')
                feicao = next(camada.getFeatures())
                camada.startEditing()
                camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('nome'), 'Nome do QGIS')
                camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('groupId'), 'gB')
                self.assertTrue(camada.commitChanges())

                for grupo in ('gA', 'gB'):   # as duas pastas vão no envio
                    build_writes(nuvem, build_push_plan(pasta(grupo), _MAPPING, _Mapa(), 'u1', cache=nuvem), 'u1')
                self.assertEqual((nuvem.docs['R1']['nome'], nuvem.docs['R1']['groupId']), ('Nome do QGIS', 'gB'))
                self.assertNotIn('geometryColorValue', nuvem.mascaras['R1'])


class TestRecebimentoGuardaOTrabalhoDoQgis(unittest.TestCase):
    """O recebimento — o manual e o automático depois de enviar QUALQUER camada — regravava a
    feição inteira quando a nuvem mudou o registro, e a edição do QGIS ainda não enviada sumia.
    Sem rede, reconstruía a camada do cache local apagando o que foi desenhado e não enviado;
    sem cache (apagado, outra conta no mesmo QGIS), o recebimento completo fazia o mesmo."""

    def setUp(self):
        from tairu_core import firestore_cache
        pasta = tempfile.mkdtemp()
        self.gpkg = os.path.join(pasta, 'records.gpkg')
        caminho = mock.patch.object(firestore_cache, 'firestore_cache_path',
                                    lambda _env: os.path.join(pasta, 'cache.sqlite'))
        caminho.start()
        self.addCleanup(caminho.stop)
        self.docs = {i: {'recordId': i, 'nome': 'R ' + i, 'tipoRegistro': 'local', 'subTipo': 'outroLocal',
                         'situation': 'Ativo', 'descricao': '', 'geometryType': 'point',
                         'geometryPoints': json.dumps([{'la': -15.8, 'lo': -47.9 + n, 'ts': 1}]),
                         'createdBy': 'u1', 'createdAt': 1, 'lastModified': 1, 'serverTimestamp': 1}
                     for n, i in enumerate(('R1', 'R2', 'R3'))}

    def _receber(self, delta=None, rede=True, uid='u1'):
        from tairu_sync import pull
        tarefa = mock.MagicMock(**{'isCanceled.return_value': False})

        def rodar(_titulo, busca, on_success, on_error, **_k):
            return on_success(busca(tarefa)) if rede else on_error('sem rede')

        dock = mock.MagicMock(**{'fs.list_record_groups.return_value': [],
                                 'fs.list_records.return_value': copy.deepcopy(list(self.docs.items())),
                                 'fs.list_records_since.return_value': copy.deepcopy(delta or [])})
        dock.env.key, dock.tokens.uid = 'dev', uid
        with mock.patch.object(pull, 'map_workspace', return_value={'gpkg': self.gpkg}), \
                mock.patch.object(pull, 'record_schema_generation', return_value=3), \
                mock.patch.object(pull, 'mark_record_schema_generation'), \
                mock.patch.object(pull, 'save_last_pull_ts'), \
                mock.patch.object(pull, 'run_or_defer'), \
                mock.patch.object(pull, 'run_task', rodar):
            pull.start_pull(dock, _Mapa())

    def _camada(self):
        return QgsVectorLayer(self.gpkg + '|layername=registros_ponto', 'pontos', 'ogr')

    def _trabalho_no_qgis(self):
        camada = self._camada()
        feicao = [f for f in camada.getFeatures() if f['recordId'] == 'R1'][0]
        camada.startEditing()
        camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('nome'), 'Nome do QGIS')
        desenhada = QgsFeature(camada.fields())
        desenhada.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(-47.5, -15.5)))
        camada.addFeature(desenhada)
        self.assertTrue(camada.commitChanges())

    def _estado(self):
        def valor(v):
            return None if v is None or (hasattr(v, 'isNull') and v.isNull()) else v
        return sorted(tuple(str(valor(f[k]) or '') for k in ('recordId', 'nome', 'situation'))
                      for f in self._camada().getFeatures())

    def _app(self, record_id, **campos):
        self.docs[record_id].update(campos, lastModified=5, serverTimestamp=10)
        return (record_id, copy.deepcopy(self.docs[record_id]))

    def test_recebimento_incremental_guarda_a_edicao_e_o_envio_junta_os_dois(self):
        from tairu_core.firestore_cache import FirestoreCache
        from tairu_sync.push import build_push_plan
        self._receber()
        self._trabalho_no_qgis()
        delta = [self._app('R1', situation='Concluído'),     # campo que o QGIS não mexeu
                 self._app('R2', nome='R2 do app'),          # registro que o QGIS não mexeu
                 self._app('R3', isDeleted=True)]            # apagado no app, sem edição aqui
        self._receber(delta)
        # R1 recebe a situação do app e fica com o nome do QGIS: campo a campo.
        self.assertEqual(self._estado(), [('', '', ''), ('R1', 'Nome do QGIS', 'Concluído'),
                                          ('R2', 'R2 do app', 'Ativo')])

        itens = {i.record.record_id: i for i in build_push_plan(
            self._camada(), _MAPPING, _Mapa(), 'u1', cache=FirestoreCache('dev', 'u1')).items}
        self.assertEqual((itens['R1'].action, itens['R1'].changed_fields), ('update', ['nome']))
        self.assertEqual(itens['R2'].action, 'unchanged')

    def test_mesmo_campo_nos_dois_lados_fica_para_o_envio_mostrar_o_conflito(self):
        from tairu_core.firestore_cache import FirestoreCache
        from tairu_sync.push import build_push_plan
        self._receber()
        self._trabalho_no_qgis()
        self._receber([self._app('R1', nome='Nome do app')])
        self.assertIn(('R1', 'Nome do QGIS', 'Ativo'), self._estado())
        # O app mexe de novo antes do envio: a situação chega, o nome do QGIS continua guardado.
        self._receber([self._app('R1', situation='Concluído')])
        self.assertIn(('R1', 'Nome do QGIS', 'Concluído'), self._estado())
        item = [i for i in build_push_plan(self._camada(), _MAPPING, _Mapa(), 'u1',
                                           cache=FirestoreCache('dev', 'u1')).items
                if i.record.record_id == 'R1'][0]
        self.assertEqual((item.action, item.send), ('conflict', False))

    def test_camada_nao_gravada_nao_deixa_o_cache_a_frente(self):
        # O cache era gravado antes da camada: com a gravação falhando, a mudança de grupo do
        # app ficava só no cache, e o envio seguinte a tomava por escolha do QGIS e a desfazia.
        from tairu_core.firestore_cache import FirestoreCache
        from tairu_sync import pull
        from tairu_sync.push import build_push_plan
        self._receber()
        with mock.patch.object(pull, 'apply_pull', side_effect=RuntimeError('disco cheio')):
            self._receber([self._app('R1', groupId='g2')])
        camada = self._camada()
        camada.setCustomProperty('tairu/folder', '%s|point|' % _MAP_ID)   # a pasta do recebimento
        feicao = [f for f in camada.getFeatures() if f['recordId'] == 'R1'][0]
        camada.startEditing()
        camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('nome'), 'Nome do QGIS')
        self.assertTrue(camada.commitChanges())
        item = [i for i in build_push_plan(camada, _MAPPING, _Mapa(), 'u1', cache=FirestoreCache('dev', 'u1')).items
                if i.record.record_id == 'R1'][0]
        self.assertEqual((item.action, item.changed_fields), ('update', ['nome']))

    def test_sem_rede_ou_sem_cache_o_recebimento_completo_nao_apaga_nada(self):
        self._receber()
        self._trabalho_no_qgis()
        esperado = self._estado()
        self._receber(rede=False)    # o automático depois de um envio, sem rede: vem do cache
        self.assertEqual(self._estado(), esperado)
        self._receber(uid='u2')      # outra conta neste QGIS (ou cache apagado): completo
        self.assertEqual(self._estado(), esperado)


class TestConflito(unittest.TestCase):
    """Mudou dos dois lados (ou não há de onde saber): não sobe sozinho; marcado, sobe."""

    def test_mesmo_campo_nos_dois_lados_so_sobe_marcado(self):
        nuvem = _Nuvem()
        caminho = _fonte(4326, 4326, -47.9, -15.8)
        camada = _abrir(caminho, '#309e3b')
        nuvem.enviar(camada)
        record_id = next(iter(nuvem.docs))
        _editar(camada, 'nome', 'Nome do QGIS')
        nuvem.docs[record_id]['nome'] = 'Nome do app'
        nuvem.receber()

        plano = nuvem.plano(camada)
        item = plano.items[0]
        self.assertEqual((item.action, item.send, item.changed_fields), ('conflict', False, ['nome']))
        self.assertEqual(plano.writable_items(), [])
        self.assertIn('1 conflitos', plano.summary())

        item.send = True       # o usuário marca "Enviar" na prévia: vale o QGIS
        nuvem.gravar(plano, camada)
        self.assertEqual(nuvem.docs[record_id]['nome'], 'Nome do QGIS')
        self.assertEqual(nuvem.plano(camada).items[0].action, 'unchanged')

    def test_sem_copia_local_nao_sobrescreve(self):
        nuvem = _Nuvem()
        caminho = _fonte(4326, 4326, -47.9, -15.8)
        camada = _abrir(caminho, '#309e3b')
        nuvem.enviar(camada)
        nuvem.cache = {}           # outra máquina, cache apagado
        _editar(camada, 'nome', 'Nome do QGIS')
        item = nuvem.plano(camada).items[0]
        self.assertEqual((item.action, item.send), ('conflict', False))
        self.assertIn('Receber Registros', item.warning)

    def test_shapefile_sem_a_coluna_do_hash_sobe_so_o_que_mudou(self):
        # O OGR corta 'tairuSyncHash' em 'tairuSyncH': o hash nunca era gravado nem lido, e todo
        # registro virava conflito a cada envio. A linha de base mora fora da camada.
        nuvem = _Nuvem()
        camada = _abrir(_fonte(4326, 4326, -47.9, -15.8, arquivo='fonte.shp'), '#309e3b')
        nuvem.enviar(camada)
        self.assertLess(camada.fields().indexOf('tairuSyncHash'), 0)
        # A data e o tamanho do registro novo não cabem no Shapefile (nomes cortados): lidos
        # vazios, não são edição do QGIS e não apagam os da nuvem.
        self.assertEqual(nuvem.plano(camada).items[0].action, 'unchanged')
        colunas = camada.fields().count()
        record_id = next(iter(nuvem.docs))
        data = nuvem.docs[record_id]['eventDateTime']

        # Enviado pela 2.0.26 (sem linha de base, sem carimbo): a feição igual à nuvem é a base;
        # editada, não há de onde saber o que mudou aqui — conflito, só no campo que difere.
        guardadas = dict(nuvem.bases)
        nuvem.bases.clear()
        self.assertEqual(nuvem.plano(camada).items[0].action, 'unchanged')
        _editar(camada, 'nome', 'Outro nome')
        item = nuvem.plano(camada).items[0]
        self.assertEqual((item.action, item.send, item.changed_fields), ('conflict', False, ['nome']))
        nuvem.bases.update(guardadas)

        nuvem.docs[record_id].update(situation='Concluído', lastModified=2)   # o app
        nuvem.receber()
        _editar(camada, 'nome', 'Nome do QGIS')
        item = nuvem.enviar(camada)
        self.assertEqual((item.action, item.changed_fields), ('update', ['nome']))
        self.assertEqual((nuvem.docs[record_id]['nome'], nuvem.docs[record_id]['situation'],
                          nuvem.docs[record_id]['eventDateTime']), ('Nome do QGIS', 'Concluído', data))
        self.assertEqual(camada.fields().count(), colunas)   # cada envio acrescentava as colunas longas

    def test_apagado_no_app_e_editado_aqui_nao_ressuscita_sozinho(self):
        # O 'update' põe isDeleted=False: editar no QGIS um registro apagado no app o trazia
        # de volta sem ninguém ver.
        from tairu_core.i18n import tr
        nuvem = _Nuvem()
        camada = _abrir(_fonte(4326, 4326, -47.9, -15.8), '#309e3b')
        nuvem.enviar(camada)
        record_id = next(iter(nuvem.docs))
        nuvem.docs[record_id]['isDeleted'] = True
        nuvem.receber()
        _editar(camada, 'descricao', 'visto no QGIS')

        plano = nuvem.plano(camada)
        item = plano.items[0]
        self.assertEqual((item.action, item.send), ('conflict', False))
        self.assertIn(tr('apagado no Tairu desde o último recebimento: marque Enviar para '
                         'restaurá-lo com a versão do QGIS'), item.warning)
        self.assertEqual(plano.writable_items(), [])

        item.send = True        # restaurar, com a versão do QGIS
        nuvem.gravar(plano, camada)
        self.assertEqual((nuvem.docs[record_id]['isDeleted'], nuvem.docs[record_id]['descricao']),
                         (False, 'visto no QGIS'))


class TestBaseGuardadaSegueACamada(unittest.TestCase):
    """A base mora fora do arquivo: só vale enquanto a feição é a do último sincronismo."""

    def test_descartar_a_edicao_depois_de_enviar_nao_desfaz_a_nuvem(self):
        nuvem = _Nuvem()
        camada = _abrir(_fonte(4326, 4326, -47.9, -15.8), '#309e3b')
        nuvem.enviar(camada)
        record_id = next(iter(nuvem.docs))
        camada.startEditing()
        camada.changeAttributeValue(next(camada.getFeatures()).id(), camada.fields().indexOf('nome'),
                                    'No buffer')
        nuvem.enviar(camada)      # a prévia do diálogo recusa; aqui o carimbo vai para o buffer
        camada.rollBack()         # "Descartar": a feição volta, o carimbo também
        self.assertEqual(nuvem.docs[record_id]['nome'], 'No buffer')
        self.assertEqual(nuvem.plano(camada).items[0].action, 'unchanged')

    def test_mesma_correcao_nos_dois_lados_anda_a_base(self):
        from tairu_sync.push import _store_baselines
        nuvem = _Nuvem()
        camada = _abrir(_fonte(4326, 4326, -47.9, -15.8), '#309e3b')
        nuvem.enviar(camada)
        record_id = next(iter(nuvem.docs))
        _editar(camada, 'nome', 'Igual')
        nuvem.docs[record_id]['nome'] = 'Igual'
        nuvem.receber()
        plano = nuvem.plano(camada)
        self.assertEqual(plano.items[0].action, 'unchanged')
        _store_baselines(plano, camada, nuvem)     # execute_push: "Nada para enviar"

        nuvem.docs[record_id].update(nome='Do app', lastModified=5)
        nuvem.receber()
        _editar(camada, 'descricao', 'do QGIS')
        item = nuvem.plano(camada).items[0]
        self.assertEqual((item.action, item.changed_fields), ('update', ['descricao']))

    def test_recebimento_ve_a_camada_com_edicao_nao_salva(self):
        from tairu_sync.record_convert import layers_with_pending_edits
        caminho = _fonte(4326, 4326, -47.9, -15.8)
        camada = _abrir(caminho, '#309e3b')
        camada.startEditing()
        self.assertEqual(layers_with_pending_edits(caminho), [])     # em edição, nada mudado
        camada.changeAttributeValue(next(camada.getFeatures()).id(), camada.fields().indexOf('nome'), 'x')
        self.assertEqual(layers_with_pending_edits(caminho), [camada])
        camada.rollBack()


class TestResumoDaPrevia(unittest.TestCase):

    @staticmethod
    def _dialogo(plano):
        from qgis.PyQt.QtWidgets import QWidget
        from tairu_ui import push_dialog

        class _Dock(QWidget):
            tokens = mock.Mock(uid='u1')

        dialogo = push_dialog.PushDialog(_Dock(), _Mapa())
        dialogo.entries = [(plano, None)]
        return dialogo

    def test_tabela_da_previa_nao_poe_na_mascara_o_que_ninguem_editou(self):
        # A célula formatada com 6 casas voltava arredondada, e a situação vazia voltava como a
        # 1ª opção do tipo: os dois entravam na máscara e subiam por cima do valor do app.
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import PushItem, PushPlan

        rec = TairuRecord('R1', nome='A', tipo_registro='local', sub_tipo='outroLocal', situation='',
                          size=150.123456789, value_estimate=0.1 + 0.2, geometry_type='circle',
                          circle_radius=150.123456789, geometry_size=2.123456789)
        plano = PushPlan(map_id=_MAP_ID, layer_name='fonte',
                         items=[PushItem('update', rec, changed_fields=['nome'])])
        dialogo = self._dialogo(plano)
        dialogo._fill_table()
        self.assertTrue(dialogo._apply_table_to_plan())
        self.assertEqual(plano.items[0].changed_fields, ['nome'])
        self.assertEqual((rec.situation, rec.size, rec.circle_radius), ('', 150.123456789, 150.123456789))

    def test_conflito_alem_do_limite_da_tabela_nao_conta_como_enviado(self):
        from tairu_core.i18n import tr
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import PushItem, PushPlan
        from tairu_ui import push_dialog

        plano = PushPlan(map_id=_MAP_ID, layer_name='fonte', items=[
            PushItem('update', TairuRecord('R1')), PushItem('new', TairuRecord('R2')),
            PushItem('conflict', TairuRecord('R3'), send=False)])
        dialogo = self._dialogo(plano)
        with mock.patch.object(push_dialog, '_MAX_PREVIEW_ROWS', 1):
            dialogo._fill_table()
        dialogo._refresh_summary()
        resumo = dialogo.summary_label.text()
        self.assertIn(tr('{n} itens além do limite da tabela (enviados assim mesmo)').format(n=1), resumo)
        self.assertIn(tr('{n} itens além do limite da tabela (não enviados)').format(n=1), resumo)

    def test_marcar_todos_os_conflitos_alcanca_os_alem_da_tabela(self):
        # Um Shapefile grande sem o carimbo é todo conflito, e além das linhas da tabela não
        # havia caixa para marcar: a camada simplesmente não podia ser enviada.
        from tairu_core.i18n import tr
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import PushItem, PushPlan
        from tairu_ui import push_dialog

        plano = PushPlan(map_id=_MAP_ID, layer_name='fonte', items=[
            PushItem('conflict', TairuRecord('R%d' % i), send=False) for i in range(3)])
        dialogo = self._dialogo(plano)
        with mock.patch.object(push_dialog, '_MAX_PREVIEW_ROWS', 1):
            dialogo._fill_table()
        dialogo._refresh_summary()
        self.assertFalse(dialogo.mark_conflicts_btn.isHidden())

        dialogo.mark_conflicts_btn.click()
        self.assertEqual([i.send for i in plano.items], [True] * 3)
        self.assertEqual(dialogo.table.item(0, push_dialog._SEND_COL).checkState(), push_dialog._CHECKED)
        self.assertIn(tr('{n} itens além do limite da tabela (enviados assim mesmo)').format(n=2),
                      dialogo.summary_label.text())
        self.assertTrue(dialogo.mark_conflicts_btn.isHidden())


class TestCopiaLocalDepoisDoEnvio(unittest.TestCase):

    def test_update_guarda_so_o_que_subiu_mesmo_sem_o_recebimento_seguinte(self):
        # A cópia local era o candidato inteiro: o nome do QGIS por cima do que o app deu.
        # Com o recebimento automático falhando, essa base fazia o nome mudado depois no
        # QGIS parecer mudança só do QGIS — e ele sobrescrevia o do app sem conflito.
        from tairu_sync import pull, push
        nuvem = _Nuvem()
        camada = _abrir(_fonte(4326, 4326, -47.9, -15.8), '#309e3b')
        nuvem.enviar(camada)
        record_id = next(iter(nuvem.docs))
        nuvem.docs[record_id]['nome'] = 'Nome do app'
        nuvem.receber()
        _editar(camada, 'descricao', 'visto no QGIS')
        plano = nuvem.plano(camada)
        self.assertEqual(plano.items[0].changed_fields, ['descricao'])

        tarefa = mock.MagicMock(**{'isCanceled.return_value': False})
        with mock.patch.object(push, 'run_task', lambda _t, envio, on_success, **_k: on_success(envio(tarefa))), \
                mock.patch.object(push, 'FirestoreCache', lambda *_a: nuvem), \
                mock.patch.object(pull, 'start_pull', side_effect=RuntimeError('sem rede')):
            push.execute_push(mock.MagicMock(fs=nuvem), _Mapa(), [(plano, camada)])
        self.assertEqual((nuvem.cache[record_id]['nome'], nuvem.cache[record_id]['descricao']),
                         ('Nome do app', 'visto no QGIS'))

        _editar(camada, 'nome', 'Nome do QGIS')
        self.assertEqual(nuvem.plano(camada).items[0].action, 'conflict')
        self.assertEqual(nuvem.docs[record_id]['nome'], 'Nome do app')

    def test_aviso_do_envio_nao_conta_conflito_desmarcado(self):
        from tairu_core.i18n import tr
        from tairu_firebase.models import TairuRecord
        from tairu_sync import pull, push

        nuvem = _Nuvem()
        nuvem.docs['R1'] = {}
        plano = push.PushPlan(map_id=_MAP_ID, items=[
            push.PushItem('update', TairuRecord('R1', nome='x'), changed_fields=['nome']),
            push.PushItem('conflict', TairuRecord('R2'), send=False)])
        dock = mock.MagicMock(fs=nuvem)
        tarefa = mock.MagicMock(**{'isCanceled.return_value': False})
        with mock.patch.object(push, 'run_task', lambda _t, envio, on_success, **_k: on_success(envio(tarefa))), \
                mock.patch.object(push, 'FirestoreCache', lambda *_a: nuvem), \
                mock.patch.object(pull, 'start_pull'):
            push.execute_push(dock, _Mapa(), [(plano, None)])
        aviso = dock.notify.call_args_list[0][0][0]
        self.assertIn(tr('{n} atualizados').format(n=1), aviso)
        self.assertNotIn(tr('{n} conflitos').format(n=1), aviso)


class TestGrupoDoEnvio(unittest.TestCase):

    def test_conflito_marcado_entra_no_grupo(self):
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import PushItem, PushPlan, apply_group_to_plan

        plano = PushPlan(map_id=_MAP_ID, items=[
            PushItem('conflict', TairuRecord('R1'), changed_fields=['nome']),
            PushItem('conflict', TairuRecord('R2'), send=False)])
        self.assertEqual(apply_group_to_plan(plano, 'g1'), 1)
        self.assertEqual([i.record.group_id for i in plano.items], ['g1', ''])
        self.assertEqual(plano.items[0].changed_fields, ['nome', 'groupId'])


if __name__ == '__main__':
    unittest.main()
