# -*- coding: utf-8 -*-

"""
Record ⇄ QgsFeature conversion and the per-map records GeoPackage.

Layer layout (one GeoPackage per map, EPSG:4326):
    registros_ponto          Point
    registros_linha          LineString
    registros_poligono       Polygon
    registros_circulo        Point + circleRadius (rendered as true-scale buffer)
    registros_sem_geometria  attribute-only (geometryType 'none' or unparseable)

Colors are stored as '#AARRGGBB' hex strings (Qt/QGIS order; app stores ARGB ints).
Qt's QColor::setNamedColor() interprets 8-digit hex as #AARRGGBB (alpha first).
geometryColor is always non-null in the gpkg (explicit value, or type-color fallback
computed in Python). push.py strips back the type-default so round-trip pulls don't
produce false diffs.
"""

import hashlib
import json
from dataclasses import dataclass, field

from qgis.PyQt.QtCore import QDateTime, QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsGeometryGeneratorSymbolLayer,
    QgsPointXY,
    QgsProject,
    QgsProperty,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

try:
    from qgis.core import QgsEditorWidgetSetup
except ImportError:  # older/limited test environments
    QgsEditorWidgetSetup = None

try:
    from ..compat import (
        _GPKG_CREATE_FILE, _GPKG_CREATE_LAYER, _WRITER_NO_ERROR,
        _PROP_FILL_COLOR, _PROP_STROKE_COLOR, _SYMBOL_TYPE_FILL,
    )
    from ..tairu_firebase.models import TairuRecord, points_to_json, now_millis
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import (
        _GPKG_CREATE_FILE, _GPKG_CREATE_LAYER, _WRITER_NO_ERROR,
        _PROP_FILL_COLOR, _PROP_STROKE_COLOR, _SYMBOL_TYPE_FILL,
    )
    from tairu_firebase.models import TairuRecord, points_to_json, now_millis

# (layer name, memory-provider geometry, display label)
LAYER_SPECS = {
    'point': ('registros_ponto', 'Point', 'Pontos'),
    'line': ('registros_linha', 'LineString', 'Linhas'),
    'polygon': ('registros_poligono', 'Polygon', 'Polígonos'),
    'circle': ('registros_circulo', 'Point', 'Círculos'),
    'none': ('registros_sem_geometria', 'None', 'Sem geometria'),
}

FIELD_DEFS = [
    ('recordId', 'string'), ('nome', 'string'), ('descricao', 'string'),
    ('tipoRegistro', 'string'), ('subTipo', 'string'), ('situation', 'string'),
    ('endereco', 'string'), ('owner', 'string'),
    ('plateTag', 'string'), ('brand', 'string'), ('model', 'string'),
    ('year', 'integer'), ('color', 'string'), ('valueEstimate', 'double'),
    ('size', 'double'), ('eventDateTime', 'datetime'),
    ('geometryColor', 'string'), ('geometryBackgroundColor', 'string'),
    ('geometrySize', 'double'), ('circleRadius', 'double'),
    ('isDeleted', 'integer'), ('createdBy', 'string'),
    ('createdAt', 'datetime'), ('lastModified', 'datetime'),
    ('tairuSyncHash', 'string'), ('tairuSyncLastModified', 'string'),
]
SYNC_HASH_FIELD = 'tairuSyncHash'
SYNC_LAST_MODIFIED_FIELD = 'tairuSyncLastModified'
SYNC_SNAPSHOT_PROPERTY = 'tairu/syncSnapshot'
_FIELD_QVARIANT_TYPES = {
    'string': QVariant.String,
    'integer': QVariant.Int,
    'double': QVariant.Double,
    'datetime': QVariant.DateTime,
}
FIELD_ALIASES = {
    'nome': 'Nome',
    'descricao': 'Descrição',
    'tipoRegistro': 'Tipo',
    'subTipo': 'Subtipo',
    'situation': 'Situação',
    'endereco': 'Endereço',
    'owner': 'Responsável',
    'plateTag': 'Placa/Tag',
    'brand': 'Marca',
    'model': 'Modelo',
    'year': 'Ano',
    'color': 'Cor',
    'valueEstimate': 'Valor estimado',
    'size': 'Tamanho',
    'eventDateTime': 'Data do evento',
    'geometrySize': 'Tamanho da geometria',
    'circleRadius': 'Raio do círculo (m)',
}
INTERNAL_FIELDS = {
    'recordId',
    'geometryColor',
    'geometryBackgroundColor',
    'isDeleted',
    'createdBy',
    'createdAt',
    'lastModified',
    SYNC_HASH_FIELD,
    SYNC_LAST_MODIFIED_FIELD,
}

