#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import math
import sqlite3
from enum import Enum
import uuid
import json
from typing import Optional

from qgis.PyQt.QtCore import QSize, Qt, QBuffer, QByteArray, QTimer, QCoreApplication
from qgis.PyQt.QtGui import QColor, QImage
from qgis.core import QgsProcessingException  # type: ignore
from datetime import datetime
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterBoolean,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRectangle,
    QgsMapSettings,
    QgsMapRendererSequentialJob,
    QgsProject,
    Qgis,
    QgsGeometry,
    QgsProcessingParameterMultipleLayers,
    QgsMapLayerType,
)


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


class OptimizationMode(Enum):
    Standard = 0
    MemoryEfficient = 1
    CPUEfficient = 2
    StorageEfficient = 3


class MetaTile:
    """Class representing a meta tile for batch rendering"""
    def __init__(self):
        self.zoom = 0
        self.tx = 0
        self.ty = 0
        self.metatile_size = 1  # Ensure non-zero default
        self.actual_size_x = 1  # Ensure non-zero default
        self.actual_size_y = 1  # Ensure non-zero default
        self.extent: Optional[QgsRectangle] = None  # Will be set later
        self.retry_count = 0  # Track retry attempts

    def is_valid(self):
        """Check if the MetaTile has valid values"""
        return (self.metatile_size > 0 and 
                self.actual_size_x > 0 and 
                self.actual_size_y > 0 and
                self.extent is not None and
                not self.extent.isEmpty())


class TairuDBWriter:

    def __init__(self, filename):
        self.filename = filename
        self.conn: Optional[sqlite3.Connection] = None
        self.cursor: Optional[sqlite3.Cursor] = None
        self.region_tables = {}  # Track which tables have been created for each region

    def create(self):
        """Create or open TairuDB database"""
        # Close any existing connection before creating a new one
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass  # Ignore errors during cleanup
            
        # Reset state variables
        self.conn = None
        self.cursor = None
        self.region_tables = {}
        
        try:
            self.conn = sqlite3.connect(self.filename)
            self.cursor = self.conn.cursor()

            # Create tables according to TairuDB specification
            self.cursor.execute("CREATE TABLE IF NOT EXISTS metadata (name text, value text);")
            
            # Create the layers table
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS vector_layers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT UNIQUE NOT NULL,
                    type TEXT,
                    name TEXT,
                    description TEXT
                );
            """)
            # Add layer_id to feature table
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT UNIQUE NOT NULL,
                    layer_id TEXT,
                    type TEXT,
                    name TEXT,
                    attributes TEXT,
                    color TEXT,
                    size INTEGER,
                    iconType TEXT,
                    points TEXT,
                    FOREIGN KEY(layer_id) REFERENCES layers(uuid)
                );
            """)
            
            # Create the regions table
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS regions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT UNIQUE NOT NULL,
                    name TEXT,
                    minzoom INTEGER,
                    maxzoom INTEGER,
                    bounds TEXT
                );
            """)
            return True
        except sqlite3.Error as e:
            print(f"SQLite error: {e}")
            return False

    def createTilesTableForRegion(self, region_id):
        """Create a tiles table for a specific region"""
        if not self.cursor:
            return False
            
        try:
            table_name = f"tiles_region_{region_id}"
            self.cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    zoom_level integer, 
                    tile_column integer, 
                    tile_row integer, 
                    tile_data blob
                );
            """)
            self.cursor.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table_name}_index ON {table_name} (zoom_level, tile_column, tile_row);")
            self.region_tables[region_id] = table_name
            return True
        except sqlite3.Error as e:
            print(f"SQLite error creating tiles table for region {region_id}: {e}")
            return False

    def setMetadataValue(self, name, value):
        """Set a metadata value in the TairuDB file"""
        if not self.cursor:
            return False

        try:
            # Delete existing entry first since table has no unique constraint
            self.cursor.execute("DELETE FROM metadata WHERE name = ?;", (name,))
            self.cursor.execute("INSERT INTO metadata VALUES (?, ?);", (name, value))
            self.conn.commit()  # type: ignore
            return True
        except sqlite3.Error as e:
            print(f"SQLite error setting metadata: {e}")
            return False

    def saveTile(self, zoom, column, row, data, region_id=0):
        """Save a tile to the appropriate TairuDB table for the region with better error handling"""
        if not self.cursor:
            return False

        try:
            # Ensure the table exists for this region
            if region_id not in self.region_tables:
                if not self.createTilesTableForRegion(region_id):
                    return False
            
            table_name = self.region_tables[region_id]
            
            # Validate input data
            if not data or len(data) == 0:
                print(f"Empty tile data for tile {zoom}/{column}/{row} in region {region_id}")
                return False
            
            # Convert to bytes if needed
            if isinstance(data, QByteArray):
                blob_data = bytes(data)
            else:
                blob_data = bytes(data)
            
            # Validate blob_data
            if len(blob_data) == 0:
                print(f"Failed to convert tile data to bytes for {zoom}/{column}/{row} in region {region_id}")
                return False
            
            # Execute the insert with explicit error checking
            self.cursor.execute(
                f"INSERT OR REPLACE INTO {table_name} VALUES (?, ?, ?, ?);",
                (zoom, column, row, blob_data)
            )
            
            # Verify the insert was successful by checking affected rows
            if self.cursor.rowcount <= 0:
                print(f"No rows affected when saving tile {zoom}/{column}/{row} to {table_name}")
                return False
                
            return True
            
        except sqlite3.Error as e:
            print(f"SQLite error saving tile {zoom}/{column}/{row} to region {region_id}: {e}")
            return False
        except Exception as e:
            print(f"Unexpected error saving tile {zoom}/{column}/{row} to region {region_id}: {e}")
            return False

    def insertVectorLayer(self, uuid, type_str, name, desc):
        """Insert a vector layer into the layers table"""
        if not self.cursor:
            return False
        try:
            self.cursor.execute(
                "INSERT OR IGNORE INTO vector_layers (uuid, type, name, description) VALUES (?, ?, ?, ?);",
                (uuid, type_str, name, desc)
            )
            return True
        except sqlite3.Error as e:
            print(f"SQLite error inserting layer: {e}")
            return False

    def insertFeature(self, type_str, name, attr, color, size, iconType, points, layer_id):
        """Insert a feature into the features table"""
        if not self.cursor:
            return False
        try:
            feature_uuid = str(uuid.uuid4())
            self.cursor.execute(
                "INSERT INTO features (uuid, layer_id, type, name, attributes, color, size, iconType, points) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (feature_uuid, layer_id, type_str, name, attr, color, size, iconType, points)
            )
            return True
        except sqlite3.Error as e:
            print(f"SQLite error inserting feature: {e}")
            return False

    def insertRegion(self, name, minzoom, maxzoom, bounds, region_id=None):
        """Insert a region into the regions table
        
        Args:
            name: Name of the region
            minzoom: Minimum zoom level
            maxzoom: Maximum zoom level  
            bounds: Bounds string
            region_id: Optional explicit region ID (if None, uses auto-increment)
        """
        if not self.cursor:
            return False
        try:
            # Generate a UUID for the region
            region_uuid = str(uuid.uuid4())
            if region_id is not None:
                # Explicit ID provided - use it
                self.cursor.execute(
                    "INSERT INTO regions (id, uuid, name, minzoom, maxzoom, bounds) VALUES (?, ?, ?, ?, ?, ?);",
                    (region_id, region_uuid, name, minzoom, maxzoom, bounds)
                )
            else:
                # Use auto-increment
                self.cursor.execute(
                    "INSERT INTO regions (uuid, name, minzoom, maxzoom, bounds) VALUES (?, ?, ?, ?, ?);",
                    (region_uuid, name, minzoom, maxzoom, bounds)
                )
            return True
        except sqlite3.Error as e:
            print(f"SQLite error inserting region: {e}")
            return False

    def finalize(self):
        """Finalize the TairuDB file"""
        if not self.conn:
            return

        try:
            self.conn.commit()
            self.conn.execute("VACUUM;")
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error as e:
            print(f"SQLite error finalizing: {e}")

    def periodicCommit(self):
        """Perform periodic commits to reduce memory usage and ensure data integrity"""
        if self.conn:
            try:
                self.conn.commit()
                return True
            except sqlite3.Error as e:
                print(f"SQLite error during periodic commit: {e}")
                return False
        return False


