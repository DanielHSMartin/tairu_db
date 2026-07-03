# -*- coding: utf-8 -*-

"""Self-check for the incremental-pull watermark. A full pull whose rows all lack a
serverTimestamp (a fully-legacy map) must save a POSITIVE watermark from the fallback,
otherwise last_full_sync stays 0 and every open re-reads the whole collection — the
unnecessary-Firestore-reads cost this cache exists to prevent."""

import importlib.abc
import importlib.machinery
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

from tairu_sync.pull import _rows_server_watermark  # noqa: E402


class TestWatermark(unittest.TestCase):
    def test_highest_server_timestamp(self):
        rows = [('a', {'serverTimestamp': 100}), ('b', {'serverTimestamp': 300}),
                ('c', {'serverTimestamp': 200})]
        self.assertEqual(_rows_server_watermark(rows), 300)

    def test_legacy_rows_without_servertimestamp_use_fallback(self):
        # THE FIX: rows exist but none has a serverTimestamp -> use the fallback
        # (pull_started_at on a full pull) instead of collapsing to 0.
        rows = [('a', {'nome': 'x'}), ('b', {})]
        self.assertEqual(_rows_server_watermark(rows, empty_fallback_ms=777), 777)

    def test_empty_snapshot_uses_fallback(self):
        self.assertEqual(_rows_server_watermark([], empty_fallback_ms=555), 555)

    def test_incremental_caller_gets_zero_without_fallback(self):
        # The incremental caller passes no fallback and does `... or since_millis`,
        # so 0 here preserves the existing cursor — unchanged behaviour.
        self.assertEqual(_rows_server_watermark([('a', {'nome': 'x'})]), 0)
        self.assertEqual(_rows_server_watermark([]), 0)


if __name__ == '__main__':
    unittest.main()
