# -*- coding: utf-8 -*-

"""Self-check for basemap tile-prefetch URL handling (the cache-warming that stops the
render from freezing on online XYZ sources). Pure Python — the QGIS/network code is
imported lazily inside _download_all, so these functions need no QGIS."""

import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tairu_core.tile_prefetch import (  # noqa: E402
    fill_template, basemap_tile_urls, _xyz_url_template,
)


class _Layer:
    def __init__(self, src):
        self._src = src

    def source(self):
        return self._src


def _xyz_uri(template):
    """A QGIS XYZ layer source string, url param percent-encoded as QGIS stores it."""
    return f'type=xyz&url={urllib.parse.quote(template, safe="")}&zmax=19&zmin=0'


class TestPrefetchUrls(unittest.TestCase):
    def test_fill_template_xyz(self):
        self.assertEqual(
            fill_template('https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', 3, 5, 17),
            'https://mt1.google.com/vt/lyrs=y&x=3&y=5&z=17')

    def test_fill_template_tms_flip(self):
        # {-y} flips vertically: 2^2 - 1 - 1 = 2
        self.assertEqual(fill_template('http://s/{z}/{x}/{-y}.png', 1, 1, 2), 'http://s/2/1/2.png')

    def test_template_recovered_from_google_xyz_layer(self):
        tmpl = 'https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}'
        self.assertEqual(_xyz_url_template(_Layer(_xyz_uri(tmpl))), tmpl)

    def test_skips_subdomain_rotation(self):
        # {s} can't be matched to the provider's own request -> cache miss -> skip.
        self.assertIsNone(_xyz_url_template(_Layer(_xyz_uri('https://{s}.tile.osm.org/{z}/{x}/{y}.png'))))

    def test_skips_non_xyz_source(self):
        self.assertIsNone(_xyz_url_template(_Layer('crs=EPSG:3857&format=image/png&layers=x&url=http://wms')))

    def test_basemap_urls_are_deduped(self):
        layer = _Layer(_xyz_uri('http://s/{z}/{x}/{y}.png'))
        urls = basemap_tile_urls([layer], [(1, 1), (1, 1), (2, 3)], 5)
        self.assertEqual(urls, ['http://s/5/1/1.png', 'http://s/5/2/3.png'])

    def test_no_xyz_layers_yields_nothing(self):
        self.assertEqual(basemap_tile_urls([_Layer('type=gdal&path=/x.tif')], [(1, 1)], 5), [])


if __name__ == '__main__':
    unittest.main()
