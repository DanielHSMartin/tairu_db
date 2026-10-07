# -*- coding: utf-8 -*-

"""Quais tiles Copernicus GLO-30 as curvas de nível baixam.

A faixa ia de floor a ceil INCLUSIVE: uma área dentro de um só tile (a ANP) baixava
4 tiles, ~165 MB, onde 1 (~41 MB) bastava — e numa sala de aula cada computador baixa
isso na primeira geração. O nome do tile é o canto SUDOESTE, e o tile vem meio pixel
deslocado do grau (medido nos GeoTIFF: S16_W048 vai de -48,0001 a -47,0001 e de
-15,9999 a -14,9999). A seleção tem de cobrir a área que o recorte usa — a bbox
encaixada na grade, com esse meio pixel — e não trazer tile que não chega perto dela.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import math
import os
import random
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import QgsRectangle
    from tairu_core.contour_generator import (
        _COPERNICUS_MARGIN_LAT, _COPERNICUS_MARGIN_LON, _copernicus_tiles, _grid_bounds)
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsRectangle = None

RES = 1.0 / 3600.0  # abaixo de 50° de latitude o Copernicus tem 1" nos dois eixos
H = RES / 2


def _cobertura(lat, lon):
    """(xmin, ymin, xmax, ymax) do tile (lat, lon), como nos GeoTIFF."""
    return (lon - H, lat + H, lon + 1 - H, lat + 1 + H)


def _geotransform(lat, lon):
    xmin, _, _, ymax = _cobertura(lat, lon)
    return (xmin, RES, 0.0, ymax, 0.0, -RES)


def _intersecta(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class CopernicusTilesTest(unittest.TestCase):

    def setUp(self):
        if QgsRectangle is None:
            self.skipTest('QGIS Python bindings not available')

    def test_anp_um_tile_so(self):
        # O retângulo desenhado sobre a ANP na aula de QGIS (06/10/2026).
        anp = QgsRectangle(-47.84782, -15.69820, -47.83782, -15.69321)
        self.assertEqual(_copernicus_tiles(anp), [(-16, -48)])

    def test_area_que_cruza_o_grau_pega_os_quatro(self):
        area = QgsRectangle(-47.1, -15.1, -46.9, -14.9)
        self.assertEqual(sorted(_copernicus_tiles(area)), [(-16, -48), (-16, -47), (-15, -48), (-15, -47)])

    def test_ponto_no_grau_nunca_fica_sem_tile(self):
        self.assertTrue(_copernicus_tiles(QgsRectangle(-47.0, -15.0, -47.0, -15.0)))

    def test_perto_de_50s_nao_pega_o_tile_da_faixa_vizinha(self):
        # Borda sul 5" ao norte de 50°S: só o S50. Com folga de 10" em latitude vinha
        # também o S51, de pixel de longitude 1,5", e a grade dele virava a do recorte.
        area = QgsRectangle(-69.6, -49.99861, -69.5, -49.95)
        self.assertEqual(_copernicus_tiles(area), [(-50, -70)])

    def test_o_tile_do_centro_vem_primeiro(self):
        # Quem vem primeiro dá a grade do recorte (_clip_tiles): a do miolo da área.
        area = QgsRectangle(-69.6, -50.1, -69.5, -49.5)
        self.assertEqual(_copernicus_tiles(area), [(-50, -70), (-51, -70)])

    def test_cobre_a_area_recortada_e_nada_longe_dela(self):
        sorteio = random.Random(20261007)
        perto_do_grau = (0.0, H / 2, H, 2 * H, -H / 2, -H, 0.3, 0.7)
        for _ in range(500):
            # metade das áreas encosta numa linha de grau, onde o meio pixel decide
            # longe do antimeridiano e abaixo de 50°, onde o pixel de longitude é 1"
            lon = sorteio.randint(-179, 176) + sorteio.choice(perto_do_grau + (sorteio.random(),))
            lat = sorteio.randint(-49, 47) + sorteio.choice(perto_do_grau + (sorteio.random(),))
            larg, alt = sorteio.choice((0.0, 1e-4, 0.01, 0.5, 1.7)), sorteio.choice((0.0, 1e-4, 0.01, 0.5, 1.3))
            bbox = QgsRectangle(lon, lat, lon + larg, lat + alt)
            tiles = _copernicus_tiles(bbox)
            # o recorte encaixa a bbox na grade do primeiro tile (_clip_tiles)
            recorte = _grid_bounds(_geotransform(*tiles[0]), bbox)
            necessarios = {
                (la, lo)
                for la in range(math.floor(recorte[1]) - 1, math.ceil(recorte[3]) + 1)
                for lo in range(math.floor(recorte[0]) - 1, math.ceil(recorte[2]) + 1)
                if _intersecta(_cobertura(la, lo), recorte)
            }
            self.assertLessEqual(necessarios, set(tiles), (bbox.toString(6), tiles))
            m_lat, m_lon = _COPERNICUS_MARGIN_LAT, _COPERNICUS_MARGIN_LON
            perto = (bbox.xMinimum() - m_lon, bbox.yMinimum() - m_lat,
                     bbox.xMaximum() + m_lon, bbox.yMaximum() + m_lat)
            longe = [t for t in tiles if not _intersecta(_cobertura(*t), perto)]
            self.assertFalse(longe, (bbox.toString(6), longe))


if __name__ == '__main__':
    unittest.main()
