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
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _stub_qgis():
    class _Any:
        def __getattr__(self, n):
            return _Any()

        def __call__(self, *a, **k):
            return _Any()

    for name in ('qgis', 'qgis.core', 'qgis.PyQt', 'qgis.PyQt.QtCore',
                 'qgis.PyQt.QtNetwork', 'qgis.PyQt.QtGui'):
        m = types.ModuleType(name)
        m.__getattr__ = lambda _n: _Any()
        sys.modules[name] = m


_stub_qgis()

from tairu_firebase.firestore import to_value  # noqa: E402
from tairu_firebase.models import TairuRecord  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
