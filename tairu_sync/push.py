# -*- coding: utf-8 -*-

"""
Push: QGIS vector features → /maps/{id}/records documents.

build_push_plan() classifies every feature against a fresh remote snapshot as
new / update / unchanged / forbidden (and optional deletions), so the user
approves an explicit diff before anything is written. execute_push() turns an
approved plan into batched Firestore commits (≤100 writes per commit).

Geometry comparison ignores the per-point 'ts' values (they change on every
serialization); only coordinates and radius/colors/styling matter.
"""

from dataclasses import dataclass, field

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsExpressionContext,
    QgsExpressionContextUtils, QgsProject, QgsRenderContext,
)

try:
    from ..tairu_firebase.models import (
        TairuRecord, SITUATIONS_BY_TYPE, now_millis, points_to_json,
        bounds_json_from_points,
    )
    from .record_convert import (
        hex_to_argb, configure_record_layer_fields, ensure_record_layer_fields,
        layer_sync_snapshot, normalized_geometry_points,
        record_to_attribute_map, resolved_background_argb,
        resolved_color_argb, sync_record_hash, SYNC_HASH_FIELD,
        SYNC_LAST_MODIFIED_FIELD,
    )
    from .tasks import run_task
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_firebase.models import (
        TairuRecord, SITUATIONS_BY_TYPE, now_millis, points_to_json,
        bounds_json_from_points,
    )
    from tairu_sync.record_convert import (
        hex_to_argb, configure_record_layer_fields, ensure_record_layer_fields,
        layer_sync_snapshot, normalized_geometry_points,
        record_to_attribute_map, resolved_background_argb,
        resolved_color_argb, sync_record_hash, SYNC_HASH_FIELD,
        SYNC_LAST_MODIFIED_FIELD,
    )
    from tairu_sync.tasks import run_task

# Default geometrySize matching the mobile app.
_DEFAULT_GEOMETRY_SIZE = {
    'none': 40.0,
    'point': 40.0,
    'line': 3.0,
    'polygon': 3.0,
    'circle': 3.0,
}

_COORD_PRECISION = 9  # ~0.1 mm; avoids float-noise diffs

# Symbol-layer data-defined property keys (the QgsSymbolLayer.Property enum).
# Defined explicitly so color extraction never references a name that only
# exists inside QGIS's render context — the source of the historical
# "name '_PROP_FILL_COLOR_' is not defined" crash on push.
try:
    from qgis.core import QgsSymbolLayer
    _PROP_FILL_COLOR = QgsSymbolLayer.PropertyFillColor
    _PROP_STROKE_COLOR = QgsSymbolLayer.PropertyStrokeColor
except Exception:  # pragma: no cover - fallback for QGIS API variations
    _PROP_FILL_COLOR = 3   # QgsSymbolLayer.PropertyFillColor
    _PROP_STROKE_COLOR = 4  # QgsSymbolLayer.PropertyStrokeColor


@dataclass
class PushItem:
    action: str                  # new | update | unchanged | forbidden | delete | remote_changed | conflict
    record: TairuRecord          # candidate state (or remote state for deletes)
    feature_id: int = None       # source QGIS fid (for recordId write-back)
    changed_fields: list = field(default_factory=list)
    warning: str = ''


@dataclass
class PushPlan:
    map_id: str
    items: list = field(default_factory=list)

    def count(self, action):
        return sum(1 for i in self.items if i.action == action)

    def summary(self):
        parts = [f'{self.count("new")} novos', f'{self.count("update")} atualizados',
                 f'{self.count("unchanged")} inalterados']
        if self.count('delete'):
            parts.append(f'{self.count("delete")} exclusões')
        if self.count('remote_changed'):
            parts.append(f'{self.count("remote_changed")} alterados no Tairu')
        if self.count('conflict'):
            parts.append(f'{self.count("conflict")} conflitos')
        if self.count('forbidden'):
            parts.append(f'{self.count("forbidden")} sem permissão')
        return ', '.join(parts)

    def writable_items(self):
        return [i for i in self.items if i.action in ('new', 'update', 'delete')]


# ------------------------------------------------------------- conversion

def _color_to_argb(color, opacity=1.0):
    alpha = int(round((color.alpha() & 0xFF) * _opacity_value(opacity)))
    return ((alpha & 0xFF) << 24) | ((color.red() & 0xFF) << 16) \
        | ((color.green() & 0xFF) << 8) | (color.blue() & 0xFF)


def _is_valid_color(color):
    if color is None:
        return False
    try:
        return bool(color.isValid())
    except Exception:
        return True


