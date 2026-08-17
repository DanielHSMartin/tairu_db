#!/usr/bin/env python3
"""Turn a normal .tairudb into the file stage C will produce, for testing TODAY.

The stage-A reader accepts geometry that lives ONLY in the `wkb` column, but no
producer writes such a file until the plugin cutover (stage C). That leaves the
branch with synthetic test coverage and nothing exercised on a real device. This
forges one so it can be opened in the app now:

  * every vector feature gets `wkb` (encoded from its `points` text when the
    export did not already carry one), then `points` is set to NULL;
  * `min_reader_version = 3` is stamped, so an older app REFUSES the file with
    "update the app" instead of opening it and rendering nothing;
  * GRG stays untouched — it is exempt from the migration and its own parser
    does not know the `wkb` column at all.

Usage:
    python3 forge_wkb_only.py entrada.tairudb [saida.tairudb]

Requires nothing outside the stdlib. See TAIRUDB_WKB_MIGRATION_PLAN.md.
"""

import os
import shutil
import struct
import sqlite3
import sys


def _wkb_point(lon, lat):
    return struct.pack('<BIdd', 1, 1, lon, lat)


def _ring(points):
    out = struct.pack('<I', len(points))
    for lon, lat in points:
        out += struct.pack('<dd', lon, lat)
    return out


def _wkb_linestring(points):
    return struct.pack('<BI', 1, 2) + _ring(points)


def _wkb_polygon(points):
    # A polygon ring must close; the exporter writes the exterior ring only.
    ring = points if points[0] == points[-1] else points + [points[0]]
    return struct.pack('<BII', 1, 3, 1) + _ring(ring)


def _wkb_multi(kind, parts):
    """kind: 4 MultiPoint, 5 MultiLineString, 6 MultiPolygon."""
    single = {4: lambda p: _wkb_point(*p[0]), 5: _wkb_linestring, 6: _wkb_polygon}[kind]
    out = struct.pack('<BII', 1, kind, len(parts))
    for part in parts:
        out += single(part)
    return out


def parse_points(text):
    """The exporter dialect: 'lon lat, lon lat; lon lat, ...' — ';' splits parts."""
    parts = []
    for chunk in text.split(';'):
        coords = []
        for pair in chunk.split(','):
            pair = pair.strip()
            if not pair:
                continue
            bits = pair.split()
            if len(bits) != 2:
                continue
            try:
                coords.append((float(bits[0]), float(bits[1])))
            except ValueError:
                continue
        if coords:
            parts.append(coords)
    return parts


def encode(type_str, parts):
    """Mirror the app's LayerFeatureTypes → OGC mapping. None when unusable."""
    if not parts:
        return None
    multipart = len(parts) > 1
    if type_str == 'point':
        if multipart:
            return _wkb_multi(4, parts)
        return _wkb_point(*parts[0][0])
    if type_str == 'polygon':
        if multipart:
            return _wkb_multi(6, parts)
        return _wkb_polygon(parts[0])
    # line, contourLine and anything else geometry-like
    if any(len(p) < 2 for p in parts):
        return None
    if multipart:
        return _wkb_multi(5, parts)
    return _wkb_linestring(parts[0])


def forge(src, dst):
    shutil.copyfile(src, dst)
    conn = sqlite3.connect(dst)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='features'")
    if not cur.fetchone():
        raise SystemExit('sem tabela `features` — nada a forjar')

    cur.execute('SELECT uuid, type, points, wkb FROM features')
    rows = cur.fetchall()

    encoded = kept = skipped = 0
    for uuid, type_str, points, wkb in rows:
        if wkb is not None:
            kept += 1
            continue
        if not points:
            skipped += 1
            continue
        blob = encode(type_str, parse_points(points))
        if blob is None:
            skipped += 1
            continue
        cur.execute('UPDATE features SET wkb=? WHERE uuid=?', (blob, uuid))
        encoded += 1

    # Drop the text ONLY where a blob now carries the geometry — a feature we
    # could not encode must keep its points, or the forged file loses it and the
    # test would be measuring our own bug instead of the reader.
    cur.execute('UPDATE features SET points=NULL WHERE wkb IS NOT NULL')
    cur.execute('SELECT count(*) FROM features WHERE points IS NOT NULL')
    leftover = cur.fetchone()[0]

    cur.execute('DELETE FROM metadata WHERE name=?', ('min_reader_version',))
    cur.execute('INSERT INTO metadata (name, value) VALUES (?, ?)',
                ('min_reader_version', '3'))

    conn.commit()
    conn.close()

    print(f'{dst}')
    print(f'  {encoded} feições codificadas, {kept} já tinham wkb, {skipped} sem geometria usável')
    print(f'  {leftover} ainda com points (esperado: {skipped})')
    print(f'  min_reader_version=3 → app < 1.0.57 deve RECUSAR com "atualize o aplicativo"')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    source = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else \
        f'{os.path.splitext(source)[0]}_wkbonly.tairudb'
    forge(source, target)
