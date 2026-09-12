# -*- coding: utf-8 -*-

"""Cor de polígono só-contorno e grupo de registros no envio.

1) Um polígono estilizado APENAS com linha colorida (o "Contorno: linha simples"
   do QGIS, ou um preenchimento com "sem pincel") virava registro com
   preenchimento OPACO na cor da linha. A causa está na API: uma camada de
   símbolo do tipo LINHA devolve QColor inválido em fillColor()/strokeColor() e a
   cor da linha em color() — que é o último acessor da cadeia de fill. O certo é
   NÃO gravar fundo nenhum: com geometryBackgroundColorValue vazio o app pinta o
   interior com a cor da linha a 30% de alfa.

2) O grupo de registros é criado com id determinístico, para que reenviar as
   mesmas camadas com o mesmo nome caia no MESMO grupo em vez de forjar um novo.

Precisa do Python do QGIS (qgis.core); pulado em outros interpretadores.
"""

import json
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import (
        QgsApplication, QgsFeature, QgsFillSymbol, QgsGeometry, QgsPointXY, QgsProject,
        QgsSimpleFillSymbolLayer, QgsSimpleLineSymbolLayer, QgsSingleSymbolRenderer,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None
_LINE = 0xFFFF8C00       # laranja da borda
_FILL = 0x800000FF       # azul 50% do preenchimento

_NO_BRUSH = 0 if QgsApplication is None else Qt.BrushStyle.NoBrush


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], False)
    _APP.initQgis()


def _qcolor(argb):
    return QColor((argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF, (argb >> 24) & 0xFF)


def _polygon_layer(name):
    layer = QgsVectorLayer('Polygon?crs=EPSG:4326&field=id:integer', name, 'memory')
    feature = QgsFeature(layer.fields())
    feature.setAttribute('id', 1)
    feature.setGeometry(QgsGeometry.fromPolygonXY(
        [[QgsPointXY(0, 0), QgsPointXY(0, 1), QgsPointXY(1, 1), QgsPointXY(0, 0)]]))
    layer.dataProvider().addFeatures([feature])
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer, False)
    return layer


def _argbs(layer):
    from tairu_sync.push import _feature_symbol_argbs

    return _feature_symbol_argbs(layer, next(layer.getFeatures()), 'polygon')


class TestOutlineOnlyPolygonKeepsItsColour(unittest.TestCase):

    def test_outline_simple_line_pushes_no_fill(self):
        layer = _polygon_layer('so contorno')
        symbol = QgsFillSymbol()
        symbol.changeSymbolLayer(0, QgsSimpleLineSymbolLayer(_qcolor(_LINE)))
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        fg_argb, bg_argb = _argbs(layer)
        self.assertEqual(fg_argb, _LINE)
        # Sem fundo: o app pinta o interior com a cor da linha a 30% de alfa.
        self.assertIsNone(bg_argb, 'contorno colorido não pode virar preenchimento opaco')

    def test_fill_with_no_brush_pushes_no_fill(self):
        layer = _polygon_layer('sem pincel')
        symbol_layer = QgsSimpleFillSymbolLayer()
        symbol_layer.setColor(_qcolor(0xFFFF0000))
        symbol_layer.setStrokeColor(_qcolor(_LINE))
        symbol_layer.setBrushStyle(_NO_BRUSH)
        symbol = QgsFillSymbol()
        symbol.changeSymbolLayer(0, symbol_layer)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        fg_argb, bg_argb = _argbs(layer)
        self.assertEqual(fg_argb, _LINE)
        self.assertIsNone(bg_argb)

    def test_solid_fill_still_pushes_its_fill(self):
        layer = _polygon_layer('solido')
        symbol_layer = QgsSimpleFillSymbolLayer()
        symbol_layer.setColor(_qcolor(_FILL))
        symbol_layer.setStrokeColor(_qcolor(_LINE))
        symbol = QgsFillSymbol()
        symbol.changeSymbolLayer(0, symbol_layer)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

        fg_argb, bg_argb = _argbs(layer)
        self.assertEqual(fg_argb, _LINE)
        self.assertEqual(bg_argb, _FILL)

    def test_point_and_line_defaults_are_untouched(self):
        """A guarda vale só para o FUNDO do polígono.

        Se ela apagasse `fill_argb` em geral, o ponto perderia a cor (a cor de um
        marcador É o seu preenchimento) e passaria a herdar o contorno cinza.
        """
        from tairu_sync.push import _feature_symbol_argbs

        point = QgsVectorLayer('Point?crs=EPSG:4326&field=id:integer', 'pt', 'memory')
        feature = QgsFeature(point.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))
        point.dataProvider().addFeatures([feature])
        QgsProject.instance().addMapLayer(point, False)

        fg_argb, bg_argb = _feature_symbol_argbs(point, next(point.getFeatures()), 'point')
        self.assertIsNotNone(fg_argb)
        self.assertIsNone(bg_argb)


