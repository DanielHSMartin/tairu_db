# -*- coding: utf-8 -*-
"""
Contour line generation from DEM data sources for inclusion in .tairudb files.
Logic adapted from CurvaDeNivel (github.com/DanielHSMartin/CurvaDeNivel).
"""

import html
import math
import os
import tempfile
import urllib.request
from datetime import datetime

try:
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    _GDAL_AVAILABLE = True
except ImportError:
    _GDAL_AVAILABLE = False
    gdal = None
    ogr = None
    osr = None

from qgis.core import QgsGeometry, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication

try:
    from .i18n import tr
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_core.i18n import tr

SOURCE_INPE = 0
SOURCE_COPERNICUS = 1

SMOOTHING_NONE = 'Nenhum'
SMOOTHING_LOW = 'Baixo'
SMOOTHING_MEDIUM = 'Médio'
SMOOTHING_HIGH = 'Alto'

_INPE_BASE_URL = 'https://data.inpe.br/bdc/data/topodata/v001/'
_COPERNICUS_BASE_URL = 'https://copernicus-dem-30m.s3.amazonaws.com/'


class ContourError(Exception):
    pass


def generate_contours(bbox_wgs84, dem_source, interval, smoothing, color, feedback,
                      clip_polygons=None):
    """
    Generate contour lines from a DEM and return a temporary QgsVectorLayer.

    The returned layer is backed by a file in a per-run temp directory.  Pass it
    immediately to export_vector_layers; do not add it to the QGIS project.

    Args:
        bbox_wgs84:  QgsRectangle in WGS84 (EPSG:4326)
        dem_source:  SOURCE_INPE (0) or SOURCE_COPERNICUS (1)
        interval:    contour interval in metres (int >= 1)
        smoothing:   SMOOTHING_* constant
        color:       QColor for the contour symbology
        feedback:    FeedbackAdapter
        clip_polygons: optional list of AOI QgsGeometry in WGS84.  When the AOI
                     is an irregular polygon (not just a rectangle), the DEM is
                     masked to it so contours are clipped to that shape instead
                     of filling the whole bounding box.

    Returns:
        QgsVectorLayer with contour lines and RuleBasedRenderer applied.

    Raises:
        ContourError on any failure.
    """
    if not _GDAL_AVAILABLE:
        raise ContourError(tr(
            'GDAL não está disponível. '
            'Instale o pacote GDAL/osgeo para gerar curvas de nível.'))

    run_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    temp_dir = os.path.join(tempfile.gettempdir(), 'TairuDB_Curvas', run_id)
    os.makedirs(temp_dir, exist_ok=True)

    try:
        feedback.push_info(tr('Baixando tiles de elevação…'))
        tile_paths = _download_tiles(bbox_wgs84, dem_source, temp_dir, feedback)
        if not tile_paths:
            if dem_source == SOURCE_INPE:
                raise ContourError(tr(
                    'Nenhum tile INPE TOPODATA baixado com sucesso. '
                    'O servidor pode estar indisponível, ou a área está fora da cobertura do Brasil '
                    '(6°N–34°S, 75°W–34.5°W). '
                    'Tente usar "Copernicus GLO-30 (Mundial)" como fonte de dados.'))
            else:
                raise ContourError(tr(
                    'Nenhum tile Copernicus GLO-30 baixado com sucesso. '
                    'Verifique a conexão com a internet e tente novamente.'))

        if feedback.is_canceled():
            raise ContourError(tr('Cancelado pelo usuário.'))

        feedback.push_info(tr('Recortando {n} tile(s) para a área de interesse…').format(n=len(tile_paths)))
        feedback.heartbeat(tr('Curvas: recortando {n} tile(s) de elevação…').format(n=len(tile_paths)))
        QCoreApplication.processEvents()
        cutline_path = _write_cutline(clip_polygons, temp_dir) if clip_polygons else None
        if cutline_path:
            feedback.push_info(tr('  Máscara de polígono aplicada — curvas recortadas à área.'))
        clipped = _clip_tiles(tile_paths, bbox_wgs84, temp_dir, feedback, cutline_path)
        if not clipped:
            raise ContourError(tr(
                'Nenhum tile de elevação intersecta a área selecionada após recorte.'))

        feedback.push_info(tr('Mesclando tiles…'))
        feedback.heartbeat(tr('Curvas: mesclando tiles de elevação…'))
        QCoreApplication.processEvents()
        merged_path = os.path.join(temp_dir, 'merged.tif')
        _merge_tiles(clipped, merged_path)

        if feedback.is_canceled():
            raise ContourError(tr('Cancelado pelo usuário.'))

        dem_path = merged_path
        if smoothing != SMOOTHING_NONE:
            feedback.push_info(tr('Suavizando terreno ({level})…').format(level=tr(smoothing)))
            feedback.heartbeat(tr('Curvas: suavizando terreno ({level})…').format(level=tr(smoothing)))
            QCoreApplication.processEvents()
            try:
                dem_path = _smooth_terrain(merged_path, smoothing, temp_dir)
            except Exception as exc:
                feedback.push_info(tr('Aviso: suavização falhou ({err}), usando terreno original.').format(err=exc))

        if feedback.is_canceled():
            raise ContourError(tr('Cancelado pelo usuário.'))

        feedback.push_info(tr('Gerando curvas de nível (intervalo: {interval} m)…').format(interval=interval))
        feedback.heartbeat(tr('Curvas: traçando linhas (intervalo {interval} m)…').format(interval=interval))
        QCoreApplication.processEvents()
        contour_path = os.path.join(temp_dir, 'contours.gpkg')
        _run_contour_generate(dem_path, interval, contour_path, feedback)

        if feedback.is_canceled():
            raise ContourError(tr('Cancelado pelo usuário.'))

        layer = QgsVectorLayer(
            contour_path + '|layername=contours', 'Curvas de Nível', 'ogr')
        if not layer.isValid():
            raise ContourError(
                tr('Falha ao carregar a camada de curvas de nível: {path}').format(path=contour_path))

        if layer.featureCount() == 0:
            feedback.push_info(tr(
                'Aviso: nenhuma curva gerada (terreno plano ou intervalo muito grande?).'))

        _apply_renderer(layer, interval, color)
        feedback.push_info(tr('Curvas de nível geradas com sucesso.'))

        # Free the intermediate DEM rasters (merged/smoothed/TPI/clipped tiles,
        # cutline) — the bulk of the temp footprint. Only contours.gpkg is still needed,
        # backing the returned layer; the DEM tile cache lives elsewhere and is kept.
        for name in os.listdir(temp_dir):
            if not name.startswith('contours.gpkg'):
                try:
                    os.remove(os.path.join(temp_dir, name))
                except OSError:
                    pass
        return layer

    except ContourError:
        raise
    except Exception as exc:
        raise ContourError(tr('Erro ao gerar curvas de nível: {err}').format(err=exc)) from exc


