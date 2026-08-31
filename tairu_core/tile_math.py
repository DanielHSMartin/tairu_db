# -*- coding: utf-8 -*-

"""
Web Mercator tile math and polygon-to-tile-set computation, extracted from
tairu_db_algorithm.prepareAlgorithm during the 2.0 refactor.

All inputs/outputs are WGS84 (EPSG:4326); tile indices are XYZ (top-left
origin). The TMS flip happens only at save time, in the render engine.
"""

import math
from dataclasses import dataclass, field

from qgis.core import QgsRectangle, QgsGeometry, QgsCoordinateTransform


def to_wgs84(geom, src, wgs84, ctx):
    """Reprojeta `geom` para WGS84, ou levanta dizendo por que nao deu.

    O retorno de QgsGeometry.transform() era ignorado em TODOS os pontos que
    reprojetam (area do assistente, algoritmo de Processamento, exportacao
    vetorial). Com uma CRS de origem invalida o QgsCoordinateTransform nasce
    invalido, transform() devolve erro e a geometria segue INTACTA, em metros.
    Dali em diante metro e tratado como grau: o intervalo de tiles colapsa e o
    usuario recebe "nenhum tile intersecta a area" — ou, pior, na exportacao
    vetorial, feicoes gravadas no lugar errado, sem aviso nenhum.

    A checagem de faixa e o guarda de verdade: nao depende da versao do QGIS nem
    do nome do enum de retorno, e pega qualquer saida que nao esteja em graus.
    """
    if not src.isValid():
        raise ValueError(
            'A área selecionada não tem sistema de coordenadas válido — nem o '
            'canvas nem o projeto informaram um SRC. Defina o SRC do projeto '
            '(canto inferior direito da janela do QGIS) e tente de novo.')
    transform = QgsCoordinateTransform(src, wgs84, ctx)
    origem = src.authid() or src.description() or 'origem desconhecida'
    if not transform.isValid():
        raise ValueError(
            f'Não há transformação de {origem} para WGS84 (EPSG:4326) neste projeto.')
    geom.transform(transform)
    bb = geom.boundingBox()
    if (-180.0 <= bb.xMinimum() and bb.xMaximum() <= 180.0
            and -90.0 <= bb.yMinimum() and bb.yMaximum() <= 90.0):
        return geom
    raise ValueError(
        f'A área não foi reprojetada de {origem} para WGS84 — os valores '
        f'continuam fora de graus ({bb.toString(2)}). {_crs_hint(bb, origem)}')


def _crs_hint(bb, origem):
    """Palpite util sobre a CRS real, a partir da GRANDEZA das coordenadas.

    O caso que motivou isto: a origem declarada e EPSG:4326, entao a
    transformacao para WGS84 vira identidade e nao converte nada — mas os dados
    estao em metros. Sem esta dica a mensagem diz apenas "nao reprojetou", e o
    usuario nao tem como saber que o defeito esta na CRS DECLARADA do dado, nao
    no plugin.
    """
    x, y = abs(bb.xMinimum()), abs(bb.yMinimum())
    if x <= 180.0 and y <= 90.0:
        return 'Verifique o SRC do projeto e as transformações de datum.'
    if x < 20037509.0 and y < 20048967.0:
        provavel = 'Web Mercator (EPSG:3857)'
    elif x < 1000000.0:
        provavel = 'uma projeção UTM local'
    else:
        provavel = 'alguma projeção métrica'
    if origem.upper().endswith('4326'):
        return (f'Os valores têm a grandeza de {provavel}, mas a origem está '
                f'declarada como {origem} — nesse caso a conversão vira '
                'identidade e nada é reprojetado. Corrija o SRC declarado da '
                'camada/projeto (clique com o botão direito na camada → '
                'Propriedades → Fonte → SRC) e tente de novo.')
    return (f'Os valores têm a grandeza de {provavel}. Confirme se o SRC '
            f'declarado ({origem}) corresponde de fato aos dados.')


def lon2tilex(lon, n):
    return int((lon + 180.0) / 360.0 * n)


def lat2tiley(lat, n):
    lat_rad = math.radians(lat)
    return int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)


