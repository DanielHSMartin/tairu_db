# -*- coding: utf-8 -*-

"""
Vector layer export into the .tairudb features/vector_layers tables, extracted
from tairu_db_algorithm during the 2.0 refactor.
"""

import contextlib
import json
import uuid

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRenderContext,
)

try:
    from .vector_types import tairudb_type_for_fields
    from .map_identity import feature_uuid_for
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_core.vector_types import tairudb_type_for_fields
    from tairu_core.map_identity import feature_uuid_for


# Categorias que nao puderam ser lidas; inspecionavel em depuracao.
_skipped_categories = []


def _layer_default_color(layer):
    """The colour a feature should get when the renderer resolves no symbol for it.

    `renderer().symbol()` only exists on a SINGLE-SYMBOL renderer. On a
    categorized/graduated/rule-based layer it raises, and the old code fell
    straight to a hardcoded blue — so a feature whose attribute matched no
    category was exported blue instead of the layer's own default. That is the
    exact case a user hits while still filling in the attribute table.

    Tries, in the order a user would call "the layer's default":
      1. single symbol;
      2. the categorized «all other values» category — what QGIS itself draws for
         an unmatched feature, so it is the most faithful answer;
      3. sourceSymbol() — the base symbol categorized/graduated renderers keep;
      4. any symbol the renderer exposes;
      5. blue, only when the layer offers nothing at all.

    Every step is guarded independently: renderer APIs vary across QGIS versions
    and a styling lookup must never fail an export.
    """
    renderer = None
    try:
        renderer = layer.renderer()
    except Exception:
        return "#0000FF"
    if renderer is None:
        return "#0000FF"

    def _name(symbol):
        try:
            return symbol.color().name() if symbol is not None else None
        except Exception:
            return None

    with contextlib.suppress(Exception):
        found = _name(renderer.symbol())
        if found:
            return found

    # «All other values»: the category QGIS renders unmatched features with. Its
    # value is null/empty, which is exactly how QGIS marks it.
    with contextlib.suppress(Exception):
        for category in renderer.categories():
            try:
                value = category.value()
            except Exception:
                # Categoria ilegivel (renderer de outra versao do QGIS): segue
                # para a proxima. Registrado para nao virar um silencio.
                _skipped_categories.append(repr(category))
                continue
            if value is None or (isinstance(value, str) and value == ''):
                found = _name(category.symbol())
                if found:
                    return found

    with contextlib.suppress(Exception):
        found = _name(renderer.sourceSymbol())
        if found:
            return found

    with contextlib.suppress(Exception):
        symbols = renderer.symbols(QgsRenderContext()) or []
        for symbol in symbols:
            found = _name(symbol)
            if found:
                return found

    return "#0000FF"


def _layer_abstract(layer):
    """Layer abstract/description across QGIS versions. QgsMapLayer.abstract() is
    deprecated in favour of serverProperties().abstract(); prefer the new API and
    fall back so it stays quiet on both old and new QGIS."""
    try:
        server_props = layer.serverProperties()
        if server_props is not None:
            return server_props.abstract() or ""
    except (AttributeError, RuntimeError):
        pass
    try:
        return layer.abstract() or ""
    except (AttributeError, RuntimeError):
        return ""


def qvariant_to_python(value):
    """
    Convert QVariant values to native Python types for JSON serialization.

    Args:
        value: Value that might be a QVariant

    Returns:
        Native Python value suitable for JSON serialization
    """
    # Handle None/NULL values
    if value is None:
        return None

    # Try to check if it's a QVariant (might not have direct type check in all QGIS versions)
    # If it has isNull method, it's likely a QVariant
    if hasattr(value, 'isNull'):
        if value.isNull():
            return None
        # Convert QVariant to Python type
        # QVariant should auto-convert with direct assignment in Python
        value = value if not hasattr(value, 'value') else value.value()

    # Handle common types that need special conversion
    if isinstance(value, (list, tuple)):
        return [qvariant_to_python(v) for v in value]
    elif isinstance(value, dict):
        return {k: qvariant_to_python(v) for k, v in value.items()}
    elif isinstance(value, (int, float, str, bool)):
        return value
    elif hasattr(value, 'toString'):  # Qt types like QDateTime, QString
        return value.toString()
    elif hasattr(value, '__str__'):
        return str(value)

    return value


