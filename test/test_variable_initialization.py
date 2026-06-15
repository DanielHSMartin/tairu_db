#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
State-initialization tests, updated for the 2.0 architecture: rendering state
lives in tairu_core.generator.TileRenderEngine (one instance per run) instead
of being reset on the Processing Algorithm between runs.
"""

import unittest
import os
import sys

# Add the plugin directory to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestVariableInitialization(unittest.TestCase):
    """Test state initialization across the algorithm wrapper and render engine"""

    def setUp(self):
        """Set up test fixtures"""
        try:
            from tairu_db_algorithm import TairuDBAlgorithm, TairuDBWriter
            from tairu_core.generator import TileRenderEngine, GenerationSpec
            from tairu_core.feedback import FeedbackAdapter
            self.algorithm_class = TairuDBAlgorithm
            self.writer_class = TairuDBWriter
            self.engine_class = TileRenderEngine
            self.spec_class = GenerationSpec
            self.feedback_class = FeedbackAdapter
        except ImportError:
            # Skip tests if QGIS modules not available
            self.skipTest("QGIS modules not available")

    def _make_spec(self, output_file="/tmp/test.tairudb"):
        from qgis.core import QgsRectangle
        return self.spec_class(
            output_file=output_file,
            layers=[],
            region_tiles={},
            filtered_tiles=[],
            bounds_list=[],
            wgs84_extent=QgsRectangle(),
            max_zoom=18,
        )

    def test_algorithm_variable_initialization(self):
        """Test that the Algorithm wrapper initializes its parameter state"""
        algorithm = self.algorithm_class()

        required_vars = [
            'selected_vector_layers', 'layers', 'max_zoom', 'tile_format',
            'jpg_quality', 'threads_number', 'transform_context',
            'region_result', 'wgs84_crs',
        ]
        for var_name in required_vars:
            self.assertTrue(hasattr(algorithm, var_name),
                          f"Algorithm missing required variable: {var_name}")

        self.assertIsNone(algorithm.region_result)

    def test_engine_variable_initialization(self):
        """Test that TileRenderEngine starts every run with clean state"""
        engine = self.engine_class(self._make_spec(), self.feedback_class())

        self.assertEqual(engine.processed_tiles, 0)
        self.assertEqual(engine.failed_tiles, 0)
        self.assertEqual(engine.retried_tiles, 0)
        self.assertEqual(engine.total_tiles, 0)
        self.assertEqual(engine.meta_tiles, [])
        self.assertEqual(engine.retry_queue, [])
        self.assertEqual(engine.failed_tiles_info, [])
        self.assertEqual(engine.renderer_jobs, {})
        self.assertEqual(engine.max_retries, 3)
        self.assertIsNone(engine.writer)
        self.assertIsNone(engine.error_message)
        self.assertFalse(engine.canceled)

    def test_engine_instances_are_independent(self):
        """Each run uses a fresh engine, so state cannot leak between runs"""
        feedback = self.feedback_class()
        first = self.engine_class(self._make_spec(), feedback)

        # Simulate a finished first run
        first.processed_tiles = 50
        first.failed_tiles = 2
        first.meta_tiles = ['tile1', 'tile2']

        second = self.engine_class(self._make_spec(), feedback)

        self.assertNotEqual(id(first), id(second))
        self.assertEqual(second.processed_tiles, 0)
        self.assertEqual(second.failed_tiles, 0)
        self.assertEqual(second.meta_tiles, [])

    def test_legacy_reexports_available(self):
        """geopdf_converter.py relies on these names living in tairu_db_algorithm"""
        import tairu_db_algorithm
        self.assertTrue(hasattr(tairu_db_algorithm, 'TairuDBWriter'))
        self.assertTrue(hasattr(tairu_db_algorithm, 'MetaTile'))
        self.assertTrue(hasattr(tairu_db_algorithm, 'qvariant_to_python'))

    def test_tairudb_writer_state_reset(self):
        """Test that TairuDBWriter properly resets state on create"""
        import tempfile

        # Create a temporary file for testing
        with tempfile.NamedTemporaryFile(suffix='.tairudb', delete=False) as temp_file:
            temp_filename = temp_file.name

        try:
            writer = self.writer_class(temp_filename)

            # Simulate some existing state
            writer.region_tables = {0: 'tiles_region_0', 1: 'tiles_region_1'}

            # Create should reset state
            writer.create()

            # Verify state is reset
            self.assertEqual(writer.region_tables, {})
            self.assertIsNotNone(writer.conn)
            self.assertIsNotNone(writer.cursor)

            # Clean up
            writer.finalize()

        finally:
            # Clean up temp file
            if os.path.exists(temp_filename):
                os.unlink(temp_filename)

    def test_algorithm_createInstance_returns_new_instance(self):
        """Test that createInstance returns a fresh Algorithm instance"""
        algorithm = self.algorithm_class()

        # Modify state
        algorithm.max_zoom = 12

        # Create new instance
        new_algorithm = algorithm.createInstance()

        # Verify it's a new instance with fresh state
        self.assertNotEqual(id(algorithm), id(new_algorithm))
        self.assertEqual(new_algorithm.max_zoom, 18)


if __name__ == '__main__':
    unittest.main()