# ---------------------------------------------------------------------------- download

def _download_tiles(bbox_wgs84, dem_source, temp_dir, feedback):
    if dem_source == SOURCE_INPE:
        return _download_inpe_tiles(bbox_wgs84, temp_dir, feedback)
    return _download_copernicus_tiles(bbox_wgs84, temp_dir, feedback)


def _inpe_tile_name(lat_norte, lon_oeste):
    """
    Construct INPE TOPODATA tile name — exact port of CurvaDeNivel's algorithm.

    Template "00S00_ZN" (8 chars):
      [0][1] = absolute latitude (tens digit, units digit)
      [2]    = 'N' if lat_norte > 0, else 'S'
      [3][4] = absolute longitude (tens digit, units digit)
      [5]    = '_' for whole-degree longitude, '5' for half-degree (.5)
      [6][7] = 'ZN' (fixed)

    The tile covers lat_norte-1 to lat_norte and lon_oeste to lon_oeste+1.5.
    """
    nome = list("00S00_ZN")
    nome[0] = str(abs(int(lat_norte / 10)))
    nome[1] = str(abs(int(lat_norte)) % 10)
    if lat_norte > 0:
        nome[2] = 'N'
    nome[3] = str(abs(int(lon_oeste / 10)))
    nome[4] = str(abs(int(lon_oeste)) % 10)
    if lon_oeste % 1.0 != 0:
        nome[5] = '5'
    return ''.join(nome)


