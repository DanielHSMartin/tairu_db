# -*- coding: utf-8 -*-

"""Self-check for TairuDBWriter atomic publish (.part -> final) and no-VACUUM finalize.

A killed/canceled generation must never leave a half-written .tairudb at the final
path for the next run to merge into, and finalize() must publish the file atomically.
Uses real sqlite3; QGIS is stubbed (the writer only imports QByteArray/QgsRectangle at
module load, not for this path)."""

import importlib.abc
import importlib.machinery
import os
import sys
import tempfile
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

        def __mro_entries__(self, bases):
            return (object,)

    class _AnyModule(types.ModuleType):
        __path__ = []

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

from tairu_core.tairudb_writer import TairuDBWriter  # noqa: E402


class TestAtomicPublish(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.final = os.path.join(self._dir, 'out.tairudb')
        self.part = self.final + '.part'

    def test_writes_to_part_then_publishes_on_finalize(self):
        w = TairuDBWriter(self.final)
        self.assertTrue(w.create())
        # Mid-write: only the .part exists, never the final path.
        self.assertTrue(os.path.exists(self.part))
        self.assertFalse(os.path.exists(self.final))
        w.setMetadataValue('name', 'x')
        self.assertTrue(w.finalize())
        # After finalize: final published, .part gone.
        self.assertTrue(os.path.exists(self.final))
        self.assertFalse(os.path.exists(self.part))

    def test_discard_removes_partial_and_never_publishes(self):
        w = TairuDBWriter(self.final)
        self.assertTrue(w.create())
        w.setMetadataValue('name', 'x')
        w.discard()
        self.assertFalse(os.path.exists(self.part))
        self.assertFalse(os.path.exists(self.final))

    def test_create_drops_stale_partial_from_a_killed_run(self):
        with open(self.part, 'wb') as f:
            f.write(b'corrupt-leftover')
        w = TairuDBWriter(self.final)
        self.assertTrue(w.create())  # stale .part removed, fresh db created
        w.setMetadataValue('name', 'x')
        self.assertTrue(w.finalize())
        self.assertTrue(os.path.exists(self.final))
        # The published file is a real sqlite db, not the corrupt leftover.
        with open(self.final, 'rb') as f:
            self.assertEqual(f.read(16), b'SQLite format 3\x00')


if __name__ == '__main__':
    unittest.main()