# Circle rendering: buffer directly in degrees with latitude correction.
# (expression transform() proved unreliable inside marker-symbol geometry generators,
# so no CRS round-trip here. ~0.7% ellipse flattening is invisible on screen.)
_CIRCLE_EXPR = (
    'buffer($geometry, coalesce("circleRadius", 0) / '
    '(111320.0 * cos(radians(y(centroid($geometry))))))'
)


def argb_to_hex(argb):
    """Signed/unsigned 32-bit ARGB int -> '#AARRGGBB' (Qt/QGIS hex order, alpha first)."""
    if argb is None:
        return None
    value = int(argb) & 0xFFFFFFFF
    a = (value >> 24) & 0xFF
    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF
    return '#%02X%02X%02X%02X' % (a, r, g, b)


def hex_to_argb(hex_str):
    """'#AARRGGBB' or '#RRGGBB' -> unsigned ARGB int (opaque if no alpha)."""
    if not hex_str:
        return None
    s = hex_str.lstrip('#')
    try:
        if len(s) == 6:
            return 0xFF000000 | int(s, 16)
        if len(s) == 8:
            return int(s, 16)   # already AARRGGBB = ARGB layout
    except ValueError:
        pass
    return None


def spec_key_for_record(rec):
    """Which layer a record belongs to; geometry problems land in 'none'."""
    gtype = rec.geometry_type or 'none'
    if gtype not in LAYER_SPECS or gtype == 'none':
        return 'none'
    if not rec.points():
        return 'none'
    return gtype


def record_geometry(rec, spec_key):
    pts = rec.points()
    if spec_key == 'point' or spec_key == 'circle':
        lat, lon = pts[0]
        return QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
    if spec_key == 'line':
        return QgsGeometry.fromPolylineXY([QgsPointXY(lon, lat) for lat, lon in pts])
    if spec_key == 'polygon':
        ring = [QgsPointXY(lon, lat) for lat, lon in pts]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        return QgsGeometry.fromPolygonXY([ring])
    return None


def _ms_to_qdt(ms):
    return QDateTime.fromMSecsSinceEpoch(int(ms)) if ms else None


def _norm_argb(value):
    return None if value is None else int(value) & 0xFFFFFFFF


def _norm_float(value, precision=9):
    return round(float(value or 0.0), precision)


_GEOMETRY_BEARING_TYPES = ('point', 'line', 'polygon', 'circle')


