# -*- coding: utf-8 -*-

"""
.tairudb → MBTiles conversion so downloaded files can be displayed in QGIS
through GDAL's MBTiles raster driver.

The tairudb writer already stores tile rows in TMS convention (Y flipped at
save time — see tairu_core.generator.convert_and_save_tile), which is exactly
the MBTiles convention, so tile rows are copied verbatim with NO row flip.

Each tiles_region_{N} table becomes one .mbtiles file. The tables use the
0-based polygon index while the regions table rows are 1-based AUTOINCREMENT
ids in the same insertion order; the mapping handles both conventions.
"""

import os
import re
import sqlite3

_TILE_TABLE_RE = re.compile(r'^tiles_region_\d+$')


def _parse_bounds_ring(ring_str):
    """'lon lat, lon lat, ...' regions.bounds ring -> (w, s, e, n) or None."""
    try:
        lons, lats = [], []
        for pair in ring_str.split(','):
            parts = pair.strip().split()
            if len(parts) < 2:
                continue
            lons.append(float(parts[0]))
            lats.append(float(parts[1]))
        if not lons:
            return None
        return min(lons), min(lats), max(lons), max(lats)
    except (ValueError, AttributeError):
        return None


def _region_for_table(table_index, regions):
    """Match tiles_region_{table_index} to its regions row (dict) if possible."""
    if not regions:
        return None
    by_id = {r['id']: r for r in regions}
    # A region row with id 0 means the writer used explicit 0-based ids that
    # match the table indices directly. Otherwise rows are 1-based
    # AUTOINCREMENT in the same insertion order as the 0-based tables.
    if 0 in by_id:
        return by_id.get(table_index)
    ordered = sorted(regions, key=lambda r: r['id'])
    if table_index < len(ordered):
        return ordered[table_index]
    return by_id.get(table_index)


def tairudb_to_mbtiles(tairudb_path, out_dir, base_name=None, progress_cb=None):
    """Convert every region tile table into an MBTiles file.

    Returns a list of (mbtiles_path, region_label). Regions without tiles are
    skipped. Raises sqlite3.Error / ValueError on malformed input files.
    """
    os.makedirs(out_dir, exist_ok=True)
    base_name = base_name or os.path.splitext(os.path.basename(tairudb_path))[0]

    src = sqlite3.connect(f'file:{tairudb_path}?mode=ro', uri=True)
    try:
        metadata = dict(src.execute('SELECT name, value FROM metadata'))

        try:
            regions = [
                {'id': row[0], 'name': row[1], 'minzoom': row[2], 'maxzoom': row[3], 'bounds': row[4]}
                for row in src.execute('SELECT id, name, minzoom, maxzoom, bounds FROM regions')
            ]
        except sqlite3.Error:
            regions = []

        tile_tables = [
            row[0] for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tiles_region_%'"
            )
            if _TILE_TABLE_RE.fullmatch(row[0])
        ]
        if not tile_tables:
            raise ValueError('O arquivo não contém tabelas de tiles (tiles_region_N).')

        results = []
        multi = len(tile_tables) > 1
        for idx, table in enumerate(sorted(tile_tables, key=lambda t: int(t.rsplit('_', 1)[-1]))):
            table_index = int(table.rsplit('_', 1)[-1])
            count = src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count == 0:
                continue

            region = _region_for_table(table_index, regions)
            region_label = (region or {}).get('name') or f'Região {table_index + 1}'

            suffix = f'_regiao_{table_index + 1}' if multi else ''
            out_path = os.path.join(out_dir, f'{base_name}{suffix}.mbtiles')
            if os.path.exists(out_path):
                os.remove(out_path)

            dst = sqlite3.connect(out_path)
            try:
                dst.execute('CREATE TABLE metadata (name text, value text);')
                dst.execute('CREATE TABLE tiles (zoom_level integer, tile_column integer, '
                            'tile_row integer, tile_data blob);')
                dst.execute('CREATE UNIQUE INDEX tile_index ON tiles '
                            '(zoom_level, tile_column, tile_row);')

                zmin, zmax = src.execute(
                    f'SELECT MIN(zoom_level), MAX(zoom_level) FROM "{table}"').fetchone()

                meta = {
                    'name': f'{metadata.get("name", base_name)} — {region_label}',
                    'format': (metadata.get('format') or 'png').lower(),
                    'type': 'overlay',
                    'version': '1.1',
                    'minzoom': str(zmin),
                    'maxzoom': str(zmax),
                }
                bounds = _parse_bounds_ring((region or {}).get('bounds') or '')
                if bounds:
                    meta['bounds'] = f'{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}'
                    meta['center'] = (f'{(bounds[0] + bounds[2]) / 2},'
                                      f'{(bounds[1] + bounds[3]) / 2},{zmax}')
                dst.executemany('INSERT INTO metadata VALUES (?, ?);', meta.items())

                # Verbatim copy: tairudb rows are already TMS like MBTiles
                cursor = src.execute(
                    f'SELECT zoom_level, tile_column, tile_row, tile_data FROM "{table}"')
                copied = 0
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    dst.executemany('INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?);', rows)
                    copied += len(rows)
                    if progress_cb:
                        progress_cb((idx + copied / count) / len(tile_tables))
                dst.commit()
            finally:
                dst.close()

            results.append((out_path, region_label))

        if not results:
            raise ValueError('O arquivo não contém tiles em nenhuma região.')
        return results
    finally:
        src.close()
