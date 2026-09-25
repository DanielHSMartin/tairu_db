# -*- coding: utf-8 -*-

"""
Best-effort pre-download of XYZ basemap source tiles into QGIS's shared HTTP cache.

The tile render (QgsMapRendererSequentialJob) renders synchronously on the GUI thread
and, for an online source like Google Hybrid, BLOCKS mid-render while it downloads the
source tiles — freezing the window with no feedback (proven on-device: a warm-cache run
renders 40 tiles in 0.1s, a cold-cache run freezes until done). This module warms the
cache first with ASYNC QgsNetworkAccessManager requests, which keep the UI responsive
and show progress; the subsequent render then finds every tile cached and is instant.

Best-effort by design: if the layer is not a plain XYZ source (no {x}/{y}/{z} url, or a
{s} subdomain rotation we can't match to what the provider will request), prefetch is
skipped and generation proceeds exactly as before.
"""

import contextlib
import urllib.parse

try:
    from .i18n import tr
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_core.i18n import tr


def _xyz_url_template(layer):
    """The '{x}/{y}/{z}' URL template of an XYZ (WMS type=xyz) raster layer, or None."""
    try:
        src = layer.source() or ''
    except Exception:
        return None
    if 'type=xyz' not in src:
        return None
    for part in src.split('&'):
        if part.startswith('url='):
            url = urllib.parse.unquote(part[len('url='):])
            # {s} subdomain rotation and {q} quadkeys can't be matched to the provider's
            # own request URL, so the cache would miss — skip those sources.
            if '{s}' in url or '{q}' in url:
                return None
            if '{x}' in url and '{y}' in url and '{z}' in url:
                return url
    return None


def fill_template(template, x, y, z):
    """Substitute a slippy-map XYZ template. Handles TMS '{-y}' as well as '{y}'."""
    url = template.replace('{z}', str(z)).replace('{x}', str(x))
    if '{-y}' in url:
        url = url.replace('{-y}', str(2 ** z - 1 - y))
    else:
        url = url.replace('{y}', str(y))
    return url


def basemap_tile_urls(layers, tiles, zoom):
    """Deduped source-tile URLs for every XYZ layer among `layers`, [] when none."""
    templates = []
    for layer in layers or []:
        t = _xyz_url_template(layer)
        if t:
            templates.append(t)
    urls = []
    for t in templates:
        for (x, y) in tiles:
            urls.append(fill_template(t, x, y, zoom))
    return list(dict.fromkeys(urls))


def prefetch_basemap_tiles(layers, tiles, zoom, feedback):
    """Warm the HTTP cache for the XYZ tiles the render will need. Returns the number of
    tiles fetched (0 when there is nothing to prefetch, or on any failure — generation
    then just proceeds with the direct render)."""
    try:
        urls = basemap_tile_urls(layers, tiles, zoom)
    except Exception:
        return 0
    if not urls:
        return 0
    try:
        return _download_all(urls, feedback)
    except Exception as e:
        with contextlib.suppress(Exception):
            feedback.push_info(tr(
                "Aviso: pré-download do mapa base falhou ({err}); "
                "seguindo com renderização direta.").format(err=e))
        return 0


def _download_all(urls, feedback, concurrency=8):
    """Fetch every URL through QgsNetworkAccessManager, keeping up to `concurrency`
    requests in flight. Runs a nested event loop so the GUI stays responsive and the
    progress/heartbeat update live; cancellable via feedback.is_canceled()."""
    from qgis.core import QgsNetworkAccessManager
    from qgis.PyQt.QtCore import QUrl, QEventLoop, QTimer
    from qgis.PyQt.QtNetwork import QNetworkRequest
    # Import LOCAL a funcao, como os de cima: este modulo e importavel sem QGIS
    # de proposito (test_tile_prefetch.py exercita a montagem de URLs sozinha), e
    # um import de topo puxando compat->qgis quebra isso.
    nam = QgsNetworkAccessManager.instance()
    total = len(urls)
    feedback.push_info(tr("Baixando {n} tiles do mapa base…").format(n=total))
    feedback.reset_progress()  # this phase grows 0 -> 100 as tiles arrive
    st = {'idx': 0, 'done': 0, 'inflight': 0}
    loop = QEventLoop()

    def all_dispatched_and_drained():
        return st['inflight'] == 0 and (st['done'] >= total or feedback.is_canceled())

    def pump():
        while st['inflight'] < concurrency and st['idx'] < total and not feedback.is_canceled():
            url = urls[st['idx']]
            st['idx'] += 1
            req = QNetworkRequest(QUrl(url))
            # A stable, browser-like UA — some tile servers reject blank UAs (403).
            # The cache key is the URL, so this doesn't break the render cache hit.
            req.setRawHeader(b'User-Agent', b'Mozilla/5.0 (compatible; QGIS TairuDB)')
            reply = nam.get(req)
            st['inflight'] += 1

            def on_done(r=reply):
                st['inflight'] -= 1
                st['done'] += 1
                with contextlib.suppress(Exception):
                    r.deleteLater()
                with contextlib.suppress(Exception):
                    feedback.set_progress(int(100 * st['done'] / total))
                    feedback.heartbeat(
                        tr("Baixando mapa base… {done}/{total} tiles").format(done=st['done'], total=total))
                if all_dispatched_and_drained():
                    loop.quit()
                else:
                    pump()

            reply.finished.connect(on_done)

    # Watchdog: quit if the user cancels while nothing is in flight (or on a stall).
    tick = QTimer()
    tick.setInterval(200)
    tick.timeout.connect(lambda: loop.quit() if all_dispatched_and_drained() else None)
    tick.start()

    pump()
    if not all_dispatched_and_drained():
        loop.exec()
    tick.stop()
    return st['done']
