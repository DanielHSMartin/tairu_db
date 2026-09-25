# -*- coding: utf-8 -*-

"""
Abrir um arquivo .tairudb no QGIS: raster, vetores e GRG como camadas, desenhados como
o aplicativo os desenha.

Somente leitura. O .tairudb nunca e alterado: as regioes raster viram MBTiles (a mesma
conversao do download da nuvem) e os vetores e o GRG viram um GeoPackage, numa pasta do
plugin chaveada por caminho + tamanho + data do arquivo. Reabrir o mesmo arquivo
reaproveita a conversao; um arquivo alterado ganha outra pasta, entao nenhuma camada ja
aberta tem o arquivo trocado por baixo dela.

A conversao roda numa QgsTask e nao toca o projeto; so a montagem das camadas roda na
thread da interface.
"""

import contextlib
import hashlib
import json
import math
import os
import shutil
import sqlite3

from qgis.PyQt.QtCore import QSizeF, QVariant
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtWidgets import QFileDialog
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateTransformContext,
    QgsEditorWidgetSetup,
    QgsExpression,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsNullSymbolRenderer,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsProperty,
    QgsRasterLayer,
    QgsRasterMarkerSymbolLayer,
    QgsRendererCategory,
    QgsSingleSymbolRenderer,
    QgsTextFormat,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)

try:
    from ..compat import (
        _GPKG_CREATE_FILE, _GPKG_CREATE_LAYER, _LABEL_OVER_POINT, _LABEL_PROP_QUADRANT,
        _WRITER_NO_ERROR)
    from ..tairu_core.grg_generator import _col_label, _dms_label
    from ..tairu_core.i18n import tr
    from ..tairu_core.layer_tree import TAIRUDB_VIEW_PROPERTY
    from ..tairu_core.mbtiles import read_only_uri, tairudb_to_mbtiles
    from ..tairu_core.record_icons import FALLBACK_ICON, ICON_CODEPOINTS
    from ..tairu_core.reentrancy_guard import run_or_defer
    from ..tairu_core.workspace import WORKSPACE_DIR_NAME, slugify_filename
    from ..tairu_firebase.storage import CanceledError
    from ..tairu_sync.push import _WEBMERC_SCALE_Z0
    from ..tairu_sync.record_convert import _qcolor, _record_symbol, argb_to_hex, hex_to_argb
    from ..tairu_sync.tasks import run_task
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import (
        _GPKG_CREATE_FILE, _GPKG_CREATE_LAYER, _LABEL_OVER_POINT, _LABEL_PROP_QUADRANT,
        _WRITER_NO_ERROR)
    from tairu_core.grg_generator import _col_label, _dms_label
    from tairu_core.i18n import tr
    from tairu_core.layer_tree import TAIRUDB_VIEW_PROPERTY
    from tairu_core.mbtiles import read_only_uri, tairudb_to_mbtiles
    from tairu_core.record_icons import FALLBACK_ICON, ICON_CODEPOINTS
    from tairu_core.reentrancy_guard import run_or_defer
    from tairu_core.workspace import WORKSPACE_DIR_NAME, slugify_filename
    from tairu_firebase.storage import CanceledError
    from tairu_sync.push import _WEBMERC_SCALE_Z0
    from tairu_sync.record_convert import _qcolor, _record_symbol, argb_to_hex, hex_to_argb
    from tairu_sync.tasks import run_task

# Sobe quando a conversao muda: a pasta em cache e chaveada por ele, entao uma conversao
# feita por versao anterior do plugin nao e reaproveitada.
_FORMAT = 1
_CACHE_DIR = 'arquivos'
_GPKG = 'vetores.gpkg'
_MANIFEST = 'manifesto.json'

# Identidade do grupo no painel de camadas: o arquivo de origem e a versao convertida.
_SOURCE_PROPERTY = 'tairu/tairudbFonte'
_KEY_PROPERTY = 'tairu/tairudbChave'

_NAME_FIELD = 'nome'
_UUID_FIELD = 'tairu_uuid'
_STYLE_FIELD = 'tairu_simbolo'   # indice do estilo da feicao; a chave do renderizador
_TECH_FIELDS = (_UUID_FIELD, _STYLE_FIELD)

