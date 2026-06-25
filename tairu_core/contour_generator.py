# -*- coding: utf-8 -*-
"""
Contour line generation from DEM data sources for inclusion in .tairudb files.
Logic adapted from CurvaDeNivel (github.com/DanielHSMartin/CurvaDeNivel).
"""

import math
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
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

from qgis.core import QgsVectorLayer

SOURCE_INPE = 0
SOURCE_COPERNICUS = 1

SMOOTHING_NONE = 'Nenhum'
SMOOTHING_LOW = 'Baixo'
SMOOTHING_MEDIUM = 'Médio'
SMOOTHING_HIGH = 'Alto'

_INPE_BASE_URL = 'http://www.dsr.inpe.br/topodata/data/geotiff/'
_COPERNICUS_BASE_URL = 'https://copernicus-dem-30m.s3.amazonaws.com/'


class ContourError(Exception):
    pass


def generate_contours(bbox_wgs84, dem_source, interval, smoothing, color, feedback):
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

    Returns:
        QgsVectorLayer with contour lines and RuleBasedRenderer applied.

    Raises:
        ContourError on any failure.
    """
    if not _GDAL_AVAILABLE:
        raise ContourError(
            'GDAL não está disponível. '
            'Instale o pacote GDAL/osgeo para gerar curvas de nível.')

    run_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    temp_dir = os.path.join(tempfile.gettempdir(), 'TairuDB_Curvas', run_id)
    os.makedirs(temp_dir, exist_ok=True)

    try:
        feedback.push_info('Baixando tiles de elevação…')
        tile_paths = _download_tiles(bbox_wgs84, dem_source, temp_dir, feedback)
        if not tile_paths:
            if dem_source == SOURCE_INPE:
                raise ContourError(
                    'Nenhum tile INPE TOPODATA baixado com sucesso. '
                    'O servidor pode estar indisponível, ou a área está fora da cobertura do Brasil '
                    '(6°N–34°S, 75°W–34.5°W). '
                    'Tente usar "Copernicus GLO-30 (Mundial)" como fonte de dados.')
            else:
                raise ContourError(
                    'Nenhum tile Copernicus GLO-30 baixado com sucesso. '
                    'Verifique a conexão com a internet e tente novamente.')

        if feedback.is_canceled():
            raise ContourError('Cancelado pelo usuário.')

        feedback.push_info(f'Recortando {len(tile_paths)} tile(s) para a área de interesse…')
        clipped = _clip_tiles(tile_paths, bbox_wgs84, temp_dir, feedback)
        if not clipped:
            raise ContourError(
                'Nenhum tile de elevação intersecta a área selecionada após recorte.')

        feedback.push_info('Mesclando tiles…')
        merged_path = os.path.join(temp_dir, 'merged.tif')
        _merge_tiles(clipped, merged_path)

        if feedback.is_canceled():
            raise ContourError('Cancelado pelo usuário.')

        if smoothing != SMOOTHING_NONE:
            feedback.push_info(f'Suavizando terreno ({smoothing})…')
            try:
                _smooth_terrain(merged_path, smoothing, temp_dir)
            except Exception as exc:
                feedback.push_info(f'Aviso: suavização falhou ({exc}), usando terreno original.')

        if feedback.is_canceled():
            raise ContourError('Cancelado pelo usuário.')

        feedback.push_info(f'Gerando curvas de nível (intervalo: {interval} m)…')
        contour_path = os.path.join(temp_dir, 'contours.gpkg')
        _run_contour_generate(merged_path, interval, contour_path, feedback)

        if feedback.is_canceled():
            raise ContourError('Cancelado pelo usuário.')

        layer = QgsVectorLayer(
            contour_path + '|layername=contours', 'Curvas de Nível', 'ogr')
        if not layer.isValid():
            raise ContourError(
                f'Falha ao carregar a camada de curvas de nível: {contour_path}')

        if layer.featureCount() == 0:
            feedback.push_info(
                'Aviso: nenhuma curva gerada (terreno plano ou intervalo muito grande?).')

        _apply_renderer(layer, interval, color)
        feedback.push_info('Curvas de nível geradas com sucesso.')
        return layer

    except ContourError:
        raise
    except Exception as exc:
        raise ContourError(f'Erro ao gerar curvas de nível: {exc}') from exc


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
        feedback.push_info(
            '  Nenhum tile INPE cobre a área selecionada. '
            'Verifique se a área está no Brasil (6°N–34°S, 75°W–34.5°W).')
        return []

    n_total = len(tiles_to_fetch)
    feedback.push_info(f'  {n_total} tile(s) INPE TOPODATA necessário(s).')

    tile_paths = []
    n_cached = n_downloaded = n_failed = 0
    for lat_norte, lon_oeste in tiles_to_fetch:
        if feedback.is_canceled():
            break
        nome = _inpe_tile_name(lat_norte, lon_oeste)
        was_cached = os.path.exists(os.path.join(cache_dir, nome + '.tif'))
        path = _fetch_inpe_tile(lat_norte, lon_oeste, cache_dir, feedback)
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
        parts.append(f'{n_downloaded} baixado(s)')
    if n_cached:
        parts.append(f'{n_cached} do cache')
    if n_failed:
        parts.append(f'{n_failed} falhou — verifique a conexão ou use Copernicus GLO-30')
    feedback.push_info(f'  Resultado: {", ".join(parts)}.')
    return tile_paths


def _fetch_inpe_tile(lat_norte, lon_oeste, cache_dir, feedback):
    nome = _inpe_tile_name(lat_norte, lon_oeste)
    fn = nome + '.zip'
    tif_path = os.path.join(cache_dir, nome + '.tif')
    zip_path = os.path.join(cache_dir, fn)

    if os.path.exists(tif_path):
        return tif_path

    url = _INPE_BASE_URL + fn
    feedback.push_info(f'  Baixando {fn}…')
    try:
        urllib.request.urlretrieve(url, zip_path)  # nosec B310
    except Exception as exc:
        feedback.push_info(f'  Falha: {fn}: {exc}')
        return None

    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            for member in z.namelist():
                if member.lower().endswith('.tif'):
                    z.extract(member, cache_dir)
                    extracted = os.path.join(cache_dir, member)
                    if os.path.abspath(extracted) != os.path.abspath(tif_path):
                        os.rename(extracted, tif_path)
                    break
    except Exception as exc:
        feedback.push_info(f'  Falha ao extrair {fn}: {exc}')
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
    feedback.push_info(f'  {n_total} tile(s) Copernicus GLO-30 necessário(s).')

    tile_paths = []
    n_cached = n_downloaded = n_failed = 0
    for lat, lon in tiles_to_fetch:
        if feedback.is_canceled():
            break
        lat_str = f'N{lat:02d}' if lat >= 0 else f'S{abs(lat):02d}'
        lon_str = f'E{lon:03d}' if lon >= 0 else f'W{abs(lon):03d}'
        fn = f'Copernicus_DSM_COG_10_{lat_str}_00_{lon_str}_00_DEM.tif'
        was_cached = os.path.exists(os.path.join(cache_dir, fn))
        path = _fetch_copernicus_tile(lat, lon, cache_dir, feedback)
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
        parts.append(f'{n_downloaded} baixado(s)')
    if n_cached:
        parts.append(f'{n_cached} do cache')
    if n_failed:
        parts.append(f'{n_failed} falhou')
    feedback.push_info(f'  Resultado: {", ".join(parts)}.')
    return tile_paths


def _fetch_copernicus_tile(lat, lon, cache_dir, feedback):
    lat_str = f'N{lat:02d}' if lat >= 0 else f'S{abs(lat):02d}'
    lon_str = f'E{lon:03d}' if lon >= 0 else f'W{abs(lon):03d}'
    name = f'Copernicus_DSM_COG_10_{lat_str}_00_{lon_str}_00_DEM'
    fn = name + '.tif'
    tif_path = os.path.join(cache_dir, fn)

    if os.path.exists(tif_path):
        return tif_path

    url = _COPERNICUS_BASE_URL + name + '/' + fn
    feedback.push_info(f'  Baixando {fn}…')
    try:
        urllib.request.urlretrieve(url, tif_path)  # nosec B310
    except Exception as exc:
        feedback.push_info(f'  Falha: {fn}: {exc}')
        return None

    return tif_path if os.path.exists(tif_path) else None


# ---------------------------------------------------------------------------- processing

def _clip_tiles(tile_paths, bbox_wgs84, temp_dir, feedback):
    clipped = []
    for i, tp in enumerate(tile_paths):
        out = os.path.join(temp_dir, f'clip_{i}.tif')
        try:
            ds = gdal.Open(tp)
            if ds is None:
                continue
            nodata = ds.GetRasterBand(1).GetNoDataValue()
            ds = None

            opts = gdal.WarpOptions(
                outputBounds=(
                    bbox_wgs84.xMinimum(), bbox_wgs84.yMinimum(),
                    bbox_wgs84.xMaximum(), bbox_wgs84.yMaximum()),
                srcSRS='EPSG:4326', dstSRS='EPSG:4326',
                format='GTiff',
                srcNodata=nodata,
                dstNodata=nodata if nodata is not None else -32768,
            )
            gdal.Warp(out, tp, options=opts)

            ds = gdal.Open(out)
            if ds and ds.RasterXSize > 0 and ds.RasterYSize > 0:
                clipped.append(out)
            ds = None
        except Exception as exc:
            feedback.push_info(
                f'  Aviso: falha ao recortar {os.path.basename(tp)}: {exc}')
    return clipped


def _merge_tiles(clipped_paths, merged_path):
    opts = gdal.WarpOptions(format='GTiff', srcSRS='EPSG:4326', dstSRS='EPSG:4326')
    gdal.Warp(merged_path, clipped_paths, options=opts)


def _smooth_terrain(merged_path, smoothing, temp_dir):
    """Apply a uniform box blur via VRT KernelFilteredSource (GDAL built-in)."""
    kernel_size_map = {SMOOTHING_LOW: 3, SMOOTHING_MEDIUM: 5, SMOOTHING_HIGH: 9}
    sz = kernel_size_map.get(smoothing, 5)
    coefs = ' '.join(['1'] * (sz * sz))

    vrt_path = os.path.join(temp_dir, 'blur.vrt')
    gdal.BuildVRT(vrt_path, [merged_path])

    with open(vrt_path, 'r', encoding='utf-8') as f:
        vrt_xml = f.read()

    # Extract the key elements from the generated SimpleSource
    src_fn_match = re.search(r'<SourceFilename[^>]*>(.*?)</SourceFilename>', vrt_xml, re.DOTALL)
    src_fn = src_fn_match.group(1).strip() if src_fn_match else os.path.abspath(merged_path)
    src_band_match = re.search(r'<SourceBand>(.*?)</SourceBand>', vrt_xml)
    src_band = src_band_match.group(1).strip() if src_band_match else '1'
    src_rect_match = re.search(r'<SrcRect[^/]*/>', vrt_xml)
    src_rect = src_rect_match.group(0) if src_rect_match else ''
    dst_rect_match = re.search(r'<DstRect[^/]*/>', vrt_xml)
    dst_rect = dst_rect_match.group(0) if dst_rect_match else ''

    kernel_block = (
        '<KernelFilteredSource>\n'
        f'      <SourceFilename relativeToVRT="0">{src_fn}</SourceFilename>\n'
        f'      <SourceBand>{src_band}</SourceBand>\n'
        f'      {src_rect}\n'
        f'      {dst_rect}\n'
        f'      <Kernel normalized="1"><Size>{sz}</Size>'
        f'<Coefs>{coefs}</Coefs></Kernel>\n'
        '    </KernelFilteredSource>'
    )
    vrt_xml = re.sub(
        r'<SimpleSource>.*?</SimpleSource>', kernel_block, vrt_xml, flags=re.DOTALL)

    with open(vrt_path, 'w', encoding='utf-8') as f:
        f.write(vrt_xml)

    smooth_path = os.path.join(temp_dir, 'smooth.tif')
    gdal.Translate(smooth_path, vrt_path, format='GTiff')
    shutil.copy2(smooth_path, merged_path)


def _run_contour_generate(merged_path, interval, contour_path, feedback):
    ds_raster = gdal.Open(merged_path)
    if ds_raster is None:
        raise ContourError(f'Não foi possível abrir o DEM mesclado: {merged_path}')

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
        raise ContourError(f'gdal.ContourGenerate falhou com código {result}')


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
