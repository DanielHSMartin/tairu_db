# -*- coding: utf-8 -*-

"""Contorno do dado válido de uma imagem, para usá-la como área de interesse.

`layer.extent()` é a CAIXA da imagem, não a imagem. Numa imagem recortada —
mosaico cortado por um rio, voo em faixa, qualquer coisa com alfa — a caixa
cobre muito mais chão do que existe pixel. Medido num mosaico de corredor
fluvial: 106.392 tiles em z18 pela caixa contra 5.949 pelo contorno real, 18x.

E o custo não para em tiles rasterizados à toa. Com a região virando um
RETÂNGULO, o estêncil de borda do gerador não tem o que recortar, e qualquer
outra camada desenhada (o mapa de fundo do projeto, outra imagem marcada)
preenche o vazio com conteúdo — o filtro de tile vazio deixa de descartar
qualquer coisa e o ARQUIVO cresce na mesma proporção. Foi assim que o defeito
apareceu: arquivo gigante, não geração lenta.
"""

import os

try:
    from osgeo import gdal, ogr
    gdal.UseExceptions()
    _GDAL_AVAILABLE = True
except ImportError:                                     # pragma: no cover
    _GDAL_AVAILABLE = False
    gdal = None
    ogr = None

from qgis.core import QgsGeometry

# Lado máximo do recorte usado para traçar o contorno. Um tile z18 tem ~150 m
# de lado; um contorno tirado de uma pirâmide de ~1500 px erra bem menos que
# isso, e erra sempre para MAIS — o polígono sai pela borda externa do pixel da
# máscara —, então nenhum tile com dado fica de fora.
_TARGET_MASK_SIDE = 1500

# Sem pirâmide, ler a máscara custa a imagem inteira. Um mosaico de 10 Gpx
# seguraria o assistente por minutos só para desenhar uma linha: acima deste
# teto devolvemos None e o chamador segue com a caixa, como antes.
_MAX_MASK_PIXELS = 64_000_000

# Máscara picotada (nuvem de respingos de 1 px) rende milhares de polígonos e a
# união fica cara. Acima disto não vale: cai na caixa.
_MAX_PARTS = 5000

# 0 continua 0; 1..255 viram 1. bytes.translate faz a binarização da máscara sem
# numpy — que este plugin não usa em lugar nenhum, e ReadAsArray exigiria.
_BINARIZE = bytes([0] + [1] * 255)

# A tabela de imagens do assistente remonta a CADA visita à página, uma linha por
# imagem: medido em 1,21 s para 13 imagens sem cache, e o usuário anda para frente
# e para trás no assistente. A chave leva mtime e tamanho, então arquivo
# regravado recalcula sozinho.
# ponytail: dicionário sem despejo — são poucas imagens por sessão; se algum dia
# crescer, trocar por functools.lru_cache num wrapper da chave.
_CACHE = {}


def _mask_level(mask_band, ds):
    """(banda a ler, largura, altura) da pirâmide mais grossa que serve.

    None quando só sobra a máscara em resolução cheia e ela passa do teto.
    """
    for i in range(mask_band.GetOverviewCount()):
        ov = mask_band.GetOverview(i)
        if max(ov.XSize, ov.YSize) <= _TARGET_MASK_SIDE:
            return ov, ov.XSize, ov.YSize
    if ds.RasterXSize * ds.RasterYSize <= _MAX_MASK_PIXELS:
        return mask_band, ds.RasterXSize, ds.RasterYSize
    return None, 0, 0


def _cache_key(source):
    """(caminho, mtime, tamanho) — None para fonte que não é arquivo em disco."""
    try:
        st = os.stat(source)
    except OSError:
        return None
    return (source, st.st_mtime_ns, st.st_size)