def normalized_geometry_points(rec, precision=9):
    """Rounded [(lat, lon), ...] with any polygon closing point stripped.

    A record's geometry is defined by its geometryType: if the type is 'none' (or
    anything not geometry-bearing) the record has no geometry, even if stray
    geometryPoints linger in the document — those must be ignored so a no-geometry
    record never diffs against the empty geometry rebuilt from the no-geometry layer.

    QGIS/WKB polygon rings are closed (last vertex == first); the app stores them
    open. Stripping a trailing duplicate on both sides of a comparison makes closed
    and open rings compare equal regardless of which side produced them — this is
    what keeps freshly pulled polygons from showing as phantom updates on push.
    """
    if (rec.geometry_type or 'none') not in _GEOMETRY_BEARING_TYPES:
        return []
    pts = [(round(la, precision), round(lo, precision)) for la, lo in rec.points()]
    if (rec.geometry_type or 'none') == 'polygon' and len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def sync_record_payload(rec):
    """Stable editable-state payload used to detect local/remote divergence.

    Geometry and colors are reduced to their *rendered* form so lossless but
    cosmetically irrelevant differences never register as edits:
    - polygon rings are normalized open (closing point stripped);
    - a record with no points is treated as geometryType 'none' regardless of the
      stored type label (it lands in the no-geometry layer either way);
    - colors are resolved to the actual rendered ARGB, so an absent color and an
      explicit type-default color (which render identically) compare equal.
    """
    pts = normalized_geometry_points(rec)
    return {
        'nome': rec.nome or '',
        'descricao': rec.descricao or '',
        'situation': rec.situation or '',
        'endereco': rec.endereco or '',
        'tipoRegistro': rec.tipo_registro or '',
        'subTipo': rec.sub_tipo or '',
        'owner': rec.owner or '',
        'plateTag': rec.plate_tag or '',
        'brand': rec.brand or '',
        'model': rec.model or '',
        'year': int(rec.year or 0),
        'color': rec.color or '',
        'valueEstimate': _norm_float(rec.value_estimate),
        'size': _norm_float(rec.size),
        'eventDateTime': int(rec.event_date_time or 0),
        'geometryType': (rec.geometry_type or 'none') if pts else 'none',
        'geometryPoints': [[la, lo] for la, lo in pts],
        'circleRadius': _norm_float(rec.circle_radius),
        'geometrySize': _norm_float(rec.geometry_size),
        'geometryColorValue': resolved_color_argb(rec),
        'geometryBackgroundColorValue': resolved_background_argb(rec),
    }


def sync_record_hash(rec):
    payload = json.dumps(sync_record_payload(rec), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def record_to_attribute_map(rec):
    """Field name -> value, in FIELD_DEFS order semantics."""
    return {
        'recordId': rec.record_id,
        'nome': rec.nome,
        'descricao': rec.descricao,
        'tipoRegistro': rec.tipo_registro,
        'subTipo': rec.sub_tipo,
        'situation': rec.situation,
        'endereco': rec.endereco,
        'owner': rec.owner,
        'plateTag': rec.plate_tag,
        'brand': rec.brand,
        'model': rec.model,
        'year': int(rec.year or 0),
        'color': rec.color,
        'valueEstimate': float(rec.value_estimate or 0.0),
        'size': float(rec.size or 0.0),
        'eventDateTime': _ms_to_qdt(rec.event_date_time),
        'geometryColor': _resolved_fg(rec),
        'geometryBackgroundColor': _resolved_bg(rec),
        'geometrySize': rec.geometry_size,
        'circleRadius': rec.circle_radius,
        'isDeleted': 1 if rec.is_deleted else 0,
        'createdBy': rec.created_by,
        'createdAt': _ms_to_qdt(rec.created_at),
        'lastModified': _ms_to_qdt(rec.last_modified),
        SYNC_HASH_FIELD: sync_record_hash(rec),
        SYNC_LAST_MODIFIED_FIELD: str(int(rec.last_modified or 0)),
    }


def ensure_record_layer_fields(layer):
    """Best-effort: add Tairu record fields to a source layer after successful push."""
    if layer is None:
        return False
    fields = layer.fields()
    additions = []
    for name, field_type in FIELD_DEFS:
        if fields.indexOf(name) < 0:
            additions.append(QgsField(name, _FIELD_QVARIANT_TYPES.get(field_type, QVariant.String)))
    if not additions:
        return True
    try:
        if layer.isEditable():
            ok = all(layer.addAttribute(field) for field in additions)
        else:
            ok = layer.dataProvider().addAttributes(additions)
        layer.updateFields()
        return bool(ok)
    except Exception:
        return False


def layer_sync_snapshot(layer):
    if layer is None:
        return {}
    raw = layer.customProperty(SYNC_SNAPSHOT_PROPERTY, '')
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _set_layer_sync_snapshot(layer):
    fields = layer.fields()
    id_idx = fields.indexOf('recordId')
    hash_idx = fields.indexOf(SYNC_HASH_FIELD)
    last_modified_idx = fields.indexOf(SYNC_LAST_MODIFIED_FIELD)
    if id_idx < 0 or hash_idx < 0 or last_modified_idx < 0:
        return
    snapshot = {}
    for feat in layer.getFeatures():
        record_id = feat.attribute(id_idx)
        if not record_id:
            continue
        snapshot[str(record_id)] = {
            'hash': str(feat.attribute(hash_idx) or ''),
            'lastModified': str(feat.attribute(last_modified_idx) or ''),
        }
    try:
        layer.setCustomProperty(
            SYNC_SNAPSHOT_PROPERTY,
            json.dumps(snapshot, sort_keys=True, separators=(',', ':')),
        )
    except Exception:
        pass


def configure_record_layer_fields(layer):
    """Hide internal sync/style fields from ordinary QGIS editing surfaces."""
    if layer is None:
        return
    fields = layer.fields()
    for name, alias in FIELD_ALIASES.items():
        idx = fields.indexOf(name)
        if idx >= 0:
            try:
                layer.setFieldAlias(idx, alias)
            except Exception:
                pass
    for name in INTERNAL_FIELDS:
        idx = fields.indexOf(name)
        if idx < 0:
            continue
        try:
            layer.setFieldEditable(idx, False)
        except Exception:
            pass
        try:
            config = layer.editFormConfig()
            config.setReadOnly(idx, True)
            layer.setEditFormConfig(config)
        except Exception:
            pass
        if QgsEditorWidgetSetup is not None:
            try:
                layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup('Hidden', {}))
            except Exception:
                pass
    try:
        config = layer.attributeTableConfig()
        columns = config.columns()
        changed = False
        for column in columns:
            if getattr(column, 'name', '') in INTERNAL_FIELDS:
                column.hidden = True
                changed = True
        if changed:
            config.setColumns(columns)
            layer.setAttributeTableConfig(config)
    except Exception:
        pass
    try:
        _set_layer_sync_snapshot(layer)
    except Exception:
        pass


# --------------------------------------------------------- local snapshot io

