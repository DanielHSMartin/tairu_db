# -*- coding: utf-8 -*-

"""Contorno do dado válido de uma imagem, e o que ele evita.

Uma imagem recortada — mosaico cortado por um rio, voo em faixa — tem caixa
muito maior do que a própria imagem. Usar a caixa como área de interesse
multiplicou por 18 o número de tiles de um mosaico de corredor fluvial, e o
arquivo junto: como a região vira um retângulo, o estêncil de borda não recorta
nada e o mapa de fundo preenche todo o vazio com conteúdo, de modo que o filtro
de tile vazio não descarta nada.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QGIS_CUSTOM_CONFIG_PATH', tempfile.mkdtemp(prefix='qgis-test-'))

try:
    from qgis.core import QgsApplication, QgsRasterLayer
    from osgeo import gdal
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

try:
    from tairu_core.raster_footprint import coverage_ratio, data_footprint
except ImportError:  # pragma: no cover
    data_footprint = None

_APP = None
_DIR = None


def setUpModule():
    global _APP, _DIR
    if QgsApplication is None or data_footprint is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], True)
    _APP.initQgis()
    _DIR = tempfile.mkdtemp(prefix='footprint-')


def _write(path, bands, fill):
    """GeoTIFF 200x200 a 1 m, UTM 21S. `fill(x, y, band)` devolve 0..255."""
    ds = gdal.GetDriverByName('GTiff').Create(path, 200, 200, bands, gdal.GDT_Byte)
    ds.SetGeoTransform((500000.0, 1.0, 0.0, 9000200.0, 0.0, -1.0))
    ds.SetProjection(
        'PROJCS["WGS 84 / UTM zone 21S",GEOGCS["WGS 84",DATUM["WGS_1984",'
        'SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],'
        'UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
        'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",-57],'
        'PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
        'PARAMETER["false_northing",10000000],UNIT["metre",1],AUTHORITY["EPSG","32721"]]')
    for b in range(1, bands + 1):
        rows = bytearray()
        for y in range(200):
            rows.extend(bytes(fill(x, y, b) for x in range(200)))
        ds.GetRasterBand(b).WriteRaster(0, 0, 200, 200, bytes(rows))
    if bands == 4:
        ds.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
    ds.FlushCache()
    ds = None
    return path


def _diagonal(x, y, b):
    """Faixa diagonal de 40 px de largura: opaca dentro, transparente fora."""
    dentro = abs(x - y) < 20
    if b == 4:
        return 255 if dentro else 0
    return 120 if dentro else 0


class DataFootprintTest(unittest.TestCase):

    def test_imagem_sem_mascara_devolve_none(self):
        """Sem alfa nem nodata a caixa JÁ é o contorno: None mantém o custo de antes."""
        path = _write(os.path.join(_DIR, 'cheia.tif'), 3, lambda x, y, b: 200)
        layer = QgsRasterLayer(path, 'cheia')
        self.assertTrue(layer.isValid())
        self.assertIsNone(data_footprint(layer))
        self.assertIsNone(coverage_ratio(layer, data_footprint(layer)))

    def test_faixa_diagonal_mede_muito_menos_que_a_caixa(self):
        """O caso que motivou tudo: a faixa ocupa ~1/5 da caixa que a envolve."""
        path = _write(os.path.join(_DIR, 'faixa.tif'), 4, _diagonal)
        layer = QgsRasterLayer(path, 'faixa')
        self.assertTrue(layer.isValid())
        fp = data_footprint(layer)
        self.assertIsNotNone(fp)
        extent = layer.extent()
        caixa = extent.width() * extent.height()
        # 40 px de largura numa diagonal de 200x200 = 20% da caixa. A folga
        # para cima cobre o degrau do polígono, que erra sempre para MAIS.
        self.assertLess(fp.area(), 0.35 * caixa)
        self.assertGreater(fp.area(), 0.10 * caixa)
        ratio = coverage_ratio(layer, fp)
        self.assertLess(ratio, 0.35)
        self.assertGreater(ratio, 0.10)

    def test_contorno_nao_escapa_da_caixa(self):
        """Erro de geotransform na pirâmide jogaria o contorno para fora — e o
        recorte sairia num canto do mundo, sem ninguém perceber."""
        path = _write(os.path.join(_DIR, 'faixa2.tif'), 4, _diagonal)
        layer = QgsRasterLayer(path, 'faixa2')
        fp = data_footprint(layer)
        bb = fp.boundingBox()
        extent = layer.extent()
        self.assertGreaterEqual(bb.xMinimum(), extent.xMinimum() - 1e-6)
        self.assertLessEqual(bb.xMaximum(), extent.xMaximum() + 1e-6)
        self.assertGreaterEqual(bb.yMinimum(), extent.yMinimum() - 1e-6)
        self.assertLessEqual(bb.yMaximum(), extent.yMaximum() + 1e-6)

    def test_contorno_cobre_todo_pixel_valido(self):
        """Erguer o contorno não pode PERDER dado: todo pixel opaco tem que cair
        dentro dele, senão o tile some do arquivo e o usuário vê buraco."""
        path = _write(os.path.join(_DIR, 'faixa3.tif'), 4, _diagonal)
        layer = QgsRasterLayer(path, 'faixa3')
        fp = data_footprint(layer)
        gt = (500000.0, 1.0, 0.0, 9000200.0, 0.0, -1.0)
        from qgis.core import QgsGeometry, QgsPointXY
        fora = 0
        for y in range(0, 200, 7):
            for x in range(0, 200, 7):
                if not (abs(x - y) < 20):
                    continue
                p = QgsPointXY(gt[0] + (x + 0.5) * gt[1], gt[3] + (y + 0.5) * gt[5])
                if not fp.contains(QgsGeometry.fromPointXY(p)):
                    fora += 1
        self.assertEqual(fora, 0)


class CacheTest(unittest.TestCase):
    """O cache não pode entregar o MESMO objeto duas vezes.

    `to_wgs84` reprojeta a geometria no lugar. Se o cache devolvesse o objeto
    guardado, a segunda chamada receberia um contorno já em graus: área
    ridícula na tabela e recorte num canto do Atlântico — e sem erro nenhum,
    que é o pior tipo.
    """

    def test_mutar_o_retorno_nao_contamina_a_proxima_chamada(self):
        path = _write(os.path.join(_DIR, 'cache.tif'), 4, _diagonal)
        layer = QgsRasterLayer(path, 'cache')
        primeiro = data_footprint(layer)
        area = primeiro.area()
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
        ct = QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:32721'),
                                    QgsCoordinateReferenceSystem('EPSG:4326'),
                                    QgsProject.instance())
        primeiro.transform(ct)                       # exatamente o que to_wgs84 faz
        self.assertLess(primeiro.area(), area / 1000)   # agora está em graus
        segundo = data_footprint(layer)
        self.assertAlmostEqual(segundo.area(), area, delta=area * 1e-6)


if __name__ == '__main__':
    unittest.main()
