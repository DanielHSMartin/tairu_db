# -*- coding: utf-8 -*-

"""
GRG (Grid Reference Graphic) generator for .tairudb files.

Generates three types of grids:
  - 'alphanumeric': columns labeled A, B, C... / rows labeled 1, 2, 3...
  - 'utm': grid lines at UTM meter intervals (requires pyproj)
  - 'dms': grid lines at degree/minute intervals in WGS84

Each grid is written as a '__grg__' vector layer in the .tairudb,
with individual features tagged with grg_line_row / grg_line_col iconTypes.
Grid configuration is stored in metadata under the 'grg_config' key.

Flutter reads these and renders them as Polylines with a HUD overlay.
"""

import json
import math
import string
import uuid

from qgis.core import QgsRectangle


_GRG_LAYER_NAME = '__grg__'
_ICON_LINE_ROW = 'grg_line_row'
_ICON_LINE_COL = 'grg_line_col'


def _col_label(index: int) -> str:
    """Map 0→A, 1→B, ..., 25→Z, 26→AA, 27→AB, ..."""
    result = ''
    n = index
    while True:
        result = string.ascii_uppercase[n % 26] + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


def _dms_label(degrees: float, is_lat: bool) -> str:
    """Format a decimal degree value as a DMS string."""
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
        grid_type: 'alphanumeric' | 'utm' | 'dms'
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
            col_count (int, default 5): number of columns
            row_count (int, default 4): number of rows
          utm:
            spacing_m (float, default 1000): grid spacing in metres
          dms:
            spacing_deg (float, default 0.01): spacing in decimal degrees
          shared:
            line_color (str, default '#FF0000'): hex color #RRGGBB
            line_opacity (float 0-1, default 0.8)
            line_width (int, default 2): pixels
            line_style (str, default 'solid'): 'solid'|'dashed'|'dotted'|'dotdash'
            font_color (str, default '#FFFFFF')
            font_size (int, default 14)
        """
        layer_uuid = str(uuid.uuid4())
        self.writer.insertVectorLayer(layer_uuid, 'line', _GRG_LAYER_NAME, '')

        if self.grid_type == 'alphanumeric':
            lines = self._alphanumeric_lines()
        elif self.grid_type == 'utm':
            lines = self._utm_lines()
        elif self.grid_type == 'dms':
            lines = self._dms_lines()
        else:
            raise ValueError(f'Unknown grid_type: {self.grid_type}')

        color = self.options.get('line_color', '#FF0000')
        size = int(self.options.get('line_width', 2))
        attr_base = {
            'grid_type': self.grid_type,
            'opacity': self.options.get('line_opacity', 0.8),
            'line_style': self.options.get('line_style', 'solid'),
            'font_color': self.options.get('font_color', '#FFFFFF'),
            'font_size': int(self.options.get('font_size', 14)),
        }

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
        if self.grid_type == 'alphanumeric':
            config['col_count'] = self.options.get('col_count', 5)
            config['row_count'] = self.options.get('row_count', 4)
        elif self.grid_type == 'utm':
            config['spacing_m'] = self.options.get('spacing_m', 1000)
        elif self.grid_type == 'dms':
            config['spacing_deg'] = self.options.get('spacing_deg', 0.01)

        self.writer.setMetadataValue('grg_config', json.dumps(config))

    # ------------------------------------------------------------------
    # Alphanumeric
    # ------------------------------------------------------------------

    def _alphanumeric_lines(self):
        """
        Yields (direction, label, points_str) tuples.

        Columns: evenly spaced vertical lines across the bounds width.
          - col_count dividers means col_count+1 cells but col_count-1 inner lines
          - We generate the BOUNDING lines too so Flutter can always find the cell edges
        Rows: evenly spaced horizontal lines.
        """
        b = self.bounds
        col_count = int(self.options.get('col_count', 5))
        row_count = int(self.options.get('row_count', 4))

        col_width = (b.xMaximum() - b.xMinimum()) / col_count
        row_height = (b.yMaximum() - b.yMinimum()) / row_count

        lines = []

        # Vertical column boundary lines (col_count + 1 lines)
        for c in range(col_count + 1):
            lon = b.xMinimum() + c * col_width
            label = _col_label(c)  # A, B, C… for the cell to the RIGHT of this line
            pts = f"{lon} {b.yMinimum()}, {lon} {b.yMaximum()}"
            lines.append(('col', label, pts))

        # Horizontal row boundary lines (row_count + 1 lines)
        # Rows numbered from top (north) downward: row 1 = topmost cell
        for r in range(row_count + 1):
            lat = b.yMaximum() - r * row_height
            label = str(r + 1)  # "1" for the cell BELOW this line
            pts = f"{b.xMinimum()} {lat}, {b.xMaximum()} {lat}"
            lines.append(('row', label, pts))

        return lines

    # ------------------------------------------------------------------
    # UTM
    # ------------------------------------------------------------------

    def _utm_lines(self):
        """Grid lines aligned to UTM round-metre multiples."""
        try:
            from pyproj import Transformer
        except ImportError:
            raise ImportError(
                'pyproj is required for UTM grids. '
                'Install it with: pip install pyproj'
            )

        b = self.bounds
        spacing = float(self.options.get('spacing_m', 1000))

        # Find UTM zone from centre
        centre_lon = (b.xMinimum() + b.xMaximum()) / 2
        centre_lat = (b.yMinimum() + b.yMaximum()) / 2
        zone = int((centre_lon + 180) / 6) + 1
        hem = 'north' if centre_lat >= 0 else 'south'
        utm_crs = f'+proj=utm +zone={zone} +{hem} +ellps=WGS84'

        to_utm = Transformer.from_crs('EPSG:4326', utm_crs, always_xy=True)
        to_wgs = Transformer.from_crs(utm_crs, 'EPSG:4326', always_xy=True)

        # Corner coords in UTM
        sw_e, sw_n = to_utm.transform(b.xMinimum(), b.yMinimum())
        ne_e, ne_n = to_utm.transform(b.xMaximum(), b.yMaximum())

        lines = []
        sample_lats = [b.yMinimum() + i * (b.yMaximum() - b.yMinimum()) / 10 for i in range(11)]

        # Vertical easting lines
        e_start = math.ceil(sw_e / spacing) * spacing
        e = e_start
        while e <= ne_e:
            pts_list = []
            for lat in sample_lats:
                _, n_utm = to_utm.transform(b.xMinimum(), lat)  # not used, just for uniform sampling
                lon_wgs, lat_wgs = to_wgs.transform(e, to_utm.transform(b.xMinimum(), lat)[1])
                # Actually sample along the easting line at uniform northing steps
            n_range = [sw_n + i * (ne_n - sw_n) / 20 for i in range(21)]
            pts_list = []
            for n in n_range:
                lon_wgs, lat_wgs = to_wgs.transform(e, n)
                if b.yMinimum() <= lat_wgs <= b.yMaximum():
                    pts_list.append(f"{lon_wgs:.6f} {lat_wgs:.6f}")
            if len(pts_list) >= 2:
                label = f"{int(e):,}".replace(',', ' ')
                lines.append(('col', label, ', '.join(pts_list)))
            e += spacing

        # Horizontal northing lines
        n_start = math.ceil(sw_n / spacing) * spacing
        n = n_start
        while n <= ne_n:
            e_range = [sw_e + i * (ne_e - sw_e) / 20 for i in range(21)]
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
        """Grid lines at round degree/minute multiples in WGS84."""
        b = self.bounds
        spacing = float(self.options.get('spacing_deg', 0.01))
        lines = []

        # Vertical longitude lines
        lon_start = math.ceil(b.xMinimum() / spacing) * spacing
        lon = lon_start
        while lon <= b.xMaximum() + 1e-9:
            pts = f"{lon:.6f} {b.yMinimum()}, {lon:.6f} {b.yMaximum()}"
            label = _dms_label(lon, is_lat=False)
            lines.append(('col', label, pts))
            lon = round(lon + spacing, 10)

        # Horizontal latitude lines
        lat_start = math.ceil(b.yMinimum() / spacing) * spacing
        lat = lat_start
        while lat <= b.yMaximum() + 1e-9:
            pts = f"{b.xMinimum():.6f} {lat:.6f}, {b.xMaximum():.6f} {lat:.6f}"
            label = _dms_label(lat, is_lat=True)
            lines.append(('row', label, pts))
            lat = round(lat + spacing, 10)

        return lines