def _gpkg_feature_to_record(feat, fields, spec_key):
    """Convert a GeoPackage QgsFeature to a TairuRecord.

    Geometry is extracted from the WKB geometry column and re-encoded as the
    geometryPoints JSON format TairuRecord.points() expects. The closing
    polygon vertex added by QGIS is stripped so coordinates match Firestore's
    open-ring encoding. Color fields are stored as pre-resolved '#AARRGGBB'
    hex in the gpkg; they are read back as ARGB ints so _diff_fields compares
    them symmetrically via resolved_color_argb() / resolved_background_argb().
    """
    def _str(name, default=''):
        idx = fields.indexOf(name)
        if idx < 0:
            return default
        v = feat.attribute(idx)
        if v is None or (hasattr(v, 'isNull') and v.isNull()):
            return default
        return str(v)

    def _int_attr(name, default=0):
        idx = fields.indexOf(name)
        if idx < 0:
            return default
        v = feat.attribute(idx)
        if v is None or (hasattr(v, 'isNull') and v.isNull()):
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _float_attr(name, default=0.0):
        idx = fields.indexOf(name)
        if idx < 0:
            return default
        v = feat.attribute(idx)
        if v is None or (hasattr(v, 'isNull') and v.isNull()):
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _opt_float_attr(name):
        idx = fields.indexOf(name)
        if idx < 0:
            return None
        v = feat.attribute(idx)
        if v is None or (hasattr(v, 'isNull') and v.isNull()):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _qdt_ms(name):
        idx = fields.indexOf(name)
        if idx < 0:
            return 0
        v = feat.attribute(idx)
        if v is None or (hasattr(v, 'isNull') and v.isNull()):
            return 0
        try:
            return int(v.toMSecsSinceEpoch())
        except (AttributeError, TypeError):
            return 0

    record_id = _str('recordId')
    if not record_id:
        return None

    # Reconstruct geometry_points_json from the WKB geometry column.
    # ts is excluded from sync_record_hash (only lat/lon matter), so the exact
    # ts value here does not affect diff correctness.
    points_json = None
    geom = feat.geometry()
    if geom and not geom.isEmpty() and spec_key != 'none':
        try:
            if spec_key in ('point', 'circle'):
                pt = geom.asPoint()
                pts = [(pt.y(), pt.x())]
            elif spec_key == 'line':
                pts = [(p.y(), p.x()) for p in geom.asPolyline()]
            elif spec_key == 'polygon':
                ring = geom.asPolygon()
                pts = [(p.y(), p.x()) for p in ring[0]] if ring else []
                # QGIS closes the ring (last == first); Firestore stores it open
                if len(pts) > 1 and pts[0] == pts[-1]:
                    pts = pts[:-1]
            else:
                pts = []
            if pts:
                points_json = points_to_json(pts, ts=_qdt_ms('lastModified') or now_millis())
        except Exception:
            pass

    # last_modified: tairuSyncLastModified is written as plain epoch-ms string
    # during pull — prefer it over the QDateTime roundtrip to keep the baseline
    # consistent with what sync_record_hash recorded at pull time.
    last_modified = _qdt_ms('lastModified')
    sync_lm_raw = _str(SYNC_LAST_MODIFIED_FIELD)
    if sync_lm_raw:
        try:
            last_modified = int(sync_lm_raw)
        except (ValueError, TypeError):
            pass

    return TairuRecord(
        record_id=record_id,
        nome=_str('nome'),
        descricao=_str('descricao'),
        situation=_str('situation'),
        endereco=_str('endereco'),
        tipo_registro=_str('tipoRegistro') or 'local',
        sub_tipo=_str('subTipo') or 'outroLocal',
        owner=_str('owner'),
        size=_float_attr('size'),
        plate_tag=_str('plateTag'),
        brand=_str('brand'),
        model=_str('model'),
        year=_int_attr('year'),
        color=_str('color'),
        value_estimate=_float_attr('valueEstimate'),
        event_date_time=_qdt_ms('eventDateTime'),
        geometry_type=spec_key if points_json else 'none',
        geometry_points_json=points_json,
        geometry_bounds_json=None,
        circle_radius=_opt_float_attr('circleRadius') if spec_key == 'circle' else None,
        geometry_size=_opt_float_attr('geometrySize'),
        geometry_color_value=hex_to_argb(_str('geometryColor')),
        geometry_background_color_value=hex_to_argb(_str('geometryBackgroundColor')),
        is_deleted=bool(_int_attr('isDeleted')),
        created_by=_str('createdBy'),
        created_at=_qdt_ms('createdAt'),
        last_modified=last_modified,
    )


def load_local_records(gpkg_path):
    """Read all non-deleted records from the local GeoPackage.

    Returns {record_id: TairuRecord} — the locally-synced snapshot used as the
    comparison baseline in build_push_plan. Returns an empty dict when the gpkg
    does not exist yet (no pull performed), in which case every feature will be
    classified as 'new' by build_push_plan.
    """
    import os
    if not os.path.exists(gpkg_path):
        return {}
    result = {}
    for spec_key in LAYER_SPECS:
        layer = open_gpkg_layer(gpkg_path, spec_key)
        if layer is None:
            continue
        fields = layer.fields()
        if fields.indexOf('recordId') < 0:
            continue
        for feat in layer.getFeatures():
            rec = _gpkg_feature_to_record(feat, fields, spec_key)
            if rec and rec.record_id and not rec.is_deleted:
                result[rec.record_id] = rec
    return result


