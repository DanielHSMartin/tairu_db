# -*- coding: utf-8 -*-
"""
The tile set and the table a .tairudb carries terrain in.

Both halves fail silently if they are wrong: a tile range off by one covers the
wrong ground, and an XYZ/TMS mix-up returns the altitude of somewhere else
without erroring. Neither shows up in a visual review of the exported file.
"""

import importlib.abc
import importlib.machinery
import os
import sqlite3
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Any:
    """Permissive stand-in for any QGIS symbol."""

    def __getattr__(self, n):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __mro_entries__(self, bases):
        return (object,)


def _stub_qgis():
    """Resolve `import qgis...` to permissive stubs — same meta-path finder the
    push/firestore tests use. tairudb_writer imports QByteArray at module level,
    and the two methods under test never touch QGIS at all."""

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

from tairu_core.elevation_tiles import (  # noqa: E402
    ELEVATION_ZOOM, AVG_ELEVATION_TILE_BYTES,
    elevation_tiles_for_extent, estimate_bytes,
)


class _Rect:
    """QgsRectangle stand-in — the tile maths only asks for these four."""

    def __init__(self, xmin, ymin, xmax, ymax):
        self._v = (xmin, ymin, xmax, ymax)

    def isEmpty(self):
        return self._v[0] >= self._v[2] or self._v[1] >= self._v[3]

    def xMinimum(self):
        return self._v[0]

    def yMinimum(self):
        return self._v[1]

    def xMaximum(self):
        return self._v[2]

    def yMaximum(self):
        return self._v[3]


class ElevationTileSetTest(unittest.TestCase):

    def test_zoom_matches_the_app(self):
        # The app decodes at z12 (kElevationZoom). A file written at any other
        # zoom is invisible to it — the lookup is by exact zoom, not by pyramid.
        self.assertEqual(ELEVATION_ZOOM, 12)

    def test_small_extent_still_yields_a_tile(self):
        # A few hundred metres of ground sits inside one 9 km tile; returning
        # nothing here would leave a small map with no terrain at all.
        tiles = elevation_tiles_for_extent(_Rect(-53.64, -26.26, -53.63, -26.25))
        self.assertEqual(len(tiles), 1)

    def test_covers_the_whole_box_including_the_far_edge(self):
        # ~1 degree square in southern Brazil: ~11 x 12 tiles. An off-by-one on
        # the max edge drops the column the user is most likely to walk into.
        tiles = elevation_tiles_for_extent(_Rect(-54.0, -27.0, -53.0, -26.0))
        xs = {x for x, _ in tiles}
        ys = {y for _, y in tiles}
        self.assertGreaterEqual(len(xs), 11)
        self.assertGreaterEqual(len(ys), 11)
        self.assertEqual(len(tiles), len(xs) * len(ys))
        self.assertEqual(len(tiles), len(set(tiles)), 'tiles must be unique')

    def test_y_grows_southward(self):
        # XYZ, top-left origin. Getting this inverted mirrors every altitude
        # across the equator and still returns a plausible number.
        north = elevation_tiles_for_extent(_Rect(-53.64, -10.01, -53.63, -10.0))
        south = elevation_tiles_for_extent(_Rect(-53.64, -20.01, -53.63, -20.0))
        self.assertLess(north[0][1], south[0][1])

    def test_poles_are_clamped_instead_of_exploding(self):
        # Web Mercator has no tile beyond ~85.05; the log/tan of anything past
        # it is not a number, and one NaN would take out the whole range.
        tiles = elevation_tiles_for_extent(_Rect(-10.0, -89.9, -9.0, 89.9))
        self.assertTrue(tiles)
        last = 2 ** ELEVATION_ZOOM - 1
        for x, y in tiles:
            self.assertTrue(0 <= x <= last)
            self.assertTrue(0 <= y <= last)

    def test_empty_extent_yields_nothing(self):
        self.assertEqual(elevation_tiles_for_extent(None), [])
        self.assertEqual(elevation_tiles_for_extent(_Rect(0, 0, 0, 0)), [])

    def test_estimate_is_the_measured_average(self):
        self.assertEqual(estimate_bytes(25), 25 * AVG_ELEVATION_TILE_BYTES)
        # The claim the "always include it" decision rests on: a 30x30 km map
        # gains well under 1 MB.
        tiles = elevation_tiles_for_extent(_Rect(-53.79, -26.40, -53.49, -26.13))
        self.assertLess(estimate_bytes(len(tiles)), 1024 * 1024)


class ElevationTableTest(unittest.TestCase):
    """The writer's SQL, exercised on a real sqlite file (no QGIS needed)."""

    def setUp(self):
        self.path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '_elev_test.sqlite')
        if os.path.exists(self.path):
            os.remove(self.path)
        self.conn = sqlite3.connect(self.path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _writer(self):
        """A TairuDBWriter with its sqlite handles swapped in — the class needs
        QGIS only for the render path, never for these two methods."""
        from tairu_core import tairudb_writer
        writer = tairudb_writer.TairuDBWriter.__new__(
            tairudb_writer.TairuDBWriter)
        writer.conn = self.conn
        writer.cursor = self.conn.cursor()
        return writer

    def test_table_is_not_a_region(self):
        # Every row of `regions` becomes a raster layer the app draws, and a
        # Terrarium PNG drawn on a map is a screenful of pink noise. This has to
        # stay a table of its own.
        w = self._writer()
        self.assertTrue(w.createElevationTable())
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('elevation_tiles', names)
        self.assertNotIn('regions', names)

    def test_columns_say_xyz_not_tms(self):
        # tiles_region_N uses tile_column/tile_row and stores TMS. Different
        # names here are the signal that the convention differs; identical ones
        # would invite a flip nobody would notice, since a wrong tile decodes
        # into a perfectly plausible altitude.
        w = self._writer()
        w.createElevationTable()
        cols = [r[1] for r in self.conn.execute(
            'PRAGMA table_info(elevation_tiles)')]
        self.assertEqual(cols, ['zoom_level', 'tile_x', 'tile_y', 'tile_data'])

    def test_round_trips_bytes_and_replaces_on_rewrite(self):
        w = self._writer()
        w.createElevationTable()
        self.assertTrue(w.saveElevationTile(12, 1517, 2310, b'\x89PNG-first'))
        self.assertTrue(w.saveElevationTile(12, 1517, 2310, b'\x89PNG-second'))
        rows = list(self.conn.execute(
            'SELECT tile_data FROM elevation_tiles '
            'WHERE zoom_level=? AND tile_x=? AND tile_y=?', (12, 1517, 2310)))
        # Re-exporting a map must overwrite, not accumulate a second copy.
        self.assertEqual(len(rows), 1)
        self.assertEqual(bytes(rows[0][0]), b'\x89PNG-second')

    def test_empty_payload_is_refused(self):
        w = self._writer()
        w.createElevationTable()
        self.assertFalse(w.saveElevationTile(12, 1, 1, b''))
        self.assertFalse(w.saveElevationTile(12, 1, 1, None))
        count = list(self.conn.execute(
            'SELECT COUNT(*) FROM elevation_tiles'))[0][0]
        self.assertEqual(count, 0)


if __name__ == '__main__':
    unittest.main()
