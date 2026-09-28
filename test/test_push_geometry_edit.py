# -*- coding: utf-8 -*-

"""Geometria editada no QGIS: o que o envio grava e o que o recebimento devolve.

1) Mover um vértice e enviar tem de SOBREVIVER ao recebimento seguinte. O app grava
   geometryWkb em todo registro, e o pull monta linha/polígono pela WKB: um envio que
   atualizava só os pontos deixava a WKB velha na nuvem, e o pull devolvia a forma
   antiga — a edição sumia de todos os aparelhos.
2) Renomear uma trilha não pode tocar na geometria (o horário de cada ponto era
   trocado pelo do envio); mover um vértice dela mantém o horário de cada ponto (M).
3) Camada sem SRC, ou com SRC declarado errado, é recusada: metros iam para a nuvem
   (e para o .tairudb) como latitude/longitude, sem aviso.
4) Furo, parte secundária e trilha dividida — o que os geometryPoints (anel externo da
   maior parte) não mostram — sobem inteiros; e a conta nova do hash, que passou a vê-los,
   não vira uma leva de escritas numa camada recebida antes dela.

Precisa do Python do QGIS (qgis.core); pulado em outros interpretadores.
"""

import base64
import copy
import json
import os
import struct
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import (
        QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
        QgsGeometry, QgsPoint, QgsPointXY, QgsProject)
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None
_MAP_ID = 'map-de-teste'
_MAPPING = {'nome_field': None, 'descricao_field': None, 'tipo': 'local',
            'sub_tipo': 'outroLocal', 'situation': 'Ativo'}
_GEOMETRIA = ('geometryType', 'geometryPoints', 'geometryBounds', 'circleRadius', 'geometryWkb')


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


class _Nuvem(object):
    """Firestore em memória: aplica a escrita mascarada e responde como o cache local."""

    def __init__(self, docs):
        self.docs = docs
        self.bases = {}     # linha de base: (camada, recordId) -> campos
        self.cache = None   # cópia local; None = a própria nuvem (recebida agora)
        self.gpkg = os.path.join(tempfile.mkdtemp(), 'records.gpkg')

    def build_update_write(self, path, fields, mask, require_existing=True):
        doc = self.docs[path.rsplit('/', 1)[1]]
        for key in mask:
            value = fields.get(key)
            # Blob chega ao plugin como base64, pelo codec REST.
            doc[key] = base64.b64encode(value).decode('ascii') if isinstance(value, bytes) else value
        return mask

    def load_records_by_id(self, _map_id, ids):
        docs = self.docs if self.cache is None else self.cache
        return {i: copy.deepcopy(docs[i]) for i in ids if i in docs}

    def load_baselines(self, _map_id, camada, ids):
        return {i: copy.deepcopy(self.bases[camada, i]) for i in ids if (camada, i) in self.bases}

    def store_baselines(self, _map_id, camada, rows):
        self.bases.update({(camada, i): copy.deepcopy(campos) for i, campos in rows})

    def receber(self, spec_key='line', base=True):
        """'Receber Registros' incremental; devolve a camada da expedição. base=False: como a
        2.0.26, que não guardava a linha de base."""
        from tairu_firebase.models import TairuRecord
        from tairu_sync.record_convert import apply_pull, sync_record_layers

        apply_pull(self.gpkg, [TairuRecord.from_fields(i, copy.deepcopy(d))
                               for i, d in self.docs.items()], remove_missing=False,
                   cache=self if base else None, map_id=_MAP_ID)
        sync_record_layers(self.gpkg, 'Exp', _MAP_ID, [])
        chave = '%s|%s|' % (_MAP_ID, spec_key)
        return [c for c in QgsProject.instance().mapLayers().values()
                if c.customProperty('tairu/folder', '') == chave][0]

    def enviar(self, camada):
        from tairu_sync.push import build_push_plan, build_writes

        plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1', cache=self)
        build_writes(self, plano, 'u1')
        return plano.items[0]


def _doc(record_id, sub_tipo, wkt, pontos):
    return {
        'recordId': record_id, 'nome': 'linha', 'tipoRegistro': 'acao', 'subTipo': sub_tipo,
        'situation': 'Concluída', 'geometryType': 'line', 'geometryPoints': json.dumps(pontos),
        'geometryWkb': base64.b64encode(bytes(QgsGeometry.fromWkt(wkt).asWkb())).decode('ascii'),
        'createdBy': 'u1', 'createdAt': 1, 'lastModified': 1,
    }


def _mover_vertice(camada, x, y, indice):
    feicao = next(camada.getFeatures())
    camada.startEditing()
    camada.moveVertex(x, y, feicao.id(), indice)
    assert camada.commitChanges(), camada.commitErrors()