# ----------------------------------------------------------------- gpkg io

def _memory_uri(geometry):
    fields = '&'.join(f'field={name}:{ftype}' for name, ftype in FIELD_DEFS)
    return f'{geometry}?crs=EPSG:4326&{fields}'


def gpkg_layer_uri(gpkg_path, spec_key):
    return f'{gpkg_path}|layername={LAYER_SPECS[spec_key][0]}'


def open_gpkg_layer(gpkg_path, spec_key):
    layer = QgsVectorLayer(gpkg_layer_uri(gpkg_path, spec_key), LAYER_SPECS[spec_key][0], 'ogr')
    return layer if layer.isValid() else None


def ensure_gpkg(gpkg_path):
    """Create the GeoPackage and any missing record layers (never wipes data)."""
    import os
    first = not os.path.exists(gpkg_path)
    for spec_key, (layer_name, geometry, _label) in LAYER_SPECS.items():
        if not first and open_gpkg_layer(gpkg_path, spec_key) is not None:
            continue
        template = QgsVectorLayer(_memory_uri(geometry), layer_name, 'memory')
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = 'GPKG'
        options.layerName = layer_name
        options.actionOnExistingFile = _GPKG_CREATE_FILE if first else _GPKG_CREATE_LAYER
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            template, gpkg_path, QgsProject.instance().transformContext(), options)
        error = result[0] if isinstance(result, (tuple, list)) else result
        if error != _WRITER_NO_ERROR:
            message = result[1] if isinstance(result, (tuple, list)) and len(result) > 1 else str(error)
            raise RuntimeError(f'Falha ao criar {layer_name} em {gpkg_path}: {message}')
        first = False


# -------------------------------------------------------------- pull merge

@dataclass
class PullResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: list = field(default_factory=list)   # (record_id, reason)


def apply_pull(gpkg_path, records, remove_missing=True):
    """Merge TairuRecord list into the map GeoPackage by recordId."""
    ensure_gpkg(gpkg_path)
    result = PullResult()

    by_spec = {key: [] for key in LAYER_SPECS}
    for rec in records:
        if rec.is_deleted:
            continue
        try:
            key = spec_key_for_record(rec)
            by_spec[key].append(rec)
        except Exception as e:
            result.errors.append((rec.record_id, str(e)))

    for spec_key, recs in by_spec.items():
        layer = open_gpkg_layer(gpkg_path, spec_key)
        if layer is None:
            result.errors.append(('*', f'Camada {LAYER_SPECS[spec_key][0]} inacessível'))
            continue
        ensure_record_layer_fields(layer)
        provider = layer.dataProvider()
        fields = layer.fields()
        id_idx = fields.indexOf('recordId')

        existing = {}
        unpushed_fids = []
        for feat in layer.getFeatures():
            rid = feat.attribute(id_idx)
            if rid:
                existing[rid] = feat.id()
            else:
                unpushed_fids.append(feat.id())

        additions = []
        attr_changes = {}
        geom_changes = {}

        for rec in recs:
            try:
                attr_map = record_to_attribute_map(rec)
                geom = record_geometry(rec, spec_key)
                if rec.record_id in existing:
                    fid = existing[rec.record_id]
                    attr_changes[fid] = {
                        fields.indexOf(name): value for name, value in attr_map.items()
                        if fields.indexOf(name) >= 0
                    }
                    if geom is not None:
                        geom_changes[fid] = geom
                    result.updated += 1
                else:
                    feat = QgsFeature(fields)
                    for name, value in attr_map.items():
                        idx = fields.indexOf(name)
                        if idx >= 0:
                            feat.setAttribute(idx, value)
                    if geom is not None:
                        feat.setGeometry(geom)
                    additions.append(feat)
                    result.added += 1
            except Exception as e:
                result.errors.append((rec.record_id, str(e)))

        removals = []
        if remove_missing:
            # Per-layer membership: also drops records that switched geometry
            # type (they re-appear in their new layer on this same pull).
            layer_ids = {rec.record_id for rec in recs}
            for rid, fid in existing.items():
                if rid not in layer_ids:
                    removals.append(fid)
            # Features with no recordId were created locally and never pushed;
            # a fresh pull replaces local state with the remote snapshot.
            removals.extend(unpushed_fids)
            result.removed += len(removals)

        if attr_changes:
            provider.changeAttributeValues(attr_changes)
        if geom_changes:
            provider.changeGeometryValues(geom_changes)
        if additions:
            provider.addFeatures(additions)
        if removals:
            provider.deleteFeatures(removals)
        layer.updateExtents()

    return result


