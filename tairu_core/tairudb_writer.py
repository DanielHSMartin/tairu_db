# -*- coding: utf-8 -*-

"""
SQLite persistence layer for .tairudb files (TairuDBWriter) and the MetaTile
render unit. Moved verbatim from tairu_db_algorithm.py during the 2.0 refactor.
"""

import os
import sqlite3
import uuid
from typing import Optional

from qgis.PyQt.QtCore import QByteArray
from qgis.core import QgsRectangle


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
        # Write to a sibling ".part" and atomically publish on finalize(), so the
        # file only ever appears complete at `filename` — a crash/kill/cancel never
        # leaves a half-written .tairudb that the next run would merge into.
        self._work_path = None
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
            self._work_path = self.filename + '.part'
            # Drop any stale partial from a previously killed run so INSERT OR REPLACE
            # never merges new tiles into a corrupt leftover.
            if os.path.exists(self._work_path):
                try:
                    os.remove(self._work_path)
                except OSError:
                    pass
            self.conn = sqlite3.connect(self._work_path)
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
                    style TEXT,
                    wkb BLOB,
                    FOREIGN KEY(layer_id) REFERENCES vector_layers(uuid)
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
            # int() at the boundary: the table name is a SQL identifier (can't be a
            # bound param), so make it provably numeric — defence-in-depth against any
            # future non-int region_id. Stored in region_tables and reused by saveTile.
            table_name = f"tiles_region_{int(region_id)}"
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

    def insertFeature(self, type_str, name, attr, color, size, iconType, points, layer_id, style=None, wkb=None, feature_uuid=None):
        """Insert a feature into the features table.

        style: optional styleJson string (polygon fill / dash / label config the
        app resolves through RecordStyle); None for a plain feature.
        wkb: optional OGC WKB bytes of the (WGS84) geometry — lets the app render
        polygon holes / multipart structure the flat `points` text can't carry;
        None for features whose `points` already round-trip losslessly.
        feature_uuid: stable id from `map_identity.feature_uuid_for`, so a
        re-export keeps the ids the app already knows. None => random, which is
        right for synthesised features (GRG grids, GeoPDF conversions).
        """
        if not self.cursor:
            return False
        sql = "INSERT INTO features (uuid, layer_id, type, name, attributes, color, size, iconType, points, style, wkb) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"

        def row(fid):
            return (fid, layer_id, type_str, name, attr, color, size, iconType, points, style, wkb)

        try:
            self.cursor.execute(sql, row(feature_uuid or str(uuid.uuid4())))
            return True
        except sqlite3.IntegrityError:
            # The stable id is already in this file — the same source layer was
            # exported twice. Keep the feature under a random id: an unstable id
            # is a nuisance, a silently dropped feature is data loss.
            try:
                self.cursor.execute(sql, row(str(uuid.uuid4())))
                return True
            except sqlite3.Error as e:
                print(f"SQLite error inserting feature: {e}")
                return False
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
        """Commit, close, and atomically publish the work file to its final path.

        VACUUM was removed deliberately: the .tairudb is written insert-only (the only
        DELETE targets the tiny metadata table), so VACUUM reclaimed almost nothing
        while being the single longest blocking call on the GUI thread — the main cause
        of the "Não Responde" freeze / OS force-close on large files. See
        QGIS_PLUGIN_AUDIT.md. Returns True when the final file is published.
        """
        if not self.conn:
            return False

        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error as e:
            print(f"SQLite error finalizing: {e}")
        finally:
            self.conn = None
            self.cursor = None

        # Atomic publish: os.replace is atomic on a single filesystem and overwrites
        # any existing final file, so readers only ever see a complete .tairudb.
        if self._work_path and os.path.exists(self._work_path):
            try:
                os.replace(self._work_path, self.filename)
                return True
            except OSError as e:
                print(f"Error publishing {self.filename}: {e}")
                return False
        return False

    def discard(self):
        """Close and delete the partial work file (cancel/error path) so no corrupt
        .part is left behind to merge into on the next attempt."""
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
            self.cursor = None
        if self._work_path and os.path.exists(self._work_path):
            try:
                os.remove(self._work_path)
            except OSError:
                pass

    def writeGrg(self, bounds, grid_type: str, options: dict) -> bool:
        """Generate and write a GRG grid into this .tairudb.

        bounds   : QgsRectangle in EPSG:4326
        grid_type: 'alphanumeric' | 'utm' | 'dms'
        options  : dict passed directly to GrgGenerator (see grg_generator.py)

        Returns True on success, False if generation fails.
        """
        try:
            from .grg_generator import GrgGenerator
        except ImportError:
            from tairu_core.grg_generator import GrgGenerator

        try:
            gen = GrgGenerator(self, bounds, grid_type, options)
            gen.generate()
            if self.conn:
                self.conn.commit()
            return True
        except Exception as e:
            print(f"GRG generation error: {e}")
            return False

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