def TairuDBAlgorithm():
    """
    This algorithm generates XYZ tiles of map canvas content with
    enhanced features and saves them as a TairuDB file, and can also export selected vector layers.
    """

    pluginVersion = "1.2"
    
    # Debug mode - set to True for detailed logging, False for production
    # Cancellation: feedback.isCanceled() doesn't work reliably in QGIS Processing framework
    DEBUG_MODE = False

    # Define constants for parameter names
    EXTENT_POLYGON = 'EXTENT_POLYGON'
    MAP_RESOLUTION = 'MAP_RESOLUTION'
    TILE_FORMAT = 'TILE_FORMAT'
    QUALITY = 'QUALITY'
    VECTOR_LAYERS = 'VECTOR_LAYERS'
    DRY_RUN = 'DRY_RUN'
    OUTPUT_FILE = 'OUTPUT_FILE'

    class Algorithm(QgsProcessingAlgorithm):
        def __init__(self):
            super().__init__()
            self.tairudb_writer: Optional[TairuDBWriter] = None
            self.wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            self.mercator_crs = QgsCoordinateReferenceSystem("EPSG:3857")

            try:
                cpu_count = os.cpu_count() or 4
            except Exception:
                cpu_count = 4

            self.extent = QgsRectangle()
            self.wgs84_extent = QgsRectangle()
            self.layers = []
            self.max_zoom = 18
            self.dpi = 96
            self.tile_format = "PNG"
            self.jpg_quality = 75
            self.metatile_size = 1
            self.optimize_storage = False
            self.filter_empty_tiles = True
            self.progressive_quality = False
            self.include_attribution = False
            self.attribution_text = "© TairuDB contributors"
            self.tile_width = 256
            self.tile_height = 256
            self.threads_number = min(cpu_count, 4)  # Limit concurrent jobs to prevent memory issues
            self.optimization_mode = OptimizationMode.Standard
            self.antialias = True

            # Processing variables
            self.total_tiles = 0
            self.processed_tiles = 0
            self.failed_tiles = 0
            self.retried_tiles = 0
            self.meta_tiles = []
            self.renderer_jobs = {}
            self.transform_context = None
            self.src2wgs = None
            self.wgs2mercator = None
            self.feedback = None

            # New: Add filtered_tiles attribute to store tiles that intersect with polygon
            self.filtered_tiles = []
            self.polygon_geom_wgs84 = None
            self.selected_vector_layers = []
            # New: Store tiles organized by region
            self.region_tiles = {}
            
            # Retry and error handling
            self.max_retries = 3
            self.retry_queue = []
            self.failed_tiles_info = []
            
            # Initialize bounds list attribute to avoid defining outside __init__
            self._all_bounds_list = []

        def debug_log(self, message):
            """Log debug messages only if DEBUG_MODE is enabled"""
            if DEBUG_MODE and self.feedback:
                self.feedback.pushInfo(f"[DEBUG] {message}")

        def name(self):
            return "tairudbgenerator"

        def displayName(self):
            return self.tr("TairuDB")

        def group(self):
            return ""

        def shortHelpString(self):
            return self.tr("Gera um arquivo TairuDB com dos dados do projeto atual e exporta camadas vetoriais selecionadas.")

        def createInstance(self):
            return TairuDBAlgorithm()

        def reset_processing_state(self):
            """Reset all processing state variables to prevent issues between runs"""
            self.debug_log("reset_processing_state: Iniciando reset do estado")
            
            # Reset counters
            self.total_tiles = 0
            self.processed_tiles = 0
            self.failed_tiles = 0
            self.retried_tiles = 0
            
            # Clear processing containers
            self.meta_tiles = []
            self.retry_queue = []
            self.failed_tiles_info = []
            self.filtered_tiles = []
            self.region_tiles = {}
            self.selected_vector_layers = []
            
            # Clear job management
            if hasattr(self, 'renderer_jobs') and self.renderer_jobs:
                # Cancel any existing jobs before clearing
                self.debug_log(f"reset_processing_state: Cancelando {len(self.renderer_jobs)} jobs existentes")
                for job in list(self.renderer_jobs.keys()):
                    try:
                        job.cancelWithoutBlocking()
                        job.deleteLater()
                    except Exception:
                        pass  # Ignore cleanup errors
                self.renderer_jobs.clear()
            else:
                self.renderer_jobs = {}
            
            # Reset transformation objects
            self.transform_context = None
            self.src2wgs = None
            self.wgs2mercator = None
            
            # Reset extent variables
            self.extent = QgsRectangle()
            self.wgs84_extent = QgsRectangle()
            self.polygon_geom_wgs84 = None
            
            # Reset bounds list
            self._all_bounds_list = []
            
            # Reset event loop
            if hasattr(self, 'event_loop') and self.event_loop and self.event_loop.isRunning():
                try:
                    self.event_loop.exit()
                except Exception:
                    pass
            self.event_loop = None
            
            # Reset layers list
            self.layers = []
            
            # Close any existing database connection
            if hasattr(self, 'tairudb_writer') and self.tairudb_writer:
                try:
                    if hasattr(self.tairudb_writer, 'conn') and self.tairudb_writer.conn:
                        self.tairudb_writer.conn.close()
                except Exception:
                    pass  # Ignore cleanup errors
                self.tairudb_writer = None

        def flags(self):
            return QgsProcessingAlgorithm.Flag.FlagNoThreading

        def tr(self, text):
            return text

        def initAlgorithm(self, configuration=None):  # pylint: disable=unused-argument
            try:
                cpu_count = os.cpu_count() or 4
            except Exception:
                cpu_count = 4

            self.addParameter(QgsProcessingParameterBoolean(
                DRY_RUN,
                self.tr("Simulação — calcular estatísticas sem gerar arquivo"),
                defaultValue=False,
                optional=False,
            ))

            self.addParameter(QgsProcessingParameterFeatureSource(
                EXTENT_POLYGON,
                self.tr("Área de interesse (polígono)"),
                [QgsProcessing.TypeVectorPolygon],
                optional=False
            ))

            map_resolutions = [
                self.tr("Altíssima (0,5 m/px)"),
                self.tr("Alta (1 m/px)"),
                self.tr("Médio Alta (2 m/px)"),
                self.tr("Média (4 m/px)"),
                self.tr("Médio Baixa (8 m/px)"),
                self.tr("Baixa (16 m/px)"),
                self.tr("Muito Baixa (32 m/px)"),
            ]

            self.addParameter(QgsProcessingParameterEnum(
                MAP_RESOLUTION,
                self.tr("Resolução do mapa (metros/pixel)"),
                options=map_resolutions,
                defaultValue=0,
                optional=False
            ))

            tile_formats = [self.tr("PNG"), self.tr("JPG"), self.tr("WebP")]
            self.addParameter(QgsProcessingParameterEnum(
                TILE_FORMAT,
                self.tr("Formato da imagem"),
                options=tile_formats,
                defaultValue=1,
                optional=False
            ))

            self.addParameter(QgsProcessingParameterNumber(
                QUALITY,
                self.tr("Qualidade (apenas JPG/WebP)"),
                QgsProcessingParameterNumber.Integer,
                90,
                False,
                1,
                100
            ))

            self.addParameter(QgsProcessingParameterMultipleLayers(
                VECTOR_LAYERS,
                self.tr("Camadas vetoriais para exportar"),
                layerType=QgsProcessing.TypeVectorAnyGeometry,
                optional=True,
            ))

            self.addParameter(QgsProcessingParameterFileDestination(
                OUTPUT_FILE,
                self.tr("Arquivo de saída"),
                fileFilter="Arquivo TairuDB (*.tairudb)",
                optional=True,
            ))

        def prepareAlgorithm(self, parameters, context, feedback):
            self.feedback = feedback

            # Reset all processing state variables to prevent issues after first execution
            self.reset_processing_state()

            # Add cancellation check at the start
            if feedback.isCanceled():
                return False

            # Get vector layers to export
            self.selected_vector_layers = []
            vector_layer_ids = self.parameterAsLayerList(parameters, VECTOR_LAYERS, context)
            if vector_layer_ids:
                if all(hasattr(l, "isValid") for l in vector_layer_ids):
                    self.selected_vector_layers = [l for l in vector_layer_ids if l.isValid()]
                else:
                    all_layers = QgsProject.instance().mapLayers()
                    for lid in vector_layer_ids:
                        if feedback.isCanceled():
                            return False
                        lyr = all_layers.get(lid)
                        if lyr and lyr.isValid():
                            self.selected_vector_layers.append(lyr)

            # --- Get polygon geometry for extent ---
            source = self.parameterAsSource(parameters, EXTENT_POLYGON, context)
            features = list(source.getFeatures())
            if not features:
                feedback.reportError(self.tr("Nenhuma feição encontrada na camada de entrada."))
                return False

            if feedback.isCanceled():
                return False

            # Get parameters
            map_resolution_idx = self.parameterAsEnum(parameters, MAP_RESOLUTION, context)
            tile_format_idx = self.parameterAsEnum(parameters, TILE_FORMAT, context)
            self.jpg_quality = self.parameterAsInt(parameters, QUALITY, context)

            # Set zoom level based on map resolution
            map_resolution_formats = [18, 17, 16, 15, 14, 13, 12]
            self.max_zoom = map_resolution_formats[map_resolution_idx]
            feedback.pushInfo(self.tr(f"Zoom máximo selecionado: {self.max_zoom}"))

            # Set tile format
            tile_formats = ["PNG", "JPG", "WEBP"]
            self.tile_format = tile_formats[tile_format_idx]

            # Get layers from current project
            self.layers = [layer for layer in QgsProject.instance().mapLayers().values() 
               if QgsProject.instance().layerTreeRoot().findLayer(layer.id()) and 
               QgsProject.instance().layerTreeRoot().findLayer(layer.id()).isVisible() and
               layer.type() in [QgsMapLayerType.RasterLayer]]

            if not self.layers:
                feedback.reportError(self.tr("Nenhuma camada encontrada para renderizar."))
                return False

            self.transform_context = context.transformContext()
            source_crs = source.sourceCrs() if hasattr(source, "sourceCrs") else context.project().crs()
            self.src2wgs = QgsCoordinateTransform(source_crs, self.wgs84_crs, self.transform_context)
            self.wgs2mercator = QgsCoordinateTransform(self.wgs84_crs, self.mercator_crs, self.transform_context)

            feedback.pushInfo(self.tr(f"CRS do polígono: {source_crs.authid()}"))

            # New: Loop through all features and organize tiles by region
            self.region_tiles = {}  # Dictionary to store tiles for each region
            bounds_list = []
            polygons_wgs84 = []

            n = 2.0 ** self.max_zoom

            for idx, feature in enumerate(features):
                # Check for cancellation in the main loop
                if feedback.isCanceled():
                    return False
                    
                # Update progress for polygon processing
                if len(features) > 1:
                    feedback.setProgress(50 * idx / len(features))  # Use first 50% for polygon processing
                    
                polygon_geom = feature.geometry()
                if polygon_geom is None or polygon_geom.isEmpty():
                    feedback.reportError(self.tr(f"Feature {idx} geometry is empty or invalid."))
                    continue

                # Transform polygon to WGS84
                polygon_geom_wgs84 = QgsGeometry(polygon_geom)
                polygon_geom_wgs84.transform(self.src2wgs)
                polygons_wgs84.append(polygon_geom_wgs84)

                # Store polygon coordinates for metadata - FIXED: One region per feature
                # Get the complete geometry as a single bounds string for this feature
                if polygon_geom_wgs84.isMultipart():
                    # For MultiPolygon: combine all parts into a single bounds entry
                    all_points = []
                    for poly in polygon_geom_wgs84.asMultiPolygon():
                        if poly and poly[0]:
                            all_points.extend(poly[0])
                    if all_points:
                        ring_str = ", ".join(f"{pt.x()} {pt.y()}" for pt in all_points)
                        bounds_list.append(ring_str)
                else:
                    # Single Polygon: store exterior ring only
                    poly = polygon_geom_wgs84.asPolygon()
                    if poly and poly[0]:
                        ring = poly[0]
                        ring_str = ", ".join(f"{pt.x()} {pt.y()}" for pt in ring)
                        bounds_list.append(ring_str)

                # Compute tile range for the polygon's bounding box
                bbox = polygon_geom_wgs84.boundingBox()
                x_min = bbox.xMinimum()
                x_max = bbox.xMaximum()
                y_min = bbox.yMinimum()
                y_max = bbox.yMaximum()

                def lon2tilex(lon, n):
                    return int((lon + 180.0) / 360.0 * n)
                def lat2tiley(lat, n):
                    lat_rad = math.radians(lat)
                    return int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)

                tile_x_min = max(0, lon2tilex(x_min, n))
                tile_x_max = min(int(n) - 1, lon2tilex(x_max, n))
                tile_y_min = max(0, lat2tiley(y_max, n))  # y_max is north
                tile_y_max = min(int(n) - 1, lat2tiley(y_min, n))  # y_min is south

                # Store tiles for this specific region
                region_tiles = set()
                tile_count = 0
                total_tiles_to_check = (tile_x_max - tile_x_min + 1) * (tile_y_max - tile_y_min + 1)
                
                for tx in range(tile_x_min, tile_x_max + 1):
                    for ty in range(tile_y_min, tile_y_max + 1):
                        # Check for cancellation during tile intersection checking
                        if feedback.isCanceled():
                            return False
                            
                        tile_count += 1
                        if tile_count % 100 == 0:  # Update progress every 100 tiles
                            progress = 50 + (50 * tile_count / total_tiles_to_check) * (idx + 1) / len(features)
                            feedback.setProgress(min(99, progress))
                        
                        # Tile bounds in WGS84
                        x1 = tx * 360.0 / n - 180.0
                        y1 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
                        x2 = (tx + 1) * 360.0 / n - 180.0
                        y2 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
                        tile_rect = QgsRectangle(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                        tile_geom = QgsGeometry.fromRect(tile_rect)
                        if polygon_geom_wgs84.intersects(tile_geom):
                            region_tiles.add((tx, ty))
                
                self.region_tiles[idx] = list(region_tiles)

            # Calculate total tiles across all regions
            all_tiles = set()
            for region_tiles in self.region_tiles.values():
                all_tiles.update(region_tiles)
            
            self.filtered_tiles = list(all_tiles)
            self.total_tiles = len(self.filtered_tiles)

            # For backward compatibility, store the first polygon's geometry for later use
            self.polygon_geom_wgs84 = polygons_wgs84[0] if polygons_wgs84 else None
            # Store the union of all regions for center calculation
            if polygons_wgs84:
                union_bbox = polygons_wgs84[0].boundingBox()
                for geom in polygons_wgs84[1:]:
                    union_bbox.combineExtentWith(geom.boundingBox())
                self.wgs84_extent = union_bbox
            else:
                self.wgs84_extent = QgsRectangle()

            total_region_tiles = sum(len(tiles) for tiles in self.region_tiles.values())
            feedback.pushInfo(self.tr(f"Encontrados {total_region_tiles} tiles em {len(self.region_tiles)} regiões"))
            feedback.pushInfo(self.tr(f"Encontrados {self.total_tiles} tiles únicos que intersectam com os polígonos selecionados."))
            
            # Debug: Log region details
            for region_id, tiles in self.region_tiles.items():
                feedback.pushInfo(self.tr(f"Região {region_id}: {len(tiles)} tiles"))
            
            feedback.pushInfo(self.tr(f"Bounds criados: {len(bounds_list)} entradas"))

            if not self.filtered_tiles:
                feedback.reportError(self.tr("Nenhum tile intersecta a extensão do polígono selecionado."))
                return False

            # Store all bounds for later use in processAlgorithm
            self._all_bounds_list = bounds_list  # Store as list instead of joined string
            self._feature_count = len(features)  # Store feature count for validation

            return True
        
        def cleanup_resources(self):
            """Enhanced cleanup with better error handling"""
            try:
                self.debug_log("cleanup_resources: Iniciando limpeza")
                if self.feedback:
                    self.feedback.pushInfo(self.tr("Limpando recursos..."))
                
                # Stop watchdog timer if it exists
                if hasattr(self, 'watchdog_timer') and self.watchdog_timer:
                    try:
                        self.debug_log("cleanup_resources: Parando watchdog timer")
                        self.watchdog_timer.stop()
                    except Exception:
                        pass
                
                # Cancel and cleanup renderer jobs AGGRESSIVELY
                if hasattr(self, 'renderer_jobs'):
                    jobs_count = len(self.renderer_jobs)
                    self.debug_log(f"cleanup_resources: Cancelando {jobs_count} jobs")
                    for job in list(self.renderer_jobs.keys()):
                        try:
                            # CRITICAL: Cancel jobs without blocking
                            job.cancelWithoutBlocking()
                            job.deleteLater()
                        except Exception:
                            pass  # Ignore cleanup errors
                    self.renderer_jobs.clear()
                    
                    # Force process events to handle deleteLater() immediately
                    if jobs_count > 0:
                        for _ in range(10):
                            QCoreApplication.processEvents()
                
                # Clear tile queues COMPLETELY
                if hasattr(self, 'meta_tiles'):
                    self.debug_log(f"cleanup_resources: Limpando {len(self.meta_tiles)} meta_tiles")
                    self.meta_tiles.clear()
                if hasattr(self, 'retry_queue'):
                    self.debug_log(f"cleanup_resources: Limpando {len(self.retry_queue)} retry_queue")
                    self.retry_queue.clear()
                
                # Close database connection with proper cleanup
                if hasattr(self, 'tairudb_writer') and self.tairudb_writer:
                    try:
                        if hasattr(self.tairudb_writer, 'conn') and self.tairudb_writer.conn:
                            self.debug_log("cleanup_resources: Fechando conexão do banco de dados")
                            # Try to commit any pending changes before closing
                            try:
                                self.tairudb_writer.conn.commit()
                            except Exception:
                                pass
                            # Close the connection
                            try:
                                self.tairudb_writer.conn.close()
                            except Exception:
                                pass
                            self.tairudb_writer.conn = None
                    except Exception as e:
                        if self.feedback:
                            self.feedback.pushInfo(self.tr(f"Aviso ao fechar banco de dados: {str(e)}"))
                
                # No more event loop to exit - just process remaining events
                self.debug_log("cleanup_resources: Processando eventos finais")
                
                # Process more events to ensure cleanup is complete
                for _ in range(20):
                    QCoreApplication.processEvents()
                
                self.debug_log("cleanup_resources: Limpeza concluída")
                        
            except Exception as e:
                if self.feedback:
                    self.feedback.pushInfo(self.tr(f"Aviso durante a limpeza: {str(e)}"))
                pass  # Don't fail on cleanup errors

        def _dry_run_report(self, feedback, tile_format, jpg_quality, num_vector_layers, vector_feature_count):
            """Report estimated statistics without generating any file."""
            total_tiles = self.total_tiles
            num_regions = len(self.region_tiles)

            # Average tile sizes in KB by format (typical QGIS raster rendering)
            size_kb = {'PNG': 70, 'JPG': 28, 'WEBP': 20}
            min_kb  = {'PNG': 20, 'JPG':  8, 'WEBP':  6}
            max_kb  = {'PNG': 180,'JPG': 70, 'WEBP': 50}

            fmt = tile_format.upper()
            # Scale JPG/WebP estimate by quality relative to baseline of 90
            if fmt in ('JPG', 'WEBP') and jpg_quality != 90:
                q = jpg_quality / 90.0
                avg_kb = max(1, size_kb[fmt] * q)
                lo_kb  = max(1, min_kb[fmt] * q)
                hi_kb  = max(1, max_kb[fmt] * q)
            else:
                avg_kb = size_kb.get(fmt, 28)
                lo_kb  = min_kb.get(fmt, 8)
                hi_kb  = max_kb.get(fmt, 70)

            def fmt_size(mb):
                return f"{mb/1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"

            avg_mb = (total_tiles * avg_kb) / 1024
            lo_mb  = (total_tiles * lo_kb)  / 1024
            hi_mb  = (total_tiles * hi_kb)  / 1024

            # Rendering time estimate: ~0.15 s/tile single-thread
            secs = total_tiles * 0.15 / max(1, self.threads_number)
            if secs < 60:
                time_str = f"~{secs:.0f} seg"
            elif secs < 3600:
                time_str = f"~{secs/60:.0f} min"
            else:
                time_str = f"~{secs/3600:.1f} h"

            # Coverage area from WGS84 bounding box
            bbox = self.wgs84_extent
            clat = math.radians((bbox.yMinimum() + bbox.yMaximum()) / 2)
            w_km = abs(bbox.xMaximum() - bbox.xMinimum()) * 111.32 * math.cos(clat)
            h_km = abs(bbox.yMaximum() - bbox.yMinimum()) * 110.574
            area_km2 = w_km * h_km

            zoom_to_label = {
                18: "Altíssima (0,5 m/px) — zoom 18",
                17: "Alta (1 m/px) — zoom 17",
                16: "Médio Alta (2 m/px) — zoom 16",
                15: "Média (4 m/px) — zoom 15",
                14: "Médio Baixa (8 m/px) — zoom 14",
                13: "Baixa (16 m/px) — zoom 13",
                12: "Muito Baixa (32 m/px) — zoom 12",
            }
            res_label = zoom_to_label.get(self.max_zoom, f"zoom {self.max_zoom}")
            quality_str = f" (qualidade {jpg_quality})" if fmt in ('JPG', 'WEBP') else ""

            sep = "─" * 34

            def line(text=""):
                feedback.pushInfo(text)

            line()
            line("[ SIMULAÇÃO] Nenhum arquivo foi gerado")
            line("=" * 34)
            line()
            line("CONFIGURAÇÃO")
            line(sep)
            line(f"  Resolução  : {res_label}")
            line(f"  Formato    : {fmt}{quality_str}")
            line(f"  Regiões    : {num_regions} polígono{'s' if num_regions != 1 else ''}")
            line(f"  Área (bbox): {area_km2:.1f} km²")
            line()
            line("TILES RASTER")
            line(sep)
            line(f"  Total de tiles : {total_tiles:,}".replace(',', '.'))
            for rid, tiles in sorted(self.region_tiles.items()):
                line(f"    Região {rid + 1}: {len(tiles):,} tiles".replace(',', '.'))
            line()
            line("TAMANHO ESTIMADO")
            line(sep)
            line(f"  Estimativa : {fmt_size(avg_mb)}  (~{avg_kb:.0f} KB/tile)")
            line(f"  Intervalo  : {fmt_size(lo_mb)} – {fmt_size(hi_mb)}")
            line()
            line("TEMPO ESTIMADO")
            line(sep)
            line(f"  {self.threads_number} thread{'s' if self.threads_number != 1 else ''} paralela{'s' if self.threads_number != 1 else ''} : {time_str}")
            line(f"  (~0,15 s/tile em hardware típico)")
            if num_vector_layers > 0:
                line()
                line("CAMADAS VETORIAIS")
                line(sep)
                line(f"  Camadas  : {num_vector_layers}")
                line(f"  Feições  : {vector_feature_count:,}".replace(',', '.'))
            line()

            # Warnings
            warns = []
            if total_tiles > 10000:
                warns.append("Mais de 10.000 tiles. Reduza a área ou use resolução menor.")
            elif total_tiles > 5000:
                warns.append("Mais de 5.000 tiles. Verifique se área/resolução são adequadas.")
            if avg_mb > 1024:
                warns.append("Estimativa acima de 1 GB. Pode impactar desempenho no dispositivo.")
            elif avg_mb > 500:
                warns.append("Estimativa acima de 500 MB. Verifique o espaço no dispositivo.")
            if warns:
                line("AVISOS")
                line(sep)
                for w in warns:
                    feedback.reportError(f"  ⚠  {w}", False)
                line()

            line("Desmarque 'Dry Run' e execute novamente para gerar o arquivo.")
            line()

        def processAlgorithm(self, parameters, context, feedback):
            # CRITICAL: Store feedback parameter (it's the authoritative source for cancellation)
            self.feedback = feedback

            if feedback.isCanceled():
                return {}

            dry_run = self.parameterAsBool(parameters, DRY_RUN, context)

            if dry_run:
                # Collect vector feature count without rendering
                num_vector_layers = len(self.selected_vector_layers)
                vector_feature_count = sum(
                    lyr.featureCount() for lyr in self.selected_vector_layers if lyr.isValid()
                )
                self._dry_run_report(
                    feedback,
                    self.tile_format,
                    self.jpg_quality,
                    num_vector_layers,
                    vector_feature_count,
                )
                feedback.setProgress(100)
                return {}

            # Ensure all variables are properly initialized
            if not hasattr(self, 'meta_tiles') or self.meta_tiles is None:
                self.meta_tiles = []
            if not hasattr(self, 'renderer_jobs') or self.renderer_jobs is None:
                self.renderer_jobs = {}
            if not hasattr(self, 'processed_tiles'):
                self.processed_tiles = 0
            if not hasattr(self, 'failed_tiles'):
                self.failed_tiles = 0

            output_file = self.parameterAsString(parameters, OUTPUT_FILE, context)
            if not output_file:
                raise QgsProcessingException(
                    self.tr("Informe o arquivo de saída ou ative a opção 'Simulação (Dry Run)'.")
                )

            self.tairudb_writer = TairuDBWriter(output_file)
            if not self.tairudb_writer.create():
                raise QgsProcessingException(self.tr(f"Falha ao criar o arquivo GeoDB {output_file}"))

            # Add cancellation check before metadata setup
            if feedback.isCanceled():
                self.cleanup_resources()
                return {}

            # Metadata
            format_lower = self.tile_format.lower()
            self.tairudb_writer.setMetadataValue("format", format_lower)
            file_info = os.path.basename(output_file)
            base_name = os.path.splitext(file_info)[0]
            self.tairudb_writer.setMetadataValue("name", base_name)
            self.tairudb_writer.setMetadataValue("description", base_name)
            self.tairudb_writer.setMetadataValue("version", "1.2")
            self.tairudb_writer.setMetadataValue("type", "overlay")
            self.tairudb_writer.setMetadataValue("minzoom", str(self.max_zoom))
            self.tairudb_writer.setMetadataValue("maxzoom", str(self.max_zoom))

            # Insert regions into the regions table - FIXED: Ensure consistent indexing
            bounds_list = getattr(self, "_all_bounds_list", [])
            feature_count = getattr(self, "_feature_count", 0)
            
            # Ensure we have exactly the same number of regions as features processed
            if len(bounds_list) != feature_count:
                feedback.pushInfo(self.tr(f"Aviso: Número de regiões ({len(bounds_list)}) difere do número de features ({feature_count})"))
            
            for idx, bound_str in enumerate(bounds_list):
                region_name = f"Região {idx + 1}"
                self.tairudb_writer.insertRegion(
                    region_name,
                    self.max_zoom,  # minzoom
                    self.max_zoom,  # maxzoom
                    bound_str       # bounds
                )

            feedback.pushInfo(self.tr(f"Regiões criadas na tabela de regiões: {len(bounds_list)}"))

            center_x = (self.wgs84_extent.xMinimum() + self.wgs84_extent.xMaximum()) / 2
            center_y = (self.wgs84_extent.yMinimum() + self.wgs84_extent.yMaximum()) / 2
            center_zoom = self.max_zoom
            center_str = f"{center_x},{center_y},{center_zoom}"
            self.tairudb_writer.setMetadataValue("center", center_str)

            if self.include_attribution and self.attribution_text:
                self.tairudb_writer.setMetadataValue("attribution", self.attribution_text)

            self.tairudb_writer.setMetadataValue("generator", "GeoPDB Generator")
            self.tairudb_writer.setMetadataValue("created", datetime.now().isoformat())

            feedback.setProgressText(self.tr(f"Preparando para renderizar {len(self.filtered_tiles)} tiles..."))

            self.meta_tiles = []
            self.processed_tiles = 0

            z = self.max_zoom
            n = 2.0 ** self.max_zoom

            # Add cancellation check during meta tile creation
            for i, (tx, ty) in enumerate(self.filtered_tiles):
                if feedback.isCanceled():
                    self.cleanup_resources()
                    return {}
                    
                # Update progress during meta tile creation
                if i % 50 == 0:
                    feedback.setProgress(10 + (20 * i / len(self.filtered_tiles)))
                    
                meta_tile = self.create_individual_metatile(z, tx, ty, n)
                self.meta_tiles.append(meta_tile)

            feedback.setProgressText(self.tr(f"Renderizando {len(self.meta_tiles)} tiles..."))

            # Start rendering jobs
            self.start_jobs()
            
            # Fast polling loop - process events until all tiles complete
            # Note: Cancellation not supported due to QGIS framework limitations
            if self.meta_tiles or self.renderer_jobs:
                while self.renderer_jobs or self.meta_tiles or self.retry_queue:
                    # Process Qt events to handle finished signals
                    QCoreApplication.processEvents()
            
            # Final event processing to ensure all signals handled
            QCoreApplication.processEvents()
            
            # Final cancellation check
            if feedback.isCanceled():
                self.debug_log("Cancelamento detectado após polling")
                self.cleanup_resources()
                feedback.pushInfo(self.tr("Operação cancelada pelo usuário"))
                return {}

            # Report tile processing results
            if self.feedback:
                self.feedback.setProgress(100)
                total_expected = len(self.filtered_tiles)
                success_rate = ((total_expected - self.failed_tiles) / total_expected * 100) if total_expected > 0 else 0

                self.feedback.pushInfo(self.tr(f"Resumo do processamento de tiles:"))
                self.feedback.pushInfo(self.tr(f"- Tiles esperados: {total_expected}"))
                self.feedback.pushInfo(self.tr(f"- Tiles processados com sucesso: {self.processed_tiles}"))
                self.feedback.pushInfo(self.tr(f"- Tiles falhados: {self.failed_tiles}"))
                self.feedback.pushInfo(self.tr(f"- Tiles reprocessados: {self.retried_tiles}"))
                self.feedback.pushInfo(self.tr(f"- Taxa de sucesso: {success_rate:.1f}%"))
                
                if self.failed_tiles_info and len(self.failed_tiles_info) <= 10:
                    self.feedback.pushInfo(self.tr("Detalhes dos tiles falhados:"))
                    for fail_info in self.failed_tiles_info:
                        self.feedback.pushInfo(
                            self.tr(f"  - Tile {fail_info['x']},{fail_info['y']}: {fail_info['reason']}")
                        )
                elif len(self.failed_tiles_info) > 10:
                    self.feedback.pushInfo(self.tr(f"({len(self.failed_tiles_info)} failed tiles - too many to list)"))

            # --- Write vector layers into database ---
            if self.feedback and self.feedback.isCanceled():
                self.cleanup_resources()
                feedback.pushInfo(self.tr("Operação cancelada pelo usuário antes da exportação de camadas vetoriais"))
                return {}
                
            self.export_vector_layers(feedback)

            # Final cancellation check
            if self.feedback and self.feedback.isCanceled():
                self.cleanup_resources()
                feedback.pushInfo(self.tr("Operação cancelada pelo usuário antes da finalização"))
                return {}

            self.tairudb_writer.finalize()
            feedback.pushInfo(self.tr("Processamento concluído com sucesso!"))
            return {OUTPUT_FILE: output_file}

        def create_individual_metatile(self, z, tx, ty, n):
            try:
                x1 = tx * 360.0 / n - 180.0
                y1 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
                x2 = (tx + 1) * 360.0 / n - 180.0
                y2 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
                meta_tile = MetaTile()
                meta_tile.zoom = z
                meta_tile.tx = tx
                meta_tile.ty = ty
                meta_tile.metatile_size = 1
                meta_tile.actual_size_x = 1
                meta_tile.actual_size_y = 1
                meta_tile.retry_count = 0  # Track retry attempts
                
                # Add error handling for coordinate transformation
                if hasattr(self, 'wgs2mercator') and self.wgs2mercator:
                    p1 = self.wgs2mercator.transform(x1, y1)
                    p2 = self.wgs2mercator.transform(x2, y2)
                    meta_tile.extent = QgsRectangle(
                        min(p1.x(), p2.x()),
                        min(p1.y(), p2.y()),
                        max(p1.x(), p2.x()),
                        max(p1.y(), p2.y())
                    )
                else:
                    # Fallback to WGS84 coordinates if transformation fails
                    meta_tile.extent = QgsRectangle(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                
                # Validate the created meta tile
                if not meta_tile.is_valid():
                    if self.feedback:
                        self.feedback.pushInfo(self.tr(f"Aviso: Metatile inválido criado para {tx},{ty}"))

                return meta_tile
            except Exception as e:
                if self.feedback:
                    self.feedback.pushInfo(self.tr(f"Erro ao criar metatile {tx},{ty}: {str(e)}"))
                # Return a basic metatile to avoid complete failure
                meta_tile = MetaTile()
                meta_tile.zoom = z
                meta_tile.tx = tx
                meta_tile.ty = ty
                meta_tile.metatile_size = 1
                meta_tile.actual_size_x = 1
                meta_tile.actual_size_y = 1
                meta_tile.retry_count = 0
                meta_tile.extent = QgsRectangle(-180, -85, 180, 85)  # Fallback extent
                return meta_tile

        def validate_state_variables(self):
            """Validate that all critical variables are properly initialized"""
            required_vars = [
                'meta_tiles', 'renderer_jobs', 'processed_tiles', 'failed_tiles',
                'retried_tiles', 'retry_queue', 'failed_tiles_info', 'filtered_tiles',
                'region_tiles', 'total_tiles', 'max_retries', 'wgs84_crs', 'mercator_crs'
            ]
            
            for var_name in required_vars:
                if not hasattr(self, var_name):
                    if self.feedback:
                        self.feedback.pushInfo(self.tr(f"Aviso: Variável {var_name} não inicializada, definindo padrão"))

                    # Set default values
                    if var_name in ['meta_tiles', 'retry_queue', 'failed_tiles_info', 'filtered_tiles', 'layers']:
                        setattr(self, var_name, [])
                    elif var_name in ['renderer_jobs', 'region_tables', 'region_tiles']:
                        setattr(self, var_name, {})
                    elif var_name in ['processed_tiles', 'failed_tiles', 'retried_tiles', 'total_tiles']:
                        setattr(self, var_name, 0)
                    elif var_name == 'max_retries':
                        setattr(self, var_name, 3)
                    elif var_name == 'wgs84_crs':
                        setattr(self, var_name, QgsCoordinateReferenceSystem("EPSG:4326"))
                    elif var_name == 'mercator_crs':
                        setattr(self, var_name, QgsCoordinateReferenceSystem("EPSG:3857"))

        def start_jobs(self):
            """Start rendering jobs for pending tiles - optimized for speed"""
            # Validate state before starting jobs
            self.validate_state_variables()
            
            while (self.meta_tiles or self.retry_queue) and len(self.renderer_jobs) < self.threads_number:
                # Prioritize retries over new tiles
                meta_tile = None
                if self.retry_queue:
                    meta_tile = self.retry_queue.pop(0)
                elif self.meta_tiles:
                    meta_tile = self.meta_tiles.pop(0)
                else:
                    break
                
                if not meta_tile:
                    continue
                
                # Validate meta tile before processing
                if hasattr(meta_tile, 'is_valid') and not meta_tile.is_valid():
                    if self.feedback:
                        self.feedback.pushInfo(self.tr(f"Pulando metatile inválido {meta_tile.tx},{meta_tile.ty}"))
                    continue
                
                try:
                    size_x = meta_tile.actual_size_x if meta_tile.actual_size_x > 0 else meta_tile.metatile_size
                    size_y = meta_tile.actual_size_y if meta_tile.actual_size_y > 0 else meta_tile.metatile_size
                    actual_tile_width = self.tile_width * size_x
                    actual_tile_height = self.tile_height * size_y
                    
                    if actual_tile_width <= 0 or actual_tile_height <= 0:
                        if self.feedback:
                            self.feedback.pushInfo(self.tr(f"Tamanho de tile inválido para tile {meta_tile.tx},{meta_tile.ty}"))
                        continue
                    
                    # Validate extent
                    if meta_tile.extent.isEmpty() or not meta_tile.extent.isFinite():
                        if self.feedback:
                            self.feedback.pushInfo(self.tr(f"Extensão inválida para tile {meta_tile.tx},{meta_tile.ty}"))
                        continue
                    
                    # Create map settings with error checking
                    map_settings = QgsMapSettings()
                    
                    if not self.layers:
                        if self.feedback:
                            self.feedback.pushInfo(self.tr("Nenhuma camada disponível para renderização"))
                        continue
                    
                    map_settings.setLayers(self.layers)
                    map_settings.setOutputDpi(self.dpi)
                    map_settings.setOutputSize(QSize(actual_tile_width, actual_tile_height))
                    map_settings.setExtent(meta_tile.extent)
                    map_settings.setDestinationCrs(self.mercator_crs)
                    map_settings.setFlag(Qgis.MapSettingsFlag.Antialiasing, self.antialias)  # type: ignore
                    
                    # Additional map settings for better rendering
                    map_settings.setFlag(Qgis.MapSettingsFlag.RenderMapTile, True)  # type: ignore
                    map_settings.setFlag(Qgis.MapSettingsFlag.DrawLabeling, True)  # type: ignore
                    map_settings.setFlag(Qgis.MapSettingsFlag.UseAdvancedEffects, False)  # Disable for stability  # type: ignore
                    
                    # Create and start job
                    job = QgsMapRendererSequentialJob(map_settings)
                    self.renderer_jobs[job] = meta_tile
                    job.finished.connect(lambda job=job: self.process_metatile(job))  # type: ignore
                    job.start()
                    
                except Exception as e:
                    if self.feedback:
                        self.feedback.pushInfo(self.tr(f"Erro ao iniciar trabalho para tile {meta_tile.tx},{meta_tile.ty}: {str(e)}"))
                    
                    # Add to failed tiles if not already retrying
                    if meta_tile.retry_count < self.max_retries:
                        meta_tile.retry_count += 1
                        self.retry_queue.append(meta_tile)
                    else:
                        self.failed_tiles_info.append({
                            'x': meta_tile.tx,
                            'y': meta_tile.ty,
                            'zoom': meta_tile.zoom,
                            'reason': f'Erro ao iniciar trabalho: {str(e)}'
                        })
                        self.failed_tiles += 1

        def process_metatile(self, job):
            """Process completed rendering job - optimized for speed"""
            try:
                meta_tile = self.renderer_jobs.get(job)
                if not meta_tile:
                    job.deleteLater()
                    return

                metatile_image = job.renderedImage()
                
                # Check for rendering failures and implement retry logic
                if metatile_image.isNull() or metatile_image.width() == 0 or metatile_image.height() == 0:
                    self.failed_tiles += 1
                    
                    # Add to retry queue if we haven't exceeded max retries
                    if meta_tile.retry_count < self.max_retries:
                        meta_tile.retry_count += 1
                        self.retry_queue.append(meta_tile)
                        self.retried_tiles += 1
                        if self.feedback:
                            self.feedback.pushInfo(
                                self.tr(f"Tentando tile novamente {meta_tile.tx},{meta_tile.ty} (tentativa {meta_tile.retry_count}/{self.max_retries})")
                            )
                    else:
                        # Max retries reached, log as failed
                        self.failed_tiles_info.append({
                            'x': meta_tile.tx,
                            'y': meta_tile.ty,
                            'zoom': meta_tile.zoom,
                            'reason': 'Falha ao renderizar após tentativas máximas'
                        })
                        if self.feedback:
                            self.feedback.pushInfo(
                                self.tr(f"Tile {meta_tile.tx},{meta_tile.ty} falhou após {self.max_retries} tentativas")
                            )
                    
                    del self.renderer_jobs[job]
                    job.deleteLater()
                    self.check_completion()
                    return

                # Successfully rendered, process the tile
                self.save_metatile_data(meta_tile, metatile_image)
                
            except Exception as e:
                # Handle any unexpected errors during tile processing
                if self.feedback:
                    self.feedback.pushInfo(self.tr(f"Erro ao processar tile: {str(e)}"))

                meta_tile = self.renderer_jobs.get(job)
                if meta_tile:
                    self.failed_tiles_info.append({
                        'x': meta_tile.tx,
                        'y': meta_tile.ty,
                        'zoom': meta_tile.zoom,
                        'reason': f'Erro ao processar tile: {str(e)}'
                    })
                    self.failed_tiles += 1
            finally:
                # Cleanup job
                if job in self.renderer_jobs:
                    del self.renderer_jobs[job]
                job.deleteLater()
                
                # Status update only every 5 tiles or at completion
                if hasattr(self, 'processed_tiles') and self.feedback:
                    remaining = len(self.meta_tiles) if hasattr(self, 'meta_tiles') else 0
                    active_jobs = len(self.renderer_jobs) if hasattr(self, 'renderer_jobs') else 0
                    retry = len(self.retry_queue) if hasattr(self, 'retry_queue') else 0
                    
                    #if self.processed_tiles % 5 == 0 or (remaining == 0 and active_jobs == 0):
                    #    self.feedback.pushInfo(
                    #        self.tr(f"Status: {self.processed_tiles} processados, {remaining} aguardando, {active_jobs} ativos, {retry} para tentar novamente")
                    #    )
                
                self.check_completion()

        def save_metatile_data(self, meta_tile, metatile_image):
            """Save individual tiles from a metatile image - optimized for speed"""
            size_x = meta_tile.actual_size_x if meta_tile.actual_size_x > 0 else meta_tile.metatile_size
            size_y = meta_tile.actual_size_y if meta_tile.actual_size_y > 0 else meta_tile.metatile_size
            max_tile_index = int(math.pow(2, meta_tile.zoom))
            tiles_saved = 0

            for i in range(size_x):
                if self.feedback and self.feedback.isCanceled():
                    return
                    
                tile_x = meta_tile.tx + i
                if tile_x >= max_tile_index:
                    continue
                    
                for j in range(size_y):
                    if self.feedback and self.feedback.isCanceled():
                        return
                        
                    tile_y = meta_tile.ty + j
                    if tile_y >= max_tile_index:
                        continue
                        
                    # Extract tile from metatile
                    x_offset = i * self.tile_width
                    y_offset = j * self.tile_height
                    
                    # Validate offsets
                    if (x_offset >= metatile_image.width() or
                        y_offset >= metatile_image.height() or
                        x_offset + self.tile_width > metatile_image.width() or
                        y_offset + self.tile_height > metatile_image.height()):
                        continue
                    
                    tile_image = metatile_image.copy(x_offset, y_offset, self.tile_width, self.tile_height)
                    
                    # Improved empty tile detection
                    if self.filter_empty_tiles and self.is_tile_empty(tile_image):
                        self.processed_tiles += 1
                        continue
                    
                    # Convert and save tile
                    if self.convert_and_save_tile(tile_image, meta_tile, tile_x, tile_y, max_tile_index):
                        tiles_saved += 1
                        self.processed_tiles += 1
                    else:
                        # Failed to save tile
                        self.failed_tiles_info.append({
                            'x': tile_x,
                            'y': tile_y,
                            'zoom': meta_tile.zoom,
                            'reason': 'Falha ao converter ou salvar dados do tile'
                        })
                        self.failed_tiles += 1

        def is_tile_empty(self, tile_image):
            """Improved empty tile detection with better sampling"""
            if tile_image.isNull():
                return True
                
            # Sample more points for better accuracy
            sample_points = [
                (0, 0), (tile_image.width()//4, tile_image.height()//4),
                (tile_image.width()//2, tile_image.height()//2),
                (3*tile_image.width()//4, 3*tile_image.height()//4),
                (tile_image.width()-1, tile_image.height()-1)
            ]
            
            first_pixel = None
            for x, y in sample_points:
                if x < tile_image.width() and y < tile_image.height():
                    pixel = tile_image.pixel(x, y)
                    if first_pixel is None:
                        first_pixel = pixel
                    elif pixel != first_pixel:
                        return False  # Found different pixels, not empty
            
            # Additional check with alpha channel
            if tile_image.hasAlphaChannel():
                alpha_format = QImage.Format_ARGB32
                if tile_image.format() != alpha_format:
                    tile_image = tile_image.convertToFormat(alpha_format)
                
                # Check if all pixels are transparent
                for x, y in sample_points:
                    if x < tile_image.width() and y < tile_image.height():
                        pixel = tile_image.pixel(x, y)
                        alpha = (pixel >> 24) & 0xFF
                        if alpha > 0:  # Not fully transparent
                            return False
            
            return True

        def convert_and_save_tile(self, tile_image, meta_tile, tile_x, tile_y, max_tile_index):
            """Convert tile image to specified format and save to database"""
            try:
                quality = self.jpg_quality
                tile_data = QByteArray()
                buffer = QBuffer(tile_data)
                buffer.open(QBuffer.WriteOnly)
                
                success = False
                if self.tile_format == "PNG":
                    success = tile_image.save(buffer, "PNG")
                elif self.tile_format == "JPG":
                    success = tile_image.save(buffer, "JPG", quality)
                elif self.tile_format == "WEBP":
                    formats = QImage.supportedImageFormats()
                    webp_format = b'WEBP'
                    if webp_format in formats:
                        success = tile_image.save(buffer, "WEBP", quality)
                    else:
                        # Fallback to PNG if WebP is not supported
                        success = tile_image.save(buffer, "PNG")
                        if self.feedback:
                            self.feedback.pushInfo(self.tr("WebP não suportado, usando PNG em vez disso"))

                if not success or tile_data.isEmpty():
                    return False
                
                # Convert to TMS Y coordinate
                tms_y = max_tile_index - 1 - tile_y
                
                # Save to database for all regions that contain this tile
                tile_coord = (tile_x, tile_y)
                saved_to_regions = 0
                
                # Debug: Check which regions should contain this tile
                containing_regions = []
                for region_id, region_tiles in self.region_tiles.items():
                    if tile_coord in region_tiles:
                        containing_regions.append(region_id)
                
                if not containing_regions and self.feedback:
                    self.feedback.pushInfo(self.tr(f"Aviso: Tile {tile_x},{tile_y} não foi encontrado em nenhuma região"))
                
                for region_id in containing_regions:
                    if self.tairudb_writer and self.tairudb_writer.saveTile(meta_tile.zoom, tile_x, tms_y, tile_data, region_id):
                        saved_to_regions += 1
                    else:
                        if self.feedback:
                            self.feedback.pushInfo(self.tr(f"Falha ao salvar tile {tile_x},{tile_y} na região {region_id}"))

                # Periodic commit every 100 tiles for data integrity
                if saved_to_regions > 0 and self.processed_tiles % 100 == 0:
                    if self.tairudb_writer:
                        self.tairudb_writer.periodicCommit()
                
                return saved_to_regions > 0
                
            except Exception as e:
                if self.feedback:
                    self.feedback.pushInfo(self.tr(f"Erro ao converter/salvar tile {tile_x},{tile_y}: {str(e)}"))
                return False

        def check_completion(self):
            """Check if processing is complete and handle retries - optimized for speed"""
            # Ensure variables are initialized
            if not hasattr(self, 'total_tiles'):
                self.total_tiles = 0
            if not hasattr(self, 'processed_tiles'):
                self.processed_tiles = 0
            if not hasattr(self, 'failed_tiles'):
                self.failed_tiles = 0
            if not hasattr(self, 'retried_tiles'):
                self.retried_tiles = 0
            if not hasattr(self, 'retry_queue'):
                self.retry_queue = []
            if not hasattr(self, 'meta_tiles'):
                self.meta_tiles = []
            if not hasattr(self, 'renderer_jobs'):
                self.renderer_jobs = {}
            
            # Update progress
            if self.total_tiles > 0 and self.feedback:
                progress = min(99, 100.0 * self.processed_tiles / self.total_tiles)
                self.feedback.setProgress(progress)
            
            # Process retry queue
            if self.retry_queue and len(self.renderer_jobs) < self.threads_number:
                self.debug_log(f"check_completion: Processando retry - {len(self.retry_queue)} na fila")
                retry_tile = self.retry_queue.pop(0)
                self.meta_tiles.insert(0, retry_tile)  # Priority to retries
                self.start_jobs()
                return
            
            # Start new jobs if available
            if self.meta_tiles:
                self.debug_log(f"check_completion: Iniciando novos jobs - {len(self.meta_tiles)} tiles aguardando")
                self.start_jobs()
            elif not self.renderer_jobs and not self.retry_queue:
                # All processing complete - only report once
                if self.feedback and not hasattr(self, '_completion_reported'):
                    self._completion_reported = True
                    self.feedback.pushInfo(self.tr("Todos os tiles processados, finalizando renderização..."))
                    if self.failed_tiles > 0:
                        self.feedback.pushInfo(
                            self.tr(f"Processamento completo. {self.failed_tiles} tiles falharam, {self.retried_tiles} tiles tentados novamente")
                        )

        def export_vector_layers(self, feedback):
            # Ensure selected_vector_layers is initialized
            if not hasattr(self, 'selected_vector_layers') or self.selected_vector_layers is None:
                self.selected_vector_layers = []
                
            if not self.selected_vector_layers:
                feedback.pushInfo(self.tr("Nenhuma camada vetorial selecionada para exportação."))
                return
            
            # Ensure transform_context is available
            if not hasattr(self, 'transform_context') or self.transform_context is None:
                self.transform_context = QgsProject.instance().transformContext()
                
            for layer_idx, layer in enumerate(self.selected_vector_layers):
                if feedback.isCanceled():
                    return
                    
                # Update progress for vector export
                progress = 90 + (10 * layer_idx / len(self.selected_vector_layers))
                feedback.setProgress(progress)
                feedback.setProgressText(self.tr(f"Exportando camada vetorial: {layer.name()}"))
                
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
                    
                # Try to get layer name/desc from the first feature's attributes
                layer_name = layer.name()
                layer_desc = layer.abstract() if hasattr(layer, "abstract") else ""

                # Read color from symbology
                try:
                    symbol = layer.renderer().symbol()
                    color = symbol.color().name()  # "#RRGGBB"
                except Exception:
                    color = "#0000FF"  # fallback
                    
                points_groups = []

                # Prepare transformation to WGS84
                layer_crs = layer.crs()
                transform = QgsCoordinateTransform(layer_crs, QgsCoordinateReferenceSystem("EPSG:4326"), self.transform_context)

                feature_count = 0
                total_features = layer.featureCount()
                
                # Generate a UUID for the layer
                layer_uuid = str(uuid.uuid4())
                # Insert the layer into the layers table
                self.tairudb_writer.insertVectorLayer(  # type: ignore
                    layer_uuid,
                    type_str,
                    layer_name,
                    layer_desc
                )
                
                for feat in layer.getFeatures():
                    if feedback.isCanceled():
                        feedback.pushInfo(self.tr(f"Exportação de camada vetorial cancelada em {layer_name}"))
                        return

                    feature_count += 1
                    # Update progress more frequently for better feedback
                    if feature_count % 10 == 0 and total_features > 0:
                        layer_progress = 90 + (10 * (layer_idx + feature_count / total_features) / len(self.selected_vector_layers))
                        feedback.setProgress(min(99, layer_progress))

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
                    self.tairudb_writer.insertFeature(  # type: ignore
                        type_str,
                        feat_name,
                        feat_attr,
                        color,
                        size,
                        iconType,
                        points_str,
                        layer_uuid
                    )
            if self.tairudb_writer.conn:  # type: ignore
                self.tairudb_writer.conn.commit()  # type: ignore
                
    return Algorithm()