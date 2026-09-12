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

import contextlib
import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass, field

try:
    from ..tairu_core.workspace import GPKG_FILE_NAME, WORKSPACE_DIR_NAME
    from ..tairu_core.record_groups import (
        folder_filter, live_groups, sort_key, sql_quote, walk_tree)
    from ..tairu_core.record_icons import (
        FALLBACK_ICON, ICON_CODEPOINTS, SUBTYPE_ICON, TYPE_ICON)
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_core.workspace import GPKG_FILE_NAME, WORKSPACE_DIR_NAME
    from tairu_core.record_groups import (
        folder_filter, live_groups, sort_key, sql_quote, walk_tree)
    from tairu_core.record_icons import (
        FALLBACK_ICON, ICON_CODEPOINTS, SUBTYPE_ICON, TYPE_ICON)

from qgis.PyQt.QtCore import QDateTime, QVariant
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsFontMarkerSymbolLayer,
    QgsMapLayerLegendUtils,
    QgsDefaultValue,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsGeometryGeneratorSymbolLayer,
    QgsLayerTree,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsProperty,
    QgsRendererCategory,
    QgsSingleSymbolRenderer,
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
    # Grupo do app (maps/{mapId}/recordGroups). Sempre por ULTIMO: a lista alimenta o
    # template de memoria do ensure_gpkg, e um GeoPackage antigo ganha a coluna por
    # ensure_record_layer_fields, que so sabe ACRESCENTAR campo que falta.
    ('groupId', 'string'),
    # styleJson CRU do app. Precisa viajar por aqui por dois motivos:
    # 1) o app le a cor do styleJson ANTES do campo simples, entao um envio que so
    #    atualize geometryColorValue nao muda nada na tela para um registro estilizado;
    #    para mesclar em vez de destruir (rotulo, icone, regras) o plugin precisa ter o
    #    estilo original em maos na hora do envio.
    # 2) e onde moram o padrao de traco e o icone que o QGIS passa a desenhar.
    ('style', 'string'),
    # Traco e icone JA RESOLVIDOS pela mesma precedencia do app (styleJson primeiro,
    # depois a sombra simples, depois o subtipo/tipo). Resolvidos aqui, e nao no
    # desenho, para o filtro e a tabela de atributos verem o mesmo que o mapa.
    ('strokePattern', 'string'),
    ('recordIcon', 'string'),
]
GROUP_FIELD = 'groupId'

# Geracao do esquema das colunas de registro. SUBA sempre que acrescentar uma coluna que
# precise vir preenchida do servidor — e o que dispara UM recebimento completo por
# expedicao para preencher o que o delta nao traria (ver workspace.record_schema_generation).
#   1 - groupId
#   2 - style, strokePattern, recordIcon
RECORD_SCHEMA_GENERATION = 2
SYNC_HASH_FIELD = 'tairuSyncHash'
SYNC_LAST_MODIFIED_FIELD = 'tairuSyncLastModified'
SYNC_SNAPSHOT_PROPERTY = 'tairu/syncSnapshot'
# Expedição a que pertence o tairuSyncHash gravado nas feições da camada. Ver
# layer_origin_map_id.
SYNC_MAP_ID_PROPERTY = 'tairu/syncMapId'
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
    GROUP_FIELD: 'Grupo',
    'strokePattern': 'Traço',
    'recordIcon': 'Ícone',
}
INTERNAL_FIELDS = {
    'recordId',
    'geometryColor',
    'geometryBackgroundColor',
    'isDeleted',
    'createdBy',
    'createdAt',
    'lastModified',
    'style',
    'strokePattern',
    'recordIcon',
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


_COORD_PRECISION = 9  # matches push._COORD_PRECISION so pull/push hashes agree


def geometry_from_wkb(wkb):
    """QgsGeometry from OGC WKB, or None. Accepts raw bytes or the base64 string the
    Firestore REST codec produces (from_fields normally decodes it first)."""
    if not wkb:
        return None
    if isinstance(wkb, str):
        import base64
        try:
            wkb = base64.b64decode(wkb)
        except (ValueError, TypeError):
            return None
    try:
        geom = QgsGeometry()
        geom.fromWkb(bytes(wkb))
    except Exception:
        return None
    return geom if not geom.isEmpty() else None


def flat_points_and_type(geom):
    """(flat [(lat, lon)], geometry_type) for a QgsGeometry, mirroring
    push._geometry_points: largest part's exterior ring, polygon closing point
    stripped, rounded to _COORD_PRECISION. Keeping this identical to the push side is
    what makes a WKB-only record's pull-time sync hash match its push-time candidate.
    """
    if geom is None or geom.isEmpty():
        return [], 'none'
    gtype = geom.type()
    type_int = int(gtype) if not isinstance(gtype, int) else gtype
    p = _COORD_PRECISION
    if type_int == 0:  # point
        if geom.isMultipart():
            mp = geom.asMultiPoint()
            if not mp:
                return [], 'none'
            pt = mp[0]
        else:
            pt = geom.asPoint()
        return [(round(pt.y(), p), round(pt.x(), p))], 'point'
    if type_int == 1:  # line
        if geom.isMultipart():
            lines = geom.asMultiPolyline()
            line = max(lines, key=len) if lines else []
        else:
            line = geom.asPolyline()
        return [(round(pt.y(), p), round(pt.x(), p)) for pt in line], 'line'
    if type_int == 2:  # polygon
        if geom.isMultipart():
            polys = geom.asMultiPolygon()
            rings = [part[0] for part in polys if part]
            ring = max(rings, key=len) if rings else []
        else:
            poly = geom.asPolygon()
            ring = poly[0] if poly else []
        if len(ring) > 1 and ring[-1].x() == ring[0].x() and ring[-1].y() == ring[0].y():
            ring = ring[:-1]
        return [(round(pt.y(), p), round(pt.x(), p)) for pt in ring], 'polygon'
    return [], 'none'


def ensure_points_from_wkb(rec):
    """WKB-only records (holed/multipart imports, over-budget writes) arrive with
    geometry_wkb but no flat geometryPoints. Reconstruct the flat points from the WKB
    so spec_key_for_record files the record into its real layer and the sync hash
    matches what the push side derives from the stored geometry. record_geometry still
    builds the QGIS geometry from the full WKB, so holes/parts are preserved on screen.
    """
    if not rec.geometry_wkb or rec.points():
        return
    geom = geometry_from_wkb(rec.geometry_wkb)
    if geom is None:
        return
    pts, gtype = flat_points_and_type(geom)
    if not pts:
        return
    rec.geometry_points_json = points_to_json(pts, ts=rec.last_modified or now_millis())
    if (rec.geometry_type or 'none') not in _GEOMETRY_BEARING_TYPES:
        rec.geometry_type = gtype


def record_geometry(rec, spec_key):
    # Prefer the lossless WKB for lines/polygons so holes and multipart survive in
    # QGIS; the flat point list only carries the exterior/largest part.
    if spec_key in ('line', 'polygon') and rec.geometry_wkb:
        geom = geometry_from_wkb(rec.geometry_wkb)
        if geom is not None:
            return geom
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
        # CRU, nunca resolvido. Um groupId apontando para grupo apagado e um orfao
        # inofensivo no app (renderiza como "Sem grupo" e mantem o vinculo); gravar aqui
        # o valor ja resolvido faria o envio seguinte apagar esse vinculo em silencio.
        # Nunca NULL: e '' que o filtro da pasta "Sem grupo" e o diff comparam.
        GROUP_FIELD: rec.group_id or '',
        'style': rec.style or '',
        'strokePattern': _resolved_stroke(rec),
        'recordIcon': _resolved_icon(rec),
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


def is_record_sync_layer(layer):
    """True when `layer` is a Tairu records-sync layer (the per-map records
    GeoPackage), not an ordinary user vector layer.

    Identity is the field schema written by `ensure_gpkg` /
    `ensure_record_layer_fields`: every records layer carries both `recordId`
    and the `tairuSyncHash` sync field. This survives layer renames and the
    push/pull state (the custom `tairu/syncSnapshot` property is only set after
    a push, so it is not a reliable marker). Used to keep these layers out of
    the .tairudb VECTOR_LAYERS export, which would otherwise bake every record's
    geometry into the basemap file as an unstyled duplicate of the live record.
    """
    if layer is None:
        return False
    try:
        fields = layer.fields()
    except Exception:
        return False
    return fields.indexOf('recordId') >= 0 and fields.indexOf(SYNC_HASH_FIELD) >= 0


def layer_origin_map_id(layer):
    """Expedição de onde vieram os recordId/tairuSyncHash desta camada, ou ''.

    O hash de sincronização é a fotografia do registro NA EXPEDIÇÃO DE ONDE ELE VEIO.
    Enviar a camada para OUTRA expedição e comparar com esse hash responde à pergunta
    errada: os registros saem todos como "inalterados" (prévia vazia, botão Enviar
    desligado), quando na expedição de destino eles nem existem. Por isso build_push_plan
    precisa saber a origem.

    Duas fontes, nesta ordem:
    1. a propriedade gravada no push (cobre a camada própria do usuário, que ganha os
       campos de registro depois de um envio);
    2. o caminho do GeoPackage do pull, {...}/tairu_workspace/{env}/{mapId}/records.gpkg
       — vale para toda camada baixada, inclusive as de projetos anteriores a esta versão,
       que não têm a propriedade.
    """
    if layer is None:
        return ''
    with contextlib.suppress(Exception):
        stored = str(layer.customProperty(SYNC_MAP_ID_PROPERTY, '') or '').strip()
        if stored:
            return stored
    with contextlib.suppress(Exception):
        source = str(layer.source() or '').split('|', 1)[0]
        parts = os.path.normpath(source).split(os.sep)
        # .../tairu_workspace/{env}/{mapId}/records.gpkg -> parts[-2] é a expedição
        if (len(parts) >= 4 and parts[-1] == GPKG_FILE_NAME
                and parts[-4] == WORKSPACE_DIR_NAME):
            return parts[-2]
    return ''


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
    with contextlib.suppress(Exception):
        layer.setCustomProperty(
            SYNC_SNAPSHOT_PROPERTY,
            json.dumps(snapshot, sort_keys=True, separators=(',', ':')),
        )


# Identidade de reserva, guardada NO PROJETO: {fid: {'id', 'hash', 'lastModified'}}.
# Camada cujo provedor recusa gravar campos (KML e afins) perdia o recordId no
# write-back do envio — e o envio seguinte reclassificava a mesma feicao como registro
# NOVO, duplicando os registros da expedicao em silencio (o app mostra poligonos
# empilhados e a edicao de um deles "nao muda nada").
FEATURE_IDS_PROPERTY = 'tairu/featureRecordIds'


def layer_feature_record_ids(layer):
    """{'<fid>': {'id', 'hash', 'lastModified'}} gravado no projeto, ou {}."""
    if layer is None:
        return {}
    try:
        data = json.loads(layer.customProperty(FEATURE_IDS_PROPERTY, '') or '{}')
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def set_layer_feature_record_ids(layer, entries):
    """Mescla {fid: {...}} no mapa de identidade de reserva da camada."""
    if layer is None or not entries:
        return
    merged = layer_feature_record_ids(layer)
    merged.update(entries)
    with contextlib.suppress(Exception):
        layer.setCustomProperty(
            FEATURE_IDS_PROPERTY,
            json.dumps(merged, sort_keys=True, separators=(',', ':')),
        )


def configure_record_layer_fields(layer, group_choices=None):
    """Hide internal sync/style fields from ordinary QGIS editing surfaces.

    `group_choices` ([{nome: id}]) transforma a coluna do grupo numa lista de valores:
    sem ela a tabela de atributos mostra um uuid cru, que nao diz nada e nao ha como
    digitar certo. Com ela, mover um registro de grupo pelo QGIS vira escolher o nome.
    """
    if layer is None:
        return
    fields = layer.fields()
    if group_choices and QgsEditorWidgetSetup is not None:
        idx = fields.indexOf(GROUP_FIELD)
        if idx >= 0:
            with contextlib.suppress(Exception):
                layer.setEditorWidgetSetup(
                    idx, QgsEditorWidgetSetup('ValueMap', {'map': group_choices}))
    for name, alias in FIELD_ALIASES.items():
        idx = fields.indexOf(name)
        if idx >= 0:
            with contextlib.suppress(Exception):
                layer.setFieldAlias(idx, alias)
    for name in INTERNAL_FIELDS:
        idx = fields.indexOf(name)
        if idx < 0:
            continue
        with contextlib.suppress(Exception):
            layer.setFieldEditable(idx, False)
        with contextlib.suppress(Exception):
            config = layer.editFormConfig()
            config.setReadOnly(idx, True)
            layer.setEditFormConfig(config)
        if QgsEditorWidgetSetup is not None:
            with contextlib.suppress(Exception):
                layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup('Hidden', {}))
    with contextlib.suppress(Exception):
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
    with contextlib.suppress(Exception):
        _set_layer_sync_snapshot(layer)


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
        with contextlib.suppress(Exception):
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

    # last_modified: tairuSyncLastModified is written as plain epoch-ms string
    # during pull — prefer it over the QDateTime roundtrip to keep the baseline
    # consistent with what sync_record_hash recorded at pull time.
    last_modified = _qdt_ms('lastModified')
    sync_lm_raw = _str(SYNC_LAST_MODIFIED_FIELD)
    if sync_lm_raw:
        with contextlib.suppress((ValueError, TypeError)):
            last_modified = int(sync_lm_raw)

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
        group_id=_str(GROUP_FIELD),
        style=_str('style') or None,
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


