# -*- coding: utf-8 -*-

"""A suavização das curvas muda o terreno — e só dentro do dado.

Da 2.0.10 à 2.0.26 ela foi uma cópia muda: o port editava a saída do gdal.BuildVRT
procurando <SimpleSource>, e o DEM mesclado, que sempre tem nodata, sai como
<ComplexSource>. Consertada, restavam duas armadilhas medidas: com <NODATA> no filtro
o GDAL lê o nodata como 0 e arrasta a borda de uma área em polígono para o nível do
mar; e o ComputeRasterMinMax de um VRT de kernel devolve o máximo da FONTE, o que
deixava o Médio e o Alto 2,3x mais lisos que no CurvaDeNivel.

Os casos abaixo têm resposta exata. Num pico isolado o |TPI| borrado é máximo no
próprio pico, então lá o peso do TPI vale 1 e todo nível devolve só a gaussiana 3x3 —
com o máximo errado, o núcleo pesado entraria. Chão plano encostado no nodata continua
plano. As faixas de leitura ficam curtas para o pico cair na emenda de duas.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    import numpy as np
    from osgeo import gdal, osr
    from qgis.core import QgsApplication, QgsRectangle
    from qgis.PyQt.QtGui import QColor
    from tairu_core import contour_generator as cg
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

LEVELS = ('Baixo', 'Médio', 'Alto')
ND = -32768.0
RES = 1.0 / 3600.0
_APP = None


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], False)
    _APP.initQgis()


class _Feedback:
    def push_info(self, _msg):
        return None

    def heartbeat(self, _msg):
        return None

    def is_canceled(self):
        return False

    def set_progress(self, _value):
        return None

    def reset_progress(self):
        return None


def _write_dem(path, array):
    ds = gdal.GetDriverByName('GTiff').Create(path, array.shape[1], array.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((-48.0, RES, 0.0, -15.0, 0.0, -RES))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.WriteArray(array)
    band.SetNoDataValue(ND)
    band = ds = None


def _read(path):
    ds = gdal.Open(path)
    array = ds.GetRasterBand(1).ReadAsArray().astype('float64')
    ds = None
    return array


class ContourSmoothingTest(unittest.TestCase):

    def setUp(self):
        gdal.UseExceptions()
        self.tmp = tempfile.mkdtemp(prefix='contour_smooth_')
        self.strip_rows = cg._STRIP_ROWS
        cg._STRIP_ROWS = 7  # várias faixas; o pico (linha 20) fecha a faixa 14-20

    def tearDown(self):
        cg._STRIP_ROWS = self.strip_rows

    def test_sharpest_point_keeps_only_the_light_gaussian(self):
        dem = np.full((41, 41), 500.0, np.float32)
        dem[20, 20] = 600.0
        dem[:, :2] = ND  # nodata, como no DEM do pipeline
        merged = os.path.join(self.tmp, 'merged.tif')
        _write_dem(merged, dem)
        center = 0.195346 / 1.000002  # peso central do 3x3, normalizado pela soma dele
        for level in LEVELS:
            out = _read(cg._smooth_terrain(merged, level, self.tmp))
            self.assertAlmostEqual(out[20, 20], 500 + 100 * center, places=3, msg=level)
            # a 3 px do pico o 3x3 não chega; o núcleo pesado do Médio e do Alto, sim
            if level == 'Baixo':
                self.assertAlmostEqual(out[20, 23], 500.0, places=3)
            else:
                self.assertGreater(out[20, 23], 500.1, msg=level)
        np.testing.assert_array_equal(_read(merged), dem)  # a entrada fica intacta

    def test_flat_ground_by_the_mask_stays_flat(self):
        dem = np.full((30, 40), 500.0, np.float32)
        dem[:, :15] = ND  # máscara de uma área em polígono
        merged = os.path.join(self.tmp, 'merged.tif')
        _write_dem(merged, dem)
        for level in LEVELS:
            out = _read(cg._smooth_terrain(merged, level, self.tmp))
            np.testing.assert_array_equal(out == ND, dem == ND)
            np.testing.assert_allclose(out[dem != ND], 500.0, atol=1e-3, err_msg=level)

    def test_the_option_changes_the_contours(self):
        # O defeito como o usuário o via: com e sem suavização saíam as mesmas curvas.
        dem = (500 + np.random.default_rng(3).normal(0, 8, (60, 60))).astype(np.float32)
        tile = os.path.join(self.tmp, 'tile.tif')
        _write_dem(tile, dem)
        bbox = QgsRectangle(-48.0, -15.0 - 60 * RES, -48.0 + 60 * RES, -15.0)
        download = cg._download_tiles
        cg._download_tiles = lambda *_args: [tile]
        try:
            curves = {}
            for level in ('Nenhum', 'Médio'):
                layer = cg.generate_contours(bbox, cg.SOURCE_COPERNICUS, 5, level, QColor('brown'), _Feedback())
                curves[level] = sorted(bytes(f.geometry().asWkb()) for f in layer.getFeatures())
        finally:
            cg._download_tiles = download
        self.assertTrue(curves['Nenhum'])
        self.assertNotEqual(curves['Nenhum'], curves['Médio'])


if __name__ == '__main__':
    unittest.main()