def tile_bounds_wgs84(tx, ty, n):
    """WGS84 bounding rectangle of XYZ tile (tx, ty) at a zoom with n = 2**zoom tiles per axis."""
    x1 = tx * 360.0 / n - 180.0
    y1 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    x2 = (tx + 1) * 360.0 / n - 180.0
    y2 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
    return QgsRectangle(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def bounds_ring_string(polygon_geom_wgs84):
    """Exterior-ring "lon lat, lon lat" string used in the regions table.

    For multipolygons all part rings are concatenated, matching the historical
    behavior of the Processing algorithm.
    """
    if polygon_geom_wgs84.isMultipart():
        all_points = []
        for poly in polygon_geom_wgs84.asMultiPolygon():
            if poly and poly[0]:
                all_points.extend(poly[0])
        if all_points:
            return ", ".join(f"{pt.x()} {pt.y()}" for pt in all_points)
    else:
        poly = polygon_geom_wgs84.asPolygon()
        if poly and poly[0]:
            ring = poly[0]
            return ", ".join(f"{pt.x()} {pt.y()}" for pt in ring)
    return None


def polygon_rings(polygon_geom_wgs84):
    """Todos os aneis do poligono como [[(lon, lat), ...], ...].

    Diferente de bounds_ring_string, que concatena as partes num anel so, aqui
    cada parte e cada buraco continua sendo um anel proprio — e o que a mascara
    do tile precisa para recortar um multipoligono ou uma ilha corretamente.
    """
    if polygon_geom_wgs84.isMultipart():
        parts = polygon_geom_wgs84.asMultiPolygon() or []
    else:
        poly = polygon_geom_wgs84.asPolygon()
        parts = [poly] if poly else []
    return [[(pt.x(), pt.y()) for pt in ring]
            for poly in parts for ring in poly if ring]


@dataclass
class RegionTilesResult:
    """Tiles intersecting each input polygon (region), plus aggregate info."""
    region_tiles: dict = field(default_factory=dict)   # region index -> list[(tx, ty)]
    region_edge_tiles: dict = field(default_factory=dict)  # region index -> set[(tx, ty)] na borda
    region_rings: dict = field(default_factory=dict)   # region index -> [[(lon, lat), ...], ...]
    filtered_tiles: list = field(default_factory=list)  # unique (tx, ty) across regions
    bounds_list: list = field(default_factory=list)     # one ring string per region
    wgs84_extent: QgsRectangle = field(default_factory=QgsRectangle)
    feature_count: int = 0

    @property
    def total_tiles(self):
        return len(self.filtered_tiles)


def compute_region_tiles(polygons_wgs84, max_zoom, feedback):
    """Compute the XYZ tile set intersecting each polygon at max_zoom.

    polygons_wgs84: list of QgsGeometry already transformed to EPSG:4326.
    Returns RegionTilesResult, or None when canceled via the feedback adapter.
    """
    result = RegionTilesResult()
    result.feature_count = len(polygons_wgs84)

    n = 2.0 ** max_zoom
    valid_polygons = []

    for idx, polygon_geom_wgs84 in enumerate(polygons_wgs84):
        if feedback.is_canceled():
            return None

        # Update progress for polygon processing
        if len(polygons_wgs84) > 1:
            feedback.set_progress(50 * idx / len(polygons_wgs84))  # Use first 50% for polygon processing

        if polygon_geom_wgs84 is None or polygon_geom_wgs84.isEmpty():
            feedback.report_error(f"Feature {idx} geometry is empty or invalid.")
            continue

        valid_polygons.append(polygon_geom_wgs84)

        # Store polygon coordinates for metadata - one region per feature
        ring_str = bounds_ring_string(polygon_geom_wgs84)
        if ring_str:
            result.bounds_list.append(ring_str)

        # Compute tile range for the polygon's bounding box
        bbox = polygon_geom_wgs84.boundingBox()
        tile_x_min = max(0, lon2tilex(bbox.xMinimum(), n))
        tile_x_max = min(int(n) - 1, lon2tilex(bbox.xMaximum(), n))
        tile_y_min = max(0, lat2tiley(bbox.yMaximum(), n))  # y_max is north
        tile_y_max = min(int(n) - 1, lat2tiley(bbox.yMinimum(), n))  # y_min is south

        region_tiles = set()
        edge_tiles = set()
        tile_count = 0
        total_tiles_to_check = (tile_x_max - tile_x_min + 1) * (tile_y_max - tile_y_min + 1)

        for tx in range(tile_x_min, tile_x_max + 1):
            for ty in range(tile_y_min, tile_y_max + 1):
                if feedback.is_canceled():
                    return None

                tile_count += 1
                if tile_count % 100 == 0:  # Update progress every 100 tiles
                    progress = 50 + (50 * tile_count / total_tiles_to_check) * (idx + 1) / len(polygons_wgs84)
                    feedback.set_progress(min(99, progress))

                tile_geom = QgsGeometry.fromRect(tile_bounds_wgs84(tx, ty, n))
                if polygon_geom_wgs84.intersects(tile_geom):
                    region_tiles.add((tx, ty))
                    # Tile de borda: entra no arquivo, mas com pedaco fora da
                    # regiao. E ele que o gerador mascara — o de dentro sai
                    # inteiro e sem recodificar.
                    if not polygon_geom_wgs84.contains(tile_geom):
                        edge_tiles.add((tx, ty))

        # Key by the DENSE valid-polygon position, not the enumerate idx: an empty
        # feature earlier in the list `continue`s without a region, so an enumerate
        # idx would leave a gap (e.g. keys {0, 2}) while bounds_list stays dense. The
        # Flutter reader maps tiles_region_$i by list position, so a gap orphans a
        # region's tiles. valid_polygons was just appended, so len-1 is this region's
        # index and matches bounds_list order. No-op when no feature was skipped.
        result.region_tiles[len(valid_polygons) - 1] = list(region_tiles)
        result.region_edge_tiles[len(valid_polygons) - 1] = edge_tiles
        result.region_rings[len(valid_polygons) - 1] = polygon_rings(polygon_geom_wgs84)

    # Calculate total tiles across all regions
    all_tiles = set()
    for region_tiles in result.region_tiles.values():
        all_tiles.update(region_tiles)
    result.filtered_tiles = list(all_tiles)

    # Union of all regions for center calculation
    if valid_polygons:
        union_bbox = valid_polygons[0].boundingBox()
        for geom in valid_polygons[1:]:
            union_bbox.combineExtentWith(geom.boundingBox())
        result.wgs84_extent = union_bbox

    return result