def _load_record_style_helpers():
    """Lazily import the per-feature styling + helpers the records push owns so the
    .tairudb export stays consistent with it (same code path, not a parallel impl):
    feature color/opacity/width, structured styleJson, layer label config, the lossy
    OGC WKB encoder (holes/multipart), and the records-sync-layer detector used to
    keep the Tairu records GeoPackage out of the export.

    Returns (feature_export_style, contour_master_modulo, argb_to_hex,
    feature_export_style_json, layer_label_config, lossy_wkb, is_record_sync_layer);
    all None when tairu_sync is unavailable (no pulled record layers exist in that
    context), in which case the export falls back to the layer's base symbol color
    and geometry-type default size with no styleJson/WKB and no layer exclusion.
    Imported lazily to keep tairu_core import-time independent of tairu_sync.
    """
    try:
        from ..tairu_sync.push import (
            feature_export_style, contour_master_modulo,
            feature_export_style_json, layer_label_config, _feature_lossy_wkb,
        )
        from ..tairu_sync.record_convert import argb_to_hex, is_record_sync_layer
        return (feature_export_style, contour_master_modulo, argb_to_hex,
                feature_export_style_json, layer_label_config,
                _feature_lossy_wkb, is_record_sync_layer)
    except ImportError:
        pass
    except Exception:
        return (None,) * 7
    try:
        from tairu_sync.push import (
            feature_export_style, contour_master_modulo,
            feature_export_style_json, layer_label_config, _feature_lossy_wkb,
        )
        from tairu_sync.record_convert import argb_to_hex, is_record_sync_layer
        return (feature_export_style, contour_master_modulo, argb_to_hex,
                feature_export_style_json, layer_label_config,
                _feature_lossy_wkb, is_record_sync_layer)
    except Exception:
        return (None,) * 7


