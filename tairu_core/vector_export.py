# -*- coding: utf-8 -*-

"""
Vector layer export into the .tairudb features/vector_layers tables, extracted
from tairu_db_algorithm during the 2.0 refactor.
"""

import json
import uuid

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject

try:
    from .vector_types import tairudb_type_for_fields
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_core.vector_types import tairudb_type_for_fields


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
        if type_str == "contourLine":
            iconType = "line"
            size = 3

        # Try to get layer name/desc from the first feature's attributes
        layer_name = layer.name()
        layer_desc = layer.abstract() if hasattr(layer, "abstract") else ""

        # Read color from symbology
        try:
            symbol = layer.renderer().symbol()
            color = symbol.color().name()  # "#RRGGBB"
        except Exception:
            color = "#0000FF"  # fallback

        # Prepare transformation to WGS84
        layer_crs = layer.crs()
        transform = QgsCoordinateTransform(layer_crs, QgsCoordinateReferenceSystem("EPSG:4326"), transform_context)

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
                layer_progress = progress_start + (progress_span * (layer_idx + feature_count / total_features) / len(layers))
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

            # Serialize all attributes as a key-value map
            # Convert QVariant values to native Python types for JSON serialization
            feat_attr = json.dumps({k: qvariant_to_python(feat[k]) for k in attrs})

            # Transform geometry to WGS84
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
            # Insert each feature as a row
            writer.insertFeature(
                type_str,
                feat_name,
                feat_attr,
                color,
                size,
                iconType,
                points_str,
                layer_uuid
            )

    if writer.conn:
        writer.conn.commit()