class TestVerticeMovidoSobreviveAoRecebimento(unittest.TestCase):

    def setUp(self):
        QgsProject.instance().clear()

    def test_ida_e_volta_devolve_a_forma_nova(self):
        pontos = [{'la': -15.8, 'lo': -47.9, 'ts': 1}, {'la': -15.79, 'lo': -47.89, 'ts': 1},
                  {'la': -15.78, 'lo': -47.88, 'ts': 1}]
        nuvem = _Nuvem({'L1': _doc('L1', 'outraAcao',
                                   'LineString(-47.9 -15.8, -47.89 -15.79, -47.88 -15.78)', pontos)})
        camada = nuvem.receber()
        _mover_vertice(camada, -47.885, -15.79, 1)   # ~536 m para leste

        item = nuvem.enviar(camada)
        self.assertEqual(item.action, 'update')
        self.assertIn('geometryWkb', item.changed_fields)

        nova = 'LineString (-47.9 -15.8, -47.885 -15.79, -47.88 -15.78)'
        camada = nuvem.receber()
        self.assertEqual(next(camada.getFeatures()).geometry().asWkt(4), nova)
        # E o envio seguinte não tem o que mandar — antes ele mandava a forma VELHA.
        self.assertEqual(nuvem.enviar(camada).action, 'unchanged')

    def test_anel_novo_junto_com_atributo_sobrevive_ao_recebimento(self):
        # Os geometryPoints levam só o anel externo: comparar por eles dava o furo novo
        # por "geometria igual", a WKB velha ficava na nuvem e o pull apagava o furo.
        pontos = [{'la': -15.8, 'lo': -47.9, 'ts': 1}, {'la': -15.8, 'lo': -47.8, 'ts': 1},
                  {'la': -15.7, 'lo': -47.8, 'ts': 1}, {'la': -15.7, 'lo': -47.9, 'ts': 1}]
        doc = _doc('P1', 'outroLocal',
                   'Polygon((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.7, -47.9 -15.8))', pontos)
        doc.update(geometryType='polygon', tipoRegistro='local')
        nuvem = _Nuvem({'P1': doc})
        camada = nuvem.receber('polygon')
        feicao = next(camada.getFeatures())
        camada.startEditing()
        camada.addRing([QgsPointXY(-47.86, -15.76), QgsPointXY(-47.84, -15.76),
                        QgsPointXY(-47.84, -15.74), QgsPointXY(-47.86, -15.76)])
        camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('nome'), 'com furo')
        self.assertTrue(camada.commitChanges())
        nuvem.enviar(camada)

        camada = nuvem.receber('polygon')
        geometria = next(camada.getFeatures()).geometry()  # constGet() de um temporário é lixo
        self.assertEqual(geometria.constGet().numInteriorRings(), 1)

    def test_geometria_que_nao_deixa_wkb_apaga_a_velha(self):
        # Pontos + WKB acima de 1 MB: sobem só os pontos (como no app) e a WKB velha vai
        # nula. Linha que ficou com um vértice: o app não grava WKB, e a velha também sai.
        # Nos dois casos a WKB velha, que o pull prefere, desfaria a edição.
        from tairu_sync.push import build_push_plan, build_writes

        coords = [(-47.9 + i * 1.234567e-5, -15.8 + i * 1.234567e-5) for i in range(14000)]
        nuvem = _Nuvem({
            'D1': _doc('D1', 'outraAcao', 'LineString(%s)' % ', '.join('%r %r' % c for c in coords),
                       [{'la': y, 'lo': x, 'ts': 1} for x, y in coords]),
            'D2': _doc('D2', 'outraAcao', 'LineString(-47.9 -15.8, -47.89 -15.79)',
                       [{'la': -15.8, 'lo': -47.9, 'ts': 1}, {'la': -15.79, 'lo': -47.89, 'ts': 1}]),
        })
        camada = nuvem.receber()
        feicoes = {f['recordId']: f.id() for f in camada.getFeatures()}
        camada.startEditing()
        camada.moveVertex(-47.95, -15.85, feicoes['D1'], 1)
        camada.changeGeometry(feicoes['D2'], QgsGeometry.fromPolylineXY([QgsPointXY(-47.7, -15.7)]))
        self.assertTrue(camada.commitChanges())
        plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1', cache=nuvem)
        build_writes(nuvem, plano, 'u1')

        for doc in nuvem.docs.values():
            self.assertIsNone(doc['geometryWkb'])
        self.assertEqual(len(json.loads(nuvem.docs['D1']['geometryPoints'])), 14000)
        camada = nuvem.receber()
        movido = [f for f in camada.getFeatures() if f['recordId'] == 'D1'][0].geometry().vertexAt(1)
        self.assertEqual((movido.x(), movido.y()), (-47.95, -15.85))


