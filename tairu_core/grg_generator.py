# -*- coding: utf-8 -*-

"""
GRG (Grid Reference Graphic) generator for .tairudb files.

Two grid types:
  - 'alphanumeric': columns A/B/C…, rows 1/2/3…; spacing chosen in metres.
    Boundary lines are unlabeled; cell-centre label points (grg_label_col /
    grg_label_row) are stored separately so Flutter can place HUD labels at
    the centre of each visible column/row strip.
  - 'geographic': grid lines at round degree/minute intervals in WGS84.
    Label stores the raw decimal degree value (e.g. "-46.630000") so the
    Flutter app can reformat it according to the map's coordinate-format setting.

Legacy types ('utm', 'dms') are still handled for backward compatibility with
existing .tairudb files already in the field.

Grid config is stored in metadata['grg_config'] as JSON.
"""

import json
import math
import string
import uuid

from qgis.core import QgsRectangle


_GRG_LAYER_NAME = '__grg__'
_ICON_LINE_ROW = 'grg_line_row'
_ICON_LINE_COL = 'grg_line_col'
_ICON_LABEL_ROW = 'grg_label_row'
_ICON_LABEL_COL = 'grg_label_col'


def _col_label(index: int) -> str:
    """Map 0→A, 1→B, …, 25→Z, 26→AA, 27→AB, …"""
    result = ''
    n = index
    while True:
        result = string.ascii_uppercase[n % 26] + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


def _dms_label(degrees: float, is_lat: bool) -> str:
    """Format a decimal degree value as a compact DMS string."""
    hemi = ('N' if degrees >= 0 else 'S') if is_lat else ('L' if degrees >= 0 else 'O')
    d = abs(degrees)
    deg = int(d)
    m_float = (d - deg) * 60
    minute = int(m_float)
    sec = round((m_float - minute) * 60)
    if sec == 60:
        minute += 1
        sec = 0
    if minute == 60:
        deg += 1
        minute = 0
    if sec == 0:
        return f"{deg}°{minute:02d}'{hemi}"
    return f"{deg}°{minute:02d}'{sec:02d}\"{hemi}"


