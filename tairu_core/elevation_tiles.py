# -*- coding: utf-8 -*-

"""
Terrain elevation tiles for a .tairudb, so an exported map carries the altitude
of its own ground instead of depending on the phone reaching the internet.

The tiles are Terrarium-encoded PNGs from the AWS Open Data "Terrain Tiles" set
(the former Mapzen one) — BYTE FOR BYTE what the app downloads when it is
online, which is the whole point: one encoding, one decoder, one set of numbers
whatever the source. Elevation is `(R * 256 + G + B / 256) - 32768` metres.

Why not derive them from the Copernicus DEM this plugin already downloads for
contour lines: it would be better data (~4 m against SRTM's ~9 m) but it would
also be a reprojection to Web Mercator, a tile cut and a PNG encode — three
places for a silently wrong altitude to come from — for an error that is already
far below what a 30 m grid can say about a hillside. If that trade ever changes,
the reader does not: it decodes Terrarium either way.

Licensing, which is not incidental here: a .tairudb IS redistributed — it is
shared between users and uploaded to the expedition's storage — so unlike a
device-local offline area it may only carry data that may be passed on. This set
is SRTM / GMTED2010 / 3DEP, all United States Geological Survey public domain.
The courtesy notice is required and is written into the file's `attribution`
metadata by [write_elevation_tiles].
"""

import math

ELEVATION_ZOOM = 12

ELEVATION_TILE_URL = (
    'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png')

# Measured across Brazilian terrain: ocean 0.7 KB, coast 8 KB, Pantanal 28 KB,
# Amazon/cerrado 35-37 KB, serra 47-52 KB. Only feeds the size estimate.
AVG_ELEVATION_TILE_BYTES = 36000

ELEVATION_ATTRIBUTION = (
    'SRTM, GMTED2010 and 3DEP terrain data courtesy of the '
    'U.S. Geological Survey')


def _lon2tilex(lon, n):
    return int((lon + 180.0) / 360.0 * n)


def _lat2tiley(lat, n):
    lat_rad = math.radians(lat)
    return int(
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0 * n)


def elevation_tiles_for_extent(extent_wgs84):
    """XYZ (x, y) tiles at [ELEVATION_ZOOM] covering `extent_wgs84`.

    Deliberately the bounding box, not a per-polygon intersection the way the
    imagery tiles are chosen: one tile is ~9 km of ground, so a precise cut
    saves a handful of 36 KB blobs, and the tiles it would drop are exactly the
    ones a track leaving the drawn area needs. Covering the box is both simpler
    and the more useful answer.
    """
    if extent_wgs84 is None or extent_wgs84.isEmpty():
        return []
    n = 2.0 ** ELEVATION_ZOOM
    last = int(n) - 1

    def clamp(v):
        return max(0, min(last, v))

    # Latitude is clamped to the Web Mercator limit before the tile maths: the
    # log/tan of anything beyond it is not a number, and one NaN here would take
    # out the whole range instead of the one tile.
    south = max(-85.05112878, extent_wgs84.yMinimum())
    north = min(85.05112878, extent_wgs84.yMaximum())
    if north < south:
        return []

    x0 = clamp(_lon2tilex(extent_wgs84.xMinimum(), n))
    x1 = clamp(_lon2tilex(extent_wgs84.xMaximum(), n))
    y0 = clamp(_lat2tiley(north, n))  # north = smaller y
    y1 = clamp(_lat2tiley(south, n))
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def estimate_bytes(tile_count):
    """Rough on-disk cost of `tile_count` elevation tiles, for the dry run."""
    return int(tile_count) * AVG_ELEVATION_TILE_BYTES


def write_elevation_tiles(writer, extent_wgs84, feedback):
    """Download the elevation tiles covering `extent_wgs84` into `writer`.

    Returns the number of tiles actually stored. Best-effort by design: a tile
    that does not arrive is skipped, not fatal — a map missing a corner of its
    terrain is worth far more than a failed export, and the app falls back to
    its own download for anything absent.
    """
    tiles = elevation_tiles_for_extent(extent_wgs84)
    if not tiles:
        return 0
    if not writer.createElevationTable():
        feedback.push_info('Aviso: não foi possível criar a tabela de altitude.')
        return 0

    stored = _download_into(writer, tiles, feedback)
    if stored:
        writer.setMetadataValue('elevation_attribution', ELEVATION_ATTRIBUTION)
        writer.periodicCommit()
    return stored