class TestHorarioDaTrilha(unittest.TestCase):

    def setUp(self):
        QgsProject.instance().clear()

    def test_renomear_nao_toca_na_geometria_e_mover_mantem_o_horario(self):
        horas = [1700000000000, 1700000060000, 1700000120000]
        pontos = [{'la': -15.8, 'lo': -47.9, 'ts': horas[0]},
                  {'la': -15.79, 'lo': -47.89, 'ts': horas[1]},
                  {'la': -15.78, 'lo': -47.88, 'ts': horas[2]}]
        nuvem = _Nuvem({'T1': _doc(
            'T1', 'rastreamento',
            'LineStringM(-47.9 -15.8 %d, -47.89 -15.79 %d, -47.88 -15.78 %d)' % tuple(horas), pontos)})
        wkb_original = nuvem.docs['T1']['geometryWkb']

        camada = nuvem.receber()
        feicao = next(camada.getFeatures())
        camada.startEditing()
        camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('nome'), 'renomeada')
        self.assertTrue(camada.commitChanges())
        item = nuvem.enviar(camada)
        self.assertEqual(item.action, 'update')
        self.assertFalse(set(_GEOMETRIA) & set(item.changed_fields))
        self.assertEqual([p['ts'] for p in json.loads(nuvem.docs['T1']['geometryPoints'])], horas)
        self.assertEqual(nuvem.docs['T1']['geometryWkb'], wkb_original)

        # Vértice movido + um acrescentado no QGIS (chega sem M): cada ponto mantém a
        # hora dele, o novo herda a do anterior, e a WKB volta como LineString M.
        camada = nuvem.receber()
        _mover_vertice(camada, -47.885, -15.79, 1)
        camada.startEditing()
        camada.insertVertex(QgsPoint(-47.882, -15.785), next(camada.getFeatures()).id(), 2)
        self.assertTrue(camada.commitChanges())
        nuvem.enviar(camada)
        esperado = [horas[0], horas[1], horas[1], horas[2]]
        self.assertEqual([p['ts'] for p in json.loads(nuvem.docs['T1']['geometryPoints'])], esperado)
        wkb = base64.b64decode(nuvem.docs['T1']['geometryWkb'])
        self.assertEqual(struct.unpack_from('<BII', wkb), (1, 2002, 4))  # encodeLineStringM
        self.assertEqual([struct.unpack_from('<ddd', wkb, 9 + 24 * i)[2] for i in range(4)], esperado)


