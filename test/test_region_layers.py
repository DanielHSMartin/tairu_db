# -*- coding: utf-8 -*-

"""Cada região desenha as próprias camadas.

Com uma imagem como área de interesse e o mapa de fundo marcado, o arquivo
saía com UMA região: o fundo era desenhado por baixo da imagem no mesmo tile, e
um TIFF opaco o escondia inteiro — baixado e nunca visto. Agora o fundo vira
região própria. Este teste gera um arquivo de verdade com duas regiões no MESMO
tile e confere que cada uma recebeu só a sua camada, com o nome dela.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import math
import os
import sqlite3
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from osgeo import gdal, osr
    from qgis.core import QgsApplication, QgsCoordinateTransformContext, QgsRasterLayer, QgsRectangle
    from qgis.PyQt.QtGui import QImage
    from tairu_core.feedback import FeedbackAdapter
    from tairu_core.generator import GenerationSpec, TileRenderEngine
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None
ZOOM = 16
LON, LAT = -47.9, -15.78


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], False)
    _APP.initQgis()


def _tile():
    n = 2 ** ZOOM
    tx = int((LON + 180.0) / 360.0 * n)
    ty = int((1.0 - math.asinh(math.tan(math.radians(LAT))) / math.pi) / 2.0 * n)
    return tx, ty


def _solid_tif(folder, name, rgb, lon=LON):
    """GeoTIFF opaco de uma cor só, cobrindo o tile com folga."""
    path = os.path.join(folder, f'{name}.tif')
    ds = gdal.GetDriverByName('GTiff').Create(path, 64, 64, 3, gdal.GDT_Byte)
    ds.SetGeoTransform((lon - 0.02, 0.04 / 64, 0, LAT + 0.02, 0, -0.04 / 64))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for band, value in enumerate(rgb, start=1):
        ds.GetRasterBand(band).Fill(value)
    ds = None
    layer = QgsRasterLayer(path, name, 'gdal')
    assert layer.isValid(), path
    return layer


class RegionLayersTest(unittest.TestCase):

    def test_fundo_e_imagem_no_mesmo_tile_saem_em_regioes_separadas(self):
        folder = tempfile.mkdtemp(prefix='regioes-')
        fundo = _solid_tif(folder, 'fundo', (0, 0, 255))
        imagem = _solid_tif(folder, 'imagem', (255, 0, 0))
        tile = _tile()
        ring = f'{LON - 0.01} {LAT - 0.01}, {LON + 0.01} {LAT - 0.01}, {LON + 0.01} {LAT + 0.01}'
        output = os.path.join(folder, 'mapa.tairudb')
        spec = GenerationSpec(
            output_file=output,
            # Composto, a imagem (por cima) tapava o fundo: os dois tiles sairiam vermelhos.
            layers=[imagem, fundo],
            region_tiles={0: [tile], 1: [tile]},
            filtered_tiles=[tile],
            bounds_list=[ring, ring],
            wgs84_extent=QgsRectangle(LON - 0.01, LAT - 0.01, LON + 0.01, LAT + 0.01),
            max_zoom=ZOOM,
            transform_context=QgsCoordinateTransformContext(),
            threads_number=1,
            region_layers={0: [fundo], 1: [imagem]},
            region_names={0: 'fundo', 1: 'imagem'},
        )
        engine = TileRenderEngine(spec, FeedbackAdapter())
        self.assertTrue(engine.run(), engine.error_message)
        self.assertEqual(engine.total_tiles, 2)   # o tile dividido renderiza uma vez por região
        self.assertTrue(engine.finalize())

        conn = sqlite3.connect(output)
        try:
            names = [row[0] for row in conn.execute('SELECT name FROM regions ORDER BY id')]
            cores = {}
            for region in (0, 1):
                (data,) = conn.execute(f'SELECT tile_data FROM tiles_region_{region}').fetchone()  # nosec B608
                pixel = QImage.fromData(bytes(data)).pixelColor(128, 128)
                cores[region] = (pixel.red(), pixel.blue())
        finally:
            conn.close()
        self.assertEqual(names, ['fundo', 'imagem'])
        self.assertGreater(cores[0][1], 200, f'região do fundo não saiu azul: {cores}')
        self.assertLess(cores[0][0], 60, f'a imagem vazou para a região do fundo: {cores}')
        self.assertGreater(cores[1][0], 200, f'região da imagem não saiu vermelha: {cores}')
        self.assertLess(cores[1][1], 60, f'o fundo vazou para a região da imagem: {cores}')


class _Feedback(FeedbackAdapter):
    def __init__(self):
        self.errors = []

    def report_error(self, text, fatal=False):
        self.errors.append(text)


class EmptyRegionTest(unittest.TestCase):

    def test_regiao_sem_nenhum_tile_sai_do_arquivo_e_as_seguintes_descem(self):
        # O fundo da internet que não baixou (sem rede, proxy) virava uma região sem
        # tabela: o app a listava vazia e refazia a pirâmide a cada abertura.
        folder = tempfile.mkdtemp(prefix='regiao-vazia-')
        longe = _solid_tif(folder, 'longe', (0, 0, 255), lon=LON + 5)   # não cobre o tile
        imagem = _solid_tif(folder, 'imagem', (255, 0, 0))
        tile = _tile()
        ring = f'{LON - 0.01} {LAT - 0.01}, {LON + 0.01} {LAT - 0.01}, {LON + 0.01} {LAT + 0.01}'
        output = os.path.join(folder, 'mapa.tairudb')
        feedback = _Feedback()
        engine = TileRenderEngine(GenerationSpec(
            output_file=output, layers=[imagem, longe],
            region_tiles={0: [tile], 1: [tile], 2: [tile]}, filtered_tiles=[tile],
            bounds_list=[ring, ring, ring],
            wgs84_extent=QgsRectangle(LON - 0.01, LAT - 0.01, LON + 0.01, LAT + 0.01),
            max_zoom=ZOOM, transform_context=QgsCoordinateTransformContext(), threads_number=1,
            # duas partes do mesmo fundo: o aviso diz o nome uma vez
            region_layers={0: [longe], 1: [longe], 2: [imagem]},
            region_names={0: 'Google Satellite', 1: 'Google Satellite', 2: 'imagem'},
        ), feedback)
        self.assertTrue(engine.run(), engine.error_message)
        self.assertTrue(engine.finalize())
        conn = sqlite3.connect(output)
        try:
            names = [row[0] for row in conn.execute('SELECT name FROM regions ORDER BY id')]
            tables = sorted(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tiles_region_%'"))
            (data,) = conn.execute('SELECT tile_data FROM tiles_region_0').fetchone()
            indexes = sorted(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tiles_region_0'"))
        finally:
            conn.close()
        self.assertEqual(names, ['imagem'])
        # a região que sobrou é a 0 agora: o app casa região e tabela pela posição
        self.assertEqual(tables, ['tiles_region_0'])
        self.assertGreater(QImage.fromData(bytes(data)).pixelColor(128, 128).red(), 200)
        self.assertEqual(indexes, ['tiles_region_0_index'])
        self.assertTrue(any('Google Satellite' in e for e in feedback.errors), feedback.errors)
        self.assertEqual(sum(e.count('Google Satellite') for e in feedback.errors), 1, feedback.errors)

    def test_regiao_que_o_recorte_deixa_vazia_tambem_sai(self):
        # A imagem cobre só a metade oeste do tile e a região 0 só a leste: recortado, o
        # tile dela fica transparente — gravado, mantinha viva uma região sem imagem.
        folder = tempfile.mkdtemp(prefix='recorte-vazio-')
        tx, ty = _tile()
        n = 2 ** ZOOM
        oeste, leste = tx / n * 360 - 180, (tx + 1) / n * 360 - 180
        meio = (oeste + leste) / 2
        path = os.path.join(folder, 'metade.tif')
        ds = gdal.GetDriverByName('GTiff').Create(path, 64, 64, 3, gdal.GDT_Byte)
        ds.SetGeoTransform((oeste - 0.01, (meio - oeste + 0.01) / 64, 0, LAT + 0.02, 0, -0.04 / 64))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())
        for band in (1, 2, 3):
            ds.GetRasterBand(band).Fill(200)
        ds = None
        metade = QgsRasterLayer(path, 'metade', 'gdal')
        sul = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
        norte = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        quarto = meio + (leste - meio) / 2
        anel_leste = [(quarto, sul), (leste, sul), (leste, norte), (quarto, norte)]
        ring = f'{oeste} {sul}, {leste} {sul}, {leste} {norte}, {oeste} {norte}'
        output = os.path.join(folder, 'mapa.tairudb')
        feedback = _Feedback()
        engine = TileRenderEngine(GenerationSpec(
            output_file=output, layers=[metade],
            region_tiles={0: [(tx, ty)], 1: [(tx, ty)]}, filtered_tiles=[(tx, ty)],
            bounds_list=[ring, ring], wgs84_extent=QgsRectangle(oeste, sul, leste, norte),
            max_zoom=ZOOM, transform_context=QgsCoordinateTransformContext(), threads_number=1,
            region_edge_tiles={0: {(tx, ty)}, 1: set()}, region_rings={0: [anel_leste], 1: []},
            region_names={0: 'faixa leste', 1: 'tudo'},
        ), feedback)
        self.assertTrue(engine.run(), engine.error_message)
        self.assertEqual(engine.failed_tiles, 0)   # vazio não é falha
        self.assertTrue(engine.finalize())
        conn = sqlite3.connect(output)
        try:
            names = [row[0] for row in conn.execute('SELECT name FROM regions ORDER BY id')]
        finally:
            conn.close()
        self.assertEqual(names, ['tudo'])
        self.assertTrue(any('faixa leste' in e for e in feedback.errors), feedback.errors)


if __name__ == '__main__':
    unittest.main()
