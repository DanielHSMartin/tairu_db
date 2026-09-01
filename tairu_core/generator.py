# -*- coding: utf-8 -*-

"""
Tile rendering engine for .tairudb generation, extracted from
tairu_db_algorithm during the 2.0 refactor so it can be driven both by the
Processing algorithm and by the dock widget's raster wizard.

IMPORTANT: TileRenderEngine drives QgsMapRendererSequentialJob through a nested
QEventLoop (not a busy processEvents() spin), so run() MUST be called from the
main (GUI) thread — the same constraint expressed by FlagNoThreading in the
Processing algorithm. Never call run() from a QgsTask worker thread.
"""

import contextlib
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from qgis.PyQt.QtCore import (
    Qt, QSize, QBuffer, QByteArray, QCoreApplication, QEventLoop, QTimer)
from qgis.PyQt.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsMessageLog,
    QgsCoordinateTransform,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsRectangle,
)

try:
    from ..compat import _OPEN_WRITE_ONLY, _FMT_ARGB32
    from .tairudb_writer import TairuDBWriter, MetaTile
    from .map_identity import map_uuid_for_output
    from .elevation_tiles import ELEVATION_ZOOM
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _OPEN_WRITE_ONLY, _FMT_ARGB32
    from tairu_core.tairudb_writer import TairuDBWriter, MetaTile
    from tairu_core.map_identity import map_uuid_for_output
    from tairu_core.elevation_tiles import ELEVATION_ZOOM

# Debug mode - set to True for detailed logging, False for production
DEBUG_MODE = False


@dataclass
class GenerationSpec:
    """Everything TileRenderEngine needs to produce a .tairudb file."""
    output_file: str
    layers: list                       # visible raster layers to render
    region_tiles: dict                 # region index -> list[(tx, ty)] (XYZ)
    filtered_tiles: list               # unique (tx, ty) across regions
    bounds_list: list                  # one "lon lat, ..." ring string per region
    wgs84_extent: QgsRectangle         # union bbox of all regions
    max_zoom: int
    tile_format: str = "JPG"           # PNG | JPG | WEBP
    jpg_quality: int = 90
    transform_context: object = None
    threads_number: int = 4
    dpi: int = 96
    tile_width: int = 256
    tile_height: int = 256
    filter_empty_tiles: bool = True
    region_edge_tiles: dict = field(default_factory=dict)  # region index -> set[(tx, ty)]
    region_rings: dict = field(default_factory=dict)       # region index -> [[(lon, lat), ...]]
    clip_to_region: bool = True                            # recortar o tile de borda pelo poligono
    antialias: bool = True
    include_attribution: bool = False
    attribution_text: str = "© TairuDB contributors"
    name: Optional[str] = None         # metadata name; defaults to output file basename


def encode_tile_bytes(image, fmt, quality=90):
    """QByteArray com a imagem no formato pedido, ou None se nao salvar."""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(_OPEN_WRITE_ONLY)
    fmt = (fmt or 'PNG').upper()
    if fmt == 'JPG':
        ok = image.save(buffer, 'JPG', quality)
    elif fmt == 'WEBP':
        ok = image.save(buffer, 'WEBP', quality)
    else:
        ok = image.save(buffer, 'PNG')
    buffer.close()
    if not ok or data.isEmpty():
        return None
    return data


@dataclass
class TileSample:
    """Medida real de alguns tiles: quanto pesam e quanto demoram."""
    fmt_kb: float = 0.0      # media no formato escolhido
    png_kb: float = 0.0      # media em PNG (borda e area sem imagem saem assim)
    sd_kb: float = 0.0       # desvio entre os tiles medidos
    secs: float = 0.0        # segundos por tile, uma thread
    count: int = 0
    blank: int = 0           # amostras que sairam SEM imagem nenhuma


