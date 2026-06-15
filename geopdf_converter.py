#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoPDF to TairuDB Converter

Converts GeoPDF files to TairuDB format by extracting raster background as tiles
and vector geometries as separate layers.

Usage:
    python geopdf_converter.py input.pdf output.tairudb [options]
"""

import sys
import os
import argparse
import math
import json
import uuid
import fnmatch
import sqlite3
from typing import Optional, List, Tuple
from datetime import datetime

# Import QGIS and GDAL
try:
    from osgeo import gdal, ogr, osr  # type: ignore
    import pyproj  # type: ignore
    from qgis.core import (  # type: ignore
        QgsApplication,
        QgsRasterLayer,
        QgsProject,
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsRectangle,
        QgsMapSettings,
        QgsMapRendererSequentialJob,
        QgsRasterDataProvider,
        QgsRasterTransparency,
        Qgis,
    )
    from qgis.PyQt.QtCore import QSize, QBuffer, QByteArray, QCoreApplication  # type: ignore
    from qgis.PyQt.QtGui import QImage, QColor  # type: ignore
except ImportError as e:
    print(f"Error importing QGIS/GDAL libraries: {e}")
    print("Make sure QGIS and its Python bindings are installed.")
    sys.exit(1)

# Import from the plugin
from tairu_db_algorithm import TairuDBWriter, MetaTile, qvariant_to_python  # type: ignore
try:
    from tairu_core.vector_types import tairudb_type_for_fields
except ImportError:
    from .tairu_core.vector_types import tairudb_type_for_fields

# QGIS 4 (PyQt6) / QGIS 3 (PyQt5) compatibility constants
try:
    from qgis.PyQt.QtCore import QIODeviceBase
    _OPEN_WRITE_ONLY = QIODeviceBase.WriteOnly         # PyQt6
except ImportError:
    _OPEN_WRITE_ONLY = QBuffer.WriteOnly               # PyQt5

try:
    _FMT_ARGB32 = QImage.Format.Format_ARGB32          # PyQt6
except AttributeError:
    _FMT_ARGB32 = QImage.Format_ARGB32                 # PyQt5


# Resolution table: zoom -> meters per pixel at equator
# Web Mercator standard: each zoom level halves the resolution
RESOLUTION_TABLE = {
    0: 156543.03,  # Whole world
    1: 78271.52,
    2: 39135.76,
    3: 19567.88,
    4: 9783.94,
    5: 4891.97,
    6: 2445.98,
    7: 1222.99,
    8: 611.50,
    9: 305.75,
    10: 152.87,
    11: 76.44,
    12: 38.22,
    13: 19.11,
    14: 9.55,
    15: 4.78,
    16: 2.39,
    17: 1.19,
    18: 0.60,
}


class GeoPDFConverter:
    """Converts GeoPDF files to TairuDB format"""
    
    def __init__(self, input_pdf: str, output_tairudb: str, 
                 tile_format: str = "png", quality: int = 90, 
                 dpi: int = 300, exclude_layers: List[str] = None,
                 raster_layer_index: Optional[int] = None, verbose: bool = False):
        self.input_pdf = input_pdf
        self.output_tairudb = output_tairudb
        self.tile_format = tile_format.upper()
        self.quality = quality
        self.dpi = dpi
        self.exclude_patterns = exclude_layers or []
        self.raster_layer_index = raster_layer_index
        self.verbose = verbose
        
        # QGIS objects
        self.wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        self.mercator_crs = QgsCoordinateReferenceSystem("EPSG:3857")
        self.transform_context = QgsProject.instance().transformContext()
        
        # PDF metadata
        self.pdf_crs: Optional[QgsCoordinateReferenceSystem] = None
        self.pdf_bounds_wgs84: Optional[QgsRectangle] = None
        self.zoom_level: int = 18
        self.resolution_mpp: float = 0.0
        self.raster_layers: List[str] = []  # List of raster background layer names
        
        # Tile processing
        self.tile_width = 256
        self.tile_height = 256
        self.processed_tiles = 0
        self.failed_tiles = 0
        self.total_tiles = 0
        
    def log(self, message: str, force: bool = False):
        """Log message if verbose mode is enabled or force is True"""
        if self.verbose or force:
            print(message)
    
    def validate_geopdf(self, dataset: gdal.Dataset) -> bool:
        """Validate that PDF has proper georeferencing"""
        geotransform = dataset.GetGeoTransform()
        
        # Check for identity matrix (no georeferencing)
        if geotransform == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
            print(f"Error: PDF does not have valid georeferencing.")
            print(f"Geotransform: {geotransform}")
            return False
        
        # Check for zero pixel sizes
        if geotransform[1] == 0 or geotransform[5] == 0:
            print(f"Error: PDF has invalid pixel sizes in geotransform.")
            print(f"Geotransform: {geotransform}")
            return False
        
        self.log(f"✓ Valid georeferencing detected")
        self.log(f"  Geotransform: {geotransform}")
        return True
    
    def extract_metadata(self, dataset: gdal.Dataset) -> bool:
        """Extract metadata from PDF and calculate zoom level"""
        # Get geotransform
        gt = dataset.GetGeoTransform()
        
        # Get projection
        proj_wkt = dataset.GetProjection()
        if not proj_wkt:
            print("Error: PDF does not have a coordinate reference system.")
            return False
        
        # Create CRS
        srs = osr.SpatialReference()
        srs.ImportFromWkt(proj_wkt)
        self.pdf_crs = QgsCoordinateReferenceSystem.fromWkt(proj_wkt)
        
        if not self.pdf_crs or not self.pdf_crs.isValid():
            print(f"Error: Could not parse PDF CRS: {proj_wkt[:100]}...")
            return False
        
        self.log(f"✓ PDF CRS: {self.pdf_crs.authid()} (Geographic: {self.pdf_crs.isGeographic()})")
        
        # Calculate ground resolution in meters per pixel
        pixel_size_x = abs(gt[1])
        pixel_size_y = abs(gt[5])
        
        self.log(f"  Raw pixel size: {pixel_size_x:.10f} {self.pdf_crs.mapUnits()}")
        
        # Convert to meters based on CRS type
        if self.pdf_crs.isGeographic():
            # Geographic CRS - units are degrees
            # At equator: 1 degree ≈ 111320 meters
            # Use center latitude for better accuracy
            width = dataset.RasterXSize
            height = dataset.RasterYSize
            center_lat = gt[3] + (gt[5] * height / 2)
            lat_radians = math.radians(abs(center_lat))
            meters_per_degree = 111320 * math.cos(lat_radians)
            pixel_size_meters = pixel_size_x * meters_per_degree
            self.log(f"  Center latitude: {center_lat:.4f}°, Meters/degree: {meters_per_degree:.2f}")
        else:
            # Projected CRS - units are already in meters (or we assume so)
            pixel_size_meters = pixel_size_x
        
        self.resolution_mpp = pixel_size_meters
        self.log(f"✓ Ground resolution: {self.resolution_mpp:.2f} m/px")
        
        # Calculate the total ground area covered by the PDF
        width = dataset.RasterXSize
        height = dataset.RasterYSize
        
        # Total ground coverage in meters
        ground_width_m = width * pixel_size_meters
        ground_height_m = height * pixel_size_meters
        total_area_m2 = ground_width_m * ground_height_m
        
        # Area per pixel
        area_per_pixel_m2 = pixel_size_meters * pixel_size_meters
        
        self.log(f"  PDF dimensions: {width}x{height} pixels")
        self.log(f"  Ground coverage: {ground_width_m:.2f}m x {ground_height_m:.2f}m")
        self.log(f"  Area per pixel: {area_per_pixel_m2:.2f} m²/px")
        
        # Calculate zoom level that preserves PDF detail
        # Strategy: Find zoom level where total output pixels ≈ total input pixels
        # This ensures we don't lose detail by downsampling too much
        
        zoom_options = sorted(RESOLUTION_TABLE.items(), key=lambda x: x[0], reverse=True)
        
        # For each zoom level, estimate how many tiles would be generated
        # and choose the one that gives similar pixel count to the PDF
        pdf_pixel_count = width * height
        best_zoom = zoom_options[0]
        min_pixel_diff = float('inf')
        
        for zoom, tile_resolution in zoom_options:
            # Calculate how many tiles would cover the ground area at this zoom
            # Each tile is 256x256 pixels, and each pixel covers tile_resolution meters
            tile_ground_size = 256 * tile_resolution  # meters per tile
            
            estimated_tiles_x = max(1, int(ground_width_m / tile_ground_size) + 1)
            estimated_tiles_y = max(1, int(ground_height_m / tile_ground_size) + 1)
            estimated_tile_count = estimated_tiles_x * estimated_tiles_y
            estimated_output_pixels = estimated_tile_count * 256 * 256
            
            # Prefer zoom levels where output pixels is similar to input pixels
            pixel_diff = abs(estimated_output_pixels - pdf_pixel_count)
            
            # Also penalize going too coarse (losing detail)
            if estimated_output_pixels < pdf_pixel_count * 0.5:
                pixel_diff *= 2  # Heavy penalty for losing too much detail
            
            if pixel_diff < min_pixel_diff:
                min_pixel_diff = pixel_diff
                best_zoom = (zoom, tile_resolution)
                
        self.zoom_level = best_zoom[0]
        
        self.log(f"✓ Calculated zoom level: {self.zoom_level}")
        self.log(f"  Tile resolution: {RESOLUTION_TABLE[self.zoom_level]:.2f} m/px")
        self.log(f"  PDF resolution: {self.resolution_mpp:.2f} m/px")
        
        # Calculate bounds in WGS84
        # width and height already calculated above
        
        # Corner coordinates in source CRS
        corners = [
            (gt[0], gt[3]),  # Top-left
            (gt[0] + gt[1] * width, gt[3]),  # Top-right
            (gt[0] + gt[1] * width, gt[3] + gt[5] * height),  # Bottom-right
            (gt[0], gt[3] + gt[5] * height),  # Bottom-left
        ]
        
        # Transform to WGS84
        if not self.pdf_crs:
            print("Error: PDF CRS is not initialized")
            return False
        transform = QgsCoordinateTransform(self.pdf_crs, self.wgs84_crs, self.transform_context)
        wgs84_corners = []
        for x, y in corners:
            point = transform.transform(x, y)
            wgs84_corners.append((point.x(), point.y()))
        
        # Create bounding rectangle
        lons = [c[0] for c in wgs84_corners]
        lats = [c[1] for c in wgs84_corners]
        self.pdf_bounds_wgs84 = QgsRectangle(min(lons), min(lats), max(lons), max(lats))
        
        if self.pdf_bounds_wgs84:
            self.log(f"✓ PDF bounds (WGS84): {self.pdf_bounds_wgs84.toString(3)}")
        
        return True
    
    def detect_raster_layers(self, dataset: gdal.Dataset) -> List[str]:
        """Detect raster background layers in PDF"""
        raster_layers = []
        
        # Check for PDF layers metadata
        layers_md = dataset.GetMetadata("LAYERS")
        if layers_md:
            # Filter for likely raster background layers
            for key, value in layers_md.items():
                if key.endswith("_NAME"):
                    layer_name = value
                    # Check if it's a raster layer (not a vector layer)
                    # Common patterns: satellite, image, background, map, etc.
                    lower_name = layer_name.lower()
                    is_likely_raster = any(keyword in lower_name for keyword in [
                        'satellite', 'image', 'background', 'map', 'google', 'osm',
                        'modificado', 'base', 'terrain', 'topo'
                    ])
                    
                    # Exclude obvious vector layers
                    is_vector = any(keyword in lower_name for keyword in [
                        'ponto', 'área', 'area', 'local', 'risk', 'explos', 'importante'
                    ])
                    
                    if is_likely_raster and not is_vector:
                        raster_layers.append(layer_name)
                        
        return raster_layers
    
    def detect_raster_layer(self, dataset: gdal.Dataset) -> Optional[gdal.Dataset]:
        """Detect and return the raster background layer (legacy single-layer method)"""
        # Check for subdatasets
        subdatasets = dataset.GetSubDatasets()
        
        if not subdatasets:
            self.log("✓ Using main dataset (no subdatasets found)")
            return dataset
        
        self.log(f"Found {len(subdatasets)} subdatasets")
        
        # Analyze subdatasets
        raster_candidates = []
        for idx, (subdataset_path, description) in enumerate(subdatasets):
            self.log(f"  [{idx}] {description}")
            
            # Open subdataset to get info
            sub_ds = gdal.Open(subdataset_path)
            if sub_ds:
                name = description.split(':')[-1] if ':' in description else description
                size = sub_ds.RasterXSize * sub_ds.RasterYSize
                band_count = sub_ds.RasterCount
                
                # Check if it's a raster layer (has bands and size)
                if band_count > 0 and size > 0:
                    # Score based on keywords and size
                    score = size
                    name_lower = name.lower()
                    if any(keyword in name_lower for keyword in ['image', 'background', 'raster']):
                        score *= 10  # Boost score for matching keywords
                    
                    raster_candidates.append({
                        'index': idx,
                        'path': subdataset_path,
                        'description': description,
                        'name': name,
                        'size': size,
                        'bands': band_count,
                        'score': score,
                        'dataset': sub_ds,
                    })
                    
                    self.log(f"      Size: {sub_ds.RasterXSize}x{sub_ds.RasterYSize}, Bands: {band_count}")
        
        if not raster_candidates:
            print("Error: No raster layers found in PDF")
            return None
        
        # Sort by score
        raster_candidates.sort(key=lambda x: x['score'], reverse=True)
        
        # Check if user specified index
        if self.raster_layer_index is not None:
            if 0 <= self.raster_layer_index < len(raster_candidates):
                selected = raster_candidates[self.raster_layer_index]
                self.log(f"✓ Using user-specified raster layer [{selected['index']}]: {selected['name']}")
                return selected['dataset']
            else:
                print(f"Error: Invalid raster layer index {self.raster_layer_index} (valid: 0-{len(raster_candidates)-1})")
                return None
        
        # Check for ambiguity (multiple candidates with similar scores)
        top_score = raster_candidates[0]['score']
        similar_candidates = [c for c in raster_candidates if c['score'] >= top_score * 0.9]
        
        if len(similar_candidates) > 1:
            # Prompt user to select
            print("\nMultiple raster layer candidates found:")
            for i, candidate in enumerate(similar_candidates):
                print(f"  [{i}] {candidate['name']}")
                print(f"      Size: {candidate['size']} pixels, Bands: {candidate['bands']}")
            
            while True:
                try:
                    choice = input(f"\nSelect raster layer (0-{len(similar_candidates)-1}): ").strip()
                    choice_idx = int(choice)
                    if 0 <= choice_idx < len(similar_candidates):
                        selected = similar_candidates[choice_idx]
                        self.log(f"✓ Using raster layer [{selected['index']}]: {selected['name']}")
                        return selected['dataset']
                    else:
                        print(f"Invalid choice. Please enter 0-{len(similar_candidates)-1}")
                except ValueError:
                    print("Invalid input. Please enter a number.")
                except KeyboardInterrupt:
                    print("\nCancelled by user")
                    return None
        else:
            # Use top candidate
            selected = raster_candidates[0]
            self.log(f"✓ Auto-selected raster layer: {selected['name']}")
            return selected['dataset']
    
    def lon2tilex(self, lon: float, n: float) -> int:
        """Convert longitude to tile X coordinate"""
        return int((lon + 180.0) / 360.0 * n)
    
    def lat2tiley(self, lat: float, n: float) -> int:
        """Convert latitude to tile Y coordinate"""
        lat_rad = math.radians(lat)
        return int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    
    def create_metatile(self, z: int, tx: int, ty: int, n: float) -> MetaTile:
        """Create a MetaTile object for given tile coordinates"""
        # Calculate WGS84 bounds
        x1 = tx * 360.0 / n - 180.0
        y1 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        x2 = (tx + 1) * 360.0 / n - 180.0
        y2 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
        
        # Create MetaTile
        meta_tile = MetaTile()
        meta_tile.zoom = z
        meta_tile.tx = tx
        meta_tile.ty = ty
        meta_tile.metatile_size = 1
        meta_tile.actual_size_x = 1
        meta_tile.actual_size_y = 1
        
        # Transform to Mercator for rendering
        transform = QgsCoordinateTransform(self.wgs84_crs, self.mercator_crs, self.transform_context)
        p1 = transform.transform(x1, y1)
        p2 = transform.transform(x2, y2)
        meta_tile.extent = QgsRectangle(
            min(p1.x(), p2.x()),
            min(p1.y(), p2.y()),
            max(p1.x(), p2.x()),
            max(p1.y(), p2.y())
        )
        
        return meta_tile
    
    def is_tile_empty(self, tile_image: QImage) -> bool:
        """Check if tile is completely empty (all pixels fully transparent)"""
        if tile_image.isNull():
            return True
        
        # If has alpha channel, check if ALL pixels are fully transparent
        if tile_image.hasAlphaChannel():
            if tile_image.format() != _FMT_ARGB32:
                tile_image = tile_image.convertToFormat(_FMT_ARGB32)
            
            # Sample more points to be thorough
            sample_points = []
            width = tile_image.width()
            height = tile_image.height()
            
            # Sample grid: 5x5 = 25 points
            for i in range(5):
                for j in range(5):
                    x = (i * width) // 5
                    y = (j * height) // 5
                    if x < width and y < height:
                        sample_points.append((x, y))
            
            # If ANY sampled pixel has alpha > 0, tile is not empty
            for x, y in sample_points:
                pixel = tile_image.pixel(x, y)
                alpha = (pixel >> 24) & 0xFF
                if alpha > 0:  # Any non-transparent pixel means tile has content
                    return False
            
            # All sampled pixels are fully transparent
            return True
        
        # No alpha channel - check if uniform color (likely background)
        sample_points = [
            (0, 0),
            (tile_image.width()//4, tile_image.height()//4),
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
                    return False
        
        return True
    
    def generate_tiles(self, raster_dataset: gdal.Dataset, tairudb_writer: TairuDBWriter, region_id: int = 0) -> bool:
        """Generate tiles from raster dataset"""
        if not self.pdf_bounds_wgs84:
            print("Error: PDF bounds not initialized")
            return False
            
        # Create QGIS raster layer
        # Use the dataset path directly instead of GDAL connection string
        dataset_path = raster_dataset.GetDescription()
        self.log(f"Creating raster layer from: {dataset_path}")
        raster_layer = QgsRasterLayer(dataset_path, "PDF Background")
        
        if not raster_layer.isValid():
            # Try with GDAL prefix
            raster_layer = QgsRasterLayer(f"GDAL:{dataset_path}", "PDF Background")
            
        if not raster_layer.isValid():
            print(f"Error: Could not create QGIS raster layer from PDF")
            print(f"  Dataset path: {dataset_path}")
            print(f"  Dataset type: {type(raster_dataset)}")
            return False
        
        self.log(f"✓ Created raster layer: {raster_layer.width()}x{raster_layer.height()}")
        
        # Set resampling to preserve quality
        # Use bilinear or cubic for better quality when scaling
        renderer = raster_layer.renderer()
        if renderer:
            provider = raster_layer.dataProvider()
            if provider:
                # Set zoom in/out resampling to cubic for best quality
                provider.setZoomedInResamplingMethod(QgsRasterDataProvider.ResamplingMethod.Cubic)
                provider.setZoomedOutResamplingMethod(QgsRasterDataProvider.ResamplingMethod.Cubic)
                provider.setMaxOversampling(2.0)
                self.log("✓ Set cubic resampling for high quality")
                
                # Configure transparency - treat white pixels as transparent
                # This is needed for PDF layers that don't have native alpha channel
                transparency = QgsRasterTransparency()
                
                # Add transparent pixel values - white (255, 255, 255) should be transparent
                transparent_pixels = []
                pixel_value = QgsRasterTransparency.TransparentThreeValuePixel()
                pixel_value.red = 255
                pixel_value.green = 255
                pixel_value.blue = 255
                pixel_value.percentTransparent = 100  # 100% transparent
                transparent_pixels.append(pixel_value)
                
                transparency.setTransparentThreeValuePixelList(transparent_pixels)
                renderer.setRasterTransparency(transparency)
                self.log("✓ Configured white pixels as transparent")
        
        # Add to project
        QgsProject.instance().addMapLayer(raster_layer, False)
        
        # Calculate tile range
        n = 2.0 ** self.zoom_level
        x_min = self.pdf_bounds_wgs84.xMinimum()
        x_max = self.pdf_bounds_wgs84.xMaximum()
        y_min = self.pdf_bounds_wgs84.yMinimum()
        y_max = self.pdf_bounds_wgs84.yMaximum()
        
        tile_x_min = max(0, self.lon2tilex(x_min, n))
        tile_x_max = min(int(n) - 1, self.lon2tilex(x_max, n))
        tile_y_min = max(0, self.lat2tiley(y_max, n))  # y_max is north
        tile_y_max = min(int(n) - 1, self.lat2tiley(y_min, n))  # y_min is south
        
        self.total_tiles = (tile_x_max - tile_x_min + 1) * (tile_y_max - tile_y_min + 1)
        self.log(f"✓ Tile range: X[{tile_x_min}..{tile_x_max}], Y[{tile_y_min}..{tile_y_max}]")
        self.log(f"✓ Total tiles to generate: {self.total_tiles}")
        
        # Generate tiles
        tile_count = 0
        for tx in range(tile_x_min, tile_x_max + 1):
            for ty in range(tile_y_min, tile_y_max + 1):
                tile_count += 1
                
                # Progress
                if tile_count % 100 == 0 or tile_count == self.total_tiles:
                    progress = 100.0 * tile_count / self.total_tiles
                    self.log(f"  Processing tiles: {tile_count}/{self.total_tiles} ({progress:.1f}%)", force=True)
                
                # Create metatile
                meta_tile = self.create_metatile(self.zoom_level, tx, ty, n)
                
                if not meta_tile.is_valid():
                    self.log(f"  Warning: Invalid metatile {tx},{ty}, skipping")
                    self.failed_tiles += 1
                    continue
                
                # Render tile
                map_settings = QgsMapSettings()
                map_settings.setLayers([raster_layer])
                map_settings.setOutputDpi(self.dpi)
                map_settings.setOutputSize(QSize(self.tile_width, self.tile_height))
                map_settings.setExtent(meta_tile.extent)
                map_settings.setDestinationCrs(self.mercator_crs)
                map_settings.setFlag(Qgis.MapSettingsFlag.Antialiasing, True)
                map_settings.setFlag(Qgis.MapSettingsFlag.RenderMapTile, True)
                
                # Set transparent background
                map_settings.setBackgroundColor(QColor(255, 255, 255, 0))  # Transparent white
                
                # Render
                job = QgsMapRendererSequentialJob(map_settings)
                job.start()
                job.waitForFinished()
                
                tile_image = job.renderedImage()
                
                if tile_image.isNull():
                    self.log(f"  Warning: Failed to render tile {tx},{ty}")
                    self.failed_tiles += 1
                    continue
                
                # Convert to ARGB32 format to ensure alpha channel is preserved
                if tile_image.format() != _FMT_ARGB32:
                    tile_image = tile_image.convertToFormat(_FMT_ARGB32)
                
                # For layers with transparency, save all tiles (even fully transparent)
                # Otherwise skip completely empty tiles to save space
                # Note: We keep the check for layers without transparency
                skip_empty = not tile_image.hasAlphaChannel()
                if skip_empty and self.is_tile_empty(tile_image):
                    self.processed_tiles += 1
                    continue
                
                # Convert to format
                tile_data = QByteArray()
                buffer = QBuffer(tile_data)
                buffer.open(_OPEN_WRITE_ONLY)
                
                success = False
                if self.tile_format == "PNG":
                    success = tile_image.save(buffer, "PNG")
                elif self.tile_format == "JPG":
                    success = tile_image.save(buffer, "JPG", self.quality)
                elif self.tile_format == "WEBP":
                    formats = QImage.supportedImageFormats()
                    if b'WEBP' in formats:
                        success = tile_image.save(buffer, "WEBP", self.quality)
                    else:
                        success = tile_image.save(buffer, "PNG")
                
                if not success or tile_data.isEmpty():
                    self.log(f"  Warning: Failed to encode tile {tx},{ty}")
                    self.failed_tiles += 1
                    continue
                
                # Save to database (TMS Y-flip)
                max_tile_index = int(2 ** self.zoom_level)
                tms_y = max_tile_index - 1 - ty
                
                if not tairudb_writer.saveTile(self.zoom_level, tx, tms_y, tile_data, region_id):
                    self.log(f"  Warning: Failed to save tile {tx},{ty} to database")
                    self.failed_tiles += 1
                    continue
                
                self.processed_tiles += 1
                
                # Periodic commit
                if self.processed_tiles % 100 == 0:
                    tairudb_writer.periodicCommit()
        
        # Remove layer from project
        QgsProject.instance().removeMapLayer(raster_layer)
        
        return True
    
    def extract_vectors(self, tairudb_writer: TairuDBWriter) -> int:
        """Extract vector layers from PDF"""
        datasource = ogr.Open(self.input_pdf)
        if not datasource:
            self.log("No vector layers found in PDF")
            return 0
        
        layer_count = datasource.GetLayerCount()
        self.log(f"Found {layer_count} vector layers in PDF")
        
        exported_count = 0
        excluded_count = 0
        
        # WGS84 spatial reference
        wgs84_srs = osr.SpatialReference()
        wgs84_srs.ImportFromEPSG(4326)
        
        for i in range(layer_count):
            layer = datasource.GetLayer(i)
            layer_name = layer.GetName()
            
            # Check exclusion patterns
            excluded = False
            for pattern in self.exclude_patterns:
                if fnmatch.fnmatch(layer_name, pattern):
                    self.log(f"  Excluding layer: {layer_name} (matches pattern '{pattern}')")
                    excluded = True
                    excluded_count += 1
                    break
            
            if excluded:
                continue
            
            # Check if it has geometry
            geom_type = layer.GetGeomType()
            if geom_type == ogr.wkbNone:
                self.log(f"  Skipping layer: {layer_name} (no geometry)")
                continue
            
            # Determine type
            if geom_type in [ogr.wkbPoint, ogr.wkbPoint25D, ogr.wkbMultiPoint, ogr.wkbMultiPoint25D]:
                type_str = "point"
                icon_type = "locationOn"
                size = 40
            elif geom_type in [ogr.wkbLineString, ogr.wkbLineString25D, ogr.wkbMultiLineString, ogr.wkbMultiLineString25D]:
                type_str = "line"
                icon_type = "line"
                size = 3
            elif geom_type in [ogr.wkbPolygon, ogr.wkbPolygon25D, ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D]:
                type_str = "polygon"
                icon_type = "polygon"
                size = 3
            else:
                type_str = "unknown"
                icon_type = "locationOn"
                size = 10

            layer_def = layer.GetLayerDefn()
            field_names = [
                layer_def.GetFieldDefn(j).GetName()
                for j in range(layer_def.GetFieldCount())
            ]
            type_str = tairudb_type_for_fields(type_str, field_names)
            if type_str == "contourLine":
                icon_type = "line"
                size = 3
            
            feature_count = layer.GetFeatureCount()
            self.log(f"  Exporting layer: {layer_name} ({type_str}, {feature_count} features)")
            
            # Create coordinate transformation
            layer_srs = layer.GetSpatialRef()
            transformer = None
            needs_transform = False
            
            if layer_srs:
                # Check if it's already WGS84 or close enough (SIRGAS 2000 = EPSG:4674 is very close to WGS84)
                if layer_srs.IsGeographic():
                    auth_code = layer_srs.GetAuthorityCode(None)
                    if auth_code in ['4326', '4674', None]:  # WGS84, SIRGAS2000, or unknown geographic
                        self.log(f"  Layer is geographic (EPSG:{auth_code}), treating as WGS84")
                        needs_transform = False
                    else:
                        needs_transform = True
                else:
                    # Projected CRS needs transformation
                    needs_transform = True
                    
                if needs_transform and not layer_srs.IsSame(wgs84_srs):
                    try:
                        # Get EPSG code for pyproj
                        epsg_code = layer_srs.GetAuthorityCode(None)
                        if epsg_code:
                            transformer = pyproj.Transformer.from_crs(
                                f"EPSG:{epsg_code}", 
                                "EPSG:4326",
                                always_xy=True
                            )
                            self.log(f"  Created pyproj transformer from EPSG:{epsg_code} to WGS84")
                        else:
                            # Fallback to WKT
                            wkt = layer_srs.ExportToWkt()
                            transformer = pyproj.Transformer.from_crs(
                                wkt,
                                "EPSG:4326",
                                always_xy=True
                            )
                            self.log(f"  Created pyproj transformer from WKT to WGS84")
                    except Exception as e:
                        self.log(f"  Warning: Could not create pyproj transformer: {e}")
                        self.log(f"  Will export without transformation")
                        transformer = None
            else:
                self.log(f"  Warning: Layer has no spatial reference, assuming WGS84")
            
            # Generate UUID for layer
            layer_uuid = str(uuid.uuid4())
            tairudb_writer.insertVectorLayer(layer_uuid, type_str, layer_name, "")
            
            # Export features
            layer.ResetReading()
            feature = layer.GetNextFeature()
            feat_count = 0
            
            while feature:
                feat_count += 1
                geom = feature.GetGeometryRef()
                
                if geom is None:
                    feature = layer.GetNextFeature()
                    continue
                
                # Clone geometry
                geom = geom.Clone()
                
                # Transform to WGS84 if needed using pyproj
                if transformer:
                    try:
                        # For points, transform directly
                        if geom_type in [ogr.wkbPoint, ogr.wkbPoint25D]:
                            x_src, y_src = geom.GetX(), geom.GetY()
                            lon, lat = transformer.transform(x_src, y_src)
                            geom = ogr.Geometry(ogr.wkbPoint)
                            geom.AddPoint(lon, lat)
                        # For lines and polygons, transform each coordinate
                        elif geom_type in [ogr.wkbLineString, ogr.wkbLineString25D]:
                            new_line = ogr.Geometry(ogr.wkbLineString)
                            for i in range(geom.GetPointCount()):
                                x_src, y_src = geom.GetX(i), geom.GetY(i)
                                lon, lat = transformer.transform(x_src, y_src)
                                new_line.AddPoint(lon, lat)
                            geom = new_line
                        elif geom_type in [ogr.wkbPolygon, ogr.wkbPolygon25D]:
                            new_poly = ogr.Geometry(ogr.wkbPolygon)
                            ring = geom.GetGeometryRef(0)
                            new_ring = ogr.Geometry(ogr.wkbLinearRing)
                            for i in range(ring.GetPointCount()):
                                x_src, y_src = ring.GetX(i), ring.GetY(i)
                                lon, lat = transformer.transform(x_src, y_src)
                                new_ring.AddPoint(lon, lat)
                            new_poly.AddGeometry(new_ring)
                            geom = new_poly
                        else:
                            # For multi-geometries, skip for now
                            self.log(f"  Warning: Skipping transformation for geometry type {geom_type}")
                    except Exception as e:
                        self.log(f"  Warning: Transform failed: {e}, skipping feature")
                        feature = layer.GetNextFeature()
                        continue
                
                # Extract coordinates
                points_groups = []
                
                if geom_type in [ogr.wkbPoint, ogr.wkbPoint25D]:
                    points_groups.append(f"{geom.GetX()} {geom.GetY()}")
                elif geom_type in [ogr.wkbMultiPoint, ogr.wkbMultiPoint25D]:
                    for i in range(geom.GetGeometryCount()):
                        pt = geom.GetGeometryRef(i)
                        points_groups.append(f"{pt.GetX()} {pt.GetY()}")
                elif geom_type in [ogr.wkbLineString, ogr.wkbLineString25D]:
                    points = [f"{geom.GetX(i)} {geom.GetY(i)}" for i in range(geom.GetPointCount())]
                    points_groups.append(", ".join(points))
                elif geom_type in [ogr.wkbMultiLineString, ogr.wkbMultiLineString25D]:
                    for i in range(geom.GetGeometryCount()):
                        line = geom.GetGeometryRef(i)
                        points = [f"{line.GetX(j)} {line.GetY(j)}" for j in range(line.GetPointCount())]
                        points_groups.append(", ".join(points))
                elif geom_type in [ogr.wkbPolygon, ogr.wkbPolygon25D]:
                    ring = geom.GetGeometryRef(0)  # Exterior ring only
                    if ring:
                        points = [f"{ring.GetX(j)} {ring.GetY(j)}" for j in range(ring.GetPointCount())]
                        points_groups.append(", ".join(points))
                elif geom_type in [ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D]:
                    for i in range(geom.GetGeometryCount()):
                        poly = geom.GetGeometryRef(i)
                        ring = poly.GetGeometryRef(0)  # Exterior ring only
                        if ring:
                            points = [f"{ring.GetX(j)} {ring.GetY(j)}" for j in range(ring.GetPointCount())]
                            points_groups.append(", ".join(points))
                
                points_str = "; ".join(points_groups)
                
                # Get attributes
                attributes = {}
                for j in range(layer_def.GetFieldCount()):
                    field_def = layer_def.GetFieldDefn(j)
                    field_name = field_def.GetName()
                    field_value = feature.GetField(j)
                    attributes[field_name] = field_value
                
                attributes_json = json.dumps(attributes)
                
                # Feature name - check multiple possible field names
                # Try: name, Name, Nome (Portuguese), NOME, description, Description
                feat_name = None
                for name_field in ['name', 'Name', 'Nome', 'NOME', 'description', 'Description', 'Descrição']:
                    if name_field in attributes and attributes[name_field]:
                        feat_name = attributes[name_field]
                        break
                
                # Fallback to layer name + count if no name field found
                if not feat_name:
                    feat_name = f"{layer_name} {feat_count}"
                
                # Extract color from feature style or use layer-based defaults
                color = "#0000FF"  # Default blue color
                
                # Try to get color from feature style first
                style_string = feature.GetStyleString()
                if style_string:
                    # Parse OGR style string for color
                    # Format examples: "PEN(c:#FF0000)", "BRUSH(fc:#00FF00)", "SYMBOL(c:#0000FF)"
                    import re
                    color_match = re.search(r'c:#([0-9A-Fa-f]{6})', style_string)
                    if color_match:
                        color = f"#{color_match.group(1).upper()}"
                    else:
                        # Try fc: (fill color) for polygons
                        color_match = re.search(r'fc:#([0-9A-Fa-f]{6})', style_string)
                        if color_match:
                            color = f"#{color_match.group(1).upper()}"
                
                # Fallback: Common layer name to color mappings
                # These are typical colors used in Brazilian military/engineering PDFs
                if color == "#0000FF":  # Still default, try layer name mapping
                    layer_name_lower = layer_name.lower()
                    # Check in priority order - most specific first
                    if 'risco' in layer_name_lower or 'risk' in layer_name_lower or 'danger' in layer_name_lower:
                        color = "#FF0000"  # Red for risk/danger areas
                    elif 'explos' in layer_name_lower or 'blast' in layer_name_lower:
                        color = "#FF8800"  # Orange for explosion areas
                    elif 'locais principais' in layer_name_lower or 'main location' in layer_name_lower:
                        color = "#FFFF00"  # Yellow for main locations
                    elif 'pontos importantes' in layer_name_lower or 'important point' in layer_name_lower:
                        color = "#0088FF"  # Light blue for important points
                    elif 'principal' in layer_name_lower or 'main' in layer_name_lower:
                        color = "#FFFF00"  # Yellow for main/principal
                    elif 'importante' in layer_name_lower or 'important' in layer_name_lower:
                        color = "#00FFFF"  # Cyan for important
                    elif 'ponto' in layer_name_lower or 'point' in layer_name_lower:
                        color = "#0088FF"  # Light blue for points
                
                # Insert feature
                tairudb_writer.insertFeature(
                    type_str,
                    str(feat_name),
                    attributes_json,
                    color,
                    size,
                    icon_type,
                    points_str,
                    layer_uuid
                )
                
                feature = layer.GetNextFeature()
            
            exported_count += 1
        
        if excluded_count > 0:
            self.log(f"✓ Excluded {excluded_count} layers matching patterns")
        
        return exported_count
    
    def convert(self) -> bool:
        """Main conversion process"""
        print(f"\n=== GeoPDF to TairuDB Converter ===")
        print(f"Input:  {self.input_pdf}")
        print(f"Output: {self.output_tairudb}")
        print()
        
        # Configure GDAL for high-quality PDF rasterization
        gdal.SetConfigOption('GDAL_PDF_DPI', str(self.dpi))
        gdal.SetConfigOption('GDAL_PDF_RENDERING_OPTIONS', 'RASTER')  # Only raster, no vectors
        self.log(f"PDF rendering DPI: {self.dpi}")
        self.log(f"Rendering mode: RASTER only (vectors excluded from tiles)")
        
        # Open PDF
        self.log("Opening PDF...")
        dataset = gdal.Open(self.input_pdf)
        if not dataset:
            print(f"Error: Could not open PDF file: {self.input_pdf}")
            return False
        
        # Validate GeoPDF
        if not self.validate_geopdf(dataset):
            return False
        
        # Extract metadata
        if not self.extract_metadata(dataset):
            return False
        
        # Detect raster layers
        self.log("\nDetecting raster background layers...")
        self.raster_layers = self.detect_raster_layers(dataset)
        
        if not self.raster_layers:
            self.log("No raster background layers found, using default rendering")
            self.raster_layers = ["default"]
        else:
            self.log(f"Found {len(self.raster_layers)} raster background layer(s):")
            for i, layer_name in enumerate(self.raster_layers):
                self.log(f"  [{i}] {layer_name}")
        
        # Create TairuDB
        self.log("\nCreating TairuDB database...")
        tairudb_writer = TairuDBWriter(self.output_tairudb)
        if not tairudb_writer.create():
            print(f"Error: Could not create TairuDB file: {self.output_tairudb}")
            return False
        
        # Set metadata
        format_lower = self.tile_format.lower()
        tairudb_writer.setMetadataValue("format", format_lower)
        base_name = os.path.splitext(os.path.basename(self.input_pdf))[0]
        tairudb_writer.setMetadataValue("name", base_name)
        tairudb_writer.setMetadataValue("description", base_name)
        tairudb_writer.setMetadataValue("version", "1.2")
        tairudb_writer.setMetadataValue("type", "overlay")
        tairudb_writer.setMetadataValue("minzoom", str(self.zoom_level))
        tairudb_writer.setMetadataValue("maxzoom", str(self.zoom_level))
        
        # Center point
        if not self.pdf_bounds_wgs84:
            print("Error: PDF bounds not initialized")
            tairudb_writer.finalize()
            return False
        center_x = (self.pdf_bounds_wgs84.xMinimum() + self.pdf_bounds_wgs84.xMaximum()) / 2
        center_y = (self.pdf_bounds_wgs84.yMinimum() + self.pdf_bounds_wgs84.yMaximum()) / 2
        center_str = f"{center_x},{center_y},{self.zoom_level}"
        tairudb_writer.setMetadataValue("center", center_str)
        
        tairudb_writer.setMetadataValue("generator", "GeoPDF Converter")
        tairudb_writer.setMetadataValue("created", datetime.now().isoformat())
        
        self.log("✓ Metadata configured")
        
        # Process each raster layer as a separate region
        total_tiles_all_regions = 0
        for region_idx, layer_name in enumerate(self.raster_layers):
            self.log(f"\n=== Processing Region {region_idx}: {layer_name} ===")
            
            # Configure GDAL to render only this layer
            if layer_name != "default":
                gdal.SetConfigOption('GDAL_PDF_LAYERS', layer_name)
            
            # Re-open dataset with layer selection
            region_dataset = gdal.Open(self.input_pdf)
            if not region_dataset:
                self.log(f"Warning: Could not open PDF with layer '{layer_name}'")
                continue
            
            # Create tiles table for this region
            tairudb_writer.createTilesTableForRegion(region_idx)
            
            # Insert region metadata
            bounds_str = f"{self.pdf_bounds_wgs84.xMinimum()} {self.pdf_bounds_wgs84.yMinimum()}, " \
                         f"{self.pdf_bounds_wgs84.xMaximum()} {self.pdf_bounds_wgs84.yMinimum()}, " \
                         f"{self.pdf_bounds_wgs84.xMaximum()} {self.pdf_bounds_wgs84.yMaximum()}, " \
                         f"{self.pdf_bounds_wgs84.xMinimum()} {self.pdf_bounds_wgs84.yMaximum()}"
            tairudb_writer.insertRegion(layer_name, self.zoom_level, self.zoom_level, bounds_str)
            
            # Generate tiles for this region
            self.processed_tiles = 0
            self.failed_tiles = 0
            if not self.generate_tiles(region_dataset, tairudb_writer, region_idx):
                self.log(f"Warning: Failed to generate tiles for region {region_idx}")
                continue
            
            total_tiles_all_regions += self.processed_tiles
            success_rate = (self.processed_tiles / self.total_tiles * 100) if self.total_tiles > 0 else 0
            self.log(f"✓ Region {region_idx} tiles: {self.processed_tiles}/{self.total_tiles} ({success_rate:.1f}%)")
        
        # Extract vectors (only once, not per region)
        self.log("\nExtracting vector layers...")
        vector_count = self.extract_vectors(tairudb_writer)
        if vector_count > 0:
            self.log(f"✓ Exported {vector_count} vector layers")
        else:
            self.log("✓ No vector layers to export")
        
        # Finalize
        self.log("\nFinalizing database...")
        tairudb_writer.finalize()
        
        print(f"\n✓ Conversion complete!")
        print(f"  Output: {self.output_tairudb}")
        print(f"  Regions: {len(self.raster_layers)}")
        print(f"  Total tiles: {total_tiles_all_regions} (zoom {self.zoom_level})")
        print(f"  Vectors: {vector_count} layers")
        
        return True


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Convert GeoPDF files to TairuDB format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input.pdf output.tairudb
  %(prog)s input.pdf output.tairudb --format png --quality 90
  %(prog)s input.pdf output.tairudb --exclude-layers "Grid*,Label*"
  %(prog)s input.pdf output.tairudb --raster-layer-index 0 --verbose
        """
    )
    
    parser.add_argument("input_pdf", help="Input GeoPDF file")
    parser.add_argument("output_tairudb", help="Output TairuDB file")
    parser.add_argument("--format", choices=["png", "jpg", "webp"], default="png",
                        help="Tile image format (default: png)")
    parser.add_argument("--quality", type=int, default=90, metavar="N",
                        help="Image quality for JPG/WebP (1-100, default: 90)")
    parser.add_argument("--dpi", type=int, default=150, metavar="N",
                        help="Rendering DPI (default: 150)")
    parser.add_argument("--exclude-layers", type=str, metavar="PATTERNS",
                        help="Comma-separated wildcard patterns for layers to exclude (e.g., 'Grid*,Label*')")
    parser.add_argument("--raster-layer-index", type=int, metavar="N",
                        help="Manually specify raster layer index (bypasses auto-detection)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose output")
    
    args = parser.parse_args()
    
    # Parse exclusion patterns
    exclude_patterns = []
    if args.exclude_layers:
        exclude_patterns = [p.strip() for p in args.exclude_layers.split(",")]
    
    # Initialize QGIS
    QgsApplication.setPrefixPath("/Applications/QGIS-LTR.app/Contents/MacOS", True)
    qgs = QgsApplication([], False)
    qgs.initQgis()
    
    try:
        # Create converter
        converter = GeoPDFConverter(
            args.input_pdf,
            args.output_tairudb,
            tile_format=args.format,
            quality=args.quality,
            dpi=args.dpi,
            exclude_layers=exclude_patterns,
            raster_layer_index=args.raster_layer_index,
            verbose=args.verbose
        )
        
        # Convert
        success = converter.convert()
        
        # Exit
        sys.exit(0 if success else 1)
        
    finally:
        # Cleanup QGIS
        qgs.exitQgis()


if __name__ == "__main__":
    main()