def _download_into(writer, tiles, feedback, concurrency=8):
    """Fetch every tile through QgsNetworkAccessManager and store it as it lands.

    Mirrors tile_prefetch._download_all: a nested event loop keeps the window
    responsive and the progress live, and the whole thing is cancellable. The
    difference is that this one KEEPS the bytes — the prefetcher only warms
    QGIS's HTTP cache for a render that happens later.
    """
    from qgis.core import QgsNetworkAccessManager
    from qgis.PyQt.QtCore import QUrl, QEventLoop, QTimer
    from qgis.PyQt.QtNetwork import QNetworkRequest
    # Local a funcao, como os de cima: o modulo tem de importar sem QGIS para
    # test_elevation_tiles.py exercitar a matematica de tiles sozinha.
    try:
        from ..compat import _exec_loop
    except ImportError:  # standalone usage with the plugin dir on sys.path
        from compat import _exec_loop

    nam = QgsNetworkAccessManager.instance()
    total = len(tiles)
    feedback.push_info(f'Baixando {total} tile(s) de altitude…')
    feedback.reset_progress()
    st = {'idx': 0, 'done': 0, 'inflight': 0, 'stored': 0,
          'failed': 0, 'rejected': 0, 'last_error': ''}
    loop = QEventLoop()

    def drained():
        return st['inflight'] == 0 and (st['done'] >= total
                                        or feedback.is_canceled())

    def pump():
        while (st['inflight'] < concurrency and st['idx'] < total
               and not feedback.is_canceled()):
            x, y = tiles[st['idx']]
            st['idx'] += 1
            url = (ELEVATION_TILE_URL
                   .replace('{z}', str(ELEVATION_ZOOM))
                   .replace('{x}', str(x))
                   .replace('{y}', str(y)))
            req = QNetworkRequest(QUrl(url))
            # Some tile servers answer 403 to a blank User-Agent; the same
            # header the basemap prefetch sends.
            req.setRawHeader(b'User-Agent',
                             b'Mozilla/5.0 (compatible; QGIS TairuDB)')
            reply = nam.get(req)
            st['inflight'] += 1

            # No `except: pass` anywhere below. The scanner at plugins.qgis.org
            # raises B110/B112 on a silently swallowed exception, and it is right
            # to: a tile that fails to store is exactly the thing whose absence
            # nobody would otherwise notice. Every failure here is counted, and
            # the count is reported at the end.
            def on_done(r=reply, tx=x, ty=y):
                st['inflight'] -= 1
                st['done'] += 1
                try:
                    data = bytes(r.readAll())
                    # A tile server error page is a 200 with HTML in it; PNG's
                    # magic number is the cheap way to refuse it before it
                    # becomes an "altitude" nobody can explain.
                    if data[:8] != b'\x89PNG\r\n\x1a\n':
                        st['rejected'] += 1
                    elif writer.saveElevationTile(ELEVATION_ZOOM, tx, ty, data):
                        st['stored'] += 1
                    else:
                        st['failed'] += 1
                except Exception as exc:  # noqa: BLE001 - reported, not hidden
                    st['failed'] += 1
                    st['last_error'] = str(exc)
                _safe_cleanup(r, st)
                _report_progress(feedback, st, total)
                if drained():
                    loop.quit()
                else:
                    pump()

            reply.finished.connect(on_done)

    tick = QTimer()
    tick.setInterval(200)
    tick.timeout.connect(lambda: loop.quit() if drained() else None)
    tick.start()

    pump()
    if not drained():
        _exec_loop(loop)
    tick.stop()

    # Said out loud rather than swallowed: a partial download is a map with
    # holes in its terrain, and the user is the only one who can decide whether
    # to redo it.
    if st['rejected']:
        feedback.push_info(
            f"Aviso: {st['rejected']} tile(s) de altitude vieram sem imagem "
            'valida e foram descartados.')
    if st['failed']:
        feedback.push_info(
            f"Aviso: {st['failed']} tile(s) de altitude nao puderam ser "
            f"gravados{(': ' + st['last_error']) if st['last_error'] else ''}.")
    return st['stored']


def _safe_cleanup(reply, st):
    """Release the reply. Counted, never silent — see the note in [_download_into]."""
    try:
        reply.deleteLater()
    except Exception as exc:  # noqa: BLE001 - counted below
        st['failed'] += 1
        st['last_error'] = str(exc)


def _report_progress(feedback, st, total):
    """Progress is cosmetic, but a broken feedback adapter still gets recorded
    rather than dropped on the floor."""
    try:
        feedback.set_progress(int(100 * st['done'] / total))
        feedback.heartbeat(f"Baixando altitude… {st['done']}/{total} tiles")
    except Exception as exc:  # noqa: BLE001 - counted, and never fatal
        st['last_error'] = str(exc)