# Python-side color resolution (mirrors Record.geometryColor / geometryBackgroundColor
# in record_model.dart). We resolve in Python so the GeoPackage always holds a valid
# non-null hex string, and the QGIS expression is a simple field reference.
# push.py uses the same dict to detect and strip back the type-default on push.
TYPE_COLORS = {
    'pessoa': '#FF2196F3',        # Colors.blue
    'local': '#FF4CAF50',         # Colors.green
    'equipamento': '#FFFF9800',   # Colors.orange
    'veiculo': '#FFF44336',       # Colors.red
    'acao': '#FF9C27B0',          # Colors.purple
}
_COLOR_FALLBACK = '#FF9E9E9E'     # Colors.grey


def _resolved_fg(rec):
    """Resolved geometry color: explicit value or type-based fallback."""
    return argb_to_hex(rec.geometry_color_value) or TYPE_COLORS.get(rec.tipo_registro or 'local', _COLOR_FALLBACK)


def _resolved_bg(rec):
    """Resolved background color: explicit, or 30% alpha of fg for poly/circle, else None."""
    if rec.geometry_background_color_value is not None:
        return argb_to_hex(rec.geometry_background_color_value)
    # Only geometries that actually have points get the poly/circle default; a
    # type-only record with no points renders nothing, so it has no background.
    if rec.geometry_type in ('polygon', 'circle') and rec.points():
        return '#4D' + _resolved_fg(rec)[3:9]   # 0x4D = 77 ≈ 30% of 255; [3:9] = RRGGBB of #AARRGGBB
    return None


def resolved_color_argb(rec):
    """Rendered foreground color as normalized ARGB (explicit value or type default).

    Diffing against this (instead of the raw nullable geometryColorValue) means an
    absent color and an explicitly-stored type-default color — which render
    identically — never look like a change.
    """
    return _norm_argb(hex_to_argb(_resolved_fg(rec)))


def resolved_background_argb(rec):
    """Rendered background color as normalized ARGB, or None when not applicable."""
    bg = _resolved_bg(rec)
    return _norm_argb(hex_to_argb(bg)) if bg else None


# ----------------------------------------------------------------- styling

def _data_defined_color(symbol_layer, prop, expression):
    symbol_layer.setDataDefinedProperty(prop, QgsProperty.fromExpression(expression))


# geometryColor is always non-null (pre-resolved in _resolved_fg), so the
# expression is a plain field reference — no CASE or coalesce needed.
_COLOR_EXPR = '"geometryColor"'
# geometryBackgroundColor is pre-resolved for polygon/circle; coalesce is a
# safety net for layers that came from an older pull (null in the gpkg).
# Colors are #AARRGGBB, so RRGGBB is at substr position 4 (1-indexed) with length 6.
_BG_EXPR = "coalesce(\"geometryBackgroundColor\", '#4D' || substr(\"geometryColor\", 4, 6))"