def data_footprint(layer):
    """Contorno do dado válido de `layer`, na CRS da camada. None = use a caixa.

    None quer dizer sempre a mesma coisa — "não há ganho, ou não vale o custo":
    imagem sem máscara (o caso comum, retangular), GDAL ausente, fonte que o
    GDAL não abre, máscara grande demais sem pirâmide, ou contorno picotado
    demais. O chamador segue com `layer.extent()` e nada muda para ele.

    Devolve sempre CÓPIA: `to_wgs84` reprojeta a geometria NO LUGAR, então
    entregar o objeto guardado no cache faria a segunda chamada receber um
    contorno já em graus — área errada na tabela e recorte no lugar errado.
    """
    if not _GDAL_AVAILABLE:
        return None
    source = layer.source() if hasattr(layer, 'source') else None
    if not source:
        return None
    key = _cache_key(source)
    if key is not None and key in _CACHE:
        hit = _CACHE[key]
        return QgsGeometry(hit) if hit is not None else None
    geom = _compute_footprint(source)
    if key is not None:
        _CACHE[key] = geom
    return QgsGeometry(geom) if geom is not None else None


def _compute_footprint(source):
    """O trabalho de verdade: contorno da máscara do arquivo `source`."""
    try:
        ds = gdal.Open(source)
    except RuntimeError:                    # fonte que o GDAL não abre sozinho
        return None
    if ds is None or ds.RasterCount < 1:
        return None
    band = ds.GetRasterBand(1)
    if band is None:
        return None
    # GMF_ALL_VALID: a imagem não tem alfa nem nodata, logo caixa == contorno.
    # Sair aqui é o que mantém a imagem retangular comum com o custo de antes.
    if band.GetMaskFlags() & gdal.GMF_ALL_VALID:
        return None
    mask = band.GetMaskBand()
    if mask is None:
        return None
    level, xs, ys = _mask_level(mask, ds)
    if level is None or xs < 1 or ys < 1:
        return None

    raw = level.ReadRaster(0, 0, xs, ys, xs, ys, gdal.GDT_Byte)
    if not raw:
        return None
    mem = gdal.GetDriverByName('MEM').Create('', xs, ys, 1, gdal.GDT_Byte)
    gt = ds.GetGeoTransform()
    # A pirâmide cobre a mesma extensão com menos pixels: o passo cresce na
    # razão dos tamanhos. Sem isto o contorno sai encolhido num canto.
    mem.SetGeoTransform((gt[0], gt[1] * ds.RasterXSize / xs, gt[2],
                         gt[3], gt[4], gt[5] * ds.RasterYSize / ys))
    mem.SetProjection(ds.GetProjection())
    mem_band = mem.GetRasterBand(1)
    mem_band.WriteRaster(0, 0, xs, ys, raw.translate(_BINARIZE))

    vds = ogr.GetDriverByName('Memory').CreateDataSource('footprint')
    out = vds.CreateLayer('f', srs=None)
    out.CreateField(ogr.FieldDefn('v', ogr.OFTInteger))
    # A própria banda binarizada como máscara: só o que vale 1 vira polígono.
    gdal.Polygonize(mem_band, mem_band, out, 0)
    if out.GetFeatureCount() > _MAX_PARTS:
        return None
    parts = []
    for feature in out:
        ref = feature.GetGeometryRef()
        if ref is None:
            continue
        geom = QgsGeometry.fromWkt(ref.ExportToWkt())
        if geom and not geom.isEmpty():
            parts.append(geom)
    if not parts:
        return None
    union = QgsGeometry.unaryUnion(parts)
    if union is None or union.isEmpty():
        return None
    return union


def coverage_ratio(layer, footprint):
    """Fração da caixa da imagem que tem pixel, 0..1. None quando não dá medir.

    É o número que faltava na tabela do assistente: ela mostrava "50,6 × 47,3
    km" para uma imagem em que 96% daquilo é vazio.
    """
    if footprint is None:
        return None
    extent = layer.extent()
    box = extent.width() * extent.height()
    if box <= 0:
        return None
    return min(1.0, footprint.area() / box)