class TestSrcInvalidoERecusado(unittest.TestCase):

    def test_sem_src_ou_declarado_errado_nao_vai_para_a_nuvem_nem_para_o_tairudb(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsFeature, QgsVectorLayer
        from tairu_core.vector_export import export_vector_layers
        from tairu_sync.push import build_push_plan

        def camada(crs):
            # Coordenada UTM 23S (metros) do mesmo ponto de Brasília.
            c = QgsVectorLayer('Point?crs=%s&field=nome:string' % crs, crs, 'memory')
            feicao = QgsFeature(c.fields())
            feicao.setGeometry(QgsGeometry.fromWkt('POINT(189302.79 8251039.04)'))
            c.dataProvider().addFeatures([feicao])
            c.updateExtents()
            return c

        sem_src = camada('EPSG:31983')
        sem_src.setCrs(QgsCoordinateReferenceSystem())      # shapefile sem .prj
        em_metros = camada('EPSG:4326')                     # SRC declarado errado
        for c in (sem_src, em_metros):
            with self.assertRaises(ValueError):
                build_push_plan(c, _MAPPING, _Mapa(), 'u1')
        # A mesma camada em UTM de verdade continua indo, em graus.
        plano = build_push_plan(camada('EPSG:31983'), _MAPPING, _Mapa(), 'u1')
        la, lo = [(p['la'], p['lo']) for p in json.loads(plano.items[0].record.geometry_points_json)][0]
        self.assertAlmostEqual(la, -15.8, 2)
        self.assertAlmostEqual(lo, -47.9, 2)

        class _Escritor(object):
            conn = None
            linhas = []

            def insertVectorLayer(self, *args):
                self.linhas.append(args)

            def insertFeature(self, *args):
                self.linhas.append(args)

        class _Retorno(object):
            erros = []

            def push_info(self, _m):
                pass

            def report_error(self, m):
                self.erros.append(m)

            def set_progress(self, _p):
                pass

            def set_progress_text(self, _t):
                pass

            def is_canceled(self):
                return False

        escritor, retorno = _Escritor(), _Retorno()
        export_vector_layers(escritor, [em_metros], None, retorno)
        self.assertEqual(escritor.linhas, [])
        self.assertEqual(len(retorno.erros), 1)


_FURO = ('Polygon((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.7, -47.9 -15.8),'
         '(-47.86 -15.76, -47.84 -15.76, -47.84 -15.74, -47.86 -15.76))')
_MULTILINHA = 'MultiLineString((-47.9 -15.8, -47.89 -15.79, -47.88 -15.78),(-47.5 -15.5, -47.49 -15.49))'
_HORAS = [1700000000000 + 60000 * i for i in range(5)]
_TRILHA_EM_PARTES = ('MultiLineStringM((-47.9 -15.8 %d, -47.89 -15.79 %d, -47.88 -15.78 %d),'
                     '(-47.87 -15.77 %d, -47.86 -15.76 %d))' % tuple(_HORAS))


def _doc_de(record_id, wkt, sub_tipo='outroLocal', horas=None):
    """O documento como o app o grava: WKB + geometryPoints do anel externo da maior parte."""
    from tairu_sync.record_convert import flat_points_and_type
    pontos, tipo = flat_points_and_type(QgsGeometry.fromWkt(wkt))
    doc = _doc(record_id, sub_tipo, wkt, [{'la': la, 'lo': lo, 'ts': horas[i] if horas else 1}
                                          for i, (la, lo) in enumerate(pontos)])
    doc.update(geometryType=tipo, tipoRegistro='local' if tipo == 'polygon' else 'acao')
    return doc


def _na_nuvem(doc):
    geometria = QgsGeometry()
    geometria.fromWkb(base64.b64decode(doc['geometryWkb']))
    return geometria


class TestGeometriaInteira(unittest.TestCase):
    """O que os geometryPoints (anel externo da maior parte) não mostram também sobe."""

    def setUp(self):
        QgsProject.instance().clear()

    def test_so_o_furo_editado_sobe_e_sobrevive_ao_recebimento(self):
        # Mexer só no furo dava o mesmo tairuSyncHash: 'unchanged', e o recebimento seguinte
        # a qualquer mudança do app devolvia o furo velho.
        def mover(camada, feicao):
            camada.moveVertex(-47.87, -15.77, feicao.id(), 5)   # 1º vértice do furo, ~1,5 km

        def acrescentar(camada, _feicao):
            camada.addRing([QgsPointXY(-47.82, -15.72), QgsPointXY(-47.81, -15.72),
                            QgsPointXY(-47.81, -15.71), QgsPointXY(-47.82, -15.72)])

        def apagar(camada, feicao):
            geometria = QgsGeometry(feicao.geometry())
            geometria.deleteRing(1)
            camada.changeGeometry(feicao.id(), geometria)

        for editar, aneis in ((mover, 2), (acrescentar, 3), (apagar, 1)):
            with self.subTest(editar.__name__):
                QgsProject.instance().clear()
                nuvem = _Nuvem({'P1': _doc_de('P1', _FURO)})
                camada = nuvem.receber('polygon')
                camada.startEditing()
                editar(camada, next(camada.getFeatures()))
                self.assertTrue(camada.commitChanges())
                editada = next(camada.getFeatures()).geometry().asWkt(6)

                item = nuvem.enviar(camada)
                self.assertEqual(item.action, 'update')
                self.assertIn('geometryWkb', item.changed_fields)
                nuvem.docs['P1']['nome'] = 'renomeado no app'
                geometria = next(nuvem.receber('polygon').getFeatures()).geometry()
                self.assertEqual(geometria.asWkt(6), editada)
                self.assertEqual(geometria.constGet().numInteriorRings() + 1, aneis)

    def test_parte_secundaria_editada_sobe(self):
        # Os geometryPoints são a maior parte: mover a menor dava o mesmo hash, 'unchanged'.
        nuvem = _Nuvem({'M1': _doc_de('M1', _MULTILINHA, 'outraAcao')})
        camada = nuvem.receber()
        _mover_vertice(camada, -47.6, -15.6, 3)   # parte menor, ~15 km
        item = nuvem.enviar(camada)
        self.assertEqual(item.action, 'update')
        movido = _na_nuvem(nuvem.docs['M1']).vertexAt(3)
        self.assertEqual((movido.x(), movido.y()), (-47.6, -15.6))

    def test_trilha_em_partes_sobe_inteira_com_os_horarios(self):
        # A trilha era montada dos geometryPoints — só a maior parte —, e as outras partes
        # eram APAGADAS da nuvem, com todos os horários trocados pelo do envio.
        vertices = ['-47.9 -15.8', '-47.89 -15.79', '-47.88 -15.78', '-47.87 -15.77', '-47.86 -15.76']
        inteira = 'LineStringM(%s)' % ', '.join('%s %d' % (v, t) for v, t in zip(vertices, _HORAS))
        ajustada = _TRILHA_EM_PARTES.replace('-47.87 -15.77', '-47.871 -15.771')
        # Dividir no QGIS sem mover vértice (sai o trecho do pico de GPS); e a trilha que já
        # tem duas partes, com um vértice da menor ajustado — o hash antigo nem a via.
        for na_nuvem, editada in ((inteira, _TRILHA_EM_PARTES), (_TRILHA_EM_PARTES, ajustada)):
            with self.subTest(editada=editada):
                QgsProject.instance().clear()
                nuvem = _Nuvem({'T1': _doc_de('T1', na_nuvem, 'rastreamento', _HORAS)})
                camada = nuvem.receber()
                camada.startEditing()
                camada.changeGeometry(next(camada.getFeatures()).id(), QgsGeometry.fromWkt(editada))
                self.assertTrue(camada.commitChanges())

                self.assertEqual(nuvem.enviar(camada).action, 'update')
                esperada = QgsGeometry.fromWkt(editada).asWkt(6)   # com o M de cada vértice
                self.assertEqual(_na_nuvem(nuvem.docs['T1']).asWkt(6), esperada)
                self.assertEqual([p['ts'] for p in json.loads(nuvem.docs['T1']['geometryPoints'])],
                                 _HORAS[:3])
                self.assertEqual(next(nuvem.receber().getFeatures()).geometry().asWkt(6), esperada)

    def test_linha_dividida_nao_passa_por_geometria_igual(self):
        # Mesmos vértices, na mesma ordem, em duas partes: a geometria mudou.
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import _same_geometry_as_cloud

        linha = 'LineString(-47.9 -15.8, -47.89 -15.79, -47.88 -15.78, -47.87 -15.77, -47.86 -15.76)'
        dividida = ('MultiLineString((-47.9 -15.8, -47.89 -15.79, -47.88 -15.78),'
                    '(-47.87 -15.77, -47.86 -15.76))')
        nuvem = TairuRecord.from_fields('L1', _doc_de('L1', linha, 'outraAcao'))
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        identidade = QgsCoordinateTransform(wgs84, wgs84, QgsProject.instance())
        feicao = QgsFeature()
        for wkt, igual in ((linha, True), (dividida, False)):
            feicao.setGeometry(QgsGeometry.fromWkt(wkt))
            self.assertEqual(_same_geometry_as_cloud(nuvem, nuvem, feicao, identidade), igual, wkt)


class TestContaNovaDoHashSemEscritaEmMassa(unittest.TestCase):
    """geometryRings mudou o tairuSyncHash de toda feição com furo ou partes. Uma camada
    recebida antes disso não pode virar uma leva de escritas no envio seguinte, nem
    desfazer o que o app mudou; a edição de verdade nela ainda sobe."""

    def setUp(self):
        QgsProject.instance().clear()

    @staticmethod
    def _receber_com_hash_antigo(nuvem, *specs):
        from tairu_sync import record_convert
        atual = record_convert.sync_record_payload

        def antigo(rec):   # o payload do plugin até 2.0.26
            return {k: v for k, v in atual(rec).items() if k != 'geometryRings'}

        with mock.patch.object(record_convert, 'sync_record_payload', antigo):
            return [nuvem.receber(spec, base=False) for spec in specs]

    def test_camada_com_hash_antigo_reenviada_sem_edicao_nao_escreve_nada(self):
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import build_push_plan, build_writes
        from tairu_sync.record_convert import ensure_points_from_wkb, sync_record_hash

        multi = ('MultiPolygon(((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.7, -47.9 -15.8)),'
                 '((-47.5 -15.5, -47.45 -15.5, -47.45 -15.45, -47.5 -15.5)))')
        nuvem = _Nuvem({
            'furo': _doc_de('furo', _FURO), 'multi': _doc_de('multi', multi),
            'multilinha': _doc_de('multilinha', _MULTILINHA, 'outraAcao'),
            'trilha': _doc_de('trilha', _TRILHA_EM_PARTES, 'rastreamento', _HORAS),
            'simples': _doc_de('simples', 'Polygon((-47 -15, -46.9 -15, -46.9 -14.9, -47 -15))'),
            'linha': _doc_de('linha', 'LineString(-47 -15, -46.9 -14.9)', 'outraAcao'),
        })
        camadas = self._receber_com_hash_antigo(nuvem, 'line', 'polygon')
        guardado = {f['recordId']: f['tairuSyncHash'] for c in camadas for f in c.getFeatures()}
        for record_id in ('furo', 'multi', 'multilinha', 'trilha'):   # a conta mudou para eles
            rec = TairuRecord.from_fields(record_id, copy.deepcopy(nuvem.docs[record_id]))
            ensure_points_from_wkb(rec)
            self.assertNotEqual(sync_record_hash(rec), guardado[record_id], record_id)

        for doc in nuvem.docs.values():
            doc['nome'] = 'renomeado no app'
        for rodada in ('hash antigo', 'recebido de novo'):
            for camada in camadas:
                plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1', cache=nuvem)
                self.assertEqual({i.action for i in plano.items}, {'unchanged'}, rodada)
                self.assertEqual(build_writes(nuvem, plano, 'u1'), [], rodada)
            camadas = [nuvem.receber(spec) for spec in ('line', 'polygon')]
        self.assertEqual({d['nome'] for d in nuvem.docs.values()}, {'renomeado no app'})

    def test_aneis_diferentes_com_hash_antigo_sao_conflito(self):
        # O hash antigo não vê anéis: "o QGIS mexeu no furo" e "o app mexeu no furo" dão a mesma
        # conta. Tomar a diferença pelo QGIS revertia em silêncio o furo que o app moveu; e o
        # último furo apagado aqui junto com o nome saía como "o app pôs um furo" — o nome subia
        # e o furo ficava na nuvem. (Com a nuvem intocada desde a base, ver o teste seguinte.)
        from tairu_sync.push import build_push_plan, build_writes

        def furo_movido_aqui(nuvem, camada):
            nuvem.docs['P1'].update(nome='renomeado no app', lastModified=2)
            _mover_vertice(camada, -47.87, -15.77, 5)

        def ultimo_furo_apagado_aqui_com_o_nome_e_o_app_mexeu(nuvem, camada):
            nuvem.docs['P1'].update(situation='Pendente', lastModified=2)
            feicao = next(camada.getFeatures())
            geometria = QgsGeometry(feicao.geometry())
            geometria.deleteRing(1)
            camada.startEditing()
            camada.changeGeometry(feicao.id(), geometria)
            camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('nome'), 'nome do QGIS')
            self.assertTrue(camada.commitChanges())

        def furo_movido_no_app(nuvem, _camada):
            movido = _doc_de('P1', _FURO.replace('-47.86 -15.76,', '-47.861 -15.761,')
                             .replace('-47.86 -15.76)', '-47.861 -15.761)'))
            nuvem.docs['P1'].update(geometryWkb=movido['geometryWkb'], lastModified=2)

        for editar in (furo_movido_aqui, ultimo_furo_apagado_aqui_com_o_nome_e_o_app_mexeu, furo_movido_no_app):
            with self.subTest(editar.__name__):
                QgsProject.instance().clear()
                nuvem = _Nuvem({'P1': _doc_de('P1', _FURO)})
                camada, = self._receber_com_hash_antigo(nuvem, 'polygon')
                editar(nuvem, camada)
                antes = copy.deepcopy(nuvem.docs)
                plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1', cache=nuvem)
                item = plano.items[0]
                self.assertEqual((item.action, item.send), ('conflict', False))
                self.assertEqual(build_writes(nuvem, plano, 'u1'), [])
                self.assertEqual(nuvem.docs, antes)

                item.send = True    # marcado na prévia: vale a geometria do QGIS
                build_writes(nuvem, plano, 'u1')
                self.assertEqual(_na_nuvem(nuvem.docs['P1']).asWkt(6),
                                 next(camada.getFeatures()).geometry().asWkt(6))

    def _receber(self, nuvem, spec, hash_antigo):
        return self._receber_com_hash_antigo(nuvem, spec)[0] if hash_antigo else nuvem.receber(spec)

    def test_geometria_que_a_camada_nao_reproduz_nao_e_regravada(self):
        # O app grava o que a camada do recebimento não reproduz: MultiPolygon com a maior
        # parte depois da 1ª (o OGR junta tudo num Polygon), o editor estruturado (pontos de
        # TODOS os anéis achatados) e GeometryCollection (o OGR descarta a linha). Nada mudou
        # no QGIS; subir a camada trocava o MultiPolygon por um Polygon com "furos" fora do
        # contorno e apagava a linha da coleção.
        from tairu_sync.push import build_push_plan, build_writes

        maior_depois = ('MultiPolygon(((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.8)),'
                        '((-46.9 -15.8, -46.8 -15.8, -46.8 -15.7, -46.85 -15.65, -46.9 -15.8)))')
        docs = {'maior_depois': _doc_de('maior_depois', maior_depois)}
        for record_id, wkt in (('estruturado_multi', maior_depois), ('estruturado_furo', _FURO)):
            docs[record_id] = _doc_de(record_id, wkt)
            docs[record_id]['geometryPoints'] = json.dumps(
                [{'la': v.y(), 'lo': v.x(), 'ts': 1} for v in QgsGeometry.fromWkt(wkt).vertices()])
        docs['colecao'] = _doc_de('colecao', 'Polygon((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.8))')
        docs['colecao']['geometryWkb'] = base64.b64encode(bytes(QgsGeometry.fromWkt(
            'GeometryCollection(Polygon((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.8)),'
            'LineString(-46.9 -15.8, -46.8 -15.7))').asWkb())).decode('ascii')
        for hash_antigo in (False, True):
            with self.subTest(hash_antigo=hash_antigo):
                QgsProject.instance().clear()
                nuvem = _Nuvem(copy.deepcopy(docs))
                camada = self._receber(nuvem, 'polygon', hash_antigo)
                nuvem.cache = copy.deepcopy(nuvem.docs)   # a cópia do recebimento: o app renomeia depois
                for doc in nuvem.docs.values():
                    doc['nome'] = 'renomeado no app'
                antes = copy.deepcopy(nuvem.docs)
                plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1', cache=nuvem)
                self.assertEqual({i.record.record_id: i.action for i in plano.items},
                                 dict.fromkeys(docs, 'unchanged'))
                self.assertEqual(build_writes(nuvem, plano, 'u1'), [])
                self.assertEqual(nuvem.docs, antes)

    def test_furo_mexido_aqui_e_contorno_no_app_e_conflito(self):
        # Na conta antiga "o QGIS não mudou nada" também batia, e o furo movido aqui sumia sem
        # aviso — o recebimento seguinte o devolvia ao lugar. Na conta nova já era conflito.
        for hash_antigo in (False, True):
            with self.subTest(hash_antigo=hash_antigo):
                QgsProject.instance().clear()
                nuvem = _Nuvem({'P1': _doc_de('P1', _FURO)})
                camada = self._receber(nuvem, 'polygon', hash_antigo)
                _mover_vertice(camada, -47.87, -15.77, 5)   # furo, ~1,5 km
                no_app = _doc_de('P1', _FURO.replace('-47.8 -15.7,', '-47.79 -15.69,'))
                nuvem.docs['P1'].update(geometryWkb=no_app['geometryWkb'],
                                        geometryPoints=no_app['geometryPoints'], lastModified=2)
                antes = copy.deepcopy(nuvem.docs)

                item = nuvem.enviar(camada)
                self.assertEqual((item.action, item.send), ('conflict', False))
                self.assertEqual(nuvem.docs, antes)

    def test_ultimo_furo_apagado_na_camada_da_2_0_26_sobe(self):
        # Na conta antiga, apagar o último furo (ou ficar com uma parte) dá o hash de antes. A
        # cópia local intocada desde o carimbo (mesmo lastModified) é a base inteira, anéis
        # inclusive: a diferença é do QGIS, e sobe — sem recebimento completo de migração.
        def apagar_furo(camada, feicao):
            geometria = QgsGeometry(feicao.geometry())
            geometria.deleteRing(1)
            camada.changeGeometry(feicao.id(), geometria)

        def ficar_com_uma_parte(camada, feicao):
            camada.changeGeometry(feicao.id(), QgsGeometry.fromWkt(
                'LineString(-47.9 -15.8, -47.89 -15.79, -47.88 -15.78)'))

        for doc, spec, editar in ((_doc_de('R', _FURO), 'polygon', apagar_furo),
                                  (_doc_de('R', _MULTILINHA, 'outraAcao'), 'line', ficar_com_uma_parte)):
            with self.subTest(editar.__name__):
                QgsProject.instance().clear()
                nuvem = _Nuvem({'R': copy.deepcopy(doc)})
                camada, = self._receber_com_hash_antigo(nuvem, spec)
                camada.startEditing()
                editar(camada, next(camada.getFeatures()))
                self.assertTrue(camada.commitChanges())
                editada = next(camada.getFeatures()).geometry().asWkt(6)

                item = nuvem.enviar(camada)
                self.assertEqual(item.action, 'update')
                self.assertIn('geometryWkb', item.changed_fields)
                nuvem.docs['R']['nome'] = 'renomeado no app'
                self.assertEqual(next(nuvem.receber(spec).getFeatures()).geometry().asWkt(6), editada)

    def test_recebimento_depois_da_2_0_26_nao_apaga_a_edicao_ainda_nao_enviada(self):
        # Completo, ele regravava toda feição com recordId: o nome mudado no QGIS e ainda não
        # enviado sumia de todas as camadas no 1º Receber depois da atualização.
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import build_push_plan
        from tairu_sync.record_convert import apply_pull

        movido = _FURO.replace('-47.86 -15.76,', '-47.861 -15.761,').replace('-47.86 -15.76)', '-47.861 -15.761)')
        nuvem = _Nuvem({'furo': _doc_de('furo', _FURO), 'app': _doc_de('app', _FURO),
                        'simples': _doc_de('simples', 'Polygon((-47 -15, -46.9 -15, -46.9 -14.9, -47 -15))')})
        camada, = self._receber_com_hash_antigo(nuvem, 'polygon')
        nuvem.docs['app'].update(geometryWkb=_doc_de('app', movido)['geometryWkb'], lastModified=2)
        camada.startEditing()
        for feicao in camada.getFeatures():
            if feicao['recordId'] != 'app':
                camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('nome'), 'editado no QGIS')
        self.assertTrue(camada.commitChanges())

        apply_pull(nuvem.gpkg, [TairuRecord.from_fields(i, copy.deepcopy(d)) for i, d in nuvem.docs.items()],
                   remove_missing=True, keep_unpushed=True, cache=nuvem, map_id=_MAP_ID)
        feicoes = {f['recordId']: f for f in camada.getFeatures()}
        self.assertEqual({i: feicoes[i]['nome'] for i in ('furo', 'simples')}, dict.fromkeys(
            ('furo', 'simples'), 'editado no QGIS'))

        plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1', cache=nuvem)
        itens = {i.record.record_id: i for i in plano.items}
        self.assertEqual({i: (itens[i].action, itens[i].changed_fields) for i in ('furo', 'simples')},
                         dict.fromkeys(('furo', 'simples'), ('update', ['nome'])))
        # Aqui a cópia local já é a do app (furo movido): o hash antigo não diz quem mexeu nos
        # anéis, e a diferença é conflito — só na geometria.
        self.assertEqual((itens['app'].action, itens['app'].send), ('conflict', False))
        self.assertIn('geometry', itens['app'].warning)

    def test_recebimento_passa_o_cache_e_nunca_apaga_o_nao_enviado(self):
        # Antes da geração 2 (grupo, estilo) o recebimento é completo, para encher as colunas;
        # depois, incremental. Os dois recebem o cache (linha de base) e nenhum apaga o que foi
        # desenhado e não enviado.
        from tairu_sync import pull
        from tairu_sync.record_convert import PullResult

        estado = {'high_watermark_ms': 5, 'last_full_sync_ms': 5}
        for geracao, completo in ((1, True), (2, False)):
            with self.subTest(geracao=geracao):
                cache = mock.MagicMock(**{'load_sync_state.return_value': estado})
                aplicar = mock.MagicMock(return_value=PullResult())
                dock = mock.MagicMock(**{'fs.list_record_groups.return_value': [],
                                         'fs.list_records.return_value': [],
                                         'fs.list_records_since.return_value': []})
                with mock.patch.object(pull, 'FirestoreCache', return_value=cache), \
                        mock.patch.object(pull, 'map_workspace', return_value={'gpkg': 'x.gpkg'}), \
                        mock.patch.object(pull, 'record_schema_generation', return_value=geracao), \
                        mock.patch.object(pull, 'mark_record_schema_generation'), \
                        mock.patch.object(pull, 'save_last_pull_ts'), \
                        mock.patch.object(pull, 'run_or_defer'), \
                        mock.patch.object(pull, 'apply_pull', aplicar), \
                        mock.patch.object(pull, 'run_task',
                                          lambda _t, busca, on_success, **_k: on_success(busca(mock.MagicMock()))):
                    pull.start_pull(dock, _Mapa())
                argumentos = aplicar.call_args.kwargs
                self.assertEqual((argumentos['remove_missing'], argumentos['cache']), (completo, cache))
                self.assertTrue(argumentos['keep_unpushed'])