def export_vector_layers(writer, layers, transform_context, feedback,
                         progress_start=90, progress_span=10):
    """Write the given QGIS vector layers into a TairuDBWriter's vector tables.

    writer: an open TairuDBWriter (created, not finalized).
    layers: list of valid QgsVectorLayer.
    feedback: FeedbackAdapter for progress/cancel/log.
    """
    if not layers:
        feedback.push_info("Nenhuma camada vetorial selecionada para exportação.")
        return

    if transform_context is None:
        transform_context = QgsProject.instance().transformContext()

    (style_fn, modulo_fn, argb_to_hex,
     style_json_fn, label_cfg_fn, lossy_wkb_fn,
     is_record_sync_layer) = _load_record_style_helpers()

    # Never bake the Tairu records-sync GeoPackage into the .tairudb: those features
    # duplicate the live records (they would render as unstyled "ghost" geometry over
    # every record in the app) AND carry record PII (owner/plate/...). The Processing
    # algorithm already filters these; do it here too — both entry points (algorithm
    # AND the generate wizard) route through this function, and the wizard didn't.
    if is_record_sync_layer is not None:
        record_layers = [lyr for lyr in layers if is_record_sync_layer(lyr)]
        if record_layers:
            layers = [lyr for lyr in layers if lyr not in record_layers]
            feedback.push_info(
                "Camada(s) de registros do Tairu ignorada(s) na exportação vetorial "
                "({}): os registros já são sincronizados pelo app; incluí-los criaria "
                "geometrias duplicadas no mapa e exporia dados dos registros.".format(
                    ", ".join(lyr.name() for lyr in record_layers)))
            if not layers:
                return

    for layer_idx, layer in enumerate(layers):
        if feedback.is_canceled():
            return

        # Update progress for vector export
        progress = progress_start + (progress_span * layer_idx / len(layers))
        feedback.set_progress(progress)
        feedback.set_progress_text(f"Exportando camada vetorial: {layer.name()}")

        if not layer.isValid():
            continue

        vector_type = layer.geometryType()
        if vector_type == 0:
            iconType = "locationOn"
            type_str = "point"
            size = 40
        elif vector_type == 1:
            iconType = "line"
            type_str = "line"
            size = 3
        elif vector_type == 2:
            iconType = "polygon"
            type_str = "polygon"
            size = 3
        else:
            iconType = "locationOn"
            type_str = "Unknown"
            size = 10  # Default size for unknown geometry types
        type_str = tairudb_type_for_fields(type_str, layer.fields().names())

        # Try to get layer name/desc from the first feature's attributes
        layer_name = layer.name()
        layer_desc = _layer_abstract(layer)

        # Per-feature styling mirrors the records push: the renderer color for
        # each feature (graduated/categorized/rule-based aware, with opacity), and
        # contour-aware width/opacity for ELEV lines. Resolved inside the feature
        # loop below; `default_color` is the fallback when it can't be resolved.
        default_color = _layer_default_color(layer)  # "#RRGGBB"

        spec_key = {0: 'point', 1: 'line', 2: 'polygon'}.get(vector_type)
        master_modulo = modulo_fn(layer) if modulo_fn is not None else None
        # Layer label settings resolved once and folded into each feature's styleJson.
        label_cfg = label_cfg_fn(layer) if label_cfg_fn is not None else None

        # Prepare transformation to WGS84
        layer_crs = layer.crs()
        transform = QgsCoordinateTransform(
            layer_crs, QgsCoordinateReferenceSystem("EPSG:4326"), transform_context)
        # Validado UMA vez, antes do laco: se a transformacao e invalida, toda
        # feicao seria gravada no .tairudb em metros — geometria no lugar errado,
        # sem erro nenhum. O retorno de transform() era ignorado abaixo.
        if not layer_crs.isValid() or not transform.isValid():
            origem = layer_crs.authid() or layer_crs.description() or 'origem desconhecida'
            feedback.report_error(
                f'Camada "{layer.name()}" não pôde ser reprojetada de {origem} '
                'para WGS84 (EPSG:4326) — não foi exportada.')
            continue

        feature_count = 0
        total_features = layer.featureCount()

        # Generate a UUID for the layer
        layer_uuid = str(uuid.uuid4())
        # Insert the layer into the layers table
        writer.insertVectorLayer(
            layer_uuid,
            type_str,
            layer_name,
            layer_desc
        )

        for feat in layer.getFeatures():
            if feedback.is_canceled():
                feedback.push_info(f"Exportação de camada vetorial cancelada em {layer_name}")
                return

            feature_count += 1
            # Update progress more frequently for better feedback
            if feature_count % 10 == 0 and total_features > 0:
                layer_progress = progress_start + (
                    progress_span
                    * (layer_idx + feature_count / total_features) / len(layers))
                feedback.set_progress(min(99, layer_progress))

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue

            # Feature name from attributes, fallback to layer name + number
            feat_name = layer_name
            attrs = feat.fields().names()
            if "name" in attrs and feat["name"]:
                feat_name = feat["name"]
            elif "Name" in attrs and feat["Name"]:
                feat_name = feat["Name"]
            elif "nome" in attrs and feat["nome"]:
                feat_name = feat["nome"]
            elif "Nome" in attrs and feat["Nome"]:
                feat_name = feat["Nome"]
            else:
                feat_name = f"{layer_name} {feature_count}"

            # Serialize all user attributes as a key-value map. Record PII can't
            # reach here: the records-sync layer is excluded above, so only genuine
            # user layers are exported (a blanket field-name blocklist would instead
            # silently drop common columns like `year`/`owner` from ordinary layers).
            # Convert QVariant values to native Python types for JSON serialization.
            feat_attr = json.dumps({k: qvariant_to_python(feat[k]) for k in attrs})

            # Transform geometry to WGS84 (transform já validado acima)
            geom_wgs = QgsGeometry(geom)
            geom_wgs.transform(transform)
            points_groups = []
            if geom_wgs.isMultipart():
                if vector_type == 0:
                    for pt in geom_wgs.asMultiPoint():
                        points_groups.append(f"{pt.x()} {pt.y()}")
                elif vector_type == 1:
                    for line in geom_wgs.asMultiPolyline():
                        points_groups.append(", ".join(f"{pt.x()} {pt.y()}" for pt in line))
                elif vector_type == 2:
                    for poly in geom_wgs.asMultiPolygon():
                        if poly:
                            ring = poly[0]
                            points_groups.append(", ".join(f"{pt.x()} {pt.y()}" for pt in ring))
            else:
                if vector_type == 0:
                    pt = geom_wgs.asPoint()
                    points_groups.append(f"{pt.x()} {pt.y()}")
                elif vector_type == 1:
                    line = geom_wgs.asPolyline()
                    points_groups.append(", ".join(f"{pt.x()} {pt.y()}" for pt in line))
                elif vector_type == 2:
                    poly = geom_wgs.asPolygon()
                    if poly and poly[0]:
                        ring = poly[0]
                        points_groups.append(", ".join(f"{pt.x()} {pt.y()}" for pt in ring))
            points_str = "; ".join(points_groups)

            # Resolve this feature's rendered color/width the same way the records
            # push does. Falls back to the layer's base color / default size.
            feat_color = default_color
            feat_size = size
            if style_fn is not None:
                with contextlib.suppress(Exception):
                    style_argb, style_size = style_fn(layer, feat, spec_key, master_modulo)
                    if style_argb is not None and argb_to_hex is not None:
                        feat_color = argb_to_hex(style_argb)  # "#AARRGGBB" (alpha = opacity)
                    if style_size is not None:
                        feat_size = style_size

            # Structured styleJson (polygon fill / dash / label) the app resolves
            # through RecordStyle; None for a plain feature (color/size suffice).
            feat_style = None
            if style_json_fn is not None:
                try:
                    feat_style = style_json_fn(layer, feat, spec_key, master_modulo, label_cfg)
                except Exception:
                    feat_style = None

            # OGC WKB (WGS84, 2D) ONLY for polygons whose holes / multipart structure
            # the flat `points` text can't express — the same lossy gate (and encoder)
            # the records push uses, so the two producers stay byte-for-byte in sync.
            # Simple single-ring polygons (and all lines/points) round-trip losslessly
            # via `points`, so they carry no redundant WKB blob.
            feat_wkb = None
            if vector_type == 2 and lossy_wkb_fn is not None:
                feat_wkb = lossy_wkb_fn(feat, transform)

            # Insert each feature as a row
            writer.insertFeature(
                type_str,
                feat_name,
                feat_attr,
                feat_color,
                feat_size,
                iconType,
                points_str,
                layer_uuid,
                feat_style,
                feat_wkb,
                # Stable across re-exports, so a feature the user already
                # incorporated is recognised instead of arriving as a duplicate.
                feature_uuid_for(layer, feat),
            )

    if writer.conn:
        writer.conn.commit()