def sample_tile_sizes(layers, tiles, max_zoom, tile_format, jpg_quality,
                      transform_context, dpi=96, tile_size=256,
                      antialias=True, samples=6):
    """Renderiza alguns tiles de verdade e mede peso e tempo.

    A estimativa antiga multiplicava o numero de tiles por uma tabela fixa de
    KB por formato, corrigida em linha reta pela qualidade. Sao dois chutes
    sobre a mesma coisa: o peso de um tile depende do CONTEUDO (ortofoto em
    zoom 19 nao pesa como satelite em zoom 15) e JPEG nao cresce em linha reta
    com a qualidade — de 90 para 100 ele quase dobra. No RJ4 isso deu 246 MB
    estimados contra 445 MB reais.

    Medir custa poucos segundos e responde as duas perguntas com a imagem, o
    zoom e a qualidade que o usuario escolheu de fato. Devolve None quando nao
    da para medir (sem camadas, sem tiles, tudo vazio) — ai vale a tabela.
    """
    if not layers or not tiles:
        return None

    ordered = sorted(tiles)
    step = max(1, len(ordered) // max(1, samples))
    picked = ordered[::step][:samples]

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    mercator = QgsCoordinateReferenceSystem("EPSG:3857")
    to_mercator = QgsCoordinateTransform(wgs84, mercator, transform_context)
    n = 2.0 ** max_zoom

    fmt_sizes, png_sizes, times = [], [], []
    vazios = 0
    for tx, ty in picked:
        x1 = tx * 360.0 / n - 180.0
        y1 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        x2 = (tx + 1) * 360.0 / n - 180.0
        y2 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
        try:
            p1 = to_mercator.transform(x1, y1)
            p2 = to_mercator.transform(x2, y2)
        except Exception as erro:
            # Nunca um `continue` mudo: o motivo do descarte vai para o log do
            # QGIS. Alem de ser a coisa certa, try/except/continue e acusado
            # pelo bandit do plugins.qgis.org (B112) e entra no relatorio de
            # seguranca do envio.
            QgsMessageLog.logMessage(
                f'Amostragem: tile {tx},{ty} nao reprojetou ({erro})',
                'TairuDB', Qgis.MessageLevel.Warning)
            continue

        settings = QgsMapSettings()
        settings.setLayers(layers)
        settings.setOutputDpi(dpi)
        settings.setOutputSize(QSize(tile_size, tile_size))
        settings.setExtent(QgsRectangle(min(p1.x(), p2.x()), min(p1.y(), p2.y()),
                                        max(p1.x(), p2.x()), max(p1.y(), p2.y())))
        settings.setDestinationCrs(mercator)
        settings.setBackgroundColor(QColor(Qt.GlobalColor.transparent))
        settings.setFlag(Qgis.MapSettingsFlag.Antialiasing, antialias)  # type: ignore
        settings.setFlag(Qgis.MapSettingsFlag.RenderMapTile, True)  # type: ignore

        started = time.time()
        job = QgsMapRendererSequentialJob(settings)
        job.start()
        job.waitForFinished()
        elapsed = time.time() - started
        image = job.renderedImage()
        # Tile sem NADA desenhado nao entra na media: ele nem chega ao arquivo
        # (o filtro de vazios o descarta) e puxaria peso e tempo para baixo.
        # Filtrar por tamanho em bytes, como antes, descartava tambem o tile
        # legitimamente liso — foi assim que a media de PNG saiu zerada no
        # primeiro teste.
        if image.isNull() or is_blank(image):
            vazios += 1
            continue

        times.append(elapsed)
        encoded = encode_tile_bytes(image, tile_format, jpg_quality)
        as_png = encode_tile_bytes(image, 'PNG')
        if encoded is not None:
            fmt_sizes.append(encoded.size() / 1024.0)
        if as_png is not None:
            png_sizes.append(as_png.size() / 1024.0)

    if not fmt_sizes:
        # Devolvido mesmo assim quando houve tile e ele saiu VAZIO: sem isto o
        # caso "nenhuma camada cobre a area" era indistinguivel de "nao deu para
        # medir", caia na tabela de KB e anunciava um tamanho plausivel para um
        # arquivo que sairia oco.
        return TileSample(blank=vazios) if vazios else None
    media = sum(fmt_sizes) / len(fmt_sizes)
    variancia = sum((v - media) ** 2 for v in fmt_sizes) / len(fmt_sizes)
    return TileSample(
        fmt_kb=media,
        png_kb=(sum(png_sizes) / len(png_sizes)) if png_sizes else 0.0,
        sd_kb=math.sqrt(variancia),
        secs=(sum(times) / len(times)) if times else 0.15,
        count=len(fmt_sizes),
    )


@dataclass
class EstimateResult:
    """Dry-run statistics for a prospective generation."""
    total_tiles: int = 0
    region_tile_counts: dict = field(default_factory=dict)
    fmt: str = "JPG"
    quality: int = 90
    avg_kb: float = 0.0
    avg_mb: float = 0.0
    lo_mb: float = 0.0
    hi_mb: float = 0.0
    secs: float = 0.0
    time_str: str = ""
    area_km2: float = 0.0
    res_label: str = ""
    max_zoom: int = 18
    threads_number: int = 4
    warnings: list = field(default_factory=list)
    stored_tiles: int = 0     # linhas gravadas: um tile entra em CADA regiao que o contem
    edge_tiles: int = 0       # dessas, as que saem em PNG por causa do recorte
    measured_from: int = 0    # quantos tiles foram renderizados para medir
    blank_samples: int = 0    # amostras que sairam sem imagem nenhuma


_ZOOM_TO_LABEL = {
    19: "Máxima (0,25 m/px) — zoom 19",
    18: "Altíssima (0,5 m/px) — zoom 18",
    17: "Alta (1 m/px) — zoom 17",
    16: "Médio Alta (2 m/px) — zoom 16",
    15: "Média (4 m/px) — zoom 15",
    14: "Médio Baixa (8 m/px) — zoom 14",
    13: "Baixa (16 m/px) — zoom 13",
    12: "Muito Baixa (32 m/px) — zoom 12",
}


def is_blank(image):
    """True se o tile nao tem NADA desenhado (todo transparente)."""
    if image.isNull() or not image.hasAlphaChannel():
        return False
    if image.format() not in (_FMT_ARGB32,
                              QImage.Format.Format_ARGB32_Premultiplied):
        image = image.convertToFormat(_FMT_ARGB32)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return max(bytes(bits)[3::4]) == 0


def has_transparency(image):
    """True se algum pixel do tile nao for totalmente opaco.

    Le a faixa de alfa de uma vez (byte 3 de cada pixel em ARGB32 little-endian)
    em vez de percorrer pixel a pixel em Python: sao 65 mil pixels por tile.
    """
    if image.isNull() or not image.hasAlphaChannel():
        return False
    # O renderizador entrega ARGB32_Premultiplied; o alfa esta no mesmo byte nos
    # dois formatos, entao converter so gastaria uma copia de 256 KB por tile.
    if image.format() not in (_FMT_ARGB32,
                              QImage.Format.Format_ARGB32_Premultiplied):
        image = image.convertToFormat(_FMT_ARGB32)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    data = bytes(bits)
    return min(data[3::4]) != 255


def _tile_pixel(lon, lat, tx, ty, n, width, height):
    """Ponto WGS84 -> pixel dentro do tile XYZ (tx, ty) de um zoom com n tiles por eixo."""
    x = (lon + 180.0) / 360.0 * n
    lat = max(-85.05112878, min(85.05112878, lat))
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n
    return ((x - tx) * width, (y - ty) * height)


def mask_tile_to_rings(tile_image, tx, ty, n, rings):
    """Copia de `tile_image` com tudo que esta FORA de `rings` apagado.

    So os tiles de borda passam por aqui: o interior sai inteiro, sem
    recodificar. O resultado tem alfa, entao quem grava precisa usar PNG — JPG
    nao guarda transparencia e devolveria a area recortada em preto.

    Aneis vem de tile_math.polygon_rings: cada parte de um multipoligono e cada
    buraco e um anel proprio, e a regra par-impar recorta os buracos.
    """
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    width, height = tile_image.width(), tile_image.height()
    for ring in rings:
        if len(ring) < 3:
            continue
        points = [_tile_pixel(lon, lat, tx, ty, n, width, height) for lon, lat in ring]
        path.moveTo(points[0][0], points[0][1])
        for px, py in points[1:]:
            path.lineTo(px, py)
        path.closeSubpath()
    if path.isEmpty():
        return None

    # Mascara de alfa desenhada a parte, e nao setClipPath: recorte por clip no
    # Qt e serrilhado. Composicao com uma IMAGEM do tamanho do tile, e nao com a
    # forma: DestinationIn so afeta o que o desenho cobre, entao desenhar a
    # figura direto deixaria o lado de fora intacto — foi exatamente o que o
    # teste pegou.
    stencil = QImage(tile_image.size(), _FMT_ARGB32)
    stencil.fill(Qt.GlobalColor.transparent)
    painter = QPainter(stencil)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0)))
        painter.drawPath(path)
    finally:
        painter.end()

    masked = tile_image.copy()
    painter = QPainter(masked)
    try:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, stencil)
    finally:
        painter.end()
    return masked


