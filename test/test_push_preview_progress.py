# -*- coding: utf-8 -*-

"""Self-check for the frozen "Enviar camadas vetoriais" window.

The preview scan runs on the GUI thread and costs ~0.3 ms per feature (measured on
QGIS 3.40 LTR; more with many vertices), so on a real layer it used to block before the
dialog's first paint: a blank window titled "(Não está respondendo)". build_push_plan
must therefore hand control back regularly through `progress`, and let the caller abort
by raising from it.

Also pins the per-layer render session: hoisting renderer.startRender() out of the
per-feature loop must not change a single pushed value.

Needs the QGIS Python (qgis.core); skipped elsewhere.
"""

import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import (QgsApplication, QgsVectorLayer, QgsFeature, QgsGeometry,
                           QgsPointXY, QgsProject)
except ImportError:  # pragma: no cover - no QGIS on this interpreter
    QgsApplication = None

_APP = None
_FEATURES = 1200


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], False)
    _APP.initQgis()


class _FakeMap:
    map_id = 'map1'
    nome = 'OP'

    def role_for(self, _uid):
        return 'owner'


def _layer(name='camada'):
    layer = QgsVectorLayer('LineString?crs=EPSG:31983&field=ELEV:double', name, 'memory')
    provider = layer.dataProvider()
    features = []
    for i in range(_FEATURES):
        feature = QgsFeature(layer.fields())
        feature.setAttribute('ELEV', float(i % 40) * 5.0)
        feature.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(200000 + i + j, 8000000 + j) for j in range(12)]))
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer, False)
    return layer


_MAPPING = {'nome_field': None, 'descricao_field': None, 'tipo': 'curvaNivel',
            'sub_tipo': 'curvaNormal', 'situation': 'Ativo'}


class TestPreviewYieldsToTheEventLoop(unittest.TestCase):

    def test_progress_is_reported_during_the_scan(self):
        from tairu_sync.push import build_push_plan, _PROGRESS_EVERY

        seen = []
        plan = build_push_plan(
            _layer(), _MAPPING, _FakeMap(), 'uid',
            progress=lambda done, total, phase: seen.append((done, total, phase)))

        self.assertEqual(len(plan.items), _FEATURES)
        # Once up front (so the window paints before the two pre-scans) then per chunk.
        self.assertEqual(seen[0], (0, _FEATURES, 'Calculando prévia'))
        self.assertTrue(all(total == _FEATURES for _done, total, _phase in seen))
        # BOTH passes report: the classification loop is where sync_record_hash runs, and
        # a preview that goes quiet there is a preview that looks frozen again.
        phases = {phase for _done, _total, phase in seen}
        self.assertEqual(phases, {'Calculando prévia', 'Comparando com o Tairu'})
        for phase in phases:
            chunks = [d for d, _t, p in seen if p == phase and d]
            self.assertGreaterEqual(len(chunks), _FEATURES // _PROGRESS_EVERY, phase)

    def test_raising_from_progress_aborts_the_scan(self):
        from tairu_sync.push import build_push_plan

        class Abort(Exception):
            pass

        calls = []

        def progress(done, _total, _phase):
            calls.append(done)
            if done:
                raise Abort()

        with self.assertRaises(Abort):
            build_push_plan(_layer(), _MAPPING, _FakeMap(), 'uid', progress=progress)
        # Aborted on the first chunk, not after scanning everything.
        self.assertLessEqual(len(calls), 2)


class TestRenderSessionIsTransparent(unittest.TestCase):

    def test_hoisted_renderer_produces_identical_records(self):
        from qgis.core import QgsCoordinateTransform, QgsCoordinateReferenceSystem
        from tairu_sync import push

        layer = _layer('equivalencia')
        transform = QgsCoordinateTransform(
            layer.crs(), QgsCoordinateReferenceSystem('EPSG:4326'),
            QgsProject.instance().transformContext())
        label_cfg = push.layer_label_config(layer)

        def scan():
            # Only the renderer-derived values: those are what hoisting startRender()
            # could change. Geometry is untouched by the session, and the records carry
            # a wall-clock ts, so comparing whole records would compare clocks.
            out = []
            for index, feature in enumerate(layer.getFeatures(), start=1):
                record, _warning = push.feature_to_record(
                    feature, layer, _MAPPING, 'uid', transform, index, None, label_cfg)
                out.append((record.geometry_color_value,
                            record.geometry_background_color_value,
                            record.style, record.geometry_size))
            return out

        without = scan()
        with push.layer_render_session(layer):
            within = scan()
        mismatches = [(i, a, b) for i, (a, b) in enumerate(zip(without, within)) if a != b]
        # Report the first mismatch only: assertEqual on two 1200-item lists spends
        # minutes in difflib before it can print anything.
        self.assertEqual(mismatches[:1], [], 'a sessão de render mudou o resultado')
        self.assertEqual(len(without), _FEATURES)
        self.assertEqual(push._render_sessions, [])  # session always closed


if __name__ == '__main__':
    unittest.main()