class GrgGenerator:
    """Generates GRG grid data and writes it into an open TairuDBWriter."""

    def __init__(self, writer, bounds: QgsRectangle, grid_type: str, options: dict):
        """
        writer   : open TairuDBWriter (after create(), before finalize())
        bounds   : QgsRectangle in EPSG:4326
        grid_type: 'alphanumeric' | 'geographic' (legacy: 'utm' | 'dms')
        options  : dict — see generate() docstring for keys
        """
        self.writer = writer
        self.bounds = bounds
        self.grid_type = grid_type
        self.options = options

    def generate(self):
        """Write GRG layer and config into the writer.

        options keys (all optional, with defaults):
          alphanumeric:
            spacing_m (float, default 500): cell size in metres
          geographic:
            spacing_deg (float, default 0.01): interval in decimal degrees
          shared:
            line_color (str, default '#000000'): hex color #RRGGBB
            line_opacity (float 0-1, default 0.8)
            line_width (int, default 2): pixels
            line_style (str, default 'solid'): 'solid'|'dashed'|'dotted'|'dotdash'
            font_color (str, default '#FFFFFF')
            font_size (int, default 14)
        """
        layer_uuid = str(uuid.uuid4())
        self.writer.insertVectorLayer(layer_uuid, 'line', _GRG_LAYER_NAME, '')

        color = self.options.get('line_color', '#000000')
        size = int(self.options.get('line_width', 2))
        attr_base = {
            'grid_type': self.grid_type,
            'opacity': self.options.get('line_opacity', 0.8),
            'line_style': self.options.get('line_style', 'solid'),
            'font_color': self.options.get('font_color', '#FFFFFF'),
            'font_size': int(self.options.get('font_size', 14)),
        }

        if self.grid_type == 'alphanumeric':
            for direction, label, points_str, is_label in self._alphanumeric_lines():
                if is_label:
                    icon_type = _ICON_LABEL_ROW if direction == 'row' else _ICON_LABEL_COL
                    feat_type = 'point'
                else:
                    icon_type = _ICON_LINE_ROW if direction == 'row' else _ICON_LINE_COL
                    feat_type = 'line'
                attr = dict(attr_base, direction=direction, label=label)
                self.writer.insertFeature(
                    type_str=feat_type,
                    name=f'grg_{direction}_{label or "boundary"}',
                    attr=json.dumps(attr),
                    color=color,
                    size=size,
                    iconType=icon_type,
                    points=points_str,
                    layer_id=layer_uuid,
                )
        else:
            if self.grid_type == 'utm':
                lines = self._utm_lines()
            elif self.grid_type == 'dms':
                lines = self._dms_lines()
            else:
                lines = self._geographic_lines()
            for direction, label, points_str in lines:
                icon_type = _ICON_LINE_ROW if direction == 'row' else _ICON_LINE_COL
                attr = dict(attr_base, direction=direction, label=label)
                self.writer.insertFeature(
                    type_str='line',
                    name=f'grg_{direction}_{label}',
                    attr=json.dumps(attr),
                    color=color,
                    size=size,
                    iconType=icon_type,
                    points=points_str,
                    layer_id=layer_uuid,
                )

        config = {
            'grid_type': self.grid_type,
            'line_color': color,
            'line_opacity': self.options.get('line_opacity', 0.8),
            'line_width': size,
            'line_style': self.options.get('line_style', 'solid'),
            'font_color': self.options.get('font_color', '#FFFFFF'),
            'font_size': int(self.options.get('font_size', 14)),
        }
        if self.grid_type == 'utm':
            config['spacing_m'] = self.options.get('spacing_m', 1000)
        elif self.grid_type in ('dms', 'geographic'):
            config['spacing_deg'] = self.options.get('spacing_deg', 0.01)

        self.writer.setMetadataValue('grg_config', json.dumps(config))

    # ------------------------------------------------------------------
    # Alphanumeric
    # ------------------------------------------------------------------

    def _alphanumeric_lines(self):
        """Yield (direction, label, points_str, is_label) tuples.

        is_label=False → Polyline boundary (label is empty string)
        is_label=True  → Point feature at cell centre (label is 'A', '1', …)
        """
        b = self.bounds
        spacing_m = float(self.options.get('spacing_m', 500))

        centre_lat = (b.yMinimum() + b.yMaximum()) / 2
        lat_rad = math.radians(abs(centre_lat))
        lon_deg_per_m = 1.0 / (111320.0 * math.cos(lat_rad))
        lat_deg_per_m = 1.0 / 111320.0

        col_width_deg = spacing_m * lon_deg_per_m
        row_height_deg = spacing_m * lat_deg_per_m

        width_deg = b.xMaximum() - b.xMinimum()
        height_deg = b.yMaximum() - b.yMinimum()

        col_count = max(1, round(width_deg / col_width_deg))
        row_count = max(1, round(height_deg / row_height_deg))

        # Recompute actual step to evenly fill the bounds
        col_step = width_deg / col_count
        row_step = height_deg / row_count

        results = []

        # ---- Boundary lines (col_count+1 vertical, row_count+1 horizontal) ----
        for c in range(col_count + 1):
            lon = b.xMinimum() + c * col_step
            pts = f"{lon} {b.yMinimum()}, {lon} {b.yMaximum()}"
            results.append(('col', '', pts, False))

        for r in range(row_count + 1):
            lat = b.yMaximum() - r * row_step
            pts = f"{b.xMinimum()} {lat}, {b.xMaximum()} {lat}"
            results.append(('row', '', pts, False))

        # ---- Cell-centre label points ----
        grid_centre_lat = (b.yMinimum() + b.yMaximum()) / 2
        grid_centre_lon = (b.xMinimum() + b.xMaximum()) / 2

        for c in range(col_count):
            centre_lon = b.xMinimum() + (c + 0.5) * col_step
            label = _col_label(c)
            pts = f"{centre_lon} {grid_centre_lat}"
            results.append(('col', label, pts, True))

        for r in range(row_count):
            centre_lat_val = b.yMaximum() - (r + 0.5) * row_step
            label = str(r + 1)
            pts = f"{grid_centre_lon} {centre_lat_val}"
            results.append(('row', label, pts, True))

        return results

    # ------------------------------------------------------------------
    # UTM
    # ------------------------------------------------------------------

    def _utm_lines(self):
        """Grid lines aligned to UTM round-metre multiples."""
        try:
            from pyproj import Transformer
        except ImportError:
            raise ImportError(
                'pyproj é necessário para grades UTM. '
                'Instale com: pip install pyproj'
            )

        b = self.bounds
        spacing = float(self.options.get('spacing_m', 1000))

        centre_lon = (b.xMinimum() + b.xMaximum()) / 2
        centre_lat = (b.yMinimum() + b.yMaximum()) / 2
        zone = int((centre_lon + 180) / 6) + 1
        hem = 'north' if centre_lat >= 0 else 'south'
        utm_crs = f'+proj=utm +zone={zone} +{hem} +ellps=WGS84'

        to_utm = Transformer.from_crs('EPSG:4326', utm_crs, always_xy=True)
        to_wgs = Transformer.from_crs(utm_crs, 'EPSG:4326', always_xy=True)

        sw_e, sw_n = to_utm.transform(b.xMinimum(), b.yMinimum())
        ne_e, ne_n = to_utm.transform(b.xMaximum(), b.yMaximum())

        lines = []
        n_range = [sw_n + i * (ne_n - sw_n) / 20 for i in range(21)]
        e_range = [sw_e + i * (ne_e - sw_e) / 20 for i in range(21)]

        e = math.ceil(sw_e / spacing) * spacing
        while e <= ne_e:
            pts_list = []
            for n in n_range:
                lon_wgs, lat_wgs = to_wgs.transform(e, n)
                if b.yMinimum() <= lat_wgs <= b.yMaximum():
                    pts_list.append(f"{lon_wgs:.6f} {lat_wgs:.6f}")
            if len(pts_list) >= 2:
                label = f"{int(e):,}".replace(',', ' ')
                lines.append(('col', label, ', '.join(pts_list)))
            e += spacing

        n = math.ceil(sw_n / spacing) * spacing
        while n <= ne_n:
            pts_list = []
            for east in e_range:
                lon_wgs, lat_wgs = to_wgs.transform(east, n)
                if b.xMinimum() <= lon_wgs <= b.xMaximum():
                    pts_list.append(f"{lon_wgs:.6f} {lat_wgs:.6f}")
            if len(pts_list) >= 2:
                label = f"{int(n):,}".replace(',', ' ')
                lines.append(('row', label, ', '.join(pts_list)))
            n += spacing

        return lines

    # ------------------------------------------------------------------
    # DMS
    # ------------------------------------------------------------------

    def _dms_lines(self):
        """Grid lines at round degree/minute multiples in WGS84 (legacy format)."""
        b = self.bounds
        spacing = float(self.options.get('spacing_deg', 0.01))
        lines = []

        lon = math.ceil(b.xMinimum() / spacing) * spacing
        while lon <= b.xMaximum() + 1e-9:
            pts = f"{lon:.6f} {b.yMinimum()}, {lon:.6f} {b.yMaximum()}"
            label = _dms_label(lon, is_lat=False)
            lines.append(('col', label, pts))
            lon = round(lon + spacing, 10)

        lat = math.ceil(b.yMinimum() / spacing) * spacing
        while lat <= b.yMaximum() + 1e-9:
            pts = f"{b.xMinimum():.6f} {lat:.6f}, {b.xMaximum():.6f} {lat:.6f}"
            label = _dms_label(lat, is_lat=True)
            lines.append(('row', label, pts))
            lat = round(lat + spacing, 10)

        return lines

    def _geographic_lines(self):
        """Grid lines at round degree/minute multiples in WGS84.

        Labels store the raw decimal degree value so the Flutter app can
        reformat them according to the map's coordinate-format setting.
        Col lines (constant longitude): label = longitude decimal string.
        Row lines (constant latitude):  label = latitude decimal string.
        """
        b = self.bounds
        spacing = float(self.options.get('spacing_deg', 0.01))
        lines = []

        lon = math.ceil(b.xMinimum() / spacing) * spacing
        while lon <= b.xMaximum() + 1e-9:
            pts = f"{lon:.6f} {b.yMinimum()}, {lon:.6f} {b.yMaximum()}"
            label = f"{lon:.6f}"
            lines.append(('col', label, pts))
            lon = round(lon + spacing, 10)

        lat = math.ceil(b.yMinimum() / spacing) * spacing
        while lat <= b.yMaximum() + 1e-9:
            pts = f"{b.xMinimum():.6f} {lat:.6f}, {b.xMaximum():.6f} {lat:.6f}"
            label = f"{lat:.6f}"
            lines.append(('row', label, pts))
            lat = round(lat + spacing, 10)

        return lines
