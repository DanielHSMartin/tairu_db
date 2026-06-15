import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tairu_core.vector_types import has_elevation_attribute, tairudb_type_for_fields


class TestVectorTypes(unittest.TestCase):
    def test_detects_elevation_attribute_case_insensitively(self):
        self.assertTrue(has_elevation_attribute(["Name", "elev", "Color"]))

    def test_uses_contour_line_type_when_elev_field_exists(self):
        self.assertEqual(tairudb_type_for_fields("line", ["Name", "ELEV"]), "contourLine")

    def test_preserves_default_type_without_elev_field(self):
        self.assertEqual(tairudb_type_for_fields("polygon", ["Name", "Area"]), "polygon")


if __name__ == "__main__":
    unittest.main()
