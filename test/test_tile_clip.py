# -*- coding: utf-8 -*-

"""Recorte do tile de borda pelo polígono da região.

O plugin gravava tiles INTEIROS: qualquer tile que encostasse no polígono
entrava no arquivo cheio, com até um tile de imagem além da área pedida (1,2 km
num arquivo de zoom 15). Quem recortava era o app, e só o nativo, na importação
— na web a sobra aparecia sempre. Agora o recorte é feito na geração, e é este
teste que garante que ele tira SÓ o que está fora: o lado errado do polígono, ou
um deslocamento na conversão graus→pixel, apagaria imagem boa sem aviso nenhum.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import math
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import (
        QgsCoordinateTransformContext, QgsGeometry, QgsPointXY, QgsRectangle)
    from qgis.PyQt.QtGui import QImage, QColor
    from tairu_core.generator import (
        GenerationSpec, TileRenderEngine, has_transparency, mask_tile_to_rings,
        _tile_pixel)
    from tairu_core.tile_math import compute_region_tiles, polygon_rings
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QImage = None


_APP = None


def setUpModule():
    """QgsApplication real: sem ela o QgsCoordinateTransform do motor reclama
    de proj.db em toda construcao e enche a saida do teste de ruido."""
    global _APP
    if QImage is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    from qgis.core import QgsApplication
    _APP = QgsApplication([], False)
    _APP.initQgis()


def tearDownModule():
    if _APP is not None:
        _APP.exitQgis()


def _tile_bounds(tx, ty, n):
    """(oeste, leste, norte, sul) em graus do tile XYZ."""
    west = tx * 360.0 / n - 180.0
    east = (tx + 1) * 360.0 / n - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
    return west, east, north, south


def _opaque_tile(size=256):
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(200, 30, 30, 255))
    return img


def _alpha(img, x, y):
    return (img.pixel(x, y) >> 24) & 0xFF


class TileClipTest(unittest.TestCase):

    # Tile real de um arquivo gerado em zoom 18 (Humaitá).
    TX, TY, ZOOM = 85148, 125559, 18
    N = 2 ** 18

    def setUp(self):
        if QImage is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_metade_oeste_sobrevive_metade_leste_e_apagada(self):
        west, east, north, south = _tile_bounds(self.TX, self.TY, self.N)
        meio = (west + east) / 2.0
        ring = [(west, north), (meio, north), (meio, south), (west, south)]
        out = mask_tile_to_rings(_opaque_tile(), self.TX, self.TY, self.N, [ring])

        self.assertEqual(_alpha(out, 10, 128), 255, 'dentro do polígono foi apagado')
        self.assertEqual(_alpha(out, 120, 128), 255, 'dentro do polígono foi apagado')
        self.assertEqual(_alpha(out, 200, 128), 0, 'fora do polígono sobreviveu')
        self.assertEqual(_alpha(out, 255, 0), 0, 'fora do polígono sobreviveu')

    def test_tile_todo_dentro_fica_intacto(self):
        west, east, north, south = _tile_bounds(self.TX, self.TY, self.N)
        folga = (east - west)
        ring = [(west - folga, north + folga), (east + folga, north + folga),
                (east + folga, south - folga), (west - folga, south - folga)]
        out = mask_tile_to_rings(_opaque_tile(), self.TX, self.TY, self.N, [ring])
        for x, y in [(0, 0), (128, 128), (255, 255), (255, 0), (0, 255)]:
            self.assertEqual(_alpha(out, x, y), 255, f'pixel {x},{y} apagado por engano')

    def test_buraco_do_poligono_fica_transparente(self):
        west, east, north, south = _tile_bounds(self.TX, self.TY, self.N)
        dx, dy = (east - west) / 4.0, (north - south) / 4.0
        externo = [(west, north), (east, north), (east, south), (west, south)]
        buraco = [(west + dx, north - dy), (east - dx, north - dy),
                  (east - dx, south + dy), (west + dx, south + dy)]
        out = mask_tile_to_rings(_opaque_tile(), self.TX, self.TY, self.N,
                                 [externo, buraco])
        self.assertEqual(_alpha(out, 128, 128), 0, 'o buraco ficou preenchido')
        self.assertEqual(_alpha(out, 5, 5), 255, 'a borda do anel externo sumiu')

    def test_pixel_do_canto_do_tile(self):
        west, east, north, south = _tile_bounds(self.TX, self.TY, self.N)
        px, py = _tile_pixel(west, north, self.TX, self.TY, self.N, 256, 256)
        self.assertAlmostEqual(px, 0.0, places=6)
        self.assertAlmostEqual(py, 0.0, places=6)
        px, py = _tile_pixel(east, south, self.TX, self.TY, self.N, 256, 256)
        self.assertAlmostEqual(px, 256.0, places=6)
        self.assertAlmostEqual(py, 256.0, places=6)


class _SilentFeedback:
    def is_canceled(self):
        return False

    def push_info(self, message):
        pass

    def set_progress_text(self, message):
        pass

    def set_progress(self, value):
        pass

    def report_error(self, message):
        raise AssertionError(message)


class RegionEdgeTilesTest(unittest.TestCase):
    """Quais tiles o gerador vai mascarar."""

    def setUp(self):
        if QImage is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_borda_e_subconjunto_e_o_miolo_fica_de_fora(self):
        # Retângulo de ~2 x 1,5 km em Humaitá, o mesmo canto dos arquivos de teste.
        west, east, south, north = -63.066894, -62.940716, -7.547701, -7.458387
        ring = [QgsPointXY(west, south), QgsPointXY(east, south),
                QgsPointXY(east, north), QgsPointXY(west, north),
                QgsPointXY(west, south)]
        geom = QgsGeometry.fromPolygonXY([ring])

        result = compute_region_tiles([geom], 15, _SilentFeedback())
        tiles = set(result.region_tiles[0])
        edge = result.region_edge_tiles[0]

        self.assertTrue(tiles, 'nenhum tile intersecta a área')
        self.assertTrue(edge.issubset(tiles), 'tile de borda fora do conjunto salvo')
        self.assertTrue(tiles - edge, 'nenhum tile de miolo — tudo seria mascarado')
        # O retângulo tem 12 x 10 tiles no zoom 15: a borda é o perímetro.
        self.assertEqual(len(edge), len(tiles) - (12 - 2) * (10 - 2))
        # E os anéis chegam inteiros para a máscara.
        self.assertEqual(len(result.region_rings[0]), 1)
        self.assertEqual(len(result.region_rings[0][0]), len(ring))

    def test_multipoligono_mantem_um_anel_por_parte(self):
        def quad(x0, y0, x1, y1):
            return [QgsPointXY(x0, y0), QgsPointXY(x1, y0),
                    QgsPointXY(x1, y1), QgsPointXY(x0, y1), QgsPointXY(x0, y0)]
        geom = QgsGeometry.fromMultiPolygonXY([
            [quad(-63.06, -7.54, -63.05, -7.53)],
            [quad(-62.96, -7.47, -62.95, -7.46)],
        ])
        rings = polygon_rings(geom)
        self.assertEqual(len(rings), 2, 'as partes foram concatenadas num anel só')


class TransparencyTest(unittest.TestCase):
    """Detecção de alfa: é ela que decide se o tile sai em PNG."""

    def setUp(self):
        if QImage is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_tile_opaco_nao_tem_transparencia(self):
        self.assertFalse(has_transparency(_opaque_tile()))

    def test_um_pixel_meio_transparente_ja_conta(self):
        img = _opaque_tile()
        img.setPixelColor(200, 100, QColor(10, 20, 30, 254))
        self.assertTrue(has_transparency(img))

    def test_tile_recortado_tem_transparencia(self):
        n = 2 ** 18
        west, east, north, south = _tile_bounds(85148, 125559, n)
        meio = (west + east) / 2.0
        recortado = mask_tile_to_rings(
            _opaque_tile(), 85148, 125559, n,
            [[(west, north), (meio, north), (meio, south), (west, south)]])
        self.assertTrue(has_transparency(recortado))


class _FakeWriter:
    """Guarda o que o gerador mandaria para o banco."""

    def __init__(self):
        self.saved = {}

    def saveTile(self, zoom, column, row, data, region_id=0):
        self.saved[region_id] = bytes(data)
        return True

    def periodicCommit(self):
        pass


class _MetaTile:
    def __init__(self, zoom):
        self.zoom = zoom


class ConvertAndSaveTileTest(unittest.TestCase):
    """O que cada região recebe: miolo inteiro, borda recortada."""

    TX, TY, ZOOM = 85148, 125559, 18

    def setUp(self):
        if QImage is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_borda_vira_png_com_alfa_e_miolo_segue_no_formato_escolhido(self):
        n = 2 ** self.ZOOM
        west, east, north, south = _tile_bounds(self.TX, self.TY, n)
        meio = (west + east) / 2.0
        ring = [(west, north), (meio, north), (meio, south), (west, south)]
        tile = (self.TX, self.TY)

        spec = GenerationSpec(
            output_file='',
            layers=[],
            region_tiles={0: [tile], 1: [tile]},
            filtered_tiles=[tile],
            bounds_list=[],
            wgs84_extent=QgsRectangle(),
            max_zoom=self.ZOOM,
            tile_format='JPG',
            transform_context=QgsCoordinateTransformContext(),
            region_edge_tiles={0: set(), 1: {tile}},
            region_rings={0: [ring], 1: [ring]},
        )
        engine = TileRenderEngine(spec, _SilentFeedback())
        engine.writer = _FakeWriter()

        self.assertTrue(engine.convert_and_save_tile(
            _opaque_tile(), _MetaTile(self.ZOOM), self.TX, self.TY, n))

        miolo = engine.writer.saved[0]
        borda = engine.writer.saved[1]
        self.assertTrue(miolo.startswith(b'\xff\xd8'), 'o miolo devia sair no formato escolhido (JPG)')
        self.assertTrue(borda.startswith(b'\x89PNG'), 'a borda precisa de PNG: JPG não guarda alfa')

        recortado = QImage.fromData(borda)
        self.assertEqual(_alpha(recortado, 10, 128), 255, 'apagou imagem de dentro da região')
        self.assertEqual(_alpha(recortado, 200, 128), 0, 'não apagou a sobra fora da região')

    def test_area_sem_imagem_de_origem_tambem_sai_em_png(self):
        # Fundo transparente: onde a origem não cobre, o tile tem alfa mesmo
        # longe da borda da região — e em JPEG isso viraria preto.
        n = 2 ** self.ZOOM
        tile = (self.TX, self.TY)
        spec = GenerationSpec(
            output_file='',
            layers=[],
            region_tiles={0: [tile]},
            filtered_tiles=[tile],
            bounds_list=[],
            wgs84_extent=QgsRectangle(),
            max_zoom=self.ZOOM,
            tile_format='JPG',
            transform_context=QgsCoordinateTransformContext(),
            region_edge_tiles={0: set()},
            region_rings={},
        )
        engine = TileRenderEngine(spec, _SilentFeedback())
        engine.writer = _FakeWriter()

        parcial = _opaque_tile()
        for y in range(256):
            for x in range(200, 256):
                parcial.setPixelColor(x, y, QColor(0, 0, 0, 0))

        self.assertTrue(engine.convert_and_save_tile(
            parcial, _MetaTile(self.ZOOM), self.TX, self.TY, n))
        self.assertTrue(engine.writer.saved[0].startswith(b'\x89PNG'))


class BlankTileTest(unittest.TestCase):
    """O tile descartado por estar vazio — e o que NÃO pode ser confundido com ele.

    A detecção era uma amostra de cinco pixels decidida por igualdade entre
    eles: tile de cor uniforme sem canal alfa caía no `return True` do fim e ia
    inteiro para o lixo, e uma estrada fina que não passasse pelos cinco pontos
    levava o tile junto. Desde que o fundo virou transparente (2.0.20) o
    descarte também deixou de ser raro — tudo o que a origem não cobre é vazio —
    e ele era contado junto dos renderizados, então um arquivo oco terminava
    anunciando sucesso completo.
    """

    ZOOM = 18

    def setUp(self):
        if QImage is None:
            raise unittest.SkipTest('QGIS Python bindings not available')
        self.engine = TileRenderEngine(
            GenerationSpec(
                output_file='', layers=[], region_tiles={}, filtered_tiles=[],
                bounds_list=[], wgs84_extent=QgsRectangle(), max_zoom=self.ZOOM,
                transform_context=QgsCoordinateTransformContext()),
            _SilentFeedback())

    def test_tile_todo_transparente_e_vazio(self):
        img = QImage(256, 256, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        self.assertTrue(self.engine.is_tile_empty(img))

    def test_tile_de_cor_uniforme_sem_alfa_nao_e_vazio(self):
        img = QImage(256, 256, QImage.Format.Format_RGB32)
        img.fill(QColor(120, 140, 90))
        self.assertFalse(self.engine.is_tile_empty(img))

    def test_um_pixel_fora_da_amostragem_ja_salva_o_tile(self):
        img = QImage(256, 256, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        img.setPixelColor(37, 211, QColor(255, 255, 255, 255))  # nenhum dos 5 pontos
        self.assertFalse(self.engine.is_tile_empty(img))

    def test_vazios_sao_contados_e_ditos_no_relatorio(self):
        registro = []

        class _Feedback(_SilentFeedback):
            def push_info(self, text):
                registro.append(text)

            def report_error(self, text, fatal=False):
                registro.append(text)

        self.engine.feedback = _Feedback()
        self.engine.spec.filtered_tiles = [(1, 1), (1, 2), (1, 3), (1, 4)]
        self.engine.processed_tiles = 4
        self.engine.skipped_blank_tiles = 3
        self.engine._report_summary()
        self.assertTrue(any('3 de 4' in t for t in registro), registro)

    def test_arquivo_inteiro_vazio_nao_anuncia_sucesso(self):
        registro = []

        class _Feedback(_SilentFeedback):
            def push_info(self, text):
                registro.append(text)

            def report_error(self, text, fatal=False):
                registro.append('ERRO: ' + text)

        self.engine.feedback = _Feedback()
        self.engine.spec.filtered_tiles = [(1, 1), (1, 2)]
        self.engine.processed_tiles = 2
        self.engine.skipped_blank_tiles = 2
        self.engine._report_summary()
        self.assertTrue(any(t.startswith('ERRO:') and 'sem mapa' in t for t in registro), registro)


if __name__ == '__main__':
    unittest.main()