class TestRecordGroup(unittest.TestCase):

    def test_same_name_always_yields_the_same_group_id(self):
        from tairu_sync.push import record_group_id

        first = record_group_id('m1', 'uid1', 'Curvas de nível')
        self.assertEqual(first, record_group_id('m1', 'uid1', '  curvas   DE Nível '))
        self.assertNotEqual(first, record_group_id('m1', 'uid1', 'Outro grupo'))
        self.assertNotEqual(first, record_group_id('m2', 'uid1', 'Curvas de nível'))
        # O uid entra na semente: o documento é sempre um que ESTE usuário criou,
        # e as regras só deixam atualizar o grupo de outra pessoa se você for
        # dono/administrador da expedição.
        self.assertNotEqual(first, record_group_id('m1', 'uid2', 'Curvas de nível'))

    def test_group_reaches_only_what_the_push_really_writes(self):
        """O grupo alcanca o que vai ser gravado, e nada alem disso.

        O modelo antigo reunia "tudo": promovia inalterado a update so para entrar no
        grupo — e a previa chega a esconder esses itens, entao o usuario via "1 novo" e
        mexia em dezenas de registros.
        """
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import (
            PushItem, PushPlan, apply_group_to_plan, group_candidate_count,
        )

        plan = PushPlan(map_id='m1', items=[
            PushItem('new', TairuRecord(record_id='a')),
            PushItem('update', TairuRecord(record_id='b'), changed_fields=['nome']),
            PushItem('unchanged', TairuRecord(record_id='c')),
            PushItem('forbidden', TairuRecord(record_id='d')),
            PushItem('new', TairuRecord(record_id='e'), send=False),
        ])
        self.assertEqual(group_candidate_count(plan), 2)
        self.assertEqual(apply_group_to_plan(plan, 'g1'), 2)

        self.assertEqual([i.action for i in plan.items],
                         ['new', 'update', 'unchanged', 'forbidden', 'new'])
        self.assertEqual([i.record.group_id for i in plan.items],
                         ['g1', 'g1', '', '', ''])
        self.assertIn('groupId', plan.items[1].changed_fields)
        self.assertEqual([i.record.record_id for i in plan.writable_items()], ['a', 'b'])

    def test_group_never_moves_a_record_the_app_already_grouped(self):
        """Enviar uma camada-pasta nao pode arrastar a organizacao do app para o grupo novo.

        Era o caso que "acaba com a organizacao do usuario": a camada vem da arvore de
        pastas, cada registro ja carrega o groupId dele, e reunir movia todos.
        """
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import (
            PushItem, PushPlan, apply_group_to_plan, group_candidate_count,
        )

        plan = PushPlan(map_id='m1', items=[
            PushItem('update', TairuRecord(record_id='a', group_id='pasta-do-app'),
                     changed_fields=['nome']),
            PushItem('new', TairuRecord(record_id='b', group_id='pasta-do-app')),
            PushItem('new', TairuRecord(record_id='c')),
        ])
        self.assertEqual(group_candidate_count(plan), 1)
        self.assertEqual(apply_group_to_plan(plan, 'g1'), 1)

        self.assertEqual([i.record.group_id for i in plan.items],
                         ['pasta-do-app', 'pasta-do-app', 'g1'])
        self.assertNotIn('groupId', plan.items[0].changed_fields)

    def test_unsent_feature_is_not_stamped_back_into_the_layer(self):
        """Carimbar recordId/tairuSyncHash em item desmarcado corrompe o proximo envio.

        A feicao passaria a parecer um registro ja existente e o envio seguinte
        viraria um 'update' de documento inexistente — o lote inteiro falha.
        """
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import (
            PushItem, PushPlan, _write_back_records_to_source_layer,
        )

        plan = PushPlan(map_id='m1', items=[
            PushItem('new', TairuRecord(record_id='a'), feature_id=1),
            PushItem('new', TairuRecord(record_id='b'), feature_id=2, send=False),
        ])
        touched = []

        class _Fields:
            def indexOf(self, name):
                return 0 if name == 'recordId' else -1

        class _Layer:
            def fields(self):
                return _Fields()

            def isEditable(self):
                return False

            def setCustomProperty(self, *_args):
                pass

            def triggerRepaint(self):
                pass

            def dataProvider(self):
                class _Provider:
                    def changeAttributeValues(self, changes):
                        touched.extend(sorted(changes))
                return _Provider()

        _write_back_records_to_source_layer(plan, _Layer())
        self.assertEqual(touched, [1])

    def test_layer_that_cannot_store_record_ids_keeps_them_in_the_project(self):
        """Camada somente-leitura (KML): sem esta reserva, cada envio duplica tudo.

        O provedor engole a gravacao (devolve False / loga "Invalid index" e segue),
        entao a feicao volta sem recordId e o envio seguinte a manda como registro
        NOVO — foi assim que um poligono KML virou varias copias empilhadas no app.
        """
        from tairu_firebase.models import TairuRecord
        from tairu_sync.push import (
            PushItem, PushPlan, _baseline_hash, _write_back_records_to_source_layer,
        )
        from tairu_sync.record_convert import FEATURE_IDS_PROPERTY

        class _Fields:
            def indexOf(self, name):
                return 0 if name == 'recordId' else -1

        class _Feature:
            def __init__(self, value):
                self._value = value

            def isValid(self):
                return True

            def fields(self):
                return _Fields()

            def attribute(self, _idx):
                return self._value

        class _Layer:
            def __init__(self, stored):
                self.stored = stored
                self.properties = {}

            def fields(self):
                return _Fields()

            def isEditable(self):
                return False

            def getFeature(self, _fid):
                return _Feature(self.stored)

            def customProperty(self, key, default=''):
                return self.properties.get(key, default)

            def setCustomProperty(self, key, value):
                self.properties[key] = value

            def triggerRepaint(self):
                pass

            def dataProvider(self):
                class _Provider:
                    def changeAttributeValues(self, _changes):
                        return False
                return _Provider()

        def _plan():
            return PushPlan(map_id='m1', items=[
                PushItem('new', TairuRecord(record_id='a', nome='X'), feature_id=1),
            ])

        swallowed = _Layer(stored='')
        self.assertFalse(_write_back_records_to_source_layer(_plan(), swallowed))
        stored = json.loads(swallowed.properties[FEATURE_IDS_PROPERTY])
        self.assertEqual(stored['1']['id'], 'a')
        self.assertTrue(stored['1']['hash'])
        # ... e o proximo envio reconhece a feicao pelo hash guardado (nao e "novo").
        self.assertEqual(_baseline_hash(_Feature(None), stored['1']), stored['1']['hash'])

        # Segundo envio na mesma camada: nem tenta gravar (fim do CRITICAL do OGR)
        # e a reserva continua valendo.
        swallowed.dataProvider = lambda: self.fail('nao deveria tentar gravar de novo')
        # ... e o aviso nao se repete a cada envio (a reserva ja existe).
        self.assertTrue(_write_back_records_to_source_layer(_plan(), swallowed))
        self.assertEqual(
            json.loads(swallowed.properties[FEATURE_IDS_PROPERTY])['1']['id'], 'a')

        # Camada que guarda de verdade nao suja o projeto.
        real = _Layer(stored='a')
        self.assertTrue(_write_back_records_to_source_layer(_plan(), real))
        self.assertNotIn(FEATURE_IDS_PROPERTY, real.properties)

    def test_group_id_reaches_the_document_fields(self):
        from tairu_firebase.models import TairuRecord

        # groupId e escrito SEMPRE, inclusive vazio: a gravacao do plugin e uma patch com
        # mascara, e sem a chave no corpo nao existiria forma de TIRAR um registro de um
        # grupo pelo QGIS. Quem decide se o campo entra na mascara e _update_fields_for.
        self.assertEqual(TairuRecord(record_id='a').to_fields()['groupId'], '')
        self.assertEqual(
            TairuRecord(record_id='a', group_id='g1').to_fields()['groupId'], 'g1')


if __name__ == '__main__':
    unittest.main()
