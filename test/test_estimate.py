# -*- coding: utf-8 -*-

"""Estimativa de tamanho do .tairudb.

O RJ4 anunciou 246 MB e saiu com 445 MB. Dois erros multiplicando-se:

1. contava tiles DISTINTOS, mas o arquivo grava uma linha por região — duas
   regiões sobrepostas gravaram 10.191 linhas para 7.918 tiles;
2. corrigia o peso do JPEG em linha reta pela qualidade (`q/90`), e de 90 para
   100 o JPEG quase dobra.

Agora a estimativa mede alguns tiles de verdade; estes testes travam a conta que
transforma essa medida no total, com os números medidos no próprio RJ4.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import QgsRectangle
    from tairu_core.generator import TileSample, estimate, _quality_factor
    from tairu_core.tile_math import RegionTilesResult
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsRectangle = None


def _region_result(por_regiao, borda_por_regiao=0):
    """Duas regiões com `por_regiao` tiles cada, sobrepostas pela metade."""
    result = RegionTilesResult()
    for r, count in enumerate(por_regiao):
        deslocamento = r * count // 2  # metade dos tiles é a mesma das outras
        tiles = [(deslocamento + i, 0) for i in range(count)]
        result.region_tiles[r] = tiles
        result.region_edge_tiles[r] = set(tiles[:borda_por_regiao])
    unicos = set()
    for tiles in result.region_tiles.values():
        unicos.update(tiles)
    result.filtered_tiles = list(unicos)
    result.wgs84_extent = QgsRectangle(-46.5, -23.5, -46.4, -23.4)
    return result


class QualityFactorTest(unittest.TestCase):
    def setUp(self):
        if QgsRectangle is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_referencia_e_a_qualidade_90(self):
        self.assertAlmostEqual(_quality_factor(90), 1.0)

    def test_qualidade_100_quase_dobra(self):
        # A conta antiga dava 100/90 = 1,11 — metade do erro do RJ4.
        self.assertGreater(_quality_factor(100), 2.0)

    def test_curva_e_monotonica(self):
        valores = [_quality_factor(q) for q in range(10, 101, 5)]
        self.assertEqual(valores, sorted(valores))


class EstimateTest(unittest.TestCase):
    def setUp(self):
        if QgsRectangle is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_conta_as_linhas_gravadas_e_nao_os_tiles_distintos(self):
        result = _region_result([4881, 5310])
        est = estimate(result, 19, 'JPG', 100, 4,
                       sample=TileSample(fmt_kb=44, png_kb=36, sd_kb=12,
                                         secs=0.2, count=6))
        self.assertEqual(est.stored_tiles, 4881 + 5310)
        self.assertLess(est.total_tiles, est.stored_tiles)
        # O tamanho segue as linhas, o tempo segue os tiles renderizados.
        self.assertAlmostEqual(
            est.avg_mb, est.stored_tiles * est.avg_kb / 1024 * 1.03, places=3)
        self.assertAlmostEqual(est.secs, est.total_tiles * 0.2 / 4, places=3)

    def test_numeros_do_rj4_batem_com_o_arquivo_gerado(self):
        # Medidos no RJ4.tairudb: 10.191 linhas, 570 em PNG (36 KB), o resto em
        # JPEG q100 (44 KB), arquivo final de 444,9 MB.
        result = _region_result([4881, 5310], borda_por_regiao=285)
        est = estimate(result, 19, 'JPG', 100, 4,
                       sample=TileSample(fmt_kb=44, png_kb=36, sd_kb=12,
                                         secs=0.2, count=6))
        self.assertEqual(est.edge_tiles, 570)
        self.assertLess(abs(est.avg_mb - 444.9) / 444.9, 0.05,
                        f'estimativa {est.avg_mb:.0f} MB longe dos 445 MB reais')

    def test_faixa_medida_e_estreita_e_contem_a_estimativa(self):
        result = _region_result([4881, 5310], borda_por_regiao=285)
        est = estimate(result, 19, 'JPG', 100, 4,
                       sample=TileSample(fmt_kb=44, png_kb=36, sd_kb=12,
                                         secs=0.2, count=6))
        self.assertLess(est.lo_mb, est.avg_mb)
        self.assertGreater(est.hi_mb, est.avg_mb)
        # Erro padrão de seis amostras, não o maior tile medido: nada de
        # "70 MB – 616 MB" para um arquivo de 445 MB.
        self.assertGreater(est.lo_mb, est.avg_mb * 0.6)
        self.assertLess(est.hi_mb, est.avg_mb * 1.6)

    def test_sem_medicao_ainda_devolve_ordem_de_grandeza(self):
        result = _region_result([100, 100])
        est = estimate(result, 17, 'JPG', 100, 4)
        self.assertEqual(est.measured_from, 0)
        self.assertGreater(est.avg_kb, 28)  # qualidade 100 pesa mais que a base
        self.assertGreater(est.avg_mb, 0)


if __name__ == '__main__':
    unittest.main()