def estimate(region_result, max_zoom, tile_format, jpg_quality, threads_number,
             layers=None, transform_context=None, sample=None, dpi=96, tile_size=256):
    """Estimate tile count, file size and processing time without rendering.

    Com `layers`, alguns tiles sao renderizados de verdade para medir peso e
    tempo (ver sample_tile_sizes); sem eles, cai na tabela de KB por formato,
    que e so uma ordem de grandeza.
    """
    est = EstimateResult()
    est.total_tiles = region_result.total_tiles
    est.region_tile_counts = {rid: len(tiles) for rid, tiles in region_result.region_tiles.items()}
    # Um tile que cai em duas regioes e GRAVADO nas duas: o arquivo tem uma
    # linha por regiao, nao uma por tile. No RJ4 foram 10.191 linhas para 7.918
    # tiles distintos — 29% a mais de arquivo que a contagem de tiles sugere.
    est.stored_tiles = sum(est.region_tile_counts.values()) or est.total_tiles
    est.edge_tiles = sum(
        len(t) for t in getattr(region_result, 'region_edge_tiles', {}).values())
    est.max_zoom = max_zoom
    est.threads_number = threads_number
    est.quality = jpg_quality

    fmt = tile_format.upper()
    est.fmt = fmt

    if sample is None and layers:
        sample = sample_tile_sizes(
            layers, region_result.filtered_tiles, max_zoom, fmt, jpg_quality,
            transform_context, dpi=dpi, tile_size=tile_size)

    est.blank_samples = sample.blank if sample is not None else 0
    if est.blank_samples and (sample is None or sample.count == 0):
        est.warnings.append(
            'Os tiles de amostra saíram sem imagem nenhuma: a camada escolhida não '
            'cobre a área (ou não chegou a baixar). O arquivo sairia sem mapa.')

    if sample is not None and sample.count > 0:
        est.measured_from = sample.count
        # O tile de borda sai em PNG (o recorte e alfa); o resto, no formato
        # escolhido. As duas medidas vem da mesma renderizacao.
        png_share = (est.edge_tiles / est.stored_tiles) if est.stored_tiles else 0.0
        png_kb = sample.png_kb or sample.fmt_kb
        est.avg_kb = sample.fmt_kb * (1 - png_share) + png_kb * png_share
        # A faixa e a incerteza da MEDIA (erro padrao), nao o maior e o menor
        # tile medido: somando dez mil tiles, o total se concentra muito mais
        # que um tile sozinho. Minimo de 10% porque seis amostras nunca
        # garantem que a area inteira e como elas.
        erro = sample.sd_kb / math.sqrt(sample.count)
        rel = min(0.5, max(0.10, 2 * erro / max(0.001, sample.fmt_kb)))
        lo_kb = est.avg_kb * (1 - rel)
        hi_kb = est.avg_kb * (1 + rel)
        secs_per_tile = sample.secs
    else:
        # Sem medicao: tabela por formato, so como ordem de grandeza. A
        # correcao por qualidade NAO e linear — JPEG de 90 para 100 quase dobra.
        size_kb = {'PNG': 70, 'JPG': 28, 'WEBP': 20}
        min_kb = {'PNG': 20, 'JPG': 8, 'WEBP': 6}
        max_kb = {'PNG': 180, 'JPG': 70, 'WEBP': 50}
        factor = _quality_factor(jpg_quality) if fmt in ('JPG', 'WEBP') else 1.0
        est.avg_kb = max(1.0, size_kb.get(fmt, 28) * factor)
        lo_kb = max(1.0, min_kb.get(fmt, 8) * factor)
        hi_kb = max(1.0, max_kb.get(fmt, 70) * factor)
        secs_per_tile = 0.15

    # Sobrecarga do SQLite (paginas, indice): 1,8% no RJ4 e 2,8% no RJ2.
    overhead = 1.03
    est.avg_mb = est.stored_tiles * est.avg_kb / 1024 * overhead
    est.lo_mb = est.stored_tiles * lo_kb / 1024 * overhead
    est.hi_mb = est.stored_tiles * hi_kb / 1024 * overhead

    # O tempo e por tile RENDERIZADO: o mesmo tile gravado em duas regioes so
    # desenha uma vez.
    est.secs = est.total_tiles * secs_per_tile / max(1, threads_number)
    if est.secs < 60:
        est.time_str = f"~{est.secs:.0f} seg"
    elif est.secs < 3600:
        est.time_str = f"~{est.secs/60:.0f} min"
    else:
        est.time_str = f"~{est.secs/3600:.1f} h"

    # Coverage area from WGS84 bounding box
    bbox = region_result.wgs84_extent
    clat = math.radians((bbox.yMinimum() + bbox.yMaximum()) / 2)
    w_km = abs(bbox.xMaximum() - bbox.xMinimum()) * 111.32 * math.cos(clat)
    h_km = abs(bbox.yMaximum() - bbox.yMinimum()) * 110.574
    est.area_km2 = w_km * h_km

    est.res_label = _ZOOM_TO_LABEL.get(max_zoom, f"zoom {max_zoom}")

    if est.total_tiles > 10000:
        est.warnings.append("Mais de 10.000 tiles. Reduza a área ou use resolução menor.")
    elif est.total_tiles > 5000:
        est.warnings.append("Mais de 5.000 tiles. Verifique se área/resolução são adequadas.")
    if est.avg_mb > 1024:
        est.warnings.append("Estimativa acima de 1 GB. Pode impactar desempenho no dispositivo.")
    elif est.avg_mb > 500:
        est.warnings.append("Estimativa acima de 500 MB. Verifique o espaço no dispositivo.")

    return est


_QUALITY_CURVE = (
    (30, 0.28), (50, 0.42), (60, 0.50), (70, 0.60), (75, 0.66),
    (80, 0.75), (85, 0.85), (90, 1.00), (95, 1.35), (100, 2.20),
)


def _quality_factor(quality):
    """Peso do JPEG/WebP em relacao a qualidade 90, interpolado.

    A conta antiga era `qualidade / 90`, que da 1,11 para qualidade 100 — na
    pratica o arquivo quase DOBRA nesse trecho. Era metade do erro da
    estimativa do RJ4.
    """
    q = max(1, min(100, int(quality)))
    if q <= _QUALITY_CURVE[0][0]:
        return _QUALITY_CURVE[0][1]
    for (q0, f0), (q1, f1) in zip(_QUALITY_CURVE, _QUALITY_CURVE[1:]):
        if q <= q1:
            return f0 + (f1 - f0) * (q - q0) / (q1 - q0)
    return _QUALITY_CURVE[-1][1]