def _check_provider(ok, provider, what, result):
    """Registra em result.errors uma gravacao que o provedor recusou.

    addFeatures & cia. devolvem False em vez de levantar; ignorar isso fazia o pull
    anunciar "N novos" com o GeoPackage intacto — o registro simplesmente nao
    aparecia na camada, sem erro em lugar nenhum.
    """
    if ok:
        return
    detail = ''
    with contextlib.suppress(Exception):
        detail = '; '.join(provider.errors() or [])
    result.errors.append(('*', f'{what} nao gravado(s): {detail or "provedor recusou"}'))


def apply_pull(gpkg_path, records, remove_missing=True, keep_unpushed=False):
    """Merge TairuRecord list into the map GeoPackage by recordId.

    remove_missing=True  (full pull): records absent from the response are
    deleted locally; unpushed features are cleared.
    keep_unpushed=True: preserva as feicoes SEM recordId — o que o usuario desenhou no
    QGIS e ainda nao enviou. Usado pelo pull de migracao, que so precisa preencher a
    coluna do grupo e nao teria por que levar junto trabalho ainda nao gravado na nuvem.
    remove_missing=False (incremental): only records in the batch are touched —
    soft-deleted ones are removed, geometry-type moves are re-homed.
    """
    ensure_gpkg(gpkg_path)
    result = PullResult()

    # Track every incoming ID regardless of deletion state: incremental mode
    # uses this to remove soft-deleted records and handle geometry-type changes
    # without scanning layers for records that weren't in the delta.
    all_incoming_ids = {rec.record_id for rec in records if rec.record_id}

    by_spec = {key: [] for key in LAYER_SPECS}
    for rec in records:
        if rec.is_deleted:
            continue
        try:
            ensure_points_from_wkb(rec)
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
        hash_idx = fields.indexOf(SYNC_HASH_FIELD)

        group_idx = fields.indexOf(GROUP_FIELD)

        existing = {}
        existing_hashes = {}
        existing_groups = {}
        unpushed_fids = []
        for feat in layer.getFeatures():
            rid = feat.attribute(id_idx)
            if rid:
                existing[rid] = feat.id()
                if hash_idx >= 0:
                    existing_hashes[rid] = str(feat.attribute(hash_idx) or '')
                if group_idx >= 0:
                    value = feat.attribute(group_idx)
                    if value is None or (hasattr(value, 'isNull') and value.isNull()):
                        value = ''
                    existing_groups[rid] = str(value)
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
                    incoming_hash = str(attr_map.get(SYNC_HASH_FIELD) or '')
                    # O grupo entra na comparacao SEPARADAMENTE, e nao no
                    # tairuSyncHash: o hash e tambem a linha de base do envio, e mexer
                    # nele invalidaria o hash gravado em toda feicao ja baixada,
                    # fazendo o proximo envio reclassificar a expedicao INTEIRA como
                    # alterada. Sem esta comparacao, mover um registro de grupo no app
                    # nunca chegava ao QGIS: o delta trazia o registro, o hash batia e
                    # a gravacao era pulada — a pasta antiga ficava com ele para sempre.
                    if (
                        not remove_missing
                        and incoming_hash
                        and existing_hashes.get(rec.record_id) == incoming_hash
                        and existing_groups.get(rec.record_id, '') == (rec.group_id or '')
                    ):
                        continue
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
        layer_ids = {rec.record_id for rec in recs}
        if remove_missing:
            # Full pull: remove every local record absent from the response
            # (deleted, moved to another layer, or never existed remotely).
            for rid, fid in existing.items():
                if rid not in layer_ids:
                    removals.append(fid)
            # Unpushed features have no recordId; a full pull resets local state.
            if not keep_unpushed:
                removals.extend(unpushed_fids)
        else:
            # Incremental: only touch records that arrived in this batch.
            # A record in all_incoming_ids but not in this layer's recs was either
            # soft-deleted (isDeleted=True) or moved to a different geometry layer —
            # remove it here; the other layer will add it if it moved.
            for rid, fid in existing.items():
                if rid in all_incoming_ids and rid not in layer_ids:
                    removals.append(fid)
        result.removed += len(removals)

        label = LAYER_SPECS[spec_key][2]
        if attr_changes:
            _check_provider(provider.changeAttributeValues(attr_changes),
                            provider, f'{label}: atributos', result)
        if geom_changes:
            _check_provider(provider.changeGeometryValues(geom_changes),
                            provider, f'{label}: geometrias', result)
        if additions:
            _check_provider(provider.addFeatures(additions),
                            provider, f'{label}: {len(additions)} registro(s)', result)
        if removals:
            _check_provider(provider.deleteFeatures(removals),
                            provider, f'{label}: remocoes', result)
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
    'ocorrencia': '#FFFF5722',    # Colors.deepOrange
    'trilha': '#FF009688',        # Colors.teal
    'pontoDeInteresse': '#FFFFC107',  # Colors.amber
    'desenho': '#FF607D8B',       # Colors.blueGrey
}
_COLOR_FALLBACK = '#FF9E9E9E'     # Colors.grey


