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
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
    QgsRenderContext,
)

try:
    from ..tairu_firebase.models import (
        TairuRecord, SITUATIONS_BY_TYPE, now_millis, points_to_json,
        bounds_json_from_points,
    )
    from .record_convert import (
        hex_to_argb, TYPE_COLORS, _COLOR_FALLBACK,
        configure_record_layer_fields, ensure_record_layer_fields,
        layer_sync_snapshot, normalized_geometry_points, record_to_attribute_map,
        resolved_background_argb, resolved_color_argb, sync_record_hash,
        SYNC_HASH_FIELD, SYNC_LAST_MODIFIED_FIELD,
    )
    from .tasks import run_task
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_firebase.models import (
        TairuRecord, SITUATIONS_BY_TYPE, now_millis, points_to_json,
        bounds_json_from_points,
    )
    from tairu_sync.record_convert import (
        hex_to_argb, TYPE_COLORS, _COLOR_FALLBACK,
        configure_record_layer_fields, ensure_record_layer_fields,
        layer_sync_snapshot, normalized_geometry_points, record_to_attribute_map,
        resolved_background_argb, resolved_color_argb, sync_record_hash,
        SYNC_HASH_FIELD, SYNC_LAST_MODIFIED_FIELD,
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

def _color_to_argb(color):
    return ((color.alpha() & 0xFF) << 24) | ((color.red() & 0xFF) << 16) \
        | ((color.green() & 0xFF) << 8) | (color.blue() & 0xFF)


def _layer_symbol_argb(layer):
    """Base symbol color of the layer renderer, as ARGB int."""
    try:
        return _color_to_argb(layer.renderer().symbol().color())
    except Exception:
        return 0xFF2196F3


def _feature_symbol_argb(layer, feature):
    """Renderer color for a specific feature, falling back to the base symbol."""
    try:
        renderer = layer.renderer()
        context = QgsRenderContext()
        renderer.startRender(context, layer.fields())
        try:
            symbol = renderer.symbolForFeature(feature, context)
        finally:
            renderer.stopRender(context)
        if symbol is not None:
            return _color_to_argb(symbol.color())
    except Exception:
        pass
    return _layer_symbol_argb(layer)


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


def _feature_name(feature, mapping, layer_name, index):
    if mapping.get('nome_field'):
        value = _attr(feature, mapping['nome_field'])
        if value:
            return str(value)
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


def feature_to_record(feature, layer, mapping, uid, transform, index):
    """Build the candidate TairuRecord for one feature. Returns (rec, warning)."""
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

    # Round-trip layers carry a pre-resolved geometryColor (always non-null since
    # this session). We strip back the type-default so records that never had an
    # explicit color in the app round-trip as "unchanged" on push.
    # Arbitrary layers (no geometryColor field) get the layer symbol color.
    if feature.fields().indexOf('geometryColor') >= 0:
        color_attr = _attr(feature, 'geometryColor')          # '#AARRGGBB' or None
        raw_color = hex_to_argb(color_attr) if color_attr else None
        if raw_color is not None:
            tipo_val = str(_attr(feature, 'tipoRegistro') or 'local')
            type_hex = TYPE_COLORS.get(tipo_val, _COLOR_FALLBACK)
            # If the stored color is just the type-default, treat as not set
            color_argb = None if _norm_argb(raw_color) == _norm_argb(hex_to_argb(type_hex)) else raw_color
        else:
            color_argb = (mapping.get('color_argb') or _feature_symbol_argb(layer, feature)) \
                if not record_id else None
        # Background: strip default (30% alpha of resolved fg) to avoid false diffs
        bg_attr = _attr(feature, 'geometryBackgroundColor')
        raw_bg = hex_to_argb(bg_attr) if bg_attr else None
        if raw_bg is not None:
            resolved_fg = color_attr or TYPE_COLORS.get(
                str(_attr(feature, 'tipoRegistro') or 'local'), _COLOR_FALLBACK)
            default_bg = hex_to_argb('#4D' + resolved_fg[3:9]) if resolved_fg else None
            bg_argb = None if (default_bg and _norm_argb(raw_bg) == _norm_argb(default_bg)) else raw_bg
        else:
            bg_argb = None
    else:
        color_argb = mapping.get('color_argb') or _feature_symbol_argb(layer, feature)
        bg_attr = _attr(feature, 'geometryBackgroundColor')
        bg_argb = hex_to_argb(bg_attr) if bg_attr else None

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

    for index, feature in enumerate(layer.getFeatures(), start=1):
        candidate, warning = feature_to_record(feature, layer, mapping, uid, transform, index)

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
            mask = list(dict.fromkeys(item.changed_fields + ['lastModified']))
            rec.last_modified = now_millis()
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