def _fmt_size(mb):
    return f"{mb/1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def format_estimate_report(est, feedback, num_vector_layers=0, vector_feature_count=0,
                           dry_run_footer=True, contour_enabled=False,
                           contour_source_label='', contour_interval=10,
                           contour_smoothing='Médio',
                           grg_enabled=False, grg_type_label='',
                           elevation_enabled=False, elevation_tiles=0,
                           elevation_mb=0.0):
    """Push the dry-run report through a feedback adapter."""
    num_regions = len(est.region_tile_counts)
    quality_str = f" (qualidade {est.quality})" if est.fmt in ('JPG', 'WEBP') else ""
    sep = "─" * 34

    def line(text=""):
        feedback.push_info(text)

    line()
    line("[ SIMULAÇÃO] Nenhum arquivo foi gerado")
    line("=" * 34)
    line()
    line("O ARQUIVO CONTERÁ")
    line(sep)
    line((f"  Tiles raster    : {est.total_tiles:,} tiles · zoom {est.max_zoom} · "
          f"{est.fmt}{quality_str} · ~{_fmt_size(est.avg_mb)}").replace(',', '.'))
    if num_vector_layers > 0:
        feat_s = 'ões' if vector_feature_count != 1 else 'ão'
        line((f"  Vetoriais       : {num_vector_layers} camada{'s' if num_vector_layers != 1 else ''} "
              f"· {vector_feature_count:,} feição{feat_s}").replace(',', '.'))
    if contour_enabled:
        line(f"  Curvas de nível : {contour_source_label} · {contour_interval} m · {contour_smoothing}")
    if elevation_enabled:
        line((f"  Altitude        : {elevation_tiles:,} tile(s) · zoom "
              f"{ELEVATION_ZOOM} · ~{_fmt_size(elevation_mb)}").replace(',', '.'))
    if grg_enabled:
        line(f"  Grade GRG       : {grg_type_label}")
    line()
    line("CONFIGURAÇÃO")
    line(sep)
    line(f"  Resolução  : {est.res_label}")
    line(f"  Formato    : {est.fmt}{quality_str}")
    line(f"  Regiões    : {num_regions} polígono{'s' if num_regions != 1 else ''}")
    line(f"  Área (bbox): {est.area_km2:.1f} km²")
    line()
    line("TILES RASTER")
    line(sep)
    line(f"  Total de tiles : {est.total_tiles:,}".replace(',', '.'))
    for rid, count in sorted(est.region_tile_counts.items()):
        line(f"    Região {rid + 1}: {count:,} tiles".replace(',', '.'))
    if est.stored_tiles > est.total_tiles:
        # Regiões que se sobrepõem gravam o mesmo tile em cada uma delas.
        line(f"  Gravados       : {est.stored_tiles:,} "
             "(tile em mais de uma região entra em cada)".replace(',', '.'))
    if est.edge_tiles:
        line(f"  Em PNG (borda) : {est.edge_tiles:,}".replace(',', '.'))
    line()
    total_mb = est.avg_mb + (elevation_mb if elevation_enabled else 0.0)
    line("TAMANHO ESTIMADO")
    line(sep)
    line(f"  Estimativa : {_fmt_size(total_mb)}  (~{est.avg_kb:.0f} KB/tile)")
    line(f"  Intervalo  : {_fmt_size(est.lo_mb)} – {_fmt_size(est.hi_mb)}")
    if est.measured_from:
        line(f"  Medido em {est.measured_from} tile(s) renderizados de verdade.")
    else:
        line("  Sem medição: tabela por formato, apenas ordem de grandeza.")
    line()
    line("TEMPO ESTIMADO")
    line(sep)
    line(f"  {est.threads_number} "
         f"thread{'s' if est.threads_number != 1 else ''} "
         f"paralela{'s' if est.threads_number != 1 else ''} : {est.time_str}")
    if est.measured_from:
        por_tile = f"{est.secs * est.threads_number / max(1, est.total_tiles):.2f}".replace('.', ',')
        line(f"  ({por_tile} s/tile medidos nesta máquina)")
    else:
        line("  (~0,15 s/tile em hardware típico)")
    if num_vector_layers > 0:
        line()
        line("CAMADAS VETORIAIS")
        line(sep)
        line(f"  Camadas  : {num_vector_layers}")
        line(f"  Feições  : {vector_feature_count:,}".replace(',', '.'))
    if contour_enabled:
        master_interval = contour_interval * 5
        line()
        line("CURVAS DE NÍVEL")
        line(sep)
        line(f"  Fonte       : {contour_source_label}")
        if 'INPE' in contour_source_label:
            line("  Cobertura   : Brasil (6°N–34°S, 75°W–34.5°W)")
        else:
            line("  Cobertura   : Global")
        line(f"  Intervalo   : {contour_interval} m  ·  Curvas mestras: {master_interval} m")
        line(f"  Suavização  : {contour_smoothing}")
        line("  Requer internet. Tiles DEM são salvos em cache localmente.")
        line("  Tempo de download não incluído nesta estimativa.")
    if elevation_enabled:
        line()
        line("ALTITUDE DO TERRENO")
        line(sep)
        line(f"  Tiles       : {elevation_tiles:,}".replace(',', '.'))
        line(f"  Tamanho     : ~{_fmt_size(elevation_mb)}  (~36 KB/tile)")
        line("  Resolução   : ~30 m  ·  1 tile cobre ~82 km²")
        line("  Fonte       : USGS (SRTM, GMTED2010, 3DEP) · domínio público")
        line("  Permite altitude e perfil de elevação no app sem internet.")
        line("  Requer internet AGORA, para baixar. Tempo não incluído acima.")
    line()

    if est.warnings:
        line("AVISOS")
        line(sep)
        for w in est.warnings:
            feedback.report_error(f"  ⚠  {w}", False)
        line()

    if dry_run_footer:
        line("Desmarque 'Dry Run' e execute novamente para gerar o arquivo.")
        line()