class TestGeometriaQueACamadaNaoGuarda(unittest.TestCase):
    """A camada de polígonos do recebimento é Polygon: o OGR junta MultiPolygon num polígono só e
    descarta o que não é polígono de uma GeometryCollection."""

    def setUp(self):
        QgsProject.instance().clear()

    def test_renomear_colecao_nao_apaga_a_parte_que_nao_e_poligono(self):
        colecao = ('GeometryCollection(Polygon((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.8)),'
                   'LineString(-46.9 -15.8, -46.8 -15.7))')
        doc = _doc_de('C', 'Polygon((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.8))')
        doc['geometryWkb'] = base64.b64encode(bytes(QgsGeometry.fromWkt(colecao).asWkb())).decode('ascii')
        nuvem = _Nuvem({'C': doc})
        camada = nuvem.receber('polygon')
        feicao = next(camada.getFeatures())
        camada.startEditing()
        camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('nome'), 'renomeado no QGIS')
        self.assertTrue(camada.commitChanges())

        item = nuvem.enviar(camada)
        self.assertEqual((item.action, item.changed_fields), ('update', ['nome']))
        self.assertEqual(_na_nuvem(nuvem.docs['C']).asWkt(6), QgsGeometry.fromWkt(colecao).asWkt(6))

    def test_multipoligono_editado_sobe_como_multipoligono(self):
        # O Polygon que o OGR montou tem as outras partes como "furos" fora do contorno: subir
        # isso deixava na nuvem um polígono inválido, sem preenchimento nas outras partes.
        multi = ('MultiPolygon(((-47.9 -15.8, -47.8 -15.8, -47.8 -15.7, -47.9 -15.8)),'
                 '((-46.9 -15.8, -46.8 -15.8, -46.8 -15.7, -46.9 -15.8)),'
                 '((-45.9 -15.8, -45.5 -15.8, -45.5 -15.4, -45.9 -15.4, -45.9 -15.8),'
                 '(-45.8 -15.7, -45.6 -15.7, -45.6 -15.5, -45.8 -15.5, -45.8 -15.7)),'
                 '((-45.75 -15.65, -45.65 -15.65, -45.65 -15.6, -45.75 -15.65)))')   # ilha no lago
        nuvem = _Nuvem({'M': _doc_de('M', multi)})
        camada = nuvem.receber('polygon')
        self.assertFalse(next(camada.getFeatures()).geometry().isMultipart())   # juntado
        _mover_vertice(camada, -47.91, -15.81, 0)

        self.assertIn('geometryWkb', nuvem.enviar(camada).changed_fields)
        na_nuvem = _na_nuvem(nuvem.docs['M'])
        self.assertTrue(na_nuvem.isGeosValid())
        # Os pontos (bounds do app, cliente sem WKB) são a maior parte, não o 1º anel da camada.
        self.assertEqual([(p['lo'], p['la']) for p in json.loads(nuvem.docs['M']['geometryPoints'])],
                         [(-45.9, -15.8), (-45.5, -15.8), (-45.5, -15.4), (-45.9, -15.4)])
        self.assertEqual(na_nuvem.asWkt(6), QgsGeometry.fromWkt(multi.replace(
            '-47.9 -15.8, -47.8', '-47.91 -15.81, -47.8').replace('-47.9 -15.8)', '-47.91 -15.81)')).asWkt(6))

    def test_traco_do_app_sobrevive_ao_simbolo_unico(self):
        # Acima de 200 registros o recebimento desenha tudo com um símbolo único de traço
        # contínuo, e mudar qualquer campo no QGIS apagava o tracejado do app.
        from tairu_sync.record_convert import style_layer

        doc = _doc_de('L', 'LineString(-47.9 -15.8, -47.8 -15.7)', 'outraAcao')
        doc['style'] = json.dumps({'v': 1, 'base': {'stroke': 'dashed'}, 'icon': 'x'})
        nuvem = _Nuvem({'L': doc})
        camada = nuvem.receber('line')
        style_layer(camada, 'line')   # o que o recebimento faz acima de MAX_RECORD_CATEGORIES
        feicao = next(camada.getFeatures())
        camada.startEditing()
        camada.changeAttributeValue(feicao.id(), camada.fields().indexOf('descricao'), 'visto no QGIS')
        self.assertTrue(camada.commitChanges())

        self.assertEqual(nuvem.enviar(camada).changed_fields, ['descricao'])
        self.assertEqual(json.loads(nuvem.docs['L']['style'])['base'].get('stroke'), 'dashed')


if __name__ == '__main__':
    unittest.main()