_KINDS = ('point', 'line', 'polygon')   # tambem a ordem no painel: pontos por cima
_KIND_LABEL = {'point': 'pontos', 'line': 'linhas', 'polygon': 'polígonos'}
_MEMORY_GEOM = {'point': 'MultiPoint', 'line': 'MultiLineString', 'polygon': 'MultiPolygon'}
_DEFAULT_SIZE = {'point': 40.0, 'line': 3.0, 'polygon': 3.0}
_FALLBACK_COLOR = '#FF9E9E9E'
_FIELD_TYPES = {'int': QVariant.LongLong, 'double': QVariant.Double, 'string': QVariant.String}

_GRG_PREFIX = 'grg_'
_GRG_TYPES = ('alphanumeric', 'utm', 'dms', 'geographic')   # os que o app aceita
_ICON_REF = 'tairu:'        # style.icon 'tairu:icon:N' aponta a linha 'icon:N' do metadata
_ICON_META = 'icon:'
_ICON_IMAGE = 'img:'        # no manifesto: icone que e uma imagem do arquivo, nao do catalogo


# ------------------------------------------------------------------ conversao

def cache_dir_for(path):
    """Pasta da conversao de `path`; muda quando o arquivo muda (tamanho ou data)."""
    info = os.stat(path)
    key = f'{_FORMAT}|{os.path.realpath(path)}|{info.st_size}|{info.st_mtime_ns}'
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]
    stem = slugify_filename(os.path.splitext(os.path.basename(path))[0])
    # ponytail: pastas de versoes antigas do arquivo ficam para tras; limpar quando pesar.
    return os.path.join(QgsApplication.qgisSettingsDirPath(), WORKSPACE_DIR_NAME,
                        _CACHE_DIR, f'{stem}-{digest}')


