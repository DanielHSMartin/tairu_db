# -*- coding: utf-8 -*-

"""Self-check for the geometry-WKB push wire format.

The plugin sends holed/multipart geometry to /records as a Firestore bytesValue
(base64) under `geometryWkb`; the app reads it back as a Blob (Record.geometryWkb)
and renders holes. If the encoding is wrong the geometry silently won't render, so
this pins the bytes -> bytesValue path and the to_fields emission. QGIS is stubbed
(the encoder and the model are pure); _geometry_is_lossy needs real QGIS and is
exercised on-device.
"""

import base64
import importlib.abc
import importlib.machinery
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Any:
    def __getattr__(self, n):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __mro_entries__(self, bases):
        # Lets stubbed symbols (QObject, QgsTask) be used as base classes.
        return (object,)


def _stub_qgis():
    """Resolve any `import qgis...` / `from qgis... import X` to permissive stubs, so
    pure-Python code under test (models, push.build_writes) imports without a QGIS
    runtime. A meta-path finder covers every submodule (qgis.core, qgis.PyQt.QtWidgets,
    ...) instead of enumerating them."""

    class _AnyModule(types.ModuleType):
        __path__ = []  # marks it a package so submodule imports proceed

        def __getattr__(self, name):
            return _Any()

    class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path, target=None):
            if fullname == 'qgis' or fullname.startswith('qgis.'):
                return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
            return None

        def create_module(self, spec):
            return _AnyModule(spec.name)

        def exec_module(self, module):
            pass

    sys.meta_path.insert(0, _Finder())


_stub_qgis()

from tairu_firebase.firestore import to_value  # noqa: E402
from tairu_firebase.models import TairuRecord  # noqa: E402
from tairu_sync.push import (  # noqa: E402
    build_writes, PushPlan, PushItem, _ALL_UPDATE_FIELDS,
)
from tairu_sync.record_convert import _resolved_fg, _resolved_bg  # noqa: E402


class TestBytesEncoding(unittest.TestCase):
    def test_bytes_become_base64_bytesValue(self):
        wkb = bytes([1, 3, 0, 0, 0, 7, 42, 255])
        v = to_value(wkb)
        self.assertEqual(list(v.keys()), ['bytesValue'])
        self.assertEqual(v['bytesValue'], base64.b64encode(wkb).decode('ascii'))
        # bytearray encodes identically.
        self.assertEqual(to_value(bytearray(wkb)), v)

    def test_scalar_encodings_unaffected(self):
        # Regression guard: bool before int, ints as integerValue strings.
        self.assertEqual(to_value(None), {'nullValue': None})
        self.assertEqual(to_value(True), {'booleanValue': True})
        self.assertEqual(to_value(7), {'integerValue': '7'})
        self.assertEqual(to_value('x'), {'stringValue': 'x'})


class TestToFieldsGeometryWkb(unittest.TestCase):
    def test_emitted_only_when_present(self):
        rec = TairuRecord(record_id='r1', geometry_wkb=b'\x01\x02\x03')
        fields = rec.to_fields()
        self.assertEqual(fields['geometryWkb'], b'\x01\x02\x03')

    def test_absent_when_none(self):
        rec = TairuRecord(record_id='r1')
        self.assertNotIn('geometryWkb', rec.to_fields())


class TestFromFieldsGeometryWkb(unittest.TestCase):
    """Pull must decode the geometryWkb Blob (base64 string over REST) to raw WKB."""

    def test_decodes_base64_string_to_bytes(self):
        wkb = bytes([1, 3, 0, 0, 0, 9, 8, 7])
        rec = TairuRecord.from_fields('r1', {
            'geometryType': 'polygon',
            'geometryWkb': base64.b64encode(wkb).decode('ascii'),
        })
        self.assertEqual(rec.geometry_wkb, wkb)
        # Round-trips back out for push unchanged.
        self.assertEqual(rec.to_fields()['geometryWkb'], wkb)

    def test_accepts_raw_bytes_and_ignores_garbage(self):
        wkb = b'\x01\x02\x03'
        self.assertEqual(TairuRecord.from_fields('r', {'geometryWkb': wkb}).geometry_wkb, wkb)
        self.assertIsNone(TairuRecord.from_fields('r', {'geometryWkb': None}).geometry_wkb)
        # A non-base64 string decodes to None rather than raising.
        self.assertIsNone(TairuRecord.from_fields('r', {'geometryWkb': '!!!not64!!!'}).geometry_wkb)


class _FakeFs:
    """Captures the fields/mask push would send, without touching Firestore."""

    def build_update_write(self, path, fields, mask):
        return {'path': path, 'fields': dict(fields), 'mask': list(mask)}

    def build_create_write(self, path, fields):
        return {'path': path, 'fields': dict(fields)}


