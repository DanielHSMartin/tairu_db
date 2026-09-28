# -*- coding: utf-8 -*-

"""O circulo recebido do app e desenhado no QGIS no raio GEODESICO, nao numa elipse
esticada — e sem travar o QGIS.

O registro-circulo e Point + circleRadius (m); o QGIS o desenha por um gerador de
geometria (_CIRCLE_EXPR). O antigo buffer($geometry, r / (111320 cos lat)) usava o
grau de LONGITUDE nos dois eixos: a borda N-S saia r/cos(lat) (1 km a 23,5 S -> +85 m,
a 55 S -> +735 m). Referencia: pyproj.Geod WGS84, a mesma geodesia do app (Vincenty).
Avalia a expressao tirada dos simbolos que o plugin realmente monta.

Precisa do Python do QGIS (qgis.core, pyproj); pulado em outros interpretadores.
"""

import os
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import (
        QgsApplication, QgsExpression, QgsExpressionContext, QgsFeature, QgsGeometry,
        QgsPointXY, QgsVectorLayer)
    from qgis.PyQt.QtGui import QColor
    from pyproj import Geod
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None
_LON = -47.9
# O gerador que o plugin 2.0.x gravou nas camadas ja recebidas (receita 9).
_OLD_BUFFER = ('buffer($geometry, coalesce("circleRadius", 0) / '
               '(111320.0 * cos(radians(y(centroid($geometry))))))')


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], False)
    _APP.initQgis()


def _generator_exprs():
    """A expressao dos dois caminhos que desenham o circulo (estilo da camada e o
    simbolo por registro), lida do simbolo montado, nao da constante."""
    from tairu_sync.record_convert import _record_symbol, style_layer

    layer = QgsVectorLayer(
        'Point?crs=EPSG:4326&field=circleRadius:double&field=geometryColor:string'
        '&field=geometryBackgroundColor:string', 'c', 'memory')
    style_layer(layer, 'circle')
    symbols = [layer.renderer().symbol(), _record_symbol('circle', QColor('#FFFF0000'), None)]
    exprs = [sl.geometryExpression() for s in symbols for sl in s.symbolLayers()
             if sl.layerType() == 'GeometryGenerator']
    return layer, exprs


def _records_layer(count):
    """Camada de circulos como o pull a deixa: um recordId por registro."""
    layer = QgsVectorLayer(
        'Point?crs=EPSG:4326&field=recordId:string&field=nome:string&field=circleRadius:double'
        '&field=geometryColor:string&field=geometryBackgroundColor:string', 'c', 'memory')
    feats = []
    for i in range(count):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(_LON + i * 0.01, -34.6)))
        feat.setAttributes(['rec%d' % i, 'Circulo %d' % i, 1000.0, '#FFFF0000', '#4DFF0000'])
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    return layer


def _legend_exprs(layer):
    return {sl.geometryExpression() for cat in layer.renderer().categories()
            for sl in cat.symbol().symbolLayers() if sl.layerType() == 'GeometryGenerator'}


class CircleGeodesicTest(unittest.TestCase):

    def test_vertices_at_geodesic_radius(self):
        geod = Geod(ellps='WGS84')
        layer, exprs = _generator_exprs()
        self.assertEqual(len(exprs), 2)
        for text in exprs:
            expr = QgsExpression(text)
            self.assertFalse(expr.hasParserError(), expr.parserErrorString())
            for lat in (0.0, -23.5, -45.0, -55.0):
                for radius in (1000.0, 10000.0):
                    feat = QgsFeature(layer.fields())
                    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(_LON, lat)))
                    feat.setAttribute('circleRadius', radius)
                    ctx = QgsExpressionContext()
                    ctx.setFeature(feat)
                    geom = expr.evaluate(ctx)
                    self.assertFalse(expr.hasEvalError(), expr.evalErrorString())
                    ring = geom.asPolygon()[0]
                    # vertices e meio de cada lado (erro de corda) no raio, +-0,5%
                    pts = [(p.x(), p.y()) for p in ring]
                    pts += [((a.x() + b.x()) / 2, (a.y() + b.y()) / 2) for a, b in zip(ring, ring[1:])]
                    for x, y in pts:
                        dist = geod.inv(_LON, lat, x, y)[2]
                        self.assertAlmostEqual(
                            dist / radius, 1.0, delta=0.005,
                            msg='lat %s r %s: ponto a %.1f m' % (lat, radius, dist))

    def test_no_radius_draws_nothing(self):
        layer, exprs = _generator_exprs()
        for value in (None, 0.0, -5.0):
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(_LON, -23.5)))
            feat.setAttribute('circleRadius', value)
            ctx = QgsExpressionContext()
            ctx.setFeature(feat)
            geom = QgsExpression(exprs[0]).evaluate(ctx)
            self.assertTrue(geom is None or QgsGeometry(geom).isEmpty())

    def test_legend_build_is_cheap(self):
        """Cada simbolo categorizado (e cada clone do renderizador, a cada desenho)
        reanalisa a expressao. Uma versao aninhada levava 138 ms por analise: 21 s para
        categorizar 50 circulos na thread principal do pull. Hoje sao ~0,02 s."""
        from tairu_sync.record_convert import apply_record_legend

        layer = _records_layer(50)
        start = time.perf_counter()
        self.assertTrue(apply_record_legend(layer, 'circle'))
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_existing_legend_gets_new_circle(self):
        """Camada categorizada por um plugin anterior tem a elipse antiga gravada; sem
        subir SYMBOL_RECIPE a assinatura bate e o proximo pull a mantem para sempre."""
        import tairu_sync.record_convert as rc

        layer = _records_layer(3)
        current = (rc._CIRCLE_EXPR, rc.SYMBOL_RECIPE)
        rc._CIRCLE_EXPR, rc.SYMBOL_RECIPE = _OLD_BUFFER, 9
        try:
            self.assertTrue(rc.apply_record_legend(layer, 'circle'))
        finally:
            rc._CIRCLE_EXPR, rc.SYMBOL_RECIPE = current
        self.assertEqual(_legend_exprs(layer), {_OLD_BUFFER})
        self.assertTrue(rc.apply_record_legend(layer, 'circle'))
        self.assertEqual(_legend_exprs(layer), {rc._CIRCLE_EXPR})


if __name__ == '__main__':
    unittest.main()