def convert(path, out_dir, report=None, canceled=None):
    """Converte `path` em camadas dentro de `out_dir` e devolve o manifesto.

    Com a pasta ja completa, so le o manifesto. A conversao e montada numa pasta
    '.parcial' renomeada no fim, entao uma interrompida nunca passa por completa.
    Levanta ValueError quando o arquivo nao e um .tairudb.
    """
    manifest_path = os.path.join(out_dir, _MANIFEST)
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding='utf-8') as f:
            return json.load(f)
    work = out_dir + '.parcial'
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        manifest = _convert_into(path, work, report or (lambda *_: None),
                                 canceled or (lambda: False))
        with open(os.path.join(work, _MANIFEST), 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
        shutil.rmtree(out_dir, ignore_errors=True)
        os.replace(work, out_dir)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return manifest


def _convert_into(path, work, report, canceled):
    try:
        conn = sqlite3.connect(read_only_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise ValueError(tr('Não foi possível ler o arquivo: {erro}').format(erro=exc)) from exc
    try:
        try:
            metadata = dict(conn.execute('SELECT name, value FROM metadata'))
        except sqlite3.DatabaseError as exc:
            raise ValueError(tr('O arquivo não é um .tairudb válido.')) from exc
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        layer_names = {}
        if 'vector_layers' in tables:
            for layer_uuid, name in conn.execute('SELECT uuid, name FROM vector_layers ORDER BY id'):
                layer_names[layer_uuid] = name or ''
        buckets, grg_rows = {}, []
        if 'features' in tables:
            cursor = conn.execute('SELECT * FROM features ORDER BY id')
            columns = [d[0] for d in cursor.description]
            for values in cursor:
                row = dict(zip(columns, values))
                # O GRG e identificado pelo iconType, como no app, e nao pelo nome da camada.
                if str(row.get('iconType') or '').startswith(_GRG_PREFIX):
                    grg_rows.append(row)
                elif row.get('type') in _KINDS:
                    buckets.setdefault((row.get('layer_id'), row['type']), []).append(row)
    finally:
        conn.close()

    icons = {k: v for k, v in metadata.items() if str(k).startswith(_ICON_META)}
    gpkg = os.path.join(work, _GPKG)
    entries = _grg_entries(grg_rows, metadata.get('grg_config'), gpkg)

    order = {layer_uuid: i for i, layer_uuid in enumerate(layer_names)}
    kinds_of = {}
    for layer_id, kind in buckets:
        kinds_of.setdefault(layer_id, set()).add(kind)
    keys = sorted(buckets, key=lambda k: (_KINDS.index(k[1]), order.get(k[0], len(order))))
    for number, (layer_id, kind) in enumerate(keys):
        if canceled():
            raise CanceledError()
        report(0.3 * number / len(keys), tr('Convertendo os vetores…'))
        name = layer_names.get(layer_id) or 'Feições'
        if len(kinds_of[layer_id]) > 1:
            name = f'{name} ({_KIND_LABEL[kind]})'
        entry = _vector_entry(buckets[(layer_id, kind)], kind, f'camada_{number + 1}', name,
                              gpkg, icons)
        if entry is not None:
            entries.append(entry)

    try:
        rasters = tairudb_to_mbtiles(
            path, work, base_name='raster',
            progress_cb=lambda f: report(0.3 + 0.7 * f, tr('Convertendo o raster…')))
    except ValueError:   # sem tabela de tiles, ou tabelas vazias: arquivo so de vetores
        rasters = []
    for mbtiles_path, label in rasters:
        entries.append({'kind': 'raster', 'file': os.path.basename(mbtiles_path), 'name': label})

    used = {s['icon'][len(_ICON_IMAGE):] for e in entries for s in e.get('styles', ())
            if s['icon'].startswith(_ICON_IMAGE)}
    stem = os.path.splitext(os.path.basename(path))[0]
    return {
        'format': _FORMAT,
        'title': metadata.get('name') or stem,
        'package': metadata.get('package') == '1',
        'icons': {k: v for k, v in icons.items() if k in used},
        'entries': entries,
    }


def _to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_part(text):
    """'lon lat, lon lat' -> [QgsPointXY], descartando coordenada invalida como o app."""
    points = []
    for pair in text.split(','):
        xy = pair.split()
        lon = _to_float(xy[0]) if len(xy) >= 2 else None
        lat = _to_float(xy[1]) if len(xy) >= 2 else None
        if (lon is not None and lat is not None and math.isfinite(lon) and math.isfinite(lat)
                and -180 <= lon <= 180 and -90 <= lat <= 90):
            points.append(QgsPointXY(lon, lat))
    return points


def _geometry(kind, points, wkb, wanted):
    """Geometria multi da feicao: o WKB quando existe (furos e partes), senao o texto."""
    if wkb:
        geom = QgsGeometry()
        with contextlib.suppress(Exception):
            geom.fromWkb(bytes(wkb))
        if not geom.isEmpty() and geom.type() == wanted:
            geom.convertToMultiType()
            return geom
    parts = [p for p in (_parse_part(t) for t in str(points or '').split(';')) if p]
    if kind == 'point':
        geom = QgsGeometry.fromMultiPointXY([p[0] for p in parts])
    elif kind == 'line':
        geom = QgsGeometry.fromMultiPolylineXY([p for p in parts if len(p) >= 2])
    else:
        rings = []
        for ring in parts:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            if len(ring) >= 4:
                rings.append([ring])
        geom = QgsGeometry.fromMultiPolygonXY(rings)
    return None if geom.isNull() or geom.isEmpty() else geom


def _attributes(text):
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {'atributos': str(text)}   # arquivo antigo com texto livre na coluna
    return value if isinstance(value, dict) else {}


def _widen(current, value):
    """Tipo da coluna: int -> double -> string, conforme os valores aparecem."""
    if value is None:
        return current
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 'string'
    if isinstance(value, int) and abs(value) >= 2 ** 63:
        return 'string'
    new = 'int' if isinstance(value, int) else 'double'
    if current in (None, new):
        return new
    return 'string' if current == 'string' else 'double'


def _cell(kind, value):
    if value is None:
        return None
    if kind == 'int':
        return int(value)
    if kind == 'double':
        return float(value)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _unique(name, taken):
    """Nome de coluna livre; o GeoPackage nao distingue maiusculas."""
    base = name.strip() or 'campo'
    candidate, n = base, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f'{base}_{n}'
    taken.add(candidate.lower())
    return candidate


def _number(value):
    """Tamanho positivo do styleJson/coluna, ou None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _argb(value):
    """Cor ARGB inteira do styleJson -> '#AARRGGBB', ou None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return argb_to_hex(value)


def _feature_style(kind, row, icons):
    """(estilo, rotulo) da feicao, na precedencia do app: styleJson, depois as colunas.

    Espelha MapElement.fromTairuDBLayerFeature: sem bgColor o poligono NAO tem
    preenchimento (o app passa fallbackBg nulo), e o ponto ignora a coluna iconType —
    usa o icone do styleJson ou o alfinete padrao.
    """
    base, label = {}, None
    try:
        data = json.loads(row['style']) if row.get('style') else {}
    except (TypeError, ValueError):
        data = {}
    if isinstance(data, dict):
        base = data['base'] if isinstance(data.get('base'), dict) else {}
        label = data['label'] if isinstance(data.get('label'), dict) else None
    # ponytail: so o `base`; regras por atributo (`rules`) nenhum produtor grava em
    # .tairudb hoje. Resolver como RecordStyle.resolve quando algum passar a gravar.
    fg = _argb(base.get('color')) or argb_to_hex(hex_to_argb(row.get('color'))) or _FALLBACK_COLOR
    bg = _argb(base.get('bgColor')) if kind == 'polygon' else None
    size = (_number(base.get('markerSize' if kind == 'point' else 'strokeWidth'))
            or _number(row.get('size')) or _DEFAULT_SIZE[kind])
    stroke = base.get('stroke') if isinstance(base.get('stroke'), str) else 'solid'
    icon = ''
    if kind == 'point':
        spec = base.get('icon')
        spec = spec if isinstance(spec, str) else ''
        if spec.startswith(_ICON_REF) and spec[len(_ICON_REF):] in icons:
            icon = _ICON_IMAGE + spec[len(_ICON_REF):]
        else:
            icon = spec if spec in ICON_CODEPOINTS else FALLBACK_ICON
    return (fg, bg, size, stroke, icon), label


def _write_gpkg(layer, gpkg, table):
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    options.layerName = table
    options.fileEncoding = 'UTF-8'
    options.actionOnExistingFile = _GPKG_CREATE_LAYER if os.path.exists(gpkg) else _GPKG_CREATE_FILE
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, gpkg, QgsCoordinateTransformContext(), options)
    error = result[0] if isinstance(result, (tuple, list)) else result
    if error != _WRITER_NO_ERROR:
        message = result[1] if isinstance(result, (tuple, list)) and len(result) > 1 else str(error)
        raise RuntimeError(tr('Falha ao gravar {tabela} em {arquivo}: {erro}').format(
            tabela=table, arquivo=gpkg, erro=message))


def _memory_layer(geometry, table, columns):
    layer = QgsVectorLayer(f'{geometry}?crs=EPSG:4326', table, 'memory')
    layer.dataProvider().addAttributes([QgsField(name, _FIELD_TYPES[kind]) for name, kind in columns])
    layer.updateFields()
    return layer


def _add_features(layer, rows):
    """rows: [(geometria, [valores])]. Levanta se o provedor recusar."""
    features = []
    for geom, values in rows:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(geom)
        feature.setAttributes(values)
        features.append(feature)
    ok = layer.dataProvider().addFeatures(features)
    ok = ok[0] if isinstance(ok, tuple) else ok
    if not ok:
        raise RuntimeError(tr('{camada}: feições recusadas ({erros})').format(
            camada=layer.name(), erros='; '.join(layer.dataProvider().errors())))


def _vector_entry(rows, kind, table, name, gpkg, icons):
    """Grava UMA camada (um tipo de geometria de uma camada do arquivo) e devolve a entrada."""
    wanted = QgsVectorLayer(f'{_MEMORY_GEOM[kind]}?crs=EPSG:4326', table, 'memory').geometryType()
    styles, style_index, label = [], {}, None
    kept = []   # (geometria, nome, atributos, uuid, indice do estilo)
    for row in rows:
        geom = _geometry(kind, row.get('points'), row.get('wkb'), wanted)
        if geom is None:
            continue
        style, row_label = _feature_style(kind, row, icons)
        if label is None and row_label is not None:
            label = row_label   # ponytail: rotulo por CAMADA — o exportador o grava por camada
        if style not in style_index:
            style_index[style] = len(styles)
            styles.append({'count': 0, 'names': set()})
        index = style_index[style]
        name_text = str(row.get('name') or '')
        styles[index]['count'] += 1
        if len(styles[index]['names']) < 2:
            styles[index]['names'].add(name_text)
        kept.append((geom, name_text, _attributes(row.get('attributes')), str(row.get('uuid') or ''),
                     index))
    if not kept:
        return None

    # Colunas: nome, os atributos do arquivo e as tecnicas. Uma chave 'name'/'nome' que so
    # repete o nome da feicao (o exportador tira o nome dela) nao vira segunda coluna igual.
    types, order, repeats = {}, [], {}
    for _geom, name_text, attrs, _uuid, _index in kept:
        for key, value in attrs.items():
            if key not in types:
                types[key] = None
                order.append(key)
            types[key] = _widen(types[key], value)
            if key.lower() in ('name', 'nome'):
                repeats[key] = repeats.get(key, True) and (
                    value is None or str(value) == name_text)
    keys = [k for k in order if not repeats.get(k)]
    taken = {'fid', _NAME_FIELD, *_TECH_FIELDS}
    columns = [(_NAME_FIELD, 'string')]
    columns += [(_unique(str(k), taken), types[k] or 'string') for k in keys]
    columns += [(_UUID_FIELD, 'string'), (_STYLE_FIELD, 'int')]
    attr_column = {k: columns[i + 1][0] for i, k in enumerate(keys)}

    layer = _memory_layer(_MEMORY_GEOM[kind], table, columns)
    _add_features(layer, [
        (geom, [name_text, *[_cell(types[k] or 'string', attrs.get(k)) for k in keys],
                uuid_text, index])
        for geom, name_text, attrs, uuid_text, index in kept])
    _write_gpkg(layer, gpkg, table)

    ordered = sorted(style_index.items(), key=lambda item: item[1])
    entry_styles = []
    for (fg, bg, size, stroke, icon), index in ordered:
        info = styles[index]
        names = [n for n in info['names'] if n]
        if len(info['names']) == 1 and names:
            legend = names[0]   # todas as feicoes do estilo tem o mesmo nome
        else:
            legend = '1 feição' if info['count'] == 1 else f'{info["count"]} feições'
        entry_styles.append({'fg': fg, 'bg': bg, 'size': size, 'stroke': stroke, 'icon': icon,
                             'label': legend})
    return {'kind': 'vector', 'file': _GPKG, 'table': table, 'name': name, 'geom': kind,
            'styles': entry_styles, 'label': _label_entry(label, attr_column)}


def _label_entry(label, attr_column):
    """Configuracao de rotulo do styleJson -> o que a camada do QGIS precisa, ou None."""
    if not label or label.get('show') is False:
        return None
    name_ref = QgsExpression.quotedColumnRef(_NAME_FIELD)
    field = label.get('field') or 'name'
    expression = name_ref
    if field != 'name' and field in attr_column:
        # Atributo ausente cai no nome, como no app: o rotulo nunca sai em branco.
        expression = f'coalesce({QgsExpression.quotedColumnRef(attr_column[field])}, {name_ref})'
    return {
        'expression': expression,
        'color': _argb(label.get('color')),
        'min_zoom': _to_float(label.get('minZoom')),
        'max_zoom': _to_float(label.get('maxZoom')),
    }


def _grg_entries(rows, config_json, gpkg):
    """Linhas e rotulos do GRG, como o GrgHudOverlay do app os posiciona."""
    try:
        config = json.loads(config_json) if config_json else {}
    except (TypeError, ValueError):
        config = {}
    grid_type = config.get('grid_type') if isinstance(config, dict) else None
    if not rows or grid_type not in _GRG_TYPES:
        return []   # o app tambem nao desenha GRG sem configuracao valida

    lines, cells = [], []
    for row in rows:
        attrs = _attributes(row.get('attributes'))
        label = str(attrs.get('label') or '')
        points = _parse_part(str(row.get('points') or '').split(';')[0])
        icon = row.get('iconType')
        direction = 'row' if str(icon).endswith('_row') else 'col'
        if icon in ('grg_line_row', 'grg_line_col') and len(points) >= 2:
            lines.append((direction, label, points))
        elif icon in ('grg_label_row', 'grg_label_col') and label and points:
            cells.append((direction, label, points[0]))
    if not lines:
        return []
    xs = [p.x() for _d, _l, pts in lines for p in pts]
    ys = [p.y() for _d, _l, pts in lines for p in pts]
    west, north = min(xs), max(ys)

    # Rotulos por fora da borda: colunas acima da borda norte, linhas a esquerda da oeste.
    marks = []
    if grid_type == 'alphanumeric':
        if not cells:   # arquivo anterior ao ponto de rotulo: o app sintetiza dos limites
            cols = sorted((pts[0].x() for d, _l, pts in lines if d == 'col'))
            rows_y = sorted((pts[0].y() for d, _l, pts in lines if d == 'row'), reverse=True)
            cells += [('col', _col_label(i), QgsPointXY((a + b) / 2, 0))
                      for i, (a, b) in enumerate(zip(cols, cols[1:]))]
            cells += [('row', str(i + 1), QgsPointXY(0, (a + b) / 2))
                      for i, (a, b) in enumerate(zip(rows_y, rows_y[1:]))]
        for direction, label, point in cells:
            marks.append((direction, label, point.x() if direction == 'col' else point.y()))
    else:
        for direction, label, pts in lines:
            if not label:
                continue
            value = _to_float(label) if grid_type == 'geographic' else None
            if value is not None:   # grau decimal cru: o app formata; aqui, GMS
                label = _dms_label(value, is_lat=direction == 'row')
            marks.append((direction, label, pts[0].x() if direction == 'col' else pts[0].y()))

    line_layer = _memory_layer('LineString', 'grg_linhas', [('rotulo', 'string'), ('direcao', 'string')])
    _add_features(line_layer, [(QgsGeometry.fromPolylineXY(pts), [label, direction])
                               for direction, label, pts in lines])
    _write_gpkg(line_layer, gpkg, 'grg_linhas')
    entries = []
    if marks:
        mark_layer = _memory_layer('Point', 'grg_rotulos', [('rotulo', 'string'), ('direcao', 'string')])
        _add_features(mark_layer, [
            (QgsGeometry.fromPointXY(QgsPointXY(pos, north) if d == 'col' else QgsPointXY(west, pos)),
             [label, d]) for d, label, pos in marks])
        _write_gpkg(mark_layer, gpkg, 'grg_rotulos')

    line_argb = hex_to_argb(str(config.get('line_color') or '#000000')) or 0xFF000000
    opacity = _to_float(config.get('line_opacity'))
    opacity = 0.8 if opacity is None else min(1.0, max(0.0, opacity))
    style = {
        'color': argb_to_hex((round(opacity * 255) << 24) | (line_argb & 0xFFFFFF)),
        'chip': argb_to_hex((204 << 24) | (line_argb & 0xFFFFFF)),   # fundo do rotulo: 80%
        'width': _number(config.get('line_width')) or 2.0,
        'stroke': str(config.get('line_style') or 'solid'),
        'font_color': argb_to_hex(hex_to_argb(str(config.get('font_color') or '#FFFFFF'))),
        'font_size': min(20.0, max(10.0, _number(config.get('font_size')) or 14.0)),
    }
    if marks:
        entries.append({'kind': 'grg_labels', 'file': _GPKG, 'table': 'grg_rotulos',
                        'name': 'GRG (rótulos)', 'grg': style})
    entries.append({'kind': 'grg_lines', 'file': _GPKG, 'table': 'grg_linhas', 'name': 'GRG',
                    'grg': style})
    return entries


# ------------------------------------------------------------------ projeto

def _symbol(kind, style, icons):
    icon = style.get('icon') or ''
    if icon.startswith(_ICON_IMAGE):
        data = icons.get(icon[len(_ICON_IMAGE):], '')
        if ',' in data:   # data:image/png;base64,....
            marker = QgsRasterMarkerSymbolLayer('base64:' + data.split(',', 1)[1], float(style['size']))
            with contextlib.suppress(Exception):
                marker.setSizeUnit(Qgis.RenderUnit.Pixels)
            symbol = QgsMarkerSymbol()
            symbol.changeSymbolLayer(0, marker)
            return symbol
        icon = FALLBACK_ICON
    fg = _qcolor(style['fg'], QColor(158, 158, 158))
    # Sem fundo = so o contorno, como no app. _record_symbol trocaria None pelos 30%.
    bg = _qcolor(style.get('bg')) or QColor(0, 0, 0, 0)
    return _record_symbol(kind, fg, bg, style['size'], style['stroke'], icon)


def _labeling(label):
    settings = QgsPalLayerSettings()
    settings.fieldName = label['expression']
    settings.isExpression = True
    if label.get('color'):
        text_format = settings.format()
        text_format.setColor(_qcolor(label['color']))
        settings.setFormat(text_format)
    if label.get('min_zoom') is not None or label.get('max_zoom') is not None:
        # minZoom do app = o mais afastado = maior denominador = minimumScale do QGIS.
        settings.scaleVisibility = True
        if label.get('min_zoom') is not None:
            settings.minimumScale = _WEBMERC_SCALE_Z0 / 2 ** label['min_zoom']
        if label.get('max_zoom') is not None:
            settings.maximumScale = _WEBMERC_SCALE_Z0 / 2 ** label['max_zoom']
    return QgsVectorLayerSimpleLabeling(settings)


def _style_vector(layer, entry, icons):
    categories = []
    for index, style in enumerate(entry['styles']):
        symbol = _symbol(entry['geom'], style, icons)
        if symbol is not None:
            categories.append(QgsRendererCategory(index, symbol, style['label']))
    if len(categories) == 1:
        layer.setRenderer(QgsSingleSymbolRenderer(categories[0].symbol().clone()))
    elif categories:
        layer.setRenderer(QgsCategorizedSymbolRenderer(_STYLE_FIELD, categories))
    if entry.get('label'):
        layer.setLabeling(_labeling(entry['label']))
        layer.setLabelsEnabled(True)
    fields = layer.fields()
    for name in _TECH_FIELDS:
        index = fields.indexOf(name)
        if index >= 0:
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup('Hidden', {}))
    config = layer.attributeTableConfig()
    columns = config.columns()
    for column in columns:
        if column.name in _TECH_FIELDS:
            column.hidden = True
    config.setColumns(columns)
    layer.setAttributeTableConfig(config)