def _style_base(rec):
    """The styleJson representative symbol (its `base` dict), or {}.

    The app is styleJson-first: Record.geometryColor / geometryBackgroundColor read
    style.representative BEFORE the flat geometryColorValue shadow (record_model.dart).
    The flat shadow can diverge from styleJson (e.g. an older write), so we must mirror
    the app's precedence here — otherwise QGIS renders a stale/wrong colour and fill.
    """
    raw = getattr(rec, 'style', None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    base = data.get('base')
    return base if isinstance(base, dict) else {}


def _resolved_fg(rec):
    """Resolved geometry color, matching the app's precedence: styleJson base color
    first, then the flat geometryColorValue shadow, then the record-type default."""
    base_color = _style_base(rec).get('color')
    if isinstance(base_color, int) and not isinstance(base_color, bool):
        return argb_to_hex(base_color)
    return argb_to_hex(rec.geometry_color_value) or TYPE_COLORS.get(rec.tipo_registro or 'local', _COLOR_FALLBACK)


def _resolved_bg(rec):
    """Resolved background color, matching the app: styleJson base bgColor first, then
    the flat shadow, then 30% alpha of fg for poly/circle, else None."""
    base_bg = _style_base(rec).get('bgColor')
    if isinstance(base_bg, int) and not isinstance(base_bg, bool):
        return argb_to_hex(base_bg)
    if rec.geometry_background_color_value is not None:
        return argb_to_hex(rec.geometry_background_color_value)
    # Only geometries that actually have points get the poly/circle default; a
    # type-only record with no points renders nothing, so it has no background.
    if rec.geometry_type in ('polygon', 'circle') and rec.points():
        return '#4D' + _resolved_fg(rec)[3:9]   # 0x4D = 77 ≈ 30% of 255; [3:9] = RRGGBB of #AARRGGBB
    return None


def _resolved_stroke(rec):
    """'solid' | 'dashed' | 'dotted', na precedencia do app.

    styleJson primeiro (Record.strokePattern le style.representative.stroke antes do
    campo simples), depois a sombra plana, depois continuo.
    """
    base_stroke = _style_base(rec).get('stroke')
    if isinstance(base_stroke, str) and base_stroke:
        return base_stroke
    return str(getattr(rec, 'stroke_pattern_name', '') or '') or 'solid'


def _resolved_icon(rec):
    """Nome do icone do catalogo que o app desenharia, ou ''.

    Mesma cadeia do app (Record.recordIcon): a escolha explicita primeiro — no
    styleJson e depois na sombra plana —, e faltando as duas o icone do SUBTIPO e por
    fim o do TIPO. Um valor com ':' e uma imagem embutida (icone de KMZ), nao um nome de
    catalogo: o QGIS nao desenha essas, entao cai na cadeia de tipo, como um app antigo.
    """
    escolhido = _style_base(rec).get('icon')
    if not isinstance(escolhido, str) or not escolhido:
        escolhido = str(getattr(rec, 'icon_name', '') or '')
    if escolhido and ':' not in escolhido and escolhido in ICON_CODEPOINTS:
        return escolhido
    por_subtipo = SUBTYPE_ICON.get(rec.sub_tipo or '')
    if por_subtipo in ICON_CODEPOINTS:
        return por_subtipo
    por_tipo = TYPE_ICON.get(rec.tipo_registro or '')
    if por_tipo in ICON_CODEPOINTS:
        return por_tipo
    return FALLBACK_ICON if FALLBACK_ICON in ICON_CODEPOINTS else ''


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

        # Persist inside the gpkg so the style survives re-opening the file.
        # SO a partir da camada da TABELA INTEIRA: o GeoPackage tem UMA vaga de estilo
        # padrao por tabela, e as vistas de grupo sao dezenas sobre a mesma tabela.
        # Gravar dali faria o estilo de um grupo virar o padrao do arquivo e as feicoes
        # dos outros grupos abrirem sem simbolo nenhum (medido: 2 de 6).
        if layer_subset_string(layer):
            return
        with contextlib.suppress(Exception):
            layer.saveStyleToDatabase(f'tairu_{spec_key}', 'Estilo Tairu Maps', True, '')
    except Exception as exc:
        # Estilo nunca pode derrubar um pull; o registro existe para que
        # "as camadas vieram sem cor" tenha onde ser investigado.
        _log_style_failed(exc)


# --------------------------------------------------------------- project

# Identidade dos nos e camadas que ESTE plugin gerencia. Sempre por propriedade, nunca
# por nome: o usuario renomeia a expedicao e o grupo no painel, e dois grupos irmaos
# podem ter o mesmo nome — casar por nome duplica a arvore ou faz um engolir o outro.
ROOT_GROUP_NAME = 'Tairu Maps'          # raiz do plugin no painel de camadas
NODE_KEY_PROPERTY = 'tairu/nodeKey'      # nos de grupo:  'map:<mapId>' | 'grp:<mapId>:<groupId>'
FOLDER_PROPERTY = 'tairu/folder'         # camadas-folha: '<mapId>|<spec>|<bucket>'
CATEGORIES_PROPERTY = 'tairu/catIds'     # assinatura do conjunto de recordId ja categorizado

# Teto de categorias por camada. Abrir o no de uma camada no painel e QUADRATICO no
# numero de categorias (o QgsLayerTreeProxyModel que o QgsLayerTreeView instala):
# medido em 3.40, 200 -> 0,9 s | 400 -> 3,3 s | 800 -> 14,8 s | 1600 -> 39,1 s.
# Acima do teto a camada volta ao simbolo unico com cor por dado — o registro continua
# com a cor dele, so nao ganha linha propria na legenda.
MAX_RECORD_CATEGORIES = 200

# Versao da RECEITA do simbolo (tamanho, contorno, glifo, traco). Entra na assinatura que
# decide se vale reconstruir o renderizador. SUBA sempre que mudar como o simbolo e
# montado: a assinatura so olha os DADOS do registro, entao uma melhoria no desenho nunca
# chegaria a quem ja tem a camada no projeto — foi o que aconteceu com o tamanho do icone,
# corrigido no codigo e invisivel no QGIS de quem ja tinha recebido.
#   1 - circulo/linha simples com a cor do registro
#   2 - icone em glifo, contorno claro e tamanho proporcional ao do aplicativo
#   3 - icone no MESMO tamanho aparente do aplicativo (pixel logico -> mm a 96 dpi)
#   4 - legenda compacta e categoria coringa fora da lista de camadas
#   5 - tamanho do icone por expressao (revertido: tirava do usuario o controle do tamanho)
#   6 - tamanho estatico de 6 mm no padrao, editavel pela simbologia do QGIS
#   7 - icone por expressao (revertido: o campo Tamanho da simbologia deixava de funcionar)
#   8 - tamanho estatico vindo do ajuste em pixels, mas convertido para milimetros
#   9 - tamanho do icone EM PIXELS de verdade (a unidade do aplicativo), sem conversao
SYMBOL_RECIPE = 9

# Teto de camadas no modo "uma camada por registro" (opcao B). Cada QgsVectorLayer custa
# ~0,64 MB so de existir, independentemente de quantas feicoes mostra, e ~2,7 KB de XML
# no projeto salvo. 300 camadas ja sao ~190 MB de memoria; acima disso o grupo cai no
# modo normal em vez de deixar o QGIS inutilizavel.

_NO_GROUP_LABEL = 'Sem grupo'
_NEW_RECORD_LABEL = 'Novo (ainda nao enviado)'
_SPEC_ORDER = ['point', 'line', 'polygon', 'circle', 'none']


def layer_subset_string(layer):
    """Filtro em vigor na camada, ou '' — tolerante a camada sem a API (testes, mocks).

    É o que distingue uma camada que mostra a TABELA de uma que mostra um RECORTE dela.
    Toda decisão que dependa de "o que não está aqui não existe" tem de consultar isto:
    o filtro entra em getFeatures(), em featureCount() e no snapshot de sincronização.
    """
    try:
        return str(layer.subsetString() or '')
    except Exception:
        return ''


def _raw_source(layer):
    """Caminho da TABELA por tras da camada, sem o filtro.

    source() de uma camada filtrada volta com '|subset=...' colado. Toda comparacao de
    identidade por source() quebra em silencio quando as vistas entram em cena — era por
    isso que cada pull empilhava de novo as camadas nao filtradas por cima da arvore.
    """
    try:
        return str(layer.source() or '').split('|subset=', 1)[0]
    except Exception:
        return ''


def _qcolor(hex_str, fallback=None):
    """QColor a partir do '#AARRGGBB' gravado no GeoPackage.

    Construido componente a componente de proposito: existe divergencia real entre
    '#AARRGGBB' e '#RRGGBBAA' na leitura de hexadecimal de 8 digitos conforme o caminho
    do QGIS, e um alfa trocado por engano rende feicao invisivel sem erro nenhum.
    """
    argb = hex_to_argb(hex_str)
    if argb is None:
        return fallback
    return QColor((argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF, (argb >> 24) & 0xFF)


# Fonte Material Icons, a MESMA que o Flutter embarca — e o que faz o marcador do QGIS
# mostrar o icone do registro em vez de um circulo. Registrada uma vez por sessao; a
# familia so e conhecida depois de registrada, dai o cache.
_FONTE_ICONES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'fonts', 'MaterialIcons-Regular.otf')
_familia_icones = None


def _glifo_pinta(familia, ponto_de_codigo):
    """True quando o glifo realmente deixa tinta na familia resolvida pelo Qt.

    Conferido DESENHANDO, e nao por QFontMetrics.inFont(), que devolve True ate para um
    ponto de codigo inexistente (medido). Sem esta prova o risco e concreto: se o sistema
    ja tiver uma fonte chamada "Material Icons" com outros pontos de codigo, o Qt pode
    resolver para ela e o marcador desenha VAZIO — o ponto simplesmente some do mapa,
    sem erro em lugar nenhum.
    """
    try:
        fonte = QFont(familia)
        fonte.setPixelSize(32)
        imagem = QImage(48, 48, QImage.Format.Format_ARGB32)
        imagem.fill(0)
        pintor = QPainter(imagem)
        try:
            pintor.setFont(fonte)
            pintor.setPen(QColor(0, 0, 0, 255))
            pintor.drawText(imagem.rect(), Qt.AlignmentFlag.AlignCenter, chr(ponto_de_codigo))
        finally:
            pintor.end()
        for y in range(0, 48, 2):
            for x in range(0, 48, 2):
                if imagem.pixelColor(x, y).alpha() > 0:
                    return True
    except Exception:
        return False
    return False


def icon_font_family():
    """Nome da familia da fonte de icones, ou '' quando ela nao pode ser usada.

    Sem a fonte o ponto volta ao circulo de sempre: o icone e fidelidade, nao requisito —
    e um ponto que nao aparece no mapa e pior do que um ponto sem icone.
    """
    global _familia_icones
    if _familia_icones is None:
        _familia_icones = ''
        with contextlib.suppress(Exception):
            if os.path.exists(_FONTE_ICONES):
                identificador = QFontDatabase.addApplicationFont(_FONTE_ICONES)
                familias = QFontDatabase.applicationFontFamilies(identificador)
                referencia = ICON_CODEPOINTS.get(FALLBACK_ICON)
                if familias and referencia and _glifo_pinta(familias[0], referencia):
                    _familia_icones = familias[0]
    return _familia_icones


_PEN_STYLES = {
    'dashed': Qt.PenStyle.DashLine,
    'dotted': Qt.PenStyle.DotLine,
    'solid': Qt.PenStyle.SolidLine,
}


def _apply_stroke(symbol_layer, stroke, para_contorno=False):
    """Aplica o padrao de traco do registro na camada de simbolo."""
    estilo = _PEN_STYLES.get(str(stroke or 'solid'))
    if estilo is None or estilo == Qt.PenStyle.SolidLine:
        return
    with contextlib.suppress(Exception):
        if para_contorno:
            symbol_layer.setStrokeStyle(estilo)
        else:
            symbol_layer.setPenStyle(estilo)


# O icone e dimensionado em PIXELS, e nao em milimetros. E a mesma unidade em que o
# aplicativo guarda markerSize, entao "40" aqui e o mesmo "40" de la, sem conversao e sem
# depender da resolucao do canvas — que num Mac responde 72 dpi e no Windows 96, e foi o
# que fez duas tentativas anteriores saírem com tamanhos diferentes em cada maquina.
#
# TAMANHO ESTATICO, sempre. Um simbolo cujo campo Tamanho o usuario mexe e nada acontece e
# pior do que qualquer problema de aparencia: e por isso que aqui NAO entra tamanho
# definido por dado (expressao). A expressao ganha do campo, em silencio, e a pessoa
# conclui que a simbologia do QGIS nao funciona nesta camada.
#
# A linha na lista de camadas acompanha o tamanho do simbolo, como em qualquer camada do
# QGIS. Nao ha como separar as duas coisas: setLegendNodeSymbolSize e
# setLegendNodeCustomSymbol valem para legenda de LAYOUT, o teto
# qgis/legendsymbolMaximumSize corta em vez de reduzir, e uma QgsMapLayerLegend escrita em
# Python DERRUBA O QGIS (o C++ assume a posse do objeto).

def icon_map_size_px():
    """Tamanho do icone de um registro no tamanho padrao, em PIXELS.

    Nao ha ajuste proprio para isto no plugin: quem quiser outro tamanho muda pela
    simbologia do QGIS, como em qualquer camada. O valor e o padrao do aplicativo.
    """
    return _APP_SIZE_DEFAULT['point']


# Contorno claro por baixo do desenho, como o IconWithStroke do aplicativo: e o que faz o
# icone ser legivel sobre imagem de satelite escura.
_ICON_HALO = QColor(255, 255, 255, 230)
_ICON_HALO_WIDTH = 0.3   # mm


def _icon_marker(icone, tamanho, fg):
    """Marcador com o glifo do icone do registro, ou None.

    Tamanho ESTATICO e so ele: o campo Tamanho da simbologia do QGIS tem de funcionar,
    nesta camada como em qualquer outra. Ver a nota no topo sobre por que aqui nao entra
    tamanho definido por dado.
    """
    ponto = ICON_CODEPOINTS.get(str(icone or ''))
    familia = icon_font_family()
    if ponto is None or not familia:
        return None
    with contextlib.suppress(Exception):
        camada = QgsFontMarkerSymbolLayer(familia, chr(ponto), float(tamanho))
        camada.setColor(fg)
        # PIXELS: o mesmo numero que o aplicativo usa. Sem isto o QGIS entende
        # milimetros e o simbolo sai com um tamanho aparente diferente em cada maquina.
        with contextlib.suppress(Exception):
            camada.setSizeUnit(Qgis.RenderUnit.Pixels)
        with contextlib.suppress(Exception):
            camada.setStrokeColor(_ICON_HALO)
            camada.setStrokeWidth(_ICON_HALO_WIDTH)
        simbolo = QgsMarkerSymbol()
        simbolo.changeSymbolLayer(0, camada)
        return simbolo
    return None


def _record_symbol(spec_key, fg, bg, app_size=None, stroke='solid', icone=''):
    """Simbolo de UM registro, com a cor ASSADA — sem propriedade definida por dados.

    A cor tem de ser estatica: a propriedade definida por dados GANHA da cor do simbolo,
    entao um simbolo categorizado que a carregasse ignoraria em silencio a cor que o
    usuario escolhesse no duplo clique (medido: escolhe verde, o mapa continua vermelho)
    — justamente a edicao individual que esta funcionalidade existe para permitir.
    """
    if fg is None:
        return None
    if bg is None:
        bg = QColor(fg.red(), fg.green(), fg.blue(), 0x4D)   # 0x4D = os 30% do app
    size = _symbol_size_mm(spec_key, app_size)
    if spec_key == 'point':
        com_icone = _icon_marker(icone, size, fg)
        if com_icone is not None:
            return com_icone
        symbol = QgsMarkerSymbol.createSimple({'size': str(size)})
        symbol.setColor(fg)
        with contextlib.suppress(Exception):
            symbol.symbolLayer(0).setSizeUnit(Qgis.RenderUnit.Pixels)
        return symbol
    if spec_key == 'line':
        symbol = QgsLineSymbol.createSimple({'line_width': str(size)})
        symbol.setColor(fg)
        _apply_stroke(symbol.symbolLayer(0), stroke)
        return symbol
    if spec_key == 'polygon':
        symbol = QgsFillSymbol.createSimple({'outline_width': str(size)})
        sym_layer = symbol.symbolLayer(0)
        sym_layer.setFillColor(bg)
        sym_layer.setStrokeColor(fg)
        _apply_stroke(sym_layer, stroke, para_contorno=True)
        return symbol
    if spec_key == 'circle':
        symbol = QgsMarkerSymbol.createSimple({'size': str(size)})
        symbol.setColor(fg)
        generator = QgsGeometryGeneratorSymbolLayer.create({'geometryModifier': _CIRCLE_EXPR})
        generator.setSymbolType(_SYMBOL_TYPE_FILL)
        sub = QgsFillSymbol.createSimple({'style': 'solid', 'outline_width': '0.4'})
        sub_layer = sub.symbolLayer(0)
        sub_layer.setFillColor(bg)
        sub_layer.setStrokeColor(fg)
        _apply_stroke(sub_layer, stroke, para_contorno=True)
        generator.setSubSymbol(sub)
        symbol.appendSymbolLayer(generator)
        return symbol
    return None


# Tamanho: o aplicativo guarda o glifo do ponto e a espessura da linha em pixels
# logicos (padrao 40 e 3, ver _DEFAULT_GEOMETRY_SIZE em push.py); o QGIS desenha em
# milimetros. Os fatores abaixo sao o que mantem o PADRAO identico ao de sempre
# (40 -> 3 mm de marcador, 3 -> 0,6 mm de linha) e fazem um registro mais grosso no
# aplicativo sair mais grosso no QGIS, em vez de todos sairem iguais.
_APP_SIZE_DEFAULT = {'point': 40.0, 'circle': 3.0, 'line': 3.0, 'polygon': 3.0}
_APP_SIZE_TO_MM = {'circle': 2.4 / 3.0, 'line': 0.6 / 3.0, 'polygon': 0.4 / 3.0}


def _symbol_size_mm(spec_key, app_size):
    """Tamanho em mm equivalente ao geometrySize do aplicativo.

    O PONTO nao tem fator fixo: ele sai do ajuste em pixels da tela da expedicao,
    convertido pela resolucao do canvas — e assim que "40 px" aqui significa o mesmo
    tamanho aparente que 40 px no aplicativo, num Mac (72 dpi) ou no Windows (96).
    """
    default = _APP_SIZE_DEFAULT.get(spec_key, 3.0)
    try:
        value = float(app_size)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        value = default
    if spec_key == 'point':
        # Em PIXELS: proporcional ao tamanho que o registro tem no aplicativo.
        return max(1.0, icon_map_size_px() * (value / _APP_SIZE_DEFAULT['point']))
    return max(0.1, value * _APP_SIZE_TO_MM.get(spec_key, 1.0))


def _record_rows(layer, limit=None):
    """[(recordId, nome, corHex, corFundoHex, tamanho)] da camada, sem geometria."""
    fields = layer.fields()
    wanted = ['recordId', 'nome', 'geometryColor', 'geometryBackgroundColor',
              'geometrySize', 'strokePattern', 'recordIcon']
    idx = {name: fields.indexOf(name) for name in wanted}
    if idx['recordId'] < 0:
        return []
    request = QgsFeatureRequest()
    request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
    request.setSubsetOfAttributes([i for i in idx.values() if i >= 0])
    rows = []
    for feature in layer.getFeatures(request):
        def _value(name):
            i = idx[name]
            if i < 0:
                return ''
            value = feature.attribute(i)
            if value is None or (hasattr(value, 'isNull') and value.isNull()):
                return ''
            return str(value)
        record_id = _value('recordId')
        if not record_id:
            continue
        rows.append((record_id, _value('nome'),
                     _value('geometryColor'), _value('geometryBackgroundColor'),
                     _value('geometrySize'), _value('strokePattern'),
                     _value('recordIcon')))
        if limit is not None and len(rows) > limit:
            break
    return rows


def apply_record_legend(layer, spec_key):
    """Uma entrada de legenda por REGISTRO (nome, caixa de visibilidade, simbolo proprio).

    Devolve True quando categorizou. Acima do teto devolve False e a camada fica com o
    simbolo unico de style_layer — a cor por registro continua, so a legenda individual
    e que nao.

    Idempotente e barata de repetir: o conjunto de recordId ja aplicado fica gravado na
    camada, e re-aplicar o renderizador com o no aberto no painel custa de 0,8 s a 15 s.
    Este e o caminho de TODO pull, entao pular quando nada mudou nao e microotimizacao.
    """
    if spec_key == 'none' or layer is None:
        return False
    rows = _record_rows(layer, limit=MAX_RECORD_CATEGORIES)
    if not rows or len(rows) > MAX_RECORD_CATEGORIES:
        return False
    # A assinatura inclui as CORES, e nao so os identificadores: uma cor trocada no
    # aplicativo nao muda o conjunto de registros, entao com a assinatura so de ids o
    # renderizador nunca era reconstruido e a legenda do QGIS ficava com a cor antiga
    # para sempre — "mudo no app e nao muda no QGIS". O nome tambem entra, porque e o
    # rotulo da entrada na legenda.
    signature = 'r%d|%s' % (
        SYMBOL_RECIPE,
        '|'.join(','.join(str(v) for v in row) for row in sorted(rows)))
    if layer.customProperty(CATEGORIES_PROPERTY, '') == signature:
        return True
    categories = []
    for record_id, nome, fg_hex, bg_hex, size, stroke, icone in rows:
        fg = _qcolor(fg_hex, _qcolor(_COLOR_FALLBACK))
        symbol = _record_symbol(spec_key, fg, _qcolor(bg_hex), size, stroke, icone)
        if symbol is None:
            continue
        # Registro sem nome viraria uma linha em branco com caixinha na legenda.
        categories.append(QgsRendererCategory(record_id, symbol, nome or record_id))
    if not categories:
        return False
    # Categoria coringa ("todos os outros valores"). Sem ela, a feicao que o usuario acaba
    # de digitalizar — recordId nulo ate o envio — nao casa com categoria nenhuma e
    # simplesmente NAO E PINTADA: existe no arquivo, aparece na tabela de atributos, conta
    # em featureCount e nao aparece no mapa. Antes desta funcao a camada usava simbolo
    # unico e ela aparecia; a legenda por registro nao pode custar isso.
    fallback = _record_symbol(spec_key, _qcolor(_COLOR_FALLBACK), None)
    if fallback is not None:
        # `None`, e nao `QVariant()`: os dois viram o MESMO QVariant invalido, que e o que
        # faz desta a categoria coringa, mas converter um QVariant() explicito faz o PyQGIS
        # despejar "Invalid conversion of QVariant(QVariant.Null)" no log a cada recebimento.
        # NAO trocar pelo `NULL` do qgis.core, que e o que esse aviso sugere: `NULL` e um
        # QVariant nulo TIPADO, entao a categoria passa a casar com o valor NULL em vez de
        # ser a coringa — a feicao recem-desenhada para de ser pintada e o rotulo de espera
        # vaza para a lista de camadas. Ha teste para as duas coisas.
        categories.append(QgsRendererCategory(None, fallback, _NEW_RECORD_LABEL))
    else:
        fallback = None
    layer.setRenderer(QgsCategorizedSymbolRenderer('recordId', categories))
    layer.setCustomProperty(CATEGORIES_PROPERTY, signature)
    # O coringa e sempre o ULTIMO da lista (acrescentado logo acima).
    _hide_legend_nodes(layer, (len(categories) - 1,) if fallback else (), len(categories))
    layer.triggerRepaint()
    return True


def _hide_legend_nodes(layer, ocultar_indices, total_nos):
    """Tira itens da LISTA DE CAMADAS sem mexer no renderizador.

    Retirar o indice da ORDEM e a unica forma que o QGIS oferece para esconder um item da
    lista. A categoria coringa precisa CONTINUAR existindo — e ela que pinta a feicao
    recem-desenhada, que so ganha identificador no envio —, mas nao tem por que aparecer
    na lista e confundir quem le.

    `total_nos` vem de quem chama (uma categoria = um no de legenda). Contar montando um
    QgsLayerTreeModel sobre a arvore VIVA do projeto era desnecessario e arriscado: e a
    mesma arvore que o painel de camadas do QGIS ja modela.

    NAO tente controlar o TAMANHO do item por aqui: setLegendNodeSymbolSize e
    setLegendNodeCustomSymbol valem para a legenda de LAYOUT e nao mexem uma virgula na
    lista de camadas (medido: minimumIconSize continua 34x30 com os dois). Quem manda no
    tamanho da linha e o tamanho ESTATICO do simbolo — ver _icon_marker.
    """
    if not ocultar_indices or total_nos <= 0:
        return
    with contextlib.suppress(Exception):
        no = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if no is None:
            return
        ocultar = set(ocultar_indices)
        QgsMapLayerLegendUtils.setLegendNodeOrder(
            no, [i for i in range(total_nos) if i not in ocultar])
        # E MANDAR RECONSTRUIR. Gravar a ordem nao basta: o painel de camadas monta os
        # itens quando o renderizador muda, e isso acontece ANTES desta gravacao —
        # entao a ordem nova fica no no e o painel segue exibindo a lista velha, com o
        # item do coringa a vista. So o refresh faz o modelo reler a propriedade.
        with contextlib.suppress(Exception):
            from qgis.utils import iface
            iface.layerTreeView().layerTreeModel().refreshLayerLegend(no)


def _bucket_of(group_id, live_ids):
    """A pasta em que o registro cai: o grupo dele, ou '' quando o vinculo nao resolve."""
    group_id = str(group_id or '')
    return group_id if group_id in live_ids else ''


def _scan_buckets(gpkg_path, live_ids):
    """{spec_key: {bucket: quantidade}} com UMA varredura por tabela.

    Contar chamando featureCount() em cada vista custaria uma consulta por pasta (medido:
    1 s com 800 camadas, a cada pull). Aqui sao 5 varreduras, so da coluna do grupo.
    """
    counts = {}
    for spec_key in LAYER_SPECS:
        layer = open_gpkg_layer(gpkg_path, spec_key)
        if layer is None:
            continue
        idx = layer.fields().indexOf(GROUP_FIELD)
        per_bucket = {}
        request = QgsFeatureRequest()
        request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        if idx >= 0:
            request.setSubsetOfAttributes([idx])
        for feature in layer.getFeatures(request):
            value = feature.attribute(idx) if idx >= 0 else None
            if value is None or (hasattr(value, 'isNull') and value.isNull()):
                value = ''
            bucket = _bucket_of(value, live_ids)
            per_bucket[bucket] = per_bucket.get(bucket, 0) + 1
        if per_bucket:
            counts[spec_key] = per_bucket
    return counts


def _find_or_create_group(map_name, map_id=''):
    """O no da expedicao dentro de ROOT_GROUP_NAME, achado por IDENTIDADE e nao por nome.

    Achar pelo nome criava um segundo grupo toda vez que a expedicao era renomeada (no
    app ou no proprio painel), deixando um grupo fantasma com metade das camadas. A
    adocao de um grupo de versao anterior e feita pelo CONTEUDO — o grupo que ja contem
    camadas desta expedicao —, porque o usuario pode ter renomeado o no antes de atualizar.
    """
    root = QgsProject.instance().layerTreeRoot()
    # Projeto de versao anterior tem o no chamado so 'Tairu': renomeia o que existe em vez
    # de criar um segundo raiz e deixar as camadas divididas entre os dois.
    tairu_group = root.findGroup(ROOT_GROUP_NAME) or root.findGroup('Tairu')
    if tairu_group is None:
        tairu_group = root.addGroup(ROOT_GROUP_NAME)
    elif tairu_group.name() != ROOT_GROUP_NAME:
        tairu_group.setName(ROOT_GROUP_NAME)
    if not map_id:
        return tairu_group.findGroup(map_name) or tairu_group.addGroup(map_name)

    key = f'map:{map_id}'
    # A busca pela CHAVE varre a arvore inteira, e nao so os filhos do raiz: o usuario
    # pode ter criado 'Tairu/Meus projetos' e arrastado a expedicao para dentro. Procurar
    # so um nivel criaria um segundo no com a mesma chave e deixaria para tras uma copia
    # vazia da hierarquia, que nenhuma sincronizacao seguinte remove.
    for child in _descendant_groups(root):
        if child.customProperty(NODE_KEY_PROPERTY, '') == key:
            return child

    def _livre(child):
        """No que nenhuma OUTRA expedicao ja reivindicou.

        Sem isto, duas expedicoes de mesmo nome ('Levantamento', 'Campo 2026' — comum)
        colapsam num unico no: a adocao rouba o no da outra e a chave fica trocando de
        dono a cada recebimento, misturando as pastas das duas de forma permanente.
        """
        claimed = child.customProperty(NODE_KEY_PROPERTY, '')
        return not claimed or claimed == key

    # Adocao de um no criado por versao anterior (sem chave). Por CONTEUDO primeiro: o
    # usuario pode ter renomeado o no antes de atualizar, e o nome nao identifica nada.
    adopted = None
    for child in tairu_group.children():
        if not QgsLayerTree.isGroup(child) or not _livre(child):
            continue
        for node in child.findLayers():
            layer = node.layer()
            if layer is not None and layer_origin_map_id(layer) == map_id:
                adopted = child
                break
        if adopted is not None:
            break
    if adopted is None:
        wanted = unicodedata.normalize('NFC', map_name or map_id)
        for child in tairu_group.children():
            if (QgsLayerTree.isGroup(child) and _livre(child)
                    and unicodedata.normalize('NFC', child.name()) == wanted):
                adopted = child
                break
    if adopted is None:
        adopted = tairu_group.addGroup(map_name or map_id)
    adopted.setCustomProperty(NODE_KEY_PROPERTY, key)
    return adopted


def _descendant_groups(node):
    """Todos os nos de grupo abaixo de `node`, em qualquer profundidade."""
    return list(node.findGroups(True))


def _nodes_by_key(home, prefix):
    """{chave: no} dos nos de grupo do plugin sob `home`.

    Tem de ser REFEITO depois de cada _move_node: mover um no clona a SUBARVORE e
    destroi a original, entao qualquer no guardado dentro do galho movido vira ponteiro
    para objeto C++ morto — e o toque seguinte levanta
    "wrapped C/C++ object has been deleted", que aborta o pull no meio e deixa o painel
    presoem "Baixando registros...". Refazer a varredura custa nada (dezenas de nos).
    """
    found = {}
    for node in _descendant_groups(home):
        key = node.customProperty(NODE_KEY_PROPERTY, '')
        if key.startswith(prefix):
            found[key] = node
    return found


def _move_node(node, new_parent, index=-1):
    """Move um no preservando filhos, camadas e filtros. Devolve o no VALIDO.

    A ordem obrigatoria e inserir o clone ANTES de remover o original, e o original morre
    no removeChildNode — quem guardou o no antigo num indice fica com um ponteiro morto
    ('wrapped C/C++ object has been deleted'). E preciso a guarda de ciclo: mandar um no
    para dentro do proprio descendente insere o clone na subarvore que o remove seguinte
    destroi, e some tudo.
    """
    if node.parent() is new_parent:
        return node
    cursor = new_parent
    while cursor is not None:
        if cursor is node:
            return node
        cursor = cursor.parent()
    clone = node.clone()
    new_parent.insertChildNode(index, clone)
    node.parent().removeChildNode(node)
    return clone


def _leaf_rank(node):
    """Posicao da camada-folha na ordem de geometria, ou None se nao for nossa."""
    layer = node.layer() if hasattr(node, 'layer') else None
    if layer is None:
        return None
    key = layer.customProperty(FOLDER_PROPERTY, '')
    parts = key.split('|')
    if len(parts) < 2 or parts[1] not in _SPEC_ORDER:
        return None
    return _SPEC_ORDER.index(parts[1])


def _add_group_node(parent, name, key, before_key=None):
    """Cria um no de grupo ANTES do no `before_key`, quando ele existir.

    Toda insercao do QGIS e no fim, entao um grupo criado no aplicativo depois da pasta
    "Sem grupo" entraria atras dela. Inserir na posicao certa evita ter de MOVER um no
    existente depois — e mover invalida o objeto original em C++, o que ja custou uma
    excecao no meio do recebimento.
    """
    index = -1
    if before_key:
        for position, child in enumerate(parent.children()):
            if (QgsLayerTree.isGroup(child)
                    and child.customProperty(NODE_KEY_PROPERTY, '') == before_key):
                index = position
                break
    node = parent.insertGroup(index, name) if index >= 0 else parent.addGroup(name)
    node.setCustomProperty(NODE_KEY_PROPERTY, key)
    return node


def _insert_leaf(parent, layer, spec_key):
    """Poe a camada na posicao certa entre as folhas irmas, e nao no fim.

    O painel de camadas do QGIS E a ordem de DESENHO. Acrescentar sempre no fim faz uma
    pasta que so tinha Poligonos e depois ganha Pontos ficar com os pontos POR BAIXO dos
    poligonos do mesmo grupo. Camadas que o usuario tenha posto na pasta nao sao tocadas.
    """
    rank = _SPEC_ORDER.index(spec_key) if spec_key in _SPEC_ORDER else len(_SPEC_ORDER)
    index = -1
    for position, child in enumerate(parent.children()):
        other = _leaf_rank(child)
        if other is not None and other > rank:
            index = position
            break
    node = parent.insertLayer(index, layer) if index >= 0 else parent.addLayer(layer)
    node.setExpanded(False)
    return node


def _has_pending_edits(layer):
    """True quando a camada tem edicao aberta e nao gravada."""
    try:
        return bool(layer.isEditable() and layer.isModified())
    except Exception:
        return False


def _drop_node(node, home):
    """Remove um no de grupo do plugin sem deixar camada orfa nem engolir a do usuario.

    removeChildNode NAO desregistra as camadas: elas continuariam no projeto, fora da
    arvore, voltariam ao reabrir e ainda fariam o pull seguinte pular a camada
    correspondente. E uma camada que o usuario tenha arrastado para dentro do grupo nao
    e nossa para apagar — volta para o no da expedicao.
    """
    project = QgsProject.instance()
    doomed = []
    for child in list(node.findLayers()):
        layer = child.layer()
        if layer is None:
            continue
        if layer.customProperty(FOLDER_PROPERTY, '') and not _has_pending_edits(layer):
            doomed.append(layer.id())
        elif home is not None:
            # Camada do usuario, ou camada nossa com edicao ainda nao gravada: nao e nossa
            # para destruir. Volta para o no da expedicao em vez de sumir com o no.
            home.addLayer(layer)
    parent = node.parent()
    if parent is not None:
        parent.removeChildNode(node)
    for layer_id in doomed:
        with contextlib.suppress(Exception):
            project.removeMapLayer(layer_id)


def _group_choices(by_id):
    """[{nome: id}] para o formulario mostrar o NOME do grupo em vez do uuid cru."""
    choices = [{_NO_GROUP_LABEL: ''}]
    for group in sorted(by_id.values(), key=lambda g: sort_key(g.name)):
        choices.append({group.name or group.group_id: group.group_id})
    return choices


def _set_map_tip(layer, nome_da_pasta=''):
    """Dica ao passar o mouse: o nome do registro, o tipo e a pasta.

    E a ligacao que faltava entre o mapa e a lista de camadas. Na lista o registro
    aparece pelo NOME, mas olhando o poligono no mapa nao da para saber o nome dele —
    numa pasta com dezenas de poligonos, achar a linha correspondente vira tentativa e
    erro. Com a dica, aponta-se a forma e le-se o nome.

    Nao e rotulo de proposito: rotular tudo sujaria o mapa e deixaria de espelhar o
    aplicativo, onde o rotulo e escolha por registro e vem desligado (nos dados reais,
    5 registros em 227 pedem rotulo). A dica aparece so quando o usuario aponta.
    """
    with contextlib.suppress(Exception):
        pasta = str(nome_da_pasta or '').replace('<', '&lt;').replace('>', '&gt;')
        partes = [
            # coalesce NAO basta: nome vazio e string '', nao nulo, e passaria batido
            # deixando a dica com o negrito em branco.
            '<b>[% CASE WHEN coalesce("nome", \'\') <> \'\' '
            'THEN "nome" ELSE \'(sem nome)\' END %]</b>',
            '[% coalesce("tipoRegistro", \'\') %]'
            '[% CASE WHEN coalesce("subTipo", \'\') <> \'\' '
            'THEN \' / \' || "subTipo" ELSE \'\' END %]',
        ]
        if pasta:
            partes.append(pasta)
        layer.setMapTipTemplate('<br/>'.join(partes))


def _leaf(gpkg_path, spec_key, subset, label, key, group_choices=None, group_id=None,
          nome_da_pasta=''):
    """Cria a camada-folha filtrada. None quando o filtro nao vinga."""
    layer = QgsVectorLayer(gpkg_layer_uri(gpkg_path, spec_key), label, 'ogr')
    if not layer.isValid():
        return None
    if subset:
        layer.setSubsetString(subset)     # devolve True mesmo para SQL invalido
        if layer.featureCount() < 0:      # -1 e o unico sintoma de filtro quebrado
            return None
    # ORDEM OBRIGATORIA: o filtro ANTES de configure_record_layer_fields, que e quem
    # grava o tairuSyncHash da camada varrendo getFeatures(). Na ordem inversa o snapshot
    # cobriria a TABELA INTEIRA e um envio desta pasta proporia apagar os registros de
    # todos os outros grupos. As duas ordens sao visualmente identicas.
    configure_record_layer_fields(layer, group_choices=group_choices)
    # Feicao DESENHADA nesta pasta tem de nascer no grupo dela: sem o valor padrao o
    # groupId nasce nulo, a feicao nao casa com o filtro e SOME ao salvar a edicao —
    # ela fica no GeoPackage, invisivel, e o envio a manda para o app sem grupo.
    # O literal vai pela mesma citacao do filtro: apostrofo nao dobrado aqui nao
    # levanta erro, so devolve nulo com um aviso no log.
    if subset is not None and group_id is not None:
        idx = layer.fields().indexOf(GROUP_FIELD)
        if idx >= 0:
            with contextlib.suppress(Exception):
                layer.setDefaultValueDefinition(idx, QgsDefaultValue(sql_quote(group_id)))
    layer.setCustomProperty(FOLDER_PROPERTY, key)
    _set_map_tip(layer, nome_da_pasta)
    return layer


def sync_record_layers(gpkg_path, map_name, map_id='', groups=()):
    """Reconcilia o painel com a arvore de grupos do app. Idempotente.

    Uma camada por (grupo, tipo de geometria), filtrada por groupId sobre as MESMAS
    tabelas — nada e copiado. Sem grupo nenhum na expedicao a arvore e exatamente a de
    antes: as camadas da expedicao, sem filtro, direto sob o no dela.

    """
    project = QgsProject.instance()
    by_id = live_groups(groups)
    home = _find_or_create_group(map_name, map_id)
    counts = _scan_buckets(gpkg_path, set(by_id))
    choices = _group_choices(by_id) if by_id else None

    # ---- nos de grupo, na ordem em que o app os desenha
    node_prefix = f'grp:{map_id}:'
    existing_nodes = _nodes_by_key(home, node_prefix)

    wanted_nodes = {}
    if by_id:
        for group, _depth, parent_id in walk_tree(by_id):
            key = f'{node_prefix}{group.group_id}'
            parent_key = f'{node_prefix}{parent_id}' if parent_id else ''
            node = existing_nodes.get(key)
            parent_node = wanted_nodes.get(parent_key, home) if parent_key else home
            if node is None:
                node = _add_group_node(parent_node, group.name or group.group_id, key,
                                       before_key=node_prefix if parent_node is home else None)
            else:
                moved = _move_node(node, parent_node)
                if moved is not node:
                    # O clone substituiu a SUBARVORE inteira: todo no guardado dentro do
                    # galho movido virou ponteiro morto. Refazer o indice aqui e o que
                    # impede um "wrapped C/C++ object has been deleted" mais adiante —
                    # excecao que aborta o pull no meio e prende o painel em
                    # "Baixando registros...".
                    existing_nodes = _nodes_by_key(home, node_prefix)
                    wanted_nodes = {k: n for k, n in _nodes_by_key(home, node_prefix).items()
                                    if k in wanted_nodes}
                node = moved
                if node.name() != (group.name or group.group_id):
                    node.setName(group.name or group.group_id)
            wanted_nodes[key] = node
        # "Sem grupo" existe SEMPRE que a expedicao tem grupos, mesmo vazia agora: e para
        # onde vai o registro que o usuario tira de um grupo pelo campo Grupo da tabela de
        # atributos. Sem a pasta, a feicao deixa de casar com qualquer filtro, some do
        # painel inteiro, nao pode ser enviada e o recebimento seguinte desfaz a escolha.
        key = f'{node_prefix}'
        node = existing_nodes.get(key)
        if node is None:
            node = _add_group_node(home, _NO_GROUP_LABEL, key)
        else:
            node = _move_node(node, home)
        wanted_nodes[key] = node

    for key, node in _nodes_by_key(home, node_prefix).items():
        if key not in wanted_nodes:
            _drop_node(node, home)

    # ---- camadas-folha
    prefix = f'{map_id}|'
    existing_leaves = {}
    for layer in list(project.mapLayers().values()):
        key = layer.customProperty(FOLDER_PROPERTY, '')
        if key.startswith(prefix):
            existing_leaves[key] = layer

    # Projeto salvo por versao anterior: as 5 camadas de entao nao tem a propriedade de
    # identidade e o codigo antigo as reconhecia pela source(). Sem adota-las aqui elas
    # ficam no painel AO LADO da arvore nova e cada registro passa a ser desenhado duas
    # vezes. Adotadas, o caminho normal cuida do resto: ganham o filtro da pasta, sao
    # relidas, renomeadas e — se aquele balde nao for desejado — removidas na limpeza,
    # preservando a simbologia que o usuario tenha ajustado nelas.
    for spec_key in LAYER_SPECS:
        table_uri = gpkg_layer_uri(gpkg_path, spec_key)
        legacy_key = f'{map_id}|{spec_key}|'
        if legacy_key in existing_leaves:
            continue
        for layer in list(project.mapLayers().values()):
            if (not layer.customProperty(FOLDER_PROPERTY, '')
                    and _raw_source(layer) == table_uri):
                layer.setCustomProperty(FOLDER_PROPERTY, legacy_key)
                existing_leaves[legacy_key] = layer
                break

    added = []
    wanted_leaves = set()
    for spec_key in _SPEC_ORDER:
        per_bucket = counts.get(spec_key) or {}
        for bucket in sorted(per_bucket, key=lambda b: (b == '', sort_key(
                by_id[b].name if b in by_id else _NO_GROUP_LABEL))):
            if not per_bucket.get(bucket):
                continue
            parent = wanted_nodes.get(f'grp:{map_id}:{bucket}', home) if by_id else home
            subset = folder_filter(set(by_id), bucket or None) if by_id else ''
            geo_label = LAYER_SPECS[spec_key][2]
            nome_da_pasta = (by_id[bucket].name if bucket in by_id
                             else (_NO_GROUP_LABEL if by_id else ''))

            key = f'{map_id}|{spec_key}|{bucket}'
            wanted_leaves.add(key)
            layer = existing_leaves.get(key)
            if layer is None:
                layer = _leaf(gpkg_path, spec_key, subset, geo_label, key,
                              group_choices=choices,
                              group_id=bucket if by_id else None,
                              nome_da_pasta=nome_da_pasta)
                if layer is None:
                    continue
                project.addMapLayer(layer, False)
                # Colapsado: materializar as linhas de legenda de um no aberto e
                # quadratico no numero de categorias. O usuario abre a pasta que quer ver.
                _insert_leaf(parent, layer, spec_key)
                added.append(layer)
            else:
                if layer_subset_string(layer) != subset:
                    layer.setSubsetString(subset)
                    if layer.featureCount() < 0 or layer_subset_string(layer) != subset:
                        # setSubsetString devolve True ate para SQL invalido, e recusa em
                        # silencio a troca com edicao em curso. Ficar com o filtro ANTIGO
                        # faria o mesmo registro aparecer em duas pastas.
                        _log_style_failed(
                            f'filtro da pasta nao aplicado em {layer.name()}: {subset}')
                if not layer.isEditable():
                    # apply_pull grava por uma SEGUNDA conexao OGR: sem reler, a camada
                    # que ja esta no projeto continua com a contagem e a extensao antigas.
                    with contextlib.suppress(Exception):
                        layer.reload()
                        layer.updateExtents()
                configure_record_layer_fields(layer, group_choices=choices)
                _set_map_tip(layer, nome_da_pasta)
                node = project.layerTreeRoot().findLayer(layer.id())
                if node is not None and node.parent() is not parent:
                    _move_node(node, parent)
            if not apply_record_legend(layer, spec_key):
                style_layer(layer, spec_key)

    for key, layer in existing_leaves.items():
        if key in wanted_leaves:
            continue
        # removeMapLayer destroi o objeto C++ com o buffer de edicao dentro, sem aviso e
        # sem log. Um balde fica sem chave por acao rotineira (mover o ultimo registro de
        # um grupo, apagar o grupo), entao isto aconteceria com o usuario digitalizando.
        # A camada sobrevivente fica com um filtro que nao rende nada — inofensivo e
        # visivel — e a proxima sincronizacao a recolhe.
        if _has_pending_edits(layer):
            continue
        with contextlib.suppress(Exception):
            project.removeMapLayer(layer.id())

    return added


def add_record_layers_to_project(gpkg_path, map_name, map_id='', groups=()):
    """Compatibilidade: o ponto de entrada continua com o nome antigo."""
    return sync_record_layers(gpkg_path, map_name, map_id, groups)


def add_raster_to_project(mbtiles_path, display_name, map_name, map_id=''):
    from qgis.core import QgsRasterLayer
    group = _find_or_create_group(map_name, map_id)
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


def _log_style_failed(exc):
    """Registra falha ao aplicar estilo numa camada puxada (nunca propaga)."""
    try:
        from qgis.core import QgsMessageLog, Qgis
        QgsMessageLog.logMessage(
            f'TairuDB: falha ao estilizar camada: {exc}', 'TairuDB', Qgis.MessageLevel.Info)
    except Exception:
        print(f'TairuDB: falha ao estilizar camada: {exc}')
