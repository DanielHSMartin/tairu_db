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
    from tairu_core.generator import TileSample, estimate, _merge_samples, _quality_factor, render_groups
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


class _Layer:
    def __init__(self, layer_id):
        self._id = layer_id

    def id(self):
        return self._id


class RenderGroupsTest(unittest.TestCase):
    """Região com camadas próprias renderiza à parte; as demais, juntas."""

    def setUp(self):
        if QgsRectangle is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_mesmas_camadas_desenham_o_tile_dividido_uma_vez(self):
        result = _region_result([10, 10])   # 15 tiles distintos
        self.assertEqual(estimate(result, 18, 'JPG', 90, 4).total_tiles, 15)
        fundo, imagem = _Layer('fundo'), _Layer('imagem')
        grupos = render_groups(result.region_tiles, {0: [imagem], 1: [imagem]}, [fundo, imagem])
        self.assertEqual([(sorted(r), len(t)) for _l, r, t in grupos], [([0, 1], 15)])

    def test_fundo_em_regiao_propria_renderiza_de_novo(self):
        result = _region_result([10, 10])
        fundo, imagem = _Layer('fundo'), _Layer('imagem')
        por_regiao = {0: [fundo], 1: [imagem]}
        grupos = render_groups(result.region_tiles, por_regiao, [imagem, fundo])
        self.assertEqual([([lyr.id() for lyr in lyrs], sorted(r), len(t)) for lyrs, r, t in grupos],
                         [(['fundo'], [0], 10), (['imagem'], [1], 10)])
        self.assertEqual(estimate(result, 18, 'JPG', 90, 4, region_layers=por_regiao).total_tiles, 20)


class MergeSamplesTest(unittest.TestCase):
    """Cada grupo é medido com as próprias camadas e as medidas viram uma só."""

    def setUp(self):
        if QgsRectangle is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_um_grupo_passa_igual(self):
        amostra = TileSample(fmt_kb=40, png_kb=90, sd_kb=4, secs=0.2, count=6)
        self.assertIs(_merge_samples([(10, amostra)]), amostra)

    def test_pesa_pelos_tiles_de_cada_grupo(self):
        fundo = TileSample(fmt_kb=20, png_kb=40, sd_kb=0, secs=0.5, count=3)
        imagem = TileSample(fmt_kb=60, png_kb=80, sd_kb=0, secs=0.1, count=3)
        junto = _merge_samples([(30, fundo), (10, imagem)])
        self.assertAlmostEqual(junto.fmt_kb, 30.0)
        self.assertAlmostEqual(junto.secs, 0.4)
        self.assertEqual(junto.count, 6)
        self.assertGreater(junto.sd_kb, 0)   # médias diferentes também são incerteza

    def test_grupo_vazio_nao_entra_na_media_mas_conta_como_vazio(self):
        imagem = TileSample(fmt_kb=60, png_kb=80, sd_kb=2, secs=0.1, count=3)
        junto = _merge_samples([(10, TileSample(blank=3)), (10, imagem)])
        self.assertAlmostEqual(junto.fmt_kb, 60.0)
        self.assertEqual(junto.blank, 3)


class GroupWarningTest(unittest.TestCase):
    """O aviso de amostra vazia por grupo é para o fundo da internet que não baixou.

    Uma imagem do disco cobre a própria região; as duas amostras de um grupo podem cair
    na moldura transparente dela, e o aviso mandava tirar uma imagem boa.
    """

    def setUp(self):
        if QgsRectangle is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_so_camada_da_internet_avisa(self):
        import tairu_core.generator as gen

        class _Fonte(_Layer):
            def __init__(self, layer_id, source):
                super().__init__(layer_id)
                self._source = source

            def name(self):
                return self.id()

            def source(self):
                return self._source

            def providerType(self):
                return 'wms' if 'url=' in self._source else 'gdal'

        # um .mbtiles local: não tem 'http', e é mapa de fundo do mesmo jeito
        fundo = _Fonte('fundo', 'type=xyz&url=file:///dados/fundo.mbtiles')
        faixa = _Fonte('faixa', '/dados/faixa.tif')
        imagem = _Fonte('imagem', '/dados/imagem.tif')

        def amostra(lyrs, _tiles, *_a, **_k):
            if lyrs[0] is imagem:
                return TileSample(fmt_kb=40, png_kb=60, sd_kb=2, secs=0.1, count=2)
            return TileSample(blank=2)   # o fundo não baixou; a faixa caiu na moldura

        originais = gen.sample_tile_sizes, gen.datum_context
        gen.sample_tile_sizes, gen.datum_context = amostra, lambda ctx, _layers, warn=None: ctx
        try:
            est = estimate(_region_result([10, 10, 10]), 18, 'JPG', 90, 4,
                           layers=[imagem, faixa, fundo],
                           region_layers={0: [fundo], 1: [faixa], 2: [imagem]})
        finally:
            gen.sample_tile_sizes, gen.datum_context = originais
        self.assertEqual(len(est.warnings), 1, est.warnings)
        self.assertIn('fundo', est.warnings[0])


class BlankSampleTest(unittest.TestCase):
    """Amostra renderizada que saiu SEM imagem nenhuma.

    `sample_tile_sizes` devolvia None tanto quando não havia o que medir quanto
    quando tudo o que mediu saiu vazio. Os dois caíam na tabela de KB por
    formato e anunciavam um tamanho plausível — para um arquivo que sairia sem
    mapa. O usuário só descobria depois de esperar a geração inteira.
    """

    def setUp(self):
        if QgsRectangle is None:
            raise unittest.SkipTest('QGIS Python bindings not available')

    def test_amostra_toda_vazia_vira_aviso(self):
        est = estimate(_region_result([10]), 18, 'JPG', 90, 4,
                       sample=TileSample(blank=6))
        self.assertEqual(est.blank_samples, 6)
        self.assertEqual(est.measured_from, 0)
        self.assertTrue(any('sem imagem nenhuma' in w for w in est.warnings), est.warnings)

    def test_sem_medicao_alguma_nao_inventa_aviso(self):
        est = estimate(_region_result([10]), 18, 'JPG', 90, 4)
        self.assertEqual(est.blank_samples, 0)
        self.assertEqual(est.warnings, [])

    def test_medida_valida_nao_vira_aviso(self):
        est = estimate(_region_result([10]), 18, 'JPG', 90, 4,
                       sample=TileSample(fmt_kb=40, png_kb=90, sd_kb=4, secs=0.2, count=6))
        self.assertEqual(est.measured_from, 6)
        self.assertEqual(est.warnings, [])


if __name__ == '__main__':
    unittest.main()