def style_layer(layer, spec_key):
    """Idempotent: builds a fresh renderer each call (safe to re-apply)."""
    try:
        from qgis.core import QgsMarkerSymbol, QgsLineSymbol, QgsSingleSymbolRenderer

        if spec_key == 'point':
            symbol = QgsMarkerSymbol.createSimple({'size': '3'})
            _data_defined_color(symbol.symbolLayer(0), _PROP_FILL_COLOR, _COLOR_EXPR)
        elif spec_key == 'line':
            symbol = QgsLineSymbol.createSimple({'line_width': '0.6'})
            _data_defined_color(symbol.symbolLayer(0), _PROP_STROKE_COLOR, _COLOR_EXPR)
        elif spec_key == 'polygon':
            symbol = QgsFillSymbol.createSimple({'outline_width': '0.4'})
            _data_defined_color(symbol.symbolLayer(0), _PROP_FILL_COLOR, _BG_EXPR)
            _data_defined_color(symbol.symbolLayer(0), _PROP_STROKE_COLOR, _COLOR_EXPR)
        elif spec_key == 'circle':
            symbol = QgsMarkerSymbol.createSimple({'size': '2.4'})
            _data_defined_color(symbol.symbolLayer(0), _PROP_FILL_COLOR, _COLOR_EXPR)
            generator = QgsGeometryGeneratorSymbolLayer.create(
                {'geometryModifier': _CIRCLE_EXPR})
            generator.setSymbolType(_SYMBOL_TYPE_FILL)
            sub = QgsFillSymbol.createSimple({'style': 'solid', 'outline_width': '0.4'})
            sub_layer = sub.symbolLayer(0)
            _data_defined_color(sub_layer, _PROP_FILL_COLOR, _BG_EXPR)
            _data_defined_color(sub_layer, _PROP_STROKE_COLOR, _COLOR_EXPR)
            generator.setSubSymbol(sub)
            symbol.appendSymbolLayer(generator)
        else:
            return

        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()

        # Persist inside the gpkg so the style survives re-opening the file
        try:
            layer.saveStyleToDatabase(f'tairu_{spec_key}', 'Estilo Tairu Maps', True, '')
        except Exception:
            pass
    except Exception:
        pass  # styling must never break a pull


# --------------------------------------------------------------- project

def _find_or_create_group(map_name):
    root = QgsProject.instance().layerTreeRoot()
    tairu_group = root.findGroup('Tairu') or root.addGroup('Tairu')
    map_group = tairu_group.findGroup(map_name) or tairu_group.addGroup(map_name)
    return map_group


def add_record_layers_to_project(gpkg_path, map_name):
    """Add the gpkg record layers to the 'Tairu/{map}' group (idempotent)."""
    group = _find_or_create_group(map_name)
    project = QgsProject.instance()
    added = []

    existing_by_source = {lyr.source(): lyr for lyr in project.mapLayers().values()}
    for spec_key, (layer_name, _geom, geo_suffix) in LAYER_SPECS.items():
        uri = gpkg_layer_uri(gpkg_path, spec_key)
        label = f'{map_name} — {geo_suffix}'
        if uri in existing_by_source:
            configure_record_layer_fields(existing_by_source[uri])
            # Re-apply styling so style fixes reach layers from older pulls
            if spec_key != 'none':
                style_layer(existing_by_source[uri], spec_key)
            continue
        layer = QgsVectorLayer(uri, label, 'ogr')
        if not layer.isValid():
            continue
        configure_record_layer_fields(layer)
        project.addMapLayer(layer, False)
        group.addLayer(layer)
        if spec_key != 'none':
            style_layer(layer, spec_key)
        added.append(layer)
    return added


def add_raster_to_project(mbtiles_path, display_name, map_name):
    from qgis.core import QgsRasterLayer
    group = _find_or_create_group(map_name)
    project = QgsProject.instance()
    for lyr in project.mapLayers().values():
        if lyr.source() == mbtiles_path:
            return lyr
    layer = QgsRasterLayer(mbtiles_path, display_name, 'gdal')
    if not layer.isValid():
        return None
    project.addMapLayer(layer, False)
    group.addLayer(layer)
    return layer