class TestBuildWritesGeometryGuard(unittest.TestCase):
    """build_writes must never NULL cloud geometry the plugin can't see. The plugin
    ignores geometryWkb it didn't derive, so an attribute-only edit of a WKB-only /
    coerced record must not clear the cloud blob (the historical data-loss bug)."""

    def _write_for(self, rec):
        plan = PushPlan(map_id='m1')
        plan.items.append(PushItem('update', rec, feature_id=1,
                                   changed_fields=list(_ALL_UPDATE_FIELDS)))
        return build_writes(_FakeFs(), plan, 'uid1')[0]

    def test_no_geometry_candidate_never_touches_cloud_geometry(self):
        # Pulled WKB-only record that landed geometry-less: no points, no wkb.
        rec = TairuRecord(record_id='r1', geometry_type='none')
        w = self._write_for(rec)
        for key in ('geometryWkb', 'geometryType', 'geometryPoints',
                    'geometryBounds', 'circleRadius'):
            self.assertNotIn(key, w['mask'], f'{key} must be dropped from the mask')
            self.assertNotIn(key, w['fields'])
        self.assertIn('lastModified', w['mask'])
        self.assertIn('isDeleted', w['mask'])

    def test_geometrywkb_never_nulled_when_candidate_has_points_only(self):
        # Simple/coerced feature: has points but no derived wkb -> keep points,
        # but NEVER null the cloud geometryWkb.
        rec = TairuRecord(record_id='r1', geometry_type='polygon',
                          geometry_points_json='[{"la":1.0,"lo":2.0,"ts":0}]')
        w = self._write_for(rec)
        self.assertNotIn('geometryWkb', w['mask'])
        self.assertIn('geometryPoints', w['mask'])
        self.assertIn('geometryType', w['mask'])

    def test_real_wkb_is_written_not_nulled(self):
        rec = TairuRecord(record_id='r1', geometry_type='polygon',
                          geometry_points_json='[{"la":1.0,"lo":2.0,"ts":0}]',
                          geometry_wkb=b'\x01\x03\x00\x00')
        w = self._write_for(rec)
        self.assertIn('geometryWkb', w['mask'])
        self.assertEqual(w['fields']['geometryWkb'], b'\x01\x03\x00\x00')


class TestBuildWritesLastModifiedBy(unittest.TestCase):
    """Every push write must credit the pushing user in `lastModifiedBy` (the app
    reads it as Record.lastEditorId). An update is a masked patch, so omitting the
    field leaves the previous app editor credited for an edit the plugin made."""

    def _writes_for(self, item):
        plan = PushPlan(map_id='m1')
        plan.items.append(item)
        return build_writes(_FakeFs(), plan, 'uid1')[0]

    def test_new_record_stamps_pusher(self):
        rec = TairuRecord(record_id='r1', tipo_registro='local', geometry_type='none')
        w = self._writes_for(PushItem('new', rec, feature_id=1))
        self.assertEqual(w['fields']['lastModifiedBy'], 'uid1')

    def test_update_stamps_pusher_in_fields_and_mask(self):
        rec = TairuRecord(record_id='r1', geometry_type='polygon',
                          geometry_points_json='[{"la":1.0,"lo":2.0,"ts":0}]')
        w = self._writes_for(PushItem('update', rec, feature_id=1,
                                      changed_fields=list(_ALL_UPDATE_FIELDS)))
        self.assertIn('lastModifiedBy', w['mask'])
        # Not in to_fields(), so the mask None-fill would blank it if set too early.
        self.assertEqual(w['fields']['lastModifiedBy'], 'uid1')

    def test_delete_tombstone_stamps_pusher(self):
        w = self._writes_for(PushItem('delete', TairuRecord(record_id='r1')))
        self.assertIn('lastModifiedBy', w['mask'])
        self.assertEqual(w['fields']['lastModifiedBy'], 'uid1')


class TestStyleFirstColorResolution(unittest.TestCase):
    """QGIS must resolve colour the way the app renders it: styleJson base first, then
    the flat geometryColorValue shadow (which can be stale/divergent). Regression for
    the observed bug where the plugin rendered the stale shadow instead of styleJson."""

    def test_stylejson_base_wins_over_flat_shadow(self):
        # style: color #FFFF2323 (red), bgColor #770000FF (47% blue) — the truth.
        # shadow: #FF232323 (gray) / #FFFF9E17 (opaque amber) — divergent/stale.
        rec = TairuRecord(
            record_id='r', tipo_registro='local', geometry_type='polygon',
            geometry_points_json='[{"la":1.0,"lo":2.0,"ts":0}]',
            geometry_color_value=0xFF232323,
            geometry_background_color_value=0xFFFF9E17,
            style='{"v":1,"base":{"color":4294910755,"bgColor":1996488959}}')
        self.assertEqual(_resolved_fg(rec), '#FFFF2323')
        self.assertEqual(_resolved_bg(rec), '#770000FF')

    def test_falls_back_to_flat_shadow_without_stylejson(self):
        rec = TairuRecord(
            record_id='r', tipo_registro='local', geometry_type='polygon',
            geometry_points_json='[{"la":1.0,"lo":2.0,"ts":0}]',
            geometry_color_value=0xFF2196F3,
            geometry_background_color_value=0xFF2196F3)
        self.assertEqual(_resolved_fg(rec), '#FF2196F3')
        self.assertEqual(_resolved_bg(rec), '#FF2196F3')

    def test_type_default_when_nothing_set(self):
        rec = TairuRecord(record_id='r', tipo_registro='local', geometry_type='none')
        self.assertEqual(_resolved_fg(rec), '#FF4CAF50')  # TYPE_COLORS['local']

    def test_malformed_stylejson_ignored(self):
        rec = TairuRecord(record_id='r', tipo_registro='local',
                          geometry_color_value=0xFF2196F3, style='{not json')
        self.assertEqual(_resolved_fg(rec), '#FF2196F3')


if __name__ == '__main__':
    unittest.main()