def _style_grg_labels(layer, grg):
    """Rotulo em "chip", como o GrgHudOverlay: fundo na cor da linha, texto em negrito."""
    layer.setRenderer(QgsNullSymbolRenderer())
    settings = QgsPalLayerSettings()
    settings.fieldName = 'rotulo'
    settings.placement = _LABEL_OVER_POINT
    props = settings.dataDefinedProperties()
    # Quadrante 1 = acima, 3 = a esquerda: o chip fica por fora da borda da grade.
    props.setProperty(_LABEL_PROP_QUADRANT, QgsProperty.fromExpression(
        "CASE WHEN \"direcao\" = 'col' THEN 1 ELSE 3 END"))
    settings.setDataDefinedProperties(props)
    text_format = QgsTextFormat()
    font = QFont()
    font.setBold(True)
    text_format.setFont(font)
    text_format.setSize(grg['font_size'])
    with contextlib.suppress(Exception):
        text_format.setSizeUnit(Qgis.RenderUnit.Pixels)
    text_format.setColor(_qcolor(grg['font_color'], QColor(255, 255, 255)))
    background = text_format.background()
    background.setEnabled(True)
    background.setFillColor(_qcolor(grg['chip'], QColor(0, 0, 0, 204)))
    background.setStrokeWidth(0)
    background.setSize(QSizeF(1.0, 0.4))
    background.setRadii(QSizeF(0.8, 0.8))
    text_format.setBackground(background)
    settings.setFormat(text_format)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)