class TileRenderEngine:
    """Renders the tile set described by a GenerationSpec into a .tairudb file."""

    def __init__(self, spec, feedback):
        self.spec = spec
        self.feedback = feedback

        self.wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        self.mercator_crs = QgsCoordinateReferenceSystem("EPSG:3857")
        self.wgs2mercator = QgsCoordinateTransform(
            self.wgs84_crs, self.mercator_crs, spec.transform_context
        )

        self.writer: Optional[TairuDBWriter] = None
        self.error_message: Optional[str] = None
        self.canceled = False

        # Processing state
        self.total_tiles = len(spec.filtered_tiles)
        self.processed_tiles = 0
        self.failed_tiles = 0
        self.retried_tiles = 0
        self.skipped_blank_tiles = 0
        self.meta_tiles = []
        self.renderer_jobs = {}
        self.max_retries = 3
        self.retry_queue = []
        self.failed_tiles_info = []
        self._completion_reported = False
        self._event_loop = None
        self._render_t0 = None
        self._job_started = {}   # job -> perf timestamp, for per-tile render timing
        self._tick_count = 0     # event-loop heartbeats during render (responsiveness probe)

    def debug_log(self, message):
        """Log debug messages only if DEBUG_MODE is enabled"""
        if DEBUG_MODE:
            self.feedback.push_info(f"[DEBUG] {message}")

    # ------------------------------------------------------------------ run

    def run(self):
        """Render all tiles and write them to the output file.

        Returns True on success; on failure/cancellation returns False with
        either self.canceled set or self.error_message populated. The writer
        is left open on success so vector layers can still be exported —
        callers must invoke finalize() (or cleanup() on abort).
        """
        spec = self.spec

        self.writer = TairuDBWriter(spec.output_file)
        if not self.writer.create():
            self.error_message = f"Falha ao criar o arquivo GeoDB {spec.output_file}"
            return False

        if self.feedback.is_canceled():
            self.cleanup_resources()
            self.canceled = True
            return False

        self._write_metadata_and_regions()

        self.feedback.set_progress_text(
            f"Preparando para renderizar {len(spec.filtered_tiles)} tiles..."
        )

        self.meta_tiles = []
        self.processed_tiles = 0
        self.skipped_blank_tiles = 0

        z = spec.max_zoom
        n = 2.0 ** spec.max_zoom

        prep_t0 = time.time()
        for i, (tx, ty) in enumerate(spec.filtered_tiles):
            if self.feedback.is_canceled():
                self.cleanup_resources()
                self.canceled = True
                return False

            # Update progress during meta tile creation
            if i % 50 == 0:
                self.feedback.set_progress(10 + (20 * i / len(spec.filtered_tiles)))

            self.meta_tiles.append(self.create_individual_metatile(z, tx, ty, n))

        self.debug_log(
            f"{len(self.meta_tiles)} metatiles preparados em {time.time() - prep_t0:.1f}s")
        self.feedback.set_progress_text(f"Renderizando {len(self.meta_tiles)} tiles...")
        self.feedback.reset_progress()  # new phase: the render bar grows from 0
        self._render_t0 = time.time()

        # Start rendering jobs, then run a REAL nested event loop until every tile
        # completes. The render machinery is signal-driven (job.finished ->
        # process_metatile -> check_completion -> start_jobs), so the old
        # `while: processEvents()` busy-spin was pure waste: it pegged the main
        # thread at 100% CPU (the macOS spinner) and starved the render worker
        # threads. A QEventLoop dispatches the same finished signals but sleeps when
        # idle, keeping the window responsive and giving the CPU to rendering.
        self.start_jobs()
        if self.renderer_jobs or self.meta_tiles or self.retry_queue:
            self._event_loop = QEventLoop()
            tick = QTimer()
            tick.setInterval(100)
            tick.timeout.connect(self._on_render_tick)
            tick.start()
            try:
                self._event_loop.exec()
            finally:
                tick.stop()
                self._event_loop = None

        if self.feedback.is_canceled():
            self.cleanup_resources()
            self.canceled = True
            self.feedback.push_info("Operação cancelada pelo usuário")
            return False

        self._report_summary()
        return True

    def _quit_render_loop(self):
        loop = self._event_loop
        if loop is not None:
            loop.quit()

    def _on_render_tick(self):
        """Fires every 100ms WHILE the nested event loop is actually being serviced.
        It (1) proves responsiveness — if these stop, the main thread is hard-blocked;
        (2) shows the user a live heartbeat instead of a dead window; (3) honors cancel;
        (4) re-kicks scheduling if the loop ever goes idle with work still pending."""
        self._tick_count += 1
        if self.feedback.is_canceled():
            self._quit_render_loop()
            return
        elapsed = time.time() - (self._render_t0 or time.time())
        self.feedback.heartbeat(
            f"Renderizando… {self.processed_tiles}/{self.total_tiles} tiles  ·  {elapsed:.0f}s "
            "(aguardando o mapa base; na 1ª vez baixa os tiles da internet)")
        if not self.renderer_jobs:
            if self.meta_tiles or self.retry_queue:
                self.start_jobs()
            else:
                self._quit_render_loop()

    def finalize(self):
        """Commit, close and atomically publish the output file. Returns True on
        success (the writer no longer VACUUMs — see TairuDBWriter.finalize)."""
        if self.writer:
            return self.writer.finalize()
        return False

    # ----------------------------------------------------------- internals

    def _write_metadata_and_regions(self):
        spec = self.spec
        writer = self.writer

        format_lower = spec.tile_format.lower()
        writer.setMetadataValue("format", format_lower)
        base_name = spec.name or os.path.splitext(os.path.basename(spec.output_file))[0]
        writer.setMetadataValue("name", base_name)
        writer.setMetadataValue("description", base_name)
        writer.setMetadataValue("version", "1.2")
        # Stable identity across re-exports: lets the app recognise a newer
        # version of a map it already has WITHOUT trusting the file name, which
        # messengers rewrite ("mapa (1).tairudb") and which two unrelated maps
        # can legitimately share.
        writer.setMetadataValue("map_uuid", map_uuid_for_output(base_name))
        writer.setMetadataValue("type", "overlay")
        writer.setMetadataValue("minzoom", str(spec.max_zoom))
        writer.setMetadataValue("maxzoom", str(spec.max_zoom))

        for idx, bound_str in enumerate(spec.bounds_list):
            region_name = f"Região {idx + 1}"
            writer.insertRegion(
                region_name,
                spec.max_zoom,  # minzoom
                spec.max_zoom,  # maxzoom
                bound_str       # bounds
            )

        self.feedback.push_info(f"Regiões criadas na tabela de regiões: {len(spec.bounds_list)}")

        center_x = (spec.wgs84_extent.xMinimum() + spec.wgs84_extent.xMaximum()) / 2
        center_y = (spec.wgs84_extent.yMinimum() + spec.wgs84_extent.yMaximum()) / 2
        center_str = f"{center_x},{center_y},{spec.max_zoom}"
        writer.setMetadataValue("center", center_str)

        if spec.include_attribution and spec.attribution_text:
            writer.setMetadataValue("attribution", spec.attribution_text)

        writer.setMetadataValue("generator", "GeoPDB Generator")
        writer.setMetadataValue("created", datetime.now().isoformat())

    def _report_summary(self):
        self.feedback.set_progress(100)
        total_expected = len(self.spec.filtered_tiles)

        # Tile sem imagem nenhuma não é gravado. Até aqui isso era mudo e ainda
        # entrava na conta dos renderizados: desde que o fundo virou
        # transparente (2.0.20), TUDO o que a fonte não cobre — e todo tile de
        # mapa de fundo que não baixou — vira tile vazio, então um arquivo quase
        # oco terminava anunciando sucesso completo.
        vazios = self.skipped_blank_tiles
        if vazios >= total_expected > 0:
            self.feedback.report_error(
                f'Nenhum dos {total_expected} tiles recebeu imagem: o arquivo saiu sem '
                f'mapa. Verifique se a camada escolhida cobre a área e, se ela vem da '
                f'internet, se o download funcionou.')
        elif vazios:
            self.feedback.push_info(
                f'{vazios} de {total_expected} tiles ficaram sem imagem nenhuma e não '
                f'foram gravados; nessas partes o app mostra o próprio mapa de fundo.')

        # Clean, one-line summary on success; detail only when tiles actually failed.
        if self.failed_tiles == 0:
            self.feedback.push_info(f"{self.processed_tiles} tiles renderizados.")
            return

        success_rate = ((total_expected - self.failed_tiles) / total_expected * 100) if total_expected > 0 else 0
        self.feedback.push_info(
            f"{self.processed_tiles} tiles renderizados, {self.failed_tiles} falharam "
            f"({success_rate:.0f}% sucesso).")
        for fail_info in self.failed_tiles_info[:10]:
            self.feedback.push_info(
                f"  - Tile {fail_info['x']},{fail_info['y']}: {fail_info['reason']}")
        if len(self.failed_tiles_info) > 10:
            self.feedback.push_info(
                f"  … e mais {len(self.failed_tiles_info) - 10} tiles falhados.")

    def cleanup_resources(self):
        """Enhanced cleanup with better error handling"""
        try:
            self.debug_log("cleanup_resources: Iniciando limpeza")
            self.feedback.push_info("Limpando recursos...")

            # Cancel and cleanup renderer jobs AGGRESSIVELY
            jobs_count = len(self.renderer_jobs)
            self.debug_log(f"cleanup_resources: Cancelando {jobs_count} jobs")
            for job in list(self.renderer_jobs.keys()):
                # Ignore cleanup errors
                with contextlib.suppress(Exception):
                    job.finished.disconnect()
                    job.cancelWithoutBlocking()
                    job.deleteLater()
            self.renderer_jobs.clear()

            # Force process events to handle deleteLater() immediately
            if jobs_count > 0:
                for _ in range(10):
                    QCoreApplication.processEvents()

            # Clear tile queues COMPLETELY
            self.debug_log(f"cleanup_resources: Limpando {len(self.meta_tiles)} meta_tiles")
            self.meta_tiles.clear()
            self.debug_log(f"cleanup_resources: Limpando {len(self.retry_queue)} retry_queue")
            self.retry_queue.clear()

            # Close database connection with proper cleanup
            if self.writer and self.writer.conn:
                try:
                    self.debug_log("cleanup_resources: Fechando conexão do banco de dados")
                    # Try to commit any pending changes before closing
                    with contextlib.suppress(Exception):
                        self.writer.conn.commit()
                    with contextlib.suppress(Exception):
                        self.writer.conn.close()
                    self.writer.conn = None
                except Exception as e:
                    self.feedback.push_info(f"Aviso ao fechar banco de dados: {str(e)}")

            # Delete the partial work file so a canceled/failed run never leaves a
            # corrupt .part behind for the next attempt to merge into.
            if self.writer:
                with contextlib.suppress(Exception):
                    self.writer.discard()

            # Process more events to ensure cleanup is complete
            self.debug_log("cleanup_resources: Processando eventos finais")
            for _ in range(20):
                QCoreApplication.processEvents()

            self.debug_log("cleanup_resources: Limpeza concluída")

        except Exception as e:
            self.feedback.push_info(f"Aviso durante a limpeza: {str(e)}")

    def create_individual_metatile(self, z, tx, ty, n):
        try:
            x1 = tx * 360.0 / n - 180.0
            y1 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
            x2 = (tx + 1) * 360.0 / n - 180.0
            y2 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
            meta_tile = MetaTile()
            meta_tile.zoom = z
            meta_tile.tx = tx
            meta_tile.ty = ty
            meta_tile.metatile_size = 1
            meta_tile.actual_size_x = 1
            meta_tile.actual_size_y = 1
            meta_tile.retry_count = 0  # Track retry attempts

            # Add error handling for coordinate transformation
            if self.wgs2mercator:
                p1 = self.wgs2mercator.transform(x1, y1)
                p2 = self.wgs2mercator.transform(x2, y2)
                meta_tile.extent = QgsRectangle(
                    min(p1.x(), p2.x()),
                    min(p1.y(), p2.y()),
                    max(p1.x(), p2.x()),
                    max(p1.y(), p2.y())
                )
            else:
                # Fallback to WGS84 coordinates if transformation fails
                meta_tile.extent = QgsRectangle(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

            # Validate the created meta tile
            if not meta_tile.is_valid():
                self.feedback.push_info(f"Aviso: Metatile inválido criado para {tx},{ty}")

            return meta_tile
        except Exception as e:
            self.feedback.push_info(f"Erro ao criar metatile {tx},{ty}: {str(e)}")
            # Return a basic metatile to avoid complete failure
            meta_tile = MetaTile()
            meta_tile.zoom = z
            meta_tile.tx = tx
            meta_tile.ty = ty
            meta_tile.metatile_size = 1
            meta_tile.actual_size_x = 1
            meta_tile.actual_size_y = 1
            meta_tile.retry_count = 0
            meta_tile.extent = QgsRectangle(-180, -85, 180, 85)  # Fallback extent
            return meta_tile

    def start_jobs(self):
        """Start rendering jobs for pending tiles - optimized for speed"""
        spec = self.spec

        while (self.meta_tiles or self.retry_queue) and len(self.renderer_jobs) < spec.threads_number:
            # Prioritize retries over new tiles
            meta_tile = None
            if self.retry_queue:
                meta_tile = self.retry_queue.pop(0)
            elif self.meta_tiles:
                meta_tile = self.meta_tiles.pop(0)
            else:
                break

            if not meta_tile:
                continue

            # Validate meta tile before processing
            if hasattr(meta_tile, 'is_valid') and not meta_tile.is_valid():
                self.feedback.push_info(f"Pulando metatile inválido {meta_tile.tx},{meta_tile.ty}")
                continue

            try:
                size_x = meta_tile.actual_size_x if meta_tile.actual_size_x > 0 else meta_tile.metatile_size
                size_y = meta_tile.actual_size_y if meta_tile.actual_size_y > 0 else meta_tile.metatile_size
                actual_tile_width = spec.tile_width * size_x
                actual_tile_height = spec.tile_height * size_y

                if actual_tile_width <= 0 or actual_tile_height <= 0:
                    self.feedback.push_info(f"Tamanho de tile inválido para tile {meta_tile.tx},{meta_tile.ty}")
                    continue

                # Validate extent
                if meta_tile.extent.isEmpty() or not meta_tile.extent.isFinite():
                    self.feedback.push_info(f"Extensão inválida para tile {meta_tile.tx},{meta_tile.ty}")
                    continue

                # Create map settings with error checking
                map_settings = QgsMapSettings()

                if not spec.layers:
                    self.feedback.push_info("Nenhuma camada disponível para renderização")
                    continue

                map_settings.setLayers(spec.layers)
                map_settings.setOutputDpi(spec.dpi)
                map_settings.setOutputSize(QSize(actual_tile_width, actual_tile_height))
                map_settings.setExtent(meta_tile.extent)
                map_settings.setDestinationCrs(self.mercator_crs)
                # Fundo TRANSPARENTE, nao o branco opaco que QgsMapSettings traz de
                # fabrica: onde a imagem de origem nao cobre — borda da area, buraco
                # de nodata, tile XYZ que nao chegou a tempo — o tile saia branco
                # solido, e branco TAPA o mapa de fundo do app em vez de deixar ver
                # por baixo. Quem nao tem alfa e o JPG, e isso e resolvido na hora de
                # salvar: tile com transparencia sai em PNG.
                map_settings.setBackgroundColor(QColor(Qt.GlobalColor.transparent))
                map_settings.setFlag(Qgis.MapSettingsFlag.Antialiasing, spec.antialias)  # type: ignore

                # Additional map settings for better rendering
                map_settings.setFlag(Qgis.MapSettingsFlag.RenderMapTile, True)  # type: ignore
                map_settings.setFlag(Qgis.MapSettingsFlag.DrawLabeling, True)  # type: ignore
                # Disable for stability
                map_settings.setFlag(
                    Qgis.MapSettingsFlag.UseAdvancedEffects, False)  # type: ignore

                # Create and start job
                job = QgsMapRendererSequentialJob(map_settings)
                self.renderer_jobs[job] = meta_tile
                job.finished.connect(lambda job=job: self.process_metatile(job))  # type: ignore
                job.start()
                self._job_started[job] = time.time()

            except Exception as e:
                self.feedback.push_info(f"Erro ao iniciar trabalho para tile {meta_tile.tx},{meta_tile.ty}: {str(e)}")

                # Add to failed tiles if not already retrying
                if meta_tile.retry_count < self.max_retries:
                    meta_tile.retry_count += 1
                    self.retry_queue.append(meta_tile)
                else:
                    self.failed_tiles_info.append({
                        'x': meta_tile.tx,
                        'y': meta_tile.ty,
                        'zoom': meta_tile.zoom,
                        'reason': f'Erro ao iniciar trabalho: {str(e)}'
                    })
                    self.failed_tiles += 1

    def process_metatile(self, job):
        """Process completed rendering job - optimized for speed"""
        try:
            meta_tile = self.renderer_jobs.get(job)
            if not meta_tile:
                # Job was already cleaned up by cleanup_resources (cancel path).
                with contextlib.suppress(RuntimeError):
                    job.deleteLater()
                return

            # Per-tile render timing: this isolates whether the wall time is the map
            # render itself (e.g. slow online XYZ tile downloads like Google Hybrid)
            # rather than the plugin. Only slow ones are logged, to avoid spam.
            render_dt = time.time() - self._job_started.pop(job, time.time())
            if render_dt >= 1.0:
                self.debug_log(
                    f"render do tile {meta_tile.tx},{meta_tile.ty}: {render_dt:.1f}s")

            save_t0 = time.time()
            metatile_image = job.renderedImage()

            # Check for rendering failures and implement retry logic
            if metatile_image.isNull() or metatile_image.width() == 0 or metatile_image.height() == 0:
                # Add to retry queue if we haven't exceeded max retries
                if meta_tile.retry_count < self.max_retries:
                    meta_tile.retry_count += 1
                    self.retry_queue.append(meta_tile)
                    self.retried_tiles += 1
                    self.feedback.push_info(
                        f"Tentando tile novamente {meta_tile.tx},{meta_tile.ty} "
                        f"(tentativa {meta_tile.retry_count}/"
                        f"{self.max_retries})")
                else:
                    # Max retries reached — count as failed exactly once here (not on
                    # every attempt), so the success-rate report isn't corrupted.
                    self.failed_tiles += 1
                    self.failed_tiles_info.append({
                        'x': meta_tile.tx,
                        'y': meta_tile.ty,
                        'zoom': meta_tile.zoom,
                        'reason': 'Falha ao renderizar após tentativas máximas'
                    })
                    self.feedback.push_info(
                        f"Tile {meta_tile.tx},{meta_tile.ty} falhou após {self.max_retries} tentativas"
                    )

                del self.renderer_jobs[job]
                job.deleteLater()
                self.check_completion()
                return

            # Successfully rendered, process the tile
            self.save_metatile_data(meta_tile, metatile_image)
            save_dt = time.time() - save_t0
            if save_dt >= 1.0:
                self.debug_log(
                    f"gravação do tile {meta_tile.tx},{meta_tile.ty}: {save_dt:.1f}s")

        except Exception as e:
            # Handle any unexpected errors during tile processing
            self.feedback.push_info(f"Erro ao processar tile: {str(e)}")

            meta_tile = self.renderer_jobs.get(job)
            if meta_tile:
                self.failed_tiles_info.append({
                    'x': meta_tile.tx,
                    'y': meta_tile.ty,
                    'zoom': meta_tile.zoom,
                    'reason': f'Erro ao processar tile: {str(e)}'
                })
                self.failed_tiles += 1
        finally:
            # Cleanup job. Guard against the case where cleanup_resources()
            # already disconnected and deleted this job (cancel during render).
            if job in self.renderer_jobs:
                del self.renderer_jobs[job]
            # C++ object already deleted by cleanup_resources
            with contextlib.suppress(RuntimeError):
                job.deleteLater()

            self.check_completion()

    def save_metatile_data(self, meta_tile, metatile_image):
        """Save individual tiles from a metatile image - optimized for speed"""
        spec = self.spec
        size_x = meta_tile.actual_size_x if meta_tile.actual_size_x > 0 else meta_tile.metatile_size
        size_y = meta_tile.actual_size_y if meta_tile.actual_size_y > 0 else meta_tile.metatile_size
        max_tile_index = int(math.pow(2, meta_tile.zoom))

        for i in range(size_x):
            if self.feedback.is_canceled():
                return

            tile_x = meta_tile.tx + i
            if tile_x >= max_tile_index:
                continue

            for j in range(size_y):
                if self.feedback.is_canceled():
                    return

                tile_y = meta_tile.ty + j
                if tile_y >= max_tile_index:
                    continue

                # Extract tile from metatile
                x_offset = i * spec.tile_width
                y_offset = j * spec.tile_height

                # Validate offsets
                if (x_offset >= metatile_image.width() or
                        y_offset >= metatile_image.height() or
                        x_offset + spec.tile_width > metatile_image.width() or
                        y_offset + spec.tile_height > metatile_image.height()):
                    continue

                tile_image = metatile_image.copy(x_offset, y_offset, spec.tile_width, spec.tile_height)

                # Improved empty tile detection
                if spec.filter_empty_tiles and self.is_tile_empty(tile_image):
                    # Contado à parte: some no relatório senão, e um arquivo que
                    # saiu quase todo vazio anuncia sucesso completo.
                    self.skipped_blank_tiles += 1
                    self.processed_tiles += 1
                    continue

                # Convert and save tile
                if self.convert_and_save_tile(tile_image, meta_tile, tile_x, tile_y, max_tile_index):
                    self.processed_tiles += 1
                else:
                    # Failed to save tile
                    self.failed_tiles_info.append({
                        'x': tile_x,
                        'y': tile_y,
                        'zoom': meta_tile.zoom,
                        'reason': 'Falha ao converter ou salvar dados do tile'
                    })
                    self.failed_tiles += 1

    def is_tile_empty(self, tile_image):
        """Tile sem NADA desenhado — o único que pode ser descartado.

        Era uma amostra de CINCO pixels, e ela decidia por igualdade entre eles:
        um tile de cor uniforme SEM canal alfa caía no `return True` do fim e era
        jogado fora inteiro, e uma estrada fina que não passasse por nenhum dos
        cinco pontos levava o tile junto. `is_blank` lê a faixa de alfa de uma
        vez (a mesma leitura que `has_transparency` já faz em cada tile gravado),
        então é exato e não custa mais nada.
        """
        return tile_image.isNull() or is_blank(tile_image)

    def _format_for(self, tile_image):
        """Formato de gravacao deste tile.

        Tile com qualquer transparencia sai em PNG, mesmo num arquivo JPG: o
        recorte da borda e a area sem imagem de origem SAO alfa, e salvar isso em
        JPEG devolveria preto. O resto sai no formato escolhido — no arquivo RJ2
        isso foi 22 tiles de 700, uns 800 KB num arquivo de 24 MB.
        """
        if has_transparency(tile_image):
            return "PNG"
        return self.spec.tile_format

    def _encode_tile(self, tile_image, fmt=None):
        """Tile codificado em `fmt` (ou no formato escolhido). None se falhar."""
        spec = self.spec
        tile_data = QByteArray()
        buffer = QBuffer(tile_data)
        buffer.open(_OPEN_WRITE_ONLY)
        fmt = fmt or spec.tile_format

        success = False
        if fmt == "PNG":
            success = tile_image.save(buffer, "PNG")
        elif fmt == "JPG":
            success = tile_image.save(buffer, "JPG", spec.jpg_quality)
        elif fmt == "WEBP":
            if b'WEBP' in QImage.supportedImageFormats():
                success = tile_image.save(buffer, "WEBP", spec.jpg_quality)
            else:
                success = tile_image.save(buffer, "PNG")
                self.feedback.push_info("WebP não suportado, usando PNG em vez disso")
        buffer.close()

        if not success or tile_data.isEmpty():
            return None
        return tile_data

    def convert_and_save_tile(self, tile_image, meta_tile, tile_x, tile_y, max_tile_index):
        """Convert tile image to specified format and save to database"""
        spec = self.spec
        try:
            # Convert to TMS Y coordinate
            tms_y = max_tile_index - 1 - tile_y

            # Save to database for all regions that contain this tile
            tile_coord = (tile_x, tile_y)
            saved_to_regions = 0

            # Check which regions should contain this tile
            containing_regions = []
            for region_id, region_tiles in spec.region_tiles.items():
                if tile_coord in region_tiles:
                    containing_regions.append(region_id)

            if not containing_regions:
                self.feedback.push_info(f"Aviso: Tile {tile_x},{tile_y} não foi encontrado em nenhuma região")

            interior_data = None
            for region_id in containing_regions:
                tile_data = None
                # Tile de borda: recorta pelo poligono da regiao, para o arquivo
                # nao carregar imagem alem da area pedida. O app nativo fazia
                # isso na importacao e a web nao fazia nunca — daqui em diante o
                # arquivo ja chega recortado para os dois.
                if (spec.clip_to_region
                        and tile_coord in spec.region_edge_tiles.get(region_id, ())):
                    rings = spec.region_rings.get(region_id)
                    if rings:
                        masked = mask_tile_to_rings(
                            tile_image, tile_x, tile_y, max_tile_index, rings)
                        if masked is not None:
                            tile_data = self._encode_tile(
                                masked, self._format_for(masked))

                if tile_data is None:
                    if interior_data is None:
                        interior_data = self._encode_tile(
                            tile_image, self._format_for(tile_image))
                        if interior_data is None:
                            return False
                    tile_data = interior_data

                if self.writer and self.writer.saveTile(meta_tile.zoom, tile_x, tms_y, tile_data, region_id):
                    saved_to_regions += 1
                else:
                    self.feedback.push_info(f"Falha ao salvar tile {tile_x},{tile_y} na região {region_id}")

            # Periodic commit every 100 tiles for data integrity
            if saved_to_regions > 0 and self.processed_tiles % 100 == 0:
                if self.writer:
                    self.writer.periodicCommit()

            return saved_to_regions > 0

        except Exception as e:
            self.feedback.push_info(f"Erro ao converter/salvar tile {tile_x},{tile_y}: {str(e)}")
            return False

    def check_completion(self):
        """Check if processing is complete and handle retries - optimized for speed"""
        # Update progress
        if self.total_tiles > 0:
            progress = min(99, 100.0 * self.processed_tiles / self.total_tiles)
            self.feedback.set_progress(progress)

        # Process retry queue
        if self.retry_queue and len(self.renderer_jobs) < self.spec.threads_number:
            self.debug_log(f"check_completion: Processando retry - {len(self.retry_queue)} na fila")
            retry_tile = self.retry_queue.pop(0)
            self.meta_tiles.insert(0, retry_tile)  # Priority to retries
            self.start_jobs()
            return

        # Start new jobs if available
        if self.meta_tiles:
            self.debug_log(f"check_completion: Iniciando novos jobs - {len(self.meta_tiles)} tiles aguardando")
            self.start_jobs()
        elif not self.renderer_jobs and not self.retry_queue:
            # All processing complete - only report once
            if not self._completion_reported:
                self._completion_reported = True
                total_dt = time.time() - (self._render_t0 or time.time())
                self.debug_log(
                    f"render de {self.total_tiles} tiles em {total_dt:.1f}s "
                    f"(~{total_dt / max(1, self.total_tiles):.2f}s/tile)")
                # Responsiveness probe (DEBUG): with a 100ms tick, a responsive event
                # loop yields ~total_dt/0.1 ticks. Far fewer ⇒ the main thread was
                # hard-blocked (the render/download does not yield), which no event
                # loop can fix — the lever then is metatiling / an offline base layer.
                self.debug_log(
                    f"heartbeats do loop: {self._tick_count} "
                    f"(esperado ~{int(total_dt / 0.1)} se responsivo)")
                # The user-facing render summary is _report_summary() (called from
                # run() after the loop); keep this block quiet to avoid duplicate lines.
            # Rendering is done — release run()'s nested event loop.
            self._quit_render_loop()
