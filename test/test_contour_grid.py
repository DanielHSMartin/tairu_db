# -*- coding: utf-8 -*-

"""O recorte e a mescla do DEM copiam os pixels da fonte, sem reamostrar.

Com os limites tirados direto da bbox, o gdal.Warp ancorava a grade no canto da
região, escolhia outra resolução e copiava o pixel mais próximo: a superfície
andava até meio pixel (~15 m em 1 arco-segundo) e as curvas saíam 6-20 m fora
do lugar, sem erro nenhum. Este teste usa um DEM sintético com a grade do
Copernicus (origem a meio pixel do grau), partido em dois tiles, e exige que o
DEM mesclado seja a janela exata da fonte: mesma resolução, grade alinhada e
cada pixel igual.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    import numpy as np
    from osgeo import gdal, osr
    from qgis.core import QgsRectangle
    from tairu_core.contour_generator import _clip_tiles, _merge_tiles
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsRectangle = None

RES = 1.0 / 3600.0
X0 = -48.0 - RES / 2  # como o Copernicus: centros de pixel nos segundos inteiros
Y0 = -15.0 + RES / 2


class _Feedback:
    def push_info(self, _msg):
        return None


def _write_tif(path, array, x0, y0):
    ds = gdal.GetDriverByName('GTiff').Create(
        path, array.shape[1], array.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, RES, 0.0, y0, 0.0, -RES))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(array)
    ds = None


class ContourGridTest(unittest.TestCase):

    def setUp(self):
        if QgsRectangle is None:
            self.skipTest('QGIS Python bindings not available')
        gdal.UseExceptions()
        self.tmp = tempfile.mkdtemp(prefix='contour_grid_')
        # Um valor diferente por pixel: qualquer pixel duplicado ou pulado aparece.
        self.src = np.arange(80 * 120, dtype=np.float32).reshape(80, 120) + 500.0
        self.tiles = [os.path.join(self.tmp, 'w.tif'), os.path.join(self.tmp, 'e.tif')]
        _write_tif(self.tiles[0], self.src[:, :60], X0, Y0)
        _write_tif(self.tiles[1], self.src[:, 60:], X0 + 60 * RES, Y0)

    def test_merged_dem_is_the_exact_source_window(self):
        # bbox fora da grade (frações de pixel) cruzando a divisa dos dois tiles
        bbox = QgsRectangle(X0 + 10.37 * RES, Y0 - 70.61 * RES, X0 + 103.72 * RES, Y0 - 5.18 * RES)
        clipped = _clip_tiles(self.tiles, bbox, self.tmp, _Feedback())
        self.assertEqual(len(clipped), 2)
        merged = os.path.join(self.tmp, 'merged.tif')
        _merge_tiles(clipped, merged)

        ds = gdal.Open(merged)
        gt = ds.GetGeoTransform()
        out = ds.GetRasterBand(1).ReadAsArray()
        ds = None

        self.assertAlmostEqual(gt[1], RES, delta=RES * 1e-9)
        self.assertAlmostEqual(gt[5], -RES, delta=RES * 1e-9)
        col = (gt[0] - X0) / RES
        row = (Y0 - gt[3]) / RES
        self.assertAlmostEqual(col, round(col), delta=1e-6)
        self.assertAlmostEqual(row, round(row), delta=1e-6)
        # bordas nos pixels mais próximos da bbox
        self.assertEqual((round(col), round(row)), (10, 5))
        self.assertEqual(out.shape, (71 - 5, 104 - 10))
        np.testing.assert_array_equal(out, self.src[5:71, 10:104])


if __name__ == '__main__':
    unittest.main()
