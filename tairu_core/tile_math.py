# -*- coding: utf-8 -*-

"""
Web Mercator tile math and polygon-to-tile-set computation, extracted from
tairu_db_algorithm.prepareAlgorithm during the 2.0 refactor.

All inputs/outputs are WGS84 (EPSG:4326); tile indices are XYZ (top-left
origin). The TMS flip happens only at save time, in the render engine.
"""

import math
from dataclasses import dataclass, field

from qgis.core import QgsRectangle, QgsGeometry


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


@dataclass
class RegionTilesResult:
    """Tiles intersecting each input polygon (region), plus aggregate info."""
    region_tiles: dict = field(default_factory=dict)   # region index -> list[(tx, ty)]
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

        result.region_tiles[idx] = list(region_tiles)

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