def _download_inpe_tiles(bbox_wgs84, temp_dir, feedback):
    """
    Download INPE TOPODATA tiles covering the bbox.

    Grid: 1° latitude × 1.5° longitude, Brazil (6°N–34°S, 75°W–34.5°W).
    Each tile (lat_norte, lon_oeste) covers lat_norte-1°→lat_norte, lon_oeste→lon_oeste+1.5°.
    Filename constructed by _inpe_tile_name(), same algorithm as CurvaDeNivel.
    """
    cache_dir = os.path.join(tempfile.gettempdir(), 'CurvaDeNivel', 'inpe')
    os.makedirs(cache_dir, exist_ok=True)

    xmin = bbox_wgs84.xMinimum()
    xmax = bbox_wgs84.xMaximum()
    ymin = bbox_wgs84.yMinimum()
    ymax = bbox_wgs84.yMaximum()

    # First pass: collect all tile coordinates that intersect the bbox
    tiles_to_fetch = []
    lat_norte = 6.0
    while lat_norte > -34.0:
        tile_south = lat_norte - 1.0
        if lat_norte < ymin or tile_south > ymax:
            lat_norte -= 1.0
            continue
        lon_oeste = -75.0
        while lon_oeste < -34.5:
            if lon_oeste + 1.5 > xmin and lon_oeste < xmax:
                tiles_to_fetch.append((lat_norte, lon_oeste))
            lon_oeste += 1.5
        lat_norte -= 1.0

    if not tiles_to_fetch:
        feedback.push_info(tr(
            '  Nenhum tile INPE cobre a área selecionada. '
            'Verifique se a área está no Brasil (6°N–34°S, 75°W–34.5°W).'))
        return []

    n_total = len(tiles_to_fetch)
    feedback.push_info(tr('  {n} tile(s) INPE TOPODATA necessário(s).').format(n=n_total))

    tile_paths = []
    n_cached = n_downloaded = n_failed = 0
    feedback.reset_progress()  # DEM download phase: bar grows 0 -> 100 across tiles
    for idx, (lat_norte, lon_oeste) in enumerate(tiles_to_fetch):
        if feedback.is_canceled():
            break
        nome = _inpe_tile_name(lat_norte, lon_oeste)
        was_cached = os.path.exists(os.path.join(cache_dir, nome + '.tif'))
        path = _fetch_inpe_tile(lat_norte, lon_oeste, cache_dir, feedback,
                                progress_base=idx / n_total * 100.0,
                                progress_span=100.0 / n_total)
        if path:
            tile_paths.append(path)
            if was_cached:
                n_cached += 1
            else:
                n_downloaded += 1
        else:
            n_failed += 1

    parts = []
    if n_downloaded:
        parts.append(tr('{n} baixado(s)').format(n=n_downloaded))
    if n_cached:
        parts.append(tr('{n} do cache').format(n=n_cached))
    if n_failed:
        parts.append(tr('{n} falhou — verifique a conexão ou use Copernicus GLO-30').format(n=n_failed))
    feedback.push_info(tr('  Resultado: {parts}.').format(parts=', '.join(parts)))
    return tile_paths