def _make_layer(entry, out_dir, icons):
    source = os.path.join(out_dir, entry['file'])
    if entry['kind'] == 'raster':
        layer = QgsRasterLayer(source, entry['name'], 'gdal')
        return layer if layer.isValid() else None
    layer = QgsVectorLayer(f'{source}|layername={entry["table"]}', entry['name'], 'ogr')
    if not layer.isValid():
        return None
    if entry['kind'] == 'vector':
        _style_vector(layer, entry, icons)
    elif entry['kind'] == 'grg_lines':
        grg = entry['grg']
        symbol = _record_symbol('line', _qcolor(grg['color']), None, grg['width'], grg['stroke'])
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    else:
        _style_grg_labels(layer, entry['grg'])
    # Etapa 1 e so leitura: editar a copia convertida nao mudaria o .tairudb.
    layer.setReadOnly(True)
    return layer


def add_to_project(iface, path, out_dir, manifest):
    """Poe as camadas do manifesto num grupo no topo do painel. Thread da interface."""
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    key = os.path.basename(out_dir)
    title = manifest['title']
    for group in root.findGroups(True):
        if group.customProperty(_SOURCE_PROPERTY, '') != path:
            continue
        if group.customProperty(_KEY_PROPERTY, '') == key and group.findLayers():
            _message(iface, tr('{titulo} já está aberto no projeto.').format(titulo=title), Qgis.MessageLevel.Info)
            return
        # Versao anterior do mesmo arquivo (ou grupo esvaziado): sai para dar lugar a atual.
        project.removeMapLayers([node.layerId() for node in group.findLayers()])
        group.parent().removeChildNode(group)

    layers, failed = [], []
    for entry in manifest['entries']:
        layer = _make_layer(entry, out_dir, manifest.get('icons', {}))
        if layer is None:
            failed.append(entry['name'])
        else:
            layers.append(layer)
    if not layers:
        if manifest.get('package'):
            text = tr('{titulo} é um pacote de registros de expedição, não um mapa: '
                      'importe-o no aplicativo Tairu Maps.').format(titulo=title)
        elif failed:
            text = tr('Não foi possível abrir as camadas de {titulo}: {camadas}.').format(
                titulo=title, camadas=', '.join(failed))
        else:
            text = tr('{titulo} não tem raster, vetor nem GRG para abrir.').format(titulo=title)
        _message(iface, text, Qgis.MessageLevel.Warning)
        return

    group = root.insertGroup(0, title)
    group.setCustomProperty(_SOURCE_PROPERTY, path)
    group.setCustomProperty(_KEY_PROPERTY, key)
    for layer in layers:
        layer.setCustomProperty(TAIRUDB_VIEW_PROPERTY, path)
        project.addMapLayer(layer, False)
        # Recolhido: montar a legenda de uma camada com muitos estilos aberta e caro.
        group.addLayer(layer).setExpanded(False)
    text = tr('{titulo}: {n} camada(s) aberta(s).').format(titulo=title, n=len(layers))
    if failed:
        text += ' ' + tr('Não abriram: {camadas}.').format(camadas=', '.join(failed))
    _message(iface, text, Qgis.MessageLevel.Warning if failed else Qgis.MessageLevel.Success)


