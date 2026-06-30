# -*- coding: utf-8 -*-

"""Self-check for the styleJson producer helpers in tairu_sync.push.

Covers the non-trivial pure logic (no QGIS needed): the omit-when-plain rule in
build_feature_style_json, the Qt pen-style -> stroke-name mapping, and the
scale -> zoom conversion. QGIS and the heavy sibling imports are stubbed (mirrors
test_firestore_cache's approach) so the module imports standalone.
"""

import json
import math
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _dummy(*args, **kwargs):
    return None


def _install_stubs():
    class _Anything:
        def __getattr__(self, name):
            return _Anything()

        def __call__(self, *a, **k):
            return _Anything()

    def _mod_getattr(_name):
        return _Anything()

    for name in ('qgis', 'qgis.core', 'qgis.PyQt', 'qgis.PyQt.QtGui', 'qgis.PyQt.QtCore'):
        mod = types.ModuleType(name)
        mod.__getattr__ = _mod_getattr
        sys.modules[name] = mod

    def _stub(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod

    _stub('tairu_core')
    _stub('tairu_core.firestore_cache', FirestoreCache=type('FirestoreCache', (), {}))
    _stub('tairu_firebase')
    _stub('tairu_firebase.models',
          TairuRecord=type('TairuRecord', (), {}),
          SITUATIONS_BY_TYPE={}, now_millis=_dummy,
          points_to_json=_dummy, bounds_json_from_points=_dummy)
    _stub('tairu_sync.record_convert',
          hex_to_argb=_dummy, configure_record_layer_fields=_dummy,
          ensure_record_layer_fields=_dummy, layer_sync_snapshot=_dummy,
          normalized_geometry_points=_dummy, record_to_attribute_map=_dummy,
          resolved_background_argb=_dummy, resolved_color_argb=_dummy,
          sync_record_hash=_dummy, SYNC_HASH_FIELD='tairuSyncHash',
          SYNC_LAST_MODIFIED_FIELD='tairuSyncLastModified')
    _stub('tairu_sync.tasks', run_task=_dummy)


_install_stubs()

from tairu_sync import push  # noqa: E402


class TestBuildFeatureStyleJson(unittest.TestCase):
    def test_plain_solid_feature_emits_no_style(self):
        # Only a foreground color (already in the color column) -> nothing to carry.
        self.assertIsNone(
            push.build_feature_style_json(0xFF112233, None, 'line', 'solid', None))
        self.assertIsNone(
            push.build_feature_style_json(0xFF112233, None, 'point', None, None))

    def test_polygon_fill_is_carried(self):
        out = push.build_feature_style_json(0xFF112233, 0x80445566, 'polygon', 'solid', None)
        style = json.loads(out)
        self.assertEqual(style['v'], 1)
        self.assertEqual(style['base']['bgColor'], 0x80445566)
        self.assertEqual(style['base']['color'], 0xFF112233)
        self.assertNotIn('stroke', style['base'])

    def test_fill_ignored_for_non_area_geometry(self):
        # A bgColor on a line/point is meaningless -> not carried, and with nothing
        # else to carry the whole style is omitted.
        self.assertIsNone(
            push.build_feature_style_json(0xFF112233, 0x80445566, 'line', 'solid', None))

    def test_dash_pattern_is_carried(self):
        out = push.build_feature_style_json(0xFF112233, None, 'line', 'dashed', None)
        style = json.loads(out)
        self.assertEqual(style['base']['stroke'], 'dashed')

    def test_label_is_carried_even_for_plain_symbol(self):
        label = {'field': 'classe', 'show': True, 'color': 0xFF000000, 'minZoom': 12.0}
        out = push.build_feature_style_json(0xFF112233, None, 'point', None, label)
        style = json.loads(out)
        self.assertEqual(style['label']['field'], 'classe')

    def test_argb_is_masked_to_unsigned(self):
        # A signed (Python) int rounds-trips as the unsigned 32-bit ARGB the app uses.
        out = push.build_feature_style_json(-1, None, 'line', 'dotted', None)
        style = json.loads(out)
        self.assertEqual(style['base']['color'], 0xFFFFFFFF)


class TestStrokeNameForPenStyle(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(push._stroke_name_for_pen_style(1), 'solid')   # SolidLine
        self.assertEqual(push._stroke_name_for_pen_style(2), 'dashed')  # DashLine
        self.assertEqual(push._stroke_name_for_pen_style(3), 'dotted')  # DotLine
        self.assertEqual(push._stroke_name_for_pen_style(4), 'dashed')  # DashDotLine
        self.assertEqual(push._stroke_name_for_pen_style(0), 'solid')   # NoPen
        self.assertIsNone(push._stroke_name_for_pen_style(None))


class TestScaleToZoom(unittest.TestCase):
    def test_reference_scale_is_zoom_zero(self):
        self.assertAlmostEqual(push._scale_to_zoom(push._WEBMERC_SCALE_Z0), 0.0, places=2)

    def test_halving_scale_adds_one_zoom(self):
        z = push._scale_to_zoom(push._WEBMERC_SCALE_Z0 / 2.0)
        self.assertAlmostEqual(z, 1.0, places=2)

    def test_invalid_scale_is_none(self):
        self.assertIsNone(push._scale_to_zoom(0))
        self.assertIsNone(push._scale_to_zoom(-5))
        self.assertIsNone(push._scale_to_zoom('not a number'))


if __name__ == '__main__':
    unittest.main()