def _first_defined(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _opacity_value(value):
    try:
        opacity = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(1.0, max(0.0, opacity))


def _object_opacity(obj):
    try:
        return _opacity_value(obj.opacity())
    except Exception:
        return 1.0


def _field_argb(feature, name):
    idx = feature.fields().indexOf(name)
    if idx < 0:
        return None
    value = feature.attribute(idx)
    if value is None or (hasattr(value, 'isNull') and value.isNull()):
        return None
    return hex_to_argb(str(value))


def _simple_property_field(prop):
    candidates = []
    for accessor in ('field', 'asExpression', 'expressionString'):
        try:
            value = getattr(prop, accessor)()
        except Exception:
            continue
        if value:
            candidates.append(str(value).strip())
    for value in candidates:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if value.replace('_', '').isalnum():
            return value
    return None


def _expression_context(layer, feature):
    context = QgsExpressionContext()
    try:
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    except Exception:
        pass
    try:
        context.setFields(layer.fields())
    except Exception:
        pass
    try:
        context.setFeature(feature)
    except Exception:
        pass
    try:
        context.setGeometry(feature.geometry())
    except Exception:
        pass
    return context


def _render_context(layer, feature):
    context = QgsRenderContext()
    try:
        context.setExpressionContext(_expression_context(layer, feature))
    except Exception:
        pass
    return context


def _static_layer_argb(symbol_layer, accessors, opacity=1.0):
    for accessor in accessors:
        try:
            color = getattr(symbol_layer, accessor)()
        except Exception:
            continue
        if _is_valid_color(color):
            return _color_to_argb(color, opacity)
    return None


def _data_defined_layer_argb(
        symbol_layer, prop_key, expr_context, feature, default_argb, opacity=1.0):
    try:
        props = symbol_layer.dataDefinedProperties()
    except Exception:
        return None, False
    try:
        if not props.isActive(prop_key):
            return None, False
    except Exception:
        return None, False

    default_color = None
    try:
        from qgis.PyQt.QtGui import QColor
        default_color = QColor.fromRgba(int(default_argb) & 0xFFFFFFFF) \
            if default_argb is not None else QColor()
    except Exception:
        pass
    try:
        if default_color is None:
            result = props.valueAsColor(prop_key, expr_context)
        else:
            result = props.valueAsColor(prop_key, expr_context, default_color)
        color, ok = result if isinstance(result, tuple) else (result, True)
        if ok and _is_valid_color(color):
            return _color_to_argb(color, opacity), True
    except Exception:
        pass

    try:
        prop = props.property(prop_key)
        field = _simple_property_field(prop)
    except Exception:
        field = None
    if field:
        value = _field_argb(feature, field) if feature is not None else None
        if value is not None:
            return value, True
    return None, True


def _symbol_layer_argb(symbol_layer, prop_key, expr_context, feature, accessors, opacity=1.0):
    static_argb = _static_layer_argb(symbol_layer, accessors, opacity)
    dynamic_argb, had_dynamic = _data_defined_layer_argb(
        symbol_layer, prop_key, expr_context, feature, static_argb, opacity)
    if had_dynamic:
        return dynamic_argb
    return static_argb


def _symbol_style_argbs(symbol, expr_context, feature, spec_key, inherited_opacity=1.0):
    fg_argb = None
    bg_argb = None
    symbol_opacity = _opacity_value(inherited_opacity) * _object_opacity(symbol)
    try:
        symbol_layers = symbol.symbolLayers()
    except Exception:
        symbol_layers = []

    for symbol_layer in symbol_layers:
        try:
            if hasattr(symbol_layer, 'enabled') and not symbol_layer.enabled():
                continue
        except Exception:
            pass

        layer_opacity = symbol_opacity * _object_opacity(symbol_layer)
        fill_argb = _symbol_layer_argb(
            symbol_layer, _PROP_FILL_COLOR, expr_context, feature,
            ('fillColor', 'color'), layer_opacity)
        stroke_argb = _symbol_layer_argb(
            symbol_layer, _PROP_STROKE_COLOR, expr_context, feature,
            ('strokeColor', 'color'), layer_opacity)

        if spec_key == 'polygon':
            bg_argb = _first_defined(bg_argb, fill_argb)
            fg_argb = _first_defined(fg_argb, stroke_argb, fill_argb)
        elif spec_key == 'circle':
            fg_argb = _first_defined(fg_argb, fill_argb, stroke_argb)
        elif spec_key == 'line':
            fg_argb = _first_defined(fg_argb, stroke_argb, fill_argb)
        else:
            fg_argb = _first_defined(fg_argb, fill_argb, stroke_argb)

        try:
            sub_symbol = symbol_layer.subSymbol()
        except Exception:
            sub_symbol = None
        if sub_symbol is not None:
            sub_fg, sub_bg = _symbol_style_argbs(
                sub_symbol, expr_context, feature,
                'polygon' if spec_key == 'circle' else spec_key,
                layer_opacity)
            fg_argb = _first_defined(fg_argb, sub_fg)
            bg_argb = _first_defined(bg_argb, sub_bg)

    if fg_argb is None:
        try:
            fg_argb = _color_to_argb(symbol.color(), symbol_opacity)
        except Exception:
            pass
    return fg_argb, bg_argb


def _symbols_style_argbs(symbols, expr_context, feature, spec_key, field_fg, field_bg):
    for symbol in symbols:
        fg_argb, bg_argb = _symbol_style_argbs(symbol, expr_context, feature, spec_key)
        fg_argb = _first_defined(fg_argb, field_fg)
        bg_argb = _first_defined(bg_argb, field_bg)
        if fg_argb is not None or bg_argb is not None:
            return fg_argb, bg_argb
    return None, None


def _layer_symbol_argb(layer):
    """Base symbol color of the layer renderer, as ARGB int."""
    try:
        symbol = layer.renderer().symbol()
        fg_argb, _bg_argb = _symbol_style_argbs(symbol, QgsExpressionContext(), None, None)
        if fg_argb is not None:
            return fg_argb
    except Exception:
        pass
    return None


def _feature_symbol_argbs(layer, feature, spec_key):
    """Renderer colors for a specific feature, falling back to stored fields."""
    field_fg = _field_argb(feature, 'geometryColor')
    field_bg = _field_argb(feature, 'geometryBackgroundColor')
    renderer = None
    context = _render_context(layer, feature)
    expr_context = context.expressionContext()
    try:
        renderer = layer.renderer()
        renderer.startRender(context, layer.fields())
        try:
            symbols = []
            try:
                symbols = renderer.symbolsForFeature(feature, context) or []
            except Exception:
                pass
            if not symbols:
                symbol = renderer.symbolForFeature(feature, context)
                symbols = [symbol] if symbol is not None else []
            if not symbols:
                symbols = _rule_symbols_for_feature(renderer, feature, context)
            fg_argb, bg_argb = _symbols_style_argbs(
                symbols, expr_context, feature, spec_key, field_fg, field_bg)
            if fg_argb is not None or bg_argb is not None:
                return fg_argb, bg_argb
        finally:
            renderer.stopRender(context)
    except Exception:
        pass
    if renderer is not None:
        symbols = _rule_symbols_for_feature(renderer, feature, context)
        fg_argb, bg_argb = _symbols_style_argbs(
            symbols, expr_context, feature, spec_key, field_fg, field_bg)
        if fg_argb is not None or bg_argb is not None:
            return fg_argb, bg_argb
    return _first_defined(field_fg, _layer_symbol_argb(layer)), field_bg


def _rule_symbols_for_feature(renderer, feature, context):
    """Best-effort fallback for QgsRuleBasedRenderer layers."""
    try:
        root_rule = renderer.rootRule()
    except Exception:
        return []
    for only_active in (True, False):
        try:
            rules = root_rule.rulesForFeature(feature, context, only_active) or []
        except Exception:
            rules = []
        symbols = []
        for rule in rules:
            if not only_active:
                try:
                    if not rule.active():
                        continue
                except Exception:
                    pass
                try:
                    if not rule.isFilterOK(feature, context):
                        continue
                except Exception:
                    pass
            try:
                symbol = rule.symbol()
            except Exception:
                symbol = None
            if symbol is not None:
                symbols.append(symbol)
        if symbols:
            return symbols
    try:
        return root_rule.symbolsForFeature(feature, context) or []
    except Exception:
        return []


def _attr(feature, name):
    idx = feature.fields().indexOf(name)
    if idx < 0:
        return None
    value = feature.attribute(idx)
    if value is None or (hasattr(value, 'isNull') and value.isNull()):
        return None
    return value


def _attr_millis(feature, name):
    value = _attr(feature, name)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(value.toMSecsSinceEpoch())
    except Exception:
        return 0


_CONTOUR_ELEV_FIELD = 'ELEV'  # matched case-insensitively
# Master (index) contours occur every 5th line; their style stands out.
_CONTOUR_MASTER_EVERY = 5
_CONTOUR_MASTER_SIZE = 3.0
_CONTOUR_NORMAL_SIZE = 2.0
_CONTOUR_MASTER_OPACITY = 0.8
_CONTOUR_NORMAL_OPACITY = 0.5
_FLOAT_EPS = 1e-6


def _elev_field_name(fields):
    """The actual (case-preserving) name of the ELEV field, or None."""
    for field in fields:
        if field.name().strip().upper() == _CONTOUR_ELEV_FIELD:
            return field.name()
    return None


def _contour_elevation_value(feature):
    """The feature's ELEV as float, or None when absent/unparseable."""
    name = _elev_field_name(feature.fields())
    if name is None:
        return None
    value = _attr(feature, name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _contour_elevation_name(feature):
    """'Curva {elev}m' when the feature carries an ELEV attribute, else None.

    Integer elevations render without a decimal point ('Curva 140m'); fractional
    ones keep it ('Curva 12.5m').
    """
    elev = _contour_elevation_value(feature)
    if elev is None:
        return None
    elev = int(elev) if elev == int(elev) else elev
    return f'Curva {elev}m'


def _contour_master_modulo(layer):
    """Elevation modulo identifying master contours: 5 * h.

    h (the vertical interval between adjacent contours) is inferred from the
    smallest positive gap between the layer's distinct ELEV values — robust even
    when some contours are missing inside the clip. Returns None when it can't be
    determined (no ELEV field or fewer than two distinct elevations), in which
    case every contour is treated as a normal (intermediate) line.
    """
    if layer is None:
        return None
    name = _elev_field_name(layer.fields())
    if name is None:
        return None
    idx = layer.fields().indexOf(name)
    if idx < 0:
        return None
    try:
        from qgis.core import QgsFeatureRequest
        request = QgsFeatureRequest().setSubsetOfAttributes([idx]).setFlags(QgsFeatureRequest.NoGeometry)
    except Exception:
        request = None

    elevations = set()
    features = layer.getFeatures(request) if request is not None else layer.getFeatures()
    for feat in features:
        value = feat.attribute(idx)
        if value is None or (hasattr(value, 'isNull') and value.isNull()):
            continue
        try:
            elevations.add(float(value))
        except (TypeError, ValueError):
            continue

    if len(elevations) < 2:
        return None
    ordered = sorted(elevations)
    gaps = [b - a for a, b in zip(ordered, ordered[1:]) if b - a > _FLOAT_EPS]
    if not gaps:
        return None
    return _CONTOUR_MASTER_EVERY * min(gaps)


def _is_master_contour(elev, master_modulo):
    """True when elev sits on a master (index) contour for the given modulo."""
    if not master_modulo or master_modulo <= 0:
        return False
    remainder = elev % master_modulo
    return remainder < _FLOAT_EPS or abs(remainder - master_modulo) < _FLOAT_EPS


def _apply_alpha(argb, opacity):
    """Return argb with its alpha channel replaced by opacity (0..1); None stays None."""
    if argb is None:
        return None
    alpha = int(round(max(0.0, min(1.0, opacity)) * 255))
    return ((alpha & 0xFF) << 24) | (int(argb) & 0x00FFFFFF)


def _feature_name(feature, mapping, layer_name, index):
    if mapping.get('nome_field'):
        value = _attr(feature, mapping['nome_field'])
        if value:
            return str(value)
    contour_name = _contour_elevation_name(feature)
    if contour_name:
        return contour_name
    for candidate in ('name', 'Name', 'nome', 'Nome'):
        value = _attr(feature, candidate)
        if value:
            return str(value)
    return f'{layer_name} {index}'


def _default_geometry_size(spec_key):
    return _DEFAULT_GEOMETRY_SIZE.get(spec_key or 'none', 40.0)


def _geometry_points(feature, transform):
    """((lat, lon) list, spec_key, warning) for the feature geometry in WGS84."""
    geom = feature.geometry()
    if geom is None or geom.isEmpty():
        return [], 'none', ''

    from qgis.core import QgsGeometry
    geom = QgsGeometry(geom)
    geom.transform(transform)

    warning = ''
    gtype = geom.type()
    type_int = int(gtype) if not isinstance(gtype, int) else gtype

    if type_int == 0:  # point
        if geom.isMultipart():
            pts = geom.asMultiPoint()
            if len(pts) > 1:
                warning = 'multiponto: usada apenas a primeira parte'
            pt = pts[0]
        else:
            pt = geom.asPoint()
        return [(pt.y(), pt.x())], 'point', warning

    if type_int == 1:  # line
        if geom.isMultipart():
            lines = geom.asMultiPolyline()
            line = max(lines, key=len) if lines else []
            if len(lines) > 1:
                warning = 'multilinha: usada a maior parte'
        else:
            line = geom.asPolyline()
        return [(p.y(), p.x()) for p in line], 'line', warning

    if type_int == 2:  # polygon
        if geom.isMultipart():
            polys = geom.asMultiPolygon()
            rings = [p[0] for p in polys if p]
            ring = max(rings, key=len) if rings else []
            if len(polys) > 1:
                warning = 'multipolígono: usada a maior parte'
        else:
            poly = geom.asPolygon()
            ring = poly[0] if poly else []
        # QGIS closes the ring (last vertex == first); strip the duplicate so
        # the point list matches the app's Firestore encoding (no closing point).
        # Use explicit .x()/.y() instead of == because QgsPointXY.__eq__ is not
        # reliably exported in all QGIS/SIP versions and would silently return False.
        if len(ring) > 1 and ring[-1].x() == ring[0].x() and ring[-1].y() == ring[0].y():
            ring = ring[:-1]
        return [(p.y(), p.x()) for p in ring], 'polygon', warning

    return [], 'none', 'tipo de geometria não suportado'


def contour_master_modulo(layer):
    """Public alias of the contour master-interval detector (5 * vertical interval).

    Computed once per layer and passed into feature_export_style/feature_to_record.
    """
    return _contour_master_modulo(layer)


def feature_export_style(layer, feature, spec_key, master_modulo=None):
    """(color_argb, size) for one feature, mirroring feature_to_record's styling.

    Used by the .tairudb vector export so an exported feature carries the same
    rendered color/opacity/width as the record that the same feature would push:
    the per-feature renderer color (graduated/categorized/rule-based aware, with
    layer/symbol opacity folded in), and — for contour lines (ELEV present) — the
    elevation-aware master/intermediate stroke width and opacity.

    color_argb is an ARGB int carrying alpha (use argb_to_hex for '#AARRGGBB'), or
    None when no renderer color could be resolved. size is the line/point width in
    px, or None to let the caller apply its geometry-type default.
    """
    color_argb, _bg_argb = _feature_symbol_argbs(layer, feature, spec_key)
    size = None
    contour_elev = _contour_elevation_value(feature)
    if contour_elev is not None and spec_key == 'line':
        if _is_master_contour(contour_elev, master_modulo):
            size = _CONTOUR_MASTER_SIZE
            color_argb = _apply_alpha(color_argb, _CONTOUR_MASTER_OPACITY)
        else:
            size = _CONTOUR_NORMAL_SIZE
            color_argb = _apply_alpha(color_argb, _CONTOUR_NORMAL_OPACITY)
    return color_argb, size


def feature_to_record(feature, layer, mapping, uid, transform, index, contour_master_modulo=None):
    """Build the candidate TairuRecord for one feature. Returns (rec, warning).

    contour_master_modulo (5 * vertical interval) styles contour lines: master
    contours get a wider, more opaque stroke than intermediate ones.
    """
    points, spec_key, warning = _geometry_points(feature, transform)
    record_id = _attr(feature, 'recordId')

    # Pulled circle layers: point geometry + circleRadius attribute
    circle_radius = _attr(feature, 'circleRadius')
    if spec_key == 'point' and circle_radius:
        spec_key = 'circle'

    # Per-feature attributes win over the dialog mapping (round-trip support).
    # Use `is not None` (not `or`) so that '' from a pulled layer is preserved;
    # the `or` form would replace '' with the mapping default, producing phantom diffs.
    tipo_raw = _attr(feature, 'tipoRegistro')
    tipo = tipo_raw if tipo_raw is not None else mapping.get('tipo', 'local')
    sub_tipo_raw = _attr(feature, 'subTipo')
    sub_tipo = sub_tipo_raw if sub_tipo_raw is not None else mapping.get('sub_tipo', 'outroLocal')

    # Empty-string situation from a pulled layer is preserved (not defaulted)
    # so unchanged features don't produce phantom diffs.
    situation = None
    if mapping.get('situation_field'):
        situation = _attr(feature, mapping['situation_field'])
    if situation is None:
        situation = _attr(feature, 'situation')
    if situation is None:
        situation = mapping.get('situation') or ''

    # The cloud record should mirror the color rendered by QGIS for this feature.
    # For pulled Tairu layers this may come from data-defined geometryColor fields;
    # for regular layers it comes from the layer renderer/category/rule symbol.
    symbol_fg_argb, symbol_bg_argb = _feature_symbol_argbs(layer, feature, spec_key)
    color_argb = _first_defined(mapping.get('color_argb'), symbol_fg_argb)
    bg_argb = symbol_bg_argb

    descricao = ''
    if mapping.get('descricao_field'):
        descricao = str(_attr(feature, mapping['descricao_field']) or '')
    elif _attr(feature, 'descricao'):
        descricao = str(_attr(feature, 'descricao'))

    now = now_millis()

    def _qdt_to_ms(value):
        try:
            return int(value.toMSecsSinceEpoch())
        except Exception:
            return 0

    event_ms = _qdt_to_ms(_attr(feature, 'eventDateTime'))
    if not event_ms and not record_id:
        event_ms = now
    geometry_size_attr = _attr(feature, 'geometrySize')
    geometry_size = float(geometry_size_attr) if geometry_size_attr is not None else None
    if geometry_size is None and not record_id:
        geometry_size = _default_geometry_size(spec_key)

    # Contour lines carry a fixed, elevation-aware style overriding the renderer:
    # master (index) contours — ELEV % (5*h) == 0, with h auto-detected — get a
    # wider, more opaque stroke than the intermediate ones. Applied on every push
    # so the style is deterministic and stable across round-trips.
    # tipo/sub_tipo are also forced here so that round-trips from old 'desenho'
    # pushes are silently migrated to the dedicated 'curvaNivel' type.
    contour_elev = _contour_elevation_value(feature)
    if contour_elev is not None and spec_key == 'line':
        tipo = 'curvaNivel'
        if _is_master_contour(contour_elev, contour_master_modulo):
            sub_tipo = 'curvaMestra'
            geometry_size = _CONTOUR_MASTER_SIZE
            color_argb = _apply_alpha(color_argb, _CONTOUR_MASTER_OPACITY)
        else:
            sub_tipo = 'curvaNormal'
            geometry_size = _CONTOUR_NORMAL_SIZE
            color_argb = _apply_alpha(color_argb, _CONTOUR_NORMAL_OPACITY)

    rec = TairuRecord(
        record_id=str(record_id) if record_id else '',
        nome=_feature_name(feature, mapping, layer.name(), index),
        descricao=descricao,
        situation=str(situation),
        endereco=str(_attr(feature, 'endereco') or ''),
        tipo_registro=str(tipo),
        sub_tipo=str(sub_tipo),
        owner=str(_attr(feature, 'owner') or ''),
        plate_tag=str(_attr(feature, 'plateTag') or ''),
        brand=str(_attr(feature, 'brand') or ''),
        model=str(_attr(feature, 'model') or ''),
        year=int(_attr(feature, 'year') or 0),
        color=str(_attr(feature, 'color') or ''),
        size=float(_attr(feature, 'size') or 0.0),
        value_estimate=float(_attr(feature, 'valueEstimate') or 0.0),
        event_date_time=event_ms,
        geometry_type=spec_key,
        geometry_points_json=points_to_json(
            [(round(la, _COORD_PRECISION), round(lo, _COORD_PRECISION)) for la, lo in points],
            ts=now) if points else None,
        geometry_bounds_json=bounds_json_from_points(points, ts=now) if points else None,
        circle_radius=float(circle_radius) if (spec_key == 'circle' and circle_radius) else None,
        # Existing records preserve an absent value for clean diffs; new records
        # show/send the same geometrySize defaults used by the mobile app.
        geometry_size=geometry_size,
        geometry_color_value=color_argb,
        geometry_background_color_value=bg_argb,
        created_by=uid,
        created_at=now,
        last_modified=now,
    )
    return rec, warning


# -------------------------------------------------------------------- diff

# Fields compared (and pushed in the update mask when changed)
_DIFF_SCALARS = [
    'nome', 'descricao', 'situation', 'endereco', 'tipoRegistro', 'subTipo',
    'owner', 'plateTag', 'brand', 'model', 'year', 'color', 'valueEstimate', 'size',
    'eventDateTime', 'geometrySize', 'geometryColorValue', 'geometryBackgroundColorValue',
]
_ALL_UPDATE_FIELDS = list(_DIFF_SCALARS) + [
    'geometryType', 'geometryPoints', 'geometryBounds', 'circleRadius',
]
_CANDIDATE_ATTRS = {
    'nome': 'nome', 'descricao': 'descricao', 'situation': 'situation',
    'endereco': 'endereco', 'tipoRegistro': 'tipo_registro', 'subTipo': 'sub_tipo',
    'owner': 'owner', 'plateTag': 'plate_tag', 'brand': 'brand', 'model': 'model',
    'year': 'year', 'color': 'color', 'valueEstimate': 'value_estimate', 'size': 'size',
    'eventDateTime': 'event_date_time',
    'geometrySize': 'geometry_size', 'geometryColorValue': 'geometry_color_value',
    'geometryBackgroundColorValue': 'geometry_background_color_value',
}


def _rounded_points(rec):
    # Shared normalization (rounds + strips polygon closing point) so closed
    # QGIS rings and open Firestore rings compare equal.
    return normalized_geometry_points(rec, _COORD_PRECISION)


def _geometry_changed(candidate, remote):
    cand_pts = _rounded_points(candidate)
    remote_pts = _rounded_points(remote)
    # A record with no points belongs to the no-geometry layer regardless of the
    # stored type label, so don't treat a 'point'/'polygon' label that never had
    # coordinates as a geometry change against the candidate's 'none'.
    cand_type = (candidate.geometry_type or 'none') if cand_pts else 'none'
    remote_type = (remote.geometry_type or 'none') if remote_pts else 'none'
    if cand_type != remote_type:
        return True
    if cand_pts != remote_pts:
        return True
    if (candidate.circle_radius or 0) != (remote.circle_radius or 0):
        return True
    return False


def _norm_argb(value):
    """Signed (Python) and unsigned (Dart) ARGB ints compare equal."""
    return None if value is None else int(value) & 0xFFFFFFFF


def _diff_fields(candidate, remote):
    changed = []
    for fs_key in _DIFF_SCALARS:
        if fs_key == 'geometryColorValue':
            # Compare the rendered color, not the raw nullable value: an absent
            # color and an explicit type-default color render identically.
            if resolved_color_argb(candidate) != resolved_color_argb(remote):
                changed.append(fs_key)
            continue
        if fs_key == 'geometryBackgroundColorValue':
            if resolved_background_argb(candidate) != resolved_background_argb(remote):
                changed.append(fs_key)
            continue
        cand_val = getattr(candidate, _CANDIDATE_ATTRS[fs_key])
        remote_val = getattr(remote, _CANDIDATE_ATTRS[fs_key])
        if isinstance(cand_val, float) or isinstance(remote_val, float):
            if abs(float(cand_val or 0) - float(remote_val or 0)) > 1e-9:
                changed.append(fs_key)
        elif (cand_val or None) != (remote_val or None):
            # '' and None are equivalent absences for string fields
            if (cand_val or '') != (remote_val or ''):
                changed.append(fs_key)
    if _geometry_changed(candidate, remote):
        changed += ['geometryType', 'geometryPoints', 'geometryBounds']
        if candidate.circle_radius is not None or remote.circle_radius is not None:
            changed.append('circleRadius')
    return changed


def _baseline_hash(feature):
    value = _attr(feature, SYNC_HASH_FIELD)
    return str(value or '')


def _baseline_last_modified(feature):
    return _attr_millis(feature, SYNC_LAST_MODIFIED_FIELD) or _attr_millis(feature, 'lastModified')


def _millis_from_snapshot(value):
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _classify_against_baseline(candidate, remote_rec, feature):
    changed = _diff_fields(candidate, remote_rec)
    base_hash = _baseline_hash(feature)
    if base_hash:
        local_hash = sync_record_hash(candidate)
        remote_hash = sync_record_hash(remote_rec)
        if local_hash == remote_hash:
            if local_hash == base_hash:
                return 'unchanged', [], ''
            # local == remote != base: the remote snapshot already reflects the
            # user's local edits (local GeoPackage mode). We can't diff field-by-
            # field against the pre-edit state, so include every updateable field.
            return 'update', _ALL_UPDATE_FIELDS, ''
        local_changed = local_hash != base_hash
        remote_changed = remote_hash != base_hash
        if remote_changed and not local_changed:
            return 'remote_changed', changed, 'alterado no Tairu Maps desde a última sincronização'
        if remote_changed and local_changed:
            return 'conflict', changed, 'alterado no QGIS e no Tairu Maps; baixe novamente para resolver'
        return ('update', changed, '') if changed else ('unchanged', [], '')

    base_last_modified = _baseline_last_modified(feature)
    if base_last_modified and remote_rec.last_modified and base_last_modified != remote_rec.last_modified:
        return 'conflict', changed, (
            'registro remoto mudou desde o último pull; baixe novamente para resolver')
    return ('update', changed, '') if changed else ('unchanged', [], '')


def build_push_plan(layer, mapping, tmap, uid, propagate_deletions=False):
    """Compare source layer features against the pull-time baseline (tairuSyncHash).

    No Firestore or GeoPackage snapshot read is performed. For round-trip layers
    (features that carry a recordId + tairuSyncHash from a previous pull), the
    stored hash is the sole baseline: if sync_record_hash(candidate) == tairuSyncHash
    the record is unchanged; otherwise it needs an update. Detection of remote
    changes or conflicts requires a fresh 'Receber Registros' pull.
    """
    role = tmap.role_for(uid)
    is_admin = role in ('owner', 'admin')

    transform = QgsCoordinateTransform(
        layer.crs(), QgsCoordinateReferenceSystem('EPSG:4326'),
        QgsProject.instance().transformContext())

    plan = PushPlan(map_id=tmap.map_id)
    seen_ids = set()
    sync_snapshot = layer_sync_snapshot(layer)
    # One scan of the layer's ELEV values infers the contour interval up front so
    # every feature can be classified master/normal without re-scanning.
    contour_master_modulo = _contour_master_modulo(layer)

    for index, feature in enumerate(layer.getFeatures(), start=1):
        candidate, warning = feature_to_record(
            feature, layer, mapping, uid, transform, index, contour_master_modulo)

        if candidate.record_id:
            # Existing record from a previous pull.
            seen_ids.add(candidate.record_id)
            created_by = str(_attr(feature, 'createdBy') or '')
            if not is_admin and created_by and created_by != uid:
                plan.items.append(PushItem('forbidden', candidate, feature.id(),
                                           warning='criado por outro usuário'))
                continue
            # Restore pull-time creation metadata so push never overwrites it.
            candidate.created_by = created_by or candidate.created_by
            candidate.created_at = _attr_millis(feature, 'createdAt') or candidate.created_at
            candidate.is_deleted = False

            base_hash = _baseline_hash(feature)
            if base_hash and sync_record_hash(candidate) == base_hash:
                plan.items.append(PushItem('unchanged', candidate, feature.id(), [], warning))
            else:
                plan.items.append(PushItem('update', candidate, feature.id(),
                                           _ALL_UPDATE_FIELDS, warning))
        else:
            candidate.record_id = TairuRecord.new_id()
            plan.items.append(PushItem('new', candidate, feature.id(), [], warning))

    if propagate_deletions:
        # Records present at last sync (sync_snapshot) but no longer in the layer
        # are treated as local deletions.
        for record_id in (sync_snapshot or {}):
            if record_id in seen_ids:
                continue
            plan.items.append(PushItem('delete', TairuRecord(record_id=record_id)))

    return plan


# ----------------------------------------------------------------- execute

def finalize_new_record(rec):
    """Fill app-style defaults that were deliberately left empty for diffing."""
    if not rec.situation:
        rec.situation = SITUATIONS_BY_TYPE.get(rec.tipo_registro, ['Ativo'])[0]
    if rec.geometry_size is None:
        rec.geometry_size = _default_geometry_size(rec.geometry_type)
    return rec


def build_writes(fs, plan, uid):
    """Firestore write ops for the approved plan."""
    writes = []
    for item in plan.writable_items():
        rec = item.record
        path = f'maps/{plan.map_id}/records/{rec.record_id}'
        if item.action == 'new':
            now = now_millis()
            rec.created_at = rec.created_at or now
            rec.last_modified = now
            rec.is_deleted = False
            fields = finalize_new_record(rec).to_fields()
            fields['isDeleted'] = False
            writes.append(fs.build_create_write(path, fields))
        elif item.action == 'update':
            # Always assert the record is live. A feature present in the source
            # layer must clear any prior soft-delete (isDeleted=True) on the
            # remote record; otherwise re-pushing a feature whose record was
            # deleted in the app just patches a tombstone that pull keeps hiding
            # (apply_pull skips isDeleted records), so the feature never reappears.
            mask = list(dict.fromkeys(item.changed_fields + ['lastModified', 'isDeleted']))
            rec.last_modified = now_millis()
            rec.is_deleted = False
            all_fields = rec.to_fields()
            fields = {k: all_fields[k] for k in mask if k in all_fields}
            # circleRadius may be in the mask but absent (circle -> other type)
            for key in mask:
                if key not in fields:
                    fields[key] = None
            fields['lastModified'] = rec.last_modified
            writes.append(fs.build_update_write(path, fields, mask))
        elif item.action == 'delete':
            rec.is_deleted = True
            rec.last_modified = now_millis()
            writes.append(fs.build_update_write(
                path, {'isDeleted': True, 'lastModified': rec.last_modified},
                ['isDeleted', 'lastModified']))
    return writes


def execute_push(dock, tmap, plan, source_layer):
    """Commit the plan in batches; mirror approved attributes into the source layer."""
    fs = dock.fs
    page = dock.detail_page
    writes = build_writes(fs, plan, dock.tokens.uid)
    if not writes:
        dock.notify('Nada para enviar — tudo já está sincronizado.')
        return

    page.set_busy(True, f'Enviando {len(writes)} alterações…')

    def send(task):
        batch = 100
        for start in range(0, len(writes), batch):
            if task.isCanceled():
                break
            fs.commit(writes[start:start + batch])
            task.report(min(1.0, (start + batch) / len(writes)),
                        f'{min(start + batch, len(writes))} de {len(writes)} enviados')
        return len(writes)

    def on_success(total):
        _write_back_records_to_source_layer(plan, source_layer)
        page.set_busy(False)
        page.set_status(f'{total} alterações enviadas com sucesso.')
        dock.notify(f'{tmap.nome}: {plan.summary()} — enviado.')

    def on_error(message):
        page.set_busy(False)
        page.set_status(message, error=True)
        dock.notify(message, error=True)

    run_task(f'Tairu Maps: enviando registros para {tmap.nome}', send,
             on_success=on_success, on_error=on_error,
             on_progress=lambda f, m: page.set_progress(f, m))


def _write_back_records_to_source_layer(plan, layer):
    """Persist approved record attributes into the source layer when possible."""
    if layer is None:
        return
    try:
        ensure_record_layer_fields(layer)
    except Exception:
        pass

    fields = layer.fields()
    changes = {}
    for item in plan.items:
        if item.action not in ('new', 'update') or item.feature_id is None:
            continue
        attr_map = record_to_attribute_map(item.record)
        row_changes = {
            fields.indexOf(name): value for name, value in attr_map.items()
            if fields.indexOf(name) >= 0
        }
        if row_changes:
            changes[item.feature_id] = row_changes
    if changes:
        try:
            if layer.isEditable():
                for fid, attrs in changes.items():
                    for idx, value in attrs.items():
                        layer.changeAttributeValue(fid, idx, value)
            else:
                layer.dataProvider().changeAttributeValues(changes)
            layer.triggerRepaint()
        except Exception:
            pass  # source may be read-only; remote commit has already succeeded
    try:
        configure_record_layer_fields(layer)
    except Exception:
        pass