def _download_dem_file(url, dest, feedback, label, progress_base=0.0, progress_span=100.0):
    """Download a DEM GeoTIFF responsively.

    DEM tiles are large files and the old blocking urllib.urlretrieve froze the whole
    QGIS window with no feedback while they downloaded (the contour analogue of the
    basemap-tile freeze). This reads in chunks, pumps the event loop between chunks so
    the UI stays alive, and reports MB progress on the heartbeat. Downloads to a
    sibling .part and os.replace()s on success, so a canceled/failed download never
    leaves a truncated .tif in the cache (which would poison every later run).
    Raises on cancel or network error.
    """
    tmp = dest + '.part'
    request = urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0 (compatible; QGIS TairuDB)'})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp, open(tmp, 'wb') as out:  # nosec B310
            try:
                total = int(resp.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                total = 0
            got = 0
            while True:
                if feedback.is_canceled():
                    raise RuntimeError(tr('cancelado'))
                chunk = resp.read(262144)  # 256 KB
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                mb = got / (1024 * 1024)
                if total:
                    feedback.set_progress(int(progress_base + (got / total) * progress_span))
                    feedback.heartbeat(tr('{label}… {mb:.0f}/{total:.0f} MB').format(
                        label=label, mb=mb, total=total / (1024 * 1024)))
                else:
                    feedback.heartbeat(tr('{label}… {mb:.0f} MB').format(label=label, mb=mb))
                QCoreApplication.processEvents()
        os.replace(tmp, dest)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _fetch_inpe_tile(lat_norte, lon_oeste, cache_dir, feedback, progress_base=0.0, progress_span=100.0):
    """
    Download one TOPODATA tile from INPE's Brazil Data Cube STAC/COG endpoint
    (data.inpe.br/bdc), served as a direct GeoTIFF (no zip). The old
    www.dsr.inpe.br server is stuck in a permanent HTTP<->HTTPS redirect
    loop; this endpoint keys tiles by the same naming convention, split
    into two path segments.
    """
    nome = _inpe_tile_name(lat_norte, lon_oeste)
    fn = nome + '.tif'
    tif_path = os.path.join(cache_dir, fn)

    if os.path.exists(tif_path):
        return tif_path

    tile6 = nome[:-2]
    url = _INPE_BASE_URL + tile6[:3] + '/' + tile6[3:6] + '/' + fn
    feedback.push_info(tr('  Baixando {fn}…').format(fn=fn))
    try:
        _download_dem_file(url, tif_path, feedback, tr('Baixando elevação {fn}').format(fn=fn),
                           progress_base, progress_span)
        if os.path.getsize(tif_path) == 0:
            raise ValueError(tr('Resposta vazia do servidor'))
    except Exception as exc:
        feedback.push_info(tr('  Falha: {fn}: {err}').format(fn=fn, err=exc))
        if os.path.exists(tif_path):
            os.remove(tif_path)
        return None

    return tif_path if os.path.exists(tif_path) else None


def _download_copernicus_tiles(bbox_wgs84, temp_dir, feedback):
    """
    Download Copernicus GLO-30 tiles covering the bbox (global, 1°×1° grid).

    Filename: Copernicus_DSM_COG_10_{lat_str}_00_{lon_str}_00_DEM.tif
    Example: Copernicus_DSM_COG_10_S06_00_W041_00_DEM.tif → 6°–7°S, 41°–42°W
    """
    cache_dir = os.path.join(tempfile.gettempdir(), 'CurvaDeNivel', 'copernicus')
    os.makedirs(cache_dir, exist_ok=True)

    lat_start = int(math.floor(bbox_wgs84.yMinimum()))
    lat_end = int(math.ceil(bbox_wgs84.yMaximum()))
    lon_start = int(math.floor(bbox_wgs84.xMinimum()))
    lon_end = int(math.ceil(bbox_wgs84.xMaximum()))

    tiles_to_fetch = [
        (lat, lon)
        for lat in range(lat_start, lat_end + 1)
        for lon in range(lon_start, lon_end + 1)
    ]
    n_total = len(tiles_to_fetch)
    feedback.push_info(tr('  {n} tile(s) Copernicus GLO-30 necessário(s).').format(n=n_total))

    tile_paths = []
    n_cached = n_downloaded = n_failed = 0
    feedback.reset_progress()  # DEM download phase: bar grows 0 -> 100 across tiles
    for idx, (lat, lon) in enumerate(tiles_to_fetch):
        if feedback.is_canceled():
            break
        lat_str = f'N{lat:02d}' if lat >= 0 else f'S{abs(lat):02d}'
        lon_str = f'E{lon:03d}' if lon >= 0 else f'W{abs(lon):03d}'
        fn = f'Copernicus_DSM_COG_10_{lat_str}_00_{lon_str}_00_DEM.tif'
        was_cached = os.path.exists(os.path.join(cache_dir, fn))
        path = _fetch_copernicus_tile(lat, lon, cache_dir, feedback,
                                      progress_base=idx / n_total * 100.0,
                                      progress_span=100.0 / n_total)
        if path:
            tile_paths.append(path)
            if was_cached:
                n_cached += 1
            else:
                n_downloaded += 1
        else:
            n_failed += 1

    parts = []
    if n_downloaded:
        parts.append(tr('{n} baixado(s)').format(n=n_downloaded))
    if n_cached:
        parts.append(tr('{n} do cache').format(n=n_cached))
    if n_failed:
        parts.append(tr('{n} falhou').format(n=n_failed))
    feedback.push_info(tr('  Resultado: {parts}.').format(parts=', '.join(parts)))
    return tile_paths


def _fetch_copernicus_tile(lat, lon, cache_dir, feedback, progress_base=0.0, progress_span=100.0):
    lat_str = f'N{lat:02d}' if lat >= 0 else f'S{abs(lat):02d}'
    lon_str = f'E{lon:03d}' if lon >= 0 else f'W{abs(lon):03d}'
    name = f'Copernicus_DSM_COG_10_{lat_str}_00_{lon_str}_00_DEM'
    fn = name + '.tif'
    tif_path = os.path.join(cache_dir, fn)

    if os.path.exists(tif_path):
        return tif_path

    url = _COPERNICUS_BASE_URL + name + '/' + fn
    feedback.push_info(tr('  Baixando {fn}…').format(fn=fn))
    try:
        _download_dem_file(url, tif_path, feedback, tr('Baixando elevação {fn}').format(fn=fn),
                           progress_base, progress_span)
    except Exception as exc:
        feedback.push_info(tr('  Falha: {fn}: {err}').format(fn=fn, err=exc))
        return None

    return tif_path if os.path.exists(tif_path) else None


# ---------------------------------------------------------------------------- processing

def _write_cutline(clip_polygons, temp_dir):
    """Write the union of the AOI polygons to a GeoJSON cutline, or return None
    when the AOI is effectively rectangular (draw/canvas extent) — a rectangle
    already equals the bbox clip, so masking would be a no-op.
    """
    union = None
    for g in clip_polygons:
        if g is None or g.isEmpty():
            continue
        union = QgsGeometry(g) if union is None else union.combine(g)
    if union is None or union.isEmpty():
        return None

    bbox = union.boundingBox()
    if bbox.width() <= 0 or bbox.height() <= 0:
        return None
    # Rectangular AOI fills ~100% of its bbox → nothing to trim.
    if union.area() >= 0.999 * bbox.width() * bbox.height():
        return None

    path = os.path.join(temp_dir, 'cutline.geojson')
    drv = ogr.GetDriverByName('GeoJSON')
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds = drv.CreateDataSource(path)
    lyr = ds.CreateLayer('cutline', srs, ogr.wkbUnknown)
    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetGeometry(ogr.CreateGeometryFromWkt(union.asWkt()))
    lyr.CreateFeature(feat)
    feat = None
    ds = None
    return path if os.path.exists(path) else None


def _grid_bounds(gt, bbox):
    """The bbox snapped to the nearest pixel edges of geotransform `gt`.

    Bounds taken straight from the bbox anchor the output grid on an arbitrary
    corner: gdal.Warp then picks a slightly different resolution and copies the
    nearest source pixel, shifting the surface by up to half a pixel (~15 m at
    1 arc-second) and the contours by 6-20 m, silently.
    """
    x0, rx, y0, ry = gt[0], gt[1], gt[3], -gt[5]
    c0 = round((bbox.xMinimum() - x0) / rx)
    c1 = max(round((bbox.xMaximum() - x0) / rx), c0 + 1)
    r0 = round((y0 - bbox.yMaximum()) / ry)
    r1 = max(round((y0 - bbox.yMinimum()) / ry), r0 + 1)
    return (x0 + c0 * rx, y0 - r1 * ry, x0 + c1 * rx, y0 - r0 * ry)


def _same_grid(gt, ref):
    """True when `gt` has the pixel size of `ref` and an origin a whole number
    of pixels away: a nearest-neighbour warp onto `ref` is then a copy.

    The slack is for INPE TOPODATA, whose neighbouring tiles sit 0.014 px apart
    (a pixel of 1.5000038°/5400, not 1"): copying them misplaces nothing by more
    than ~0.4 m, while resampling would blend values for no gain."""
    fx = (gt[0] - ref[0]) / ref[1]
    fy = (gt[3] - ref[3]) / ref[5]
    return (math.isclose(gt[1], ref[1], rel_tol=1e-5) and math.isclose(gt[5], ref[5], rel_tol=1e-5)
            and abs(fx - round(fx)) < 0.05 and abs(fy - round(fy)) < 0.05)


def _clip_tiles(tile_paths, bbox_wgs84, temp_dir, feedback, cutline_path=None):
    clipped = []
    ref_gt = bounds = None
    for i, tp in enumerate(tile_paths):
        out = os.path.join(temp_dir, f'clip_{i}.tif')
        try:
            ds = gdal.Open(tp)
            if ds is None:
                continue
            nodata = ds.GetRasterBand(1).GetNoDataValue()
            gt = ds.GetGeoTransform()
            ds = None

            # Every clip lands on the pixel grid of the first tile, at its native
            # resolution, so _merge_tiles only mosaics. A tile on another grid
            # (another product, or Copernicus's coarser longitude step past 50°)
            # is really resampled, and bilinear is then the right kernel.
            # ponytail: first tile sets the grid, not the finest one; pick the
            # finest if a region mixing Copernicus latitude zones needs it.
            if ref_gt is None:
                ref_gt = gt
                bounds = _grid_bounds(gt, bbox_wgs84)

            # cutlineDSName masks pixels outside the AOI polygon to dstNodata
            # (cropToCutline stays off → the bbox extent is preserved). gdal.Warp
            # ignores cutlineDSName=None, so the no-polygon path is unchanged.
            opts = gdal.WarpOptions(
                outputBounds=bounds,
                xRes=ref_gt[1], yRes=-ref_gt[5],
                resampleAlg='near' if _same_grid(gt, ref_gt) else 'bilinear',
                srcSRS='EPSG:4326', dstSRS='EPSG:4326',
                format='GTiff',
                srcNodata=nodata,
                dstNodata=nodata if nodata is not None else -32768,
                cutlineDSName=cutline_path,
            )
            gdal.Warp(out, tp, options=opts)

            ds = gdal.Open(out)
            if ds and ds.RasterXSize > 0 and ds.RasterYSize > 0:
                clipped.append(out)
            ds = None
        except Exception as exc:
            feedback.push_info(
                tr('  Aviso: falha ao recortar {name}: {err}').format(name=os.path.basename(tp), err=exc))
    return clipped


def _merge_tiles(clipped_paths, merged_path):
    # The clips share one pixel grid and extent (_clip_tiles): a mosaic copies
    # them. A warp here would choose its own resolution and resample again.
    vrt = gdal.BuildVRT('', clipped_paths)
    gdal.Translate(merged_path, vrt, format='GTiff')


# Núcleos gaussianos do suavizaTerreno do CurvaDeNivel, copiados como estão: mesmos
# pesos, mesmas curvas. O 3x3, o 7x7 e o 9x9 são a mesma gaussiana (σ≈1 px) em
# tamanhos diferentes; só o 13x13 é mais larga (σ=2). (tamanho, coeficientes)
_GAUSS_3 = (3, '0.077847 0.123317 0.077847 0.123317 0.195346 0.123317 0.077847 0.123317 0.077847')
_GAUSS_7 = (7, (
    '0.000036 0.000363 0.001446 0.002291 0.001446 0.000363 0.000036 0.000363 0.003676 0.014662 '
    '0.023226 0.014662 0.003676 0.000363 0.001446 0.014662 0.058488 0.092651 0.058488 0.014662 '
    '0.001446 0.002291 0.023226 0.092651 0.146768 0.092651 0.023226 0.002291 0.001446 0.014662 '
    '0.058488 0.092651 0.058488 0.014662 0.001446 0.000363 0.003676 0.014662 0.023226 0.014662 '
    '0.003676 0.000363 0.000036 0.000363 0.001446 0.002291 0.001446 0.000363 0.000036'))
_GAUSS_9 = (9, (
    '0 0.000001 0.000014 0.000055 0.000088 0.000055 0.000014 0.000001 0 0.000001 0.000036 0.000362 '
    '0.001445 0.002289 0.001445 0.000362 0.000036 0.000001 0.000014 0.000362 0.003672 0.014648 '
    '0.023205 0.014648 0.003672 0.000362 0.000014 0.000055 0.001445 0.014648 0.058434 0.092566 '
    '0.058434 0.014648 0.001445 0.000055 0.000088 0.002289 0.023205 0.092566 0.146634 0.092566 '
    '0.023205 0.002289 0.000088 0.000055 0.001445 0.014648 0.058434 0.092566 0.058434 0.014648 '
    '0.001445 0.000055 0.000014 0.000362 0.003672 0.014648 0.023205 0.014648 0.003672 0.000362 '
    '0.000014 0.000001 0.000036 0.000362 0.001445 0.002289 0.001445 0.000362 0.000036 0.000001 0 '
    '0.000001 0.000014 0.000055 0.000088 0.000055 0.000014 0.000001 0'))
_GAUSS_13 = (13, (
    '0.000005 0.000019 0.000060 0.000144 0.000269 0.000391 0.000443 0.000391 0.000269 0.000144 '
    '0.000060 0.000019 0.000005 0.000019 0.000077 0.000237 0.000569 0.001063 0.001546 0.001752 '
    '0.001546 0.001063 0.000569 0.000237 0.000077 0.000019 0.000060 0.000237 0.000730 0.001752 '
    '0.003273 0.004762 0.005396 0.004762 0.003273 0.001752 0.000730 0.000237 0.000060 0.000144 '
    '0.000569 0.001752 0.004202 0.007851 0.011423 0.012944 0.011423 0.007851 0.004202 0.001752 '
    '0.000569 0.000144 0.000269 0.001063 0.003273 0.007851 0.014667 0.021341 0.024183 0.021341 '
    '0.014667 0.007851 0.003273 0.001063 0.000269 0.000391 0.001546 0.004762 0.011423 0.021341 '
    '0.031051 0.035185 0.031051 0.021341 0.011423 0.004762 0.001546 0.000391 0.000443 0.001752 '
    '0.005396 0.012944 0.024183 0.035185 0.039870 0.035185 0.024183 0.012944 0.005396 0.001752 '
    '0.000443 0.000391 0.001546 0.004762 0.011423 0.021341 0.031051 0.035185 0.031051 0.021341 '
    '0.011423 0.004762 0.001546 0.000391 0.000269 0.001063 0.003273 0.007851 0.014667 0.021341 '
    '0.024183 0.021341 0.014667 0.007851 0.003273 0.001063 0.000269 0.000144 0.000569 0.001752 '
    '0.004202 0.007851 0.011423 0.012944 0.011423 0.007851 0.004202 0.001752 0.000569 0.000144 '
    '0.000060 0.000237 0.000730 0.001752 0.003273 0.004762 0.005396 0.004762 0.003273 0.001752 '
    '0.000730 0.000237 0.000060 0.000019 0.000077 0.000237 0.000569 0.001063 0.001546 0.001752 '
    '0.001546 0.001063 0.000569 0.000237 0.000077 0.000019 0.000005 0.000019 0.000060 0.000144 '
    '0.000269 0.000391 0.000443 0.000391 0.000269 0.000144 0.000060 0.000019 0.000005'))

# O núcleo pesado de cada nível, que o TPI mistura ao 3x3. Baixo não tem: é o 3x3 puro.
_HEAVY_KERNEL = {SMOOTHING_LOW: None, SMOOTHING_MEDIUM: _GAUSS_7, SMOOTHING_HIGH: _GAUSS_13}

# Linhas por faixa nas passadas com numpy: a memória não cresce com a área de interesse.
_STRIP_ROWS = 256


def _kernel_vrt(path, kernel):
    """`path` visto através da convolução normalizada `kernel` (VRT em memória, Float32).

    O XML é escrito do zero, nunca editando a saída do gdal.BuildVRT: ela chama a fonte
    de <ComplexSource> quando há nodata e grava o caminho relativo ao VRT, e o port, que
    procurava <SimpleSource> e reescrevia o caminho, fez da suavização uma cópia muda da
    2.0.10 à 2.0.26. Sem <NODATA> na fonte, de propósito: assim o GDAL pula o nodata da
    banda na soma e o devolve como nodata; com <NODATA> (como no CurvaDeNivel) ele o lê
    como 0 e puxa para o nível do mar a borda de uma área em polígono.
    """
    size, coefs = kernel
    ds = gdal.Open(path)
    w, h = ds.RasterXSize, ds.RasterYSize
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    ds = None
    nd = '' if nodata is None else f'<NoDataValue>{nodata!r}</NoDataValue>'
    src = html.escape(os.path.abspath(path), quote=False)
    rect = f'xOff="0" yOff="0" xSize="{w}" ySize="{h}"'
    return gdal.Open(
        f'<VRTDataset rasterXSize="{w}" rasterYSize="{h}">'
        f'<VRTRasterBand dataType="Float32" band="1">{nd}<KernelFilteredSource>'
        f'<SourceFilename relativeToVRT="0">{src}</SourceFilename><SourceBand>1</SourceBand>'
        f'<SrcRect {rect}/><DstRect {rect}/>'
        f'<Kernel normalized="1"><Size>{size}</Size><Coefs>{coefs}</Coefs></Kernel>'
        '</KernelFilteredSource></VRTRasterBand></VRTDataset>')


def _smooth_terrain(merged_path, smoothing, temp_dir):
    """Suaviza o DEM como o suavizaTerreno do CurvaDeNivel; devolve o caminho do novo DEM.

    Gaussiana 3x3 misturada a uma mais pesada (7x7 no Médio, 13x13 no Alto) pelo |TPI|
    borrado e normalizado: onde o relevo quebra — crista, vale — pesa o 3x3 e a forma
    fica; onde é liso, o pesado limpa o ruído. Baixo é o 3x3 puro. merged_path fica
    intacto: nada de sobrescrever um arquivo que o VRT ainda segura aberto no Windows.

    Diferenças deliberadas do original: o TPI é calculado também na margem
    (computeEdges), onde o CurvaDeNivel perdia o pixel da borda; o nodata é pulado em
    vez de lido como 0; e o máximo do TPI vem do GDAL, não de um regex sobre o gdal.Info.
    """
    import numpy as np  # só aqui: sem numpy a suavização falha com aviso e as curvas saem

    heavy = _HEAVY_KERNEL[smoothing]
    dem = gdal.Open(merged_path)
    band = dem.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    w, h = dem.RasterXSize, dem.RasterYSize
    lo, hi = band.ComputeRasterMinMax(False)

    light = _kernel_vrt(merged_path, _GAUSS_3)
    tpi_blur = heavy_vrt = None
    tpi_max = 0.0
    if heavy:
        tpi_path = os.path.join(temp_dir, 'tpi.tif')
        gdal.DEMProcessing(tpi_path, merged_path, 'TPI', computeEdges=True)
        tpi = gdal.Open(tpi_path, gdal.GA_Update)
        tb = tpi.GetRasterBand(1)
        tnd = tb.GetNoDataValue()
        for y in range(0, h, _STRIP_ROWS):
            a = tb.ReadAsArray(0, y, w, min(_STRIP_ROWS, h - y))
            tb.WriteArray(np.where(a == tnd, a, np.abs(a)), 0, y)
        tb = tpi = None
        tpi_blur = _kernel_vrt(tpi_path, _GAUSS_9)
        # O máximo sai da leitura. ComputeRasterMinMax num VRT de kernel devolve o da
        # FONTE, sem o filtro — 32,6 em vez de 14,2 num DEM real —, e a mistura pendia
        # para o núcleo pesado: 2,3x mais liso que o CurvaDeNivel.
        # ponytail: o 9x9 roda duas vezes (aqui e na mistura), ~1/3 do tempo (12 s no
        # Médio para 1°x1°); gravar o TPI borrado num .tif se área grande pesar.
        for y in range(0, h, _STRIP_ROWS):
            t = tpi_blur.ReadAsArray(0, y, w, min(_STRIP_ROWS, h - y))
            tpi_max = max(tpi_max, float(t[t != tnd].max(initial=0.0)))
            QCoreApplication.processEvents()
        heavy_vrt = _kernel_vrt(merged_path, heavy)

    out_path = os.path.join(temp_dir, 'smooth.tif')
    out = gdal.GetDriverByName('GTiff').Create(out_path, w, h, 1, gdal.GDT_Float32)
    out.SetGeoTransform(dem.GetGeoTransform())
    out.SetProjection(dem.GetProjection())
    ob = out.GetRasterBand(1)
    if nodata is not None:
        ob.SetNoDataValue(nodata)
    for y in range(0, h, _STRIP_ROWS):
        n = min(_STRIP_ROWS, h - y)
        d = band.ReadAsArray(0, y, w, n)
        valid = np.isfinite(d) if nodata is None else np.isfinite(d) & (d != nodata)
        s = light.ReadAsArray(0, y, w, n)
        if heavy_vrt is not None:
            a = np.clip(tpi_blur.ReadAsArray(0, y, w, n) / tpi_max, 0, 1) if tpi_max > 0 else 0.0
            s = a * s + (1 - a) * heavy_vrt.ReadAsArray(0, y, w, n)
        # Média ponderada normalizada não sai da faixa do dado. Se saiu, entrou nodata na
        # soma (outra versão do GDAL?): melhor o terreno original com aviso do que um anel
        # de curvas falsas na borda da área.
        v = s[valid]
        if v.size and (v.min() < lo - 0.01 or v.max() > hi + 0.01):
            raise ContourError(tr('o terreno suavizado saiu da faixa de elevação do original'))
        ob.WriteArray(np.where(valid, s, d), 0, y)
        QCoreApplication.processEvents()
    ob = out = None
    return out_path


def _run_contour_generate(merged_path, interval, contour_path, feedback):
    ds_raster = gdal.Open(merged_path)
    if ds_raster is None:
        raise ContourError(tr('Não foi possível abrir o DEM mesclado: {path}').format(path=merged_path))

    band = ds_raster.GetRasterBand(1)
    nodata = band.GetNoDataValue()

    drv = ogr.GetDriverByName('GPKG')
    if os.path.exists(contour_path):
        drv.DeleteDataSource(contour_path)
    ds_out = drv.CreateDataSource(contour_path)

    srs_out = osr.SpatialReference()
    srs_out.ImportFromEPSG(4326)
    layer_out = ds_out.CreateLayer('contours', srs_out, ogr.wkbLineString)
    layer_out.CreateField(ogr.FieldDefn('ID', ogr.OFTInteger))
    layer_out.CreateField(ogr.FieldDefn('ELEV', ogr.OFTReal))

    def _progress(complete, _msg, _data):
        feedback.set_progress(int(complete * 100))
        return 0 if feedback.is_canceled() else 1

    result = gdal.ContourGenerate(
        band, interval, 0, [],
        1 if nodata is not None else 0,
        nodata if nodata is not None else 0,
        layer_out, 0, 1,
        callback=_progress,
    )

    ds_out.FlushCache()
    ds_out = None
    ds_raster = None

    if result != 0:
        raise ContourError(tr('gdal.ContourGenerate falhou com código {code}').format(code=result))


def _apply_renderer(layer, interval, color):
    """Apply a RuleBasedRenderer distinguishing master (index) and normal contours."""
    from qgis.core import QgsRuleBasedRenderer, QgsSymbol, QgsSimpleLineSymbolLayer

    master_modulo = interval * 5

    def _make_sym(width):
        sym = QgsSymbol.defaultSymbol(layer.geometryType())
        sym.deleteSymbolLayer(0)
        line = QgsSimpleLineSymbolLayer()
        line.setColor(color)
        line.setWidth(width)
        sym.appendSymbolLayer(line)
        return sym

    root = QgsRuleBasedRenderer.Rule(None)

    master = QgsRuleBasedRenderer.Rule(_make_sym(0.5))
    master.setLabel('Curva Mestra')
    master.setFilterExpression(f'"ELEV" % {master_modulo} = 0')
    root.appendChild(master)

    normal = QgsRuleBasedRenderer.Rule(_make_sym(0.25))
    normal.setLabel('Curva Normal')
    normal.setIsElse(True)
    root.appendChild(normal)

    layer.setRenderer(QgsRuleBasedRenderer(root))