def _message(iface, text, level):
    iface.messageBar().pushMessage(tr('TairuDB'), text, level, 6)


def open_tairudb(iface, path):
    """Converte `path` em segundo plano e abre as camadas no projeto."""
    path = os.path.abspath(path)
    try:
        out_dir = cache_dir_for(path)
    except OSError as exc:
        _message(iface, tr('Não foi possível abrir {arquivo}: {erro}').format(
            arquivo=os.path.basename(path), erro=exc), Qgis.MessageLevel.Critical)
        return

    def work(task):
        try:
            return convert(path, out_dir, task.report, task.isCanceled)
        except ValueError as exc:
            # Arquivo que nao e .tairudb e erro do usuario, nao "Erro inesperado".
            return {'erro': f'{os.path.basename(path)}: {exc}'}

    def done(manifest):
        if manifest.get('erro'):
            _message(iface, manifest['erro'], Qgis.MessageLevel.Critical)
            return
        run_or_defer(lambda: add_to_project(iface, path, out_dir, manifest))

    run_task(tr('Tairu Maps: abrindo {arquivo}').format(arquivo=os.path.basename(path)), work, on_success=done,
             on_error=lambda message: _message(iface, message, Qgis.MessageLevel.Critical))


def open_tairudb_dialog(iface):
    path, _filter = QFileDialog.getOpenFileName(
        iface.mainWindow(), tr('Abrir arquivo TairuDB'), '', tr('Arquivo TairuDB (*.tairudb)'))
    if path:
        open_tairudb(iface, path)
