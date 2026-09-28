# -*- coding: utf-8 -*-

"""A transformação de datum do app, imposta em todo lugar que o plugin reprojeta.

Sem entrada no contexto, o QGIS escolhe a operação sozinho: SAD69 / UTM 23S sai
pela EPSG:5882 (10 m longe da grade do IBGE em Manaus) e PSAD56 em Santiago pela
EPSG:1201 (57 m longe da EPSG:6972). O app converte pela operação da região
(datum_ops.py), então o dado exportado, enviado ou renderizado pelo plugin caía
longe de onde o app o põe. Referência: o PROJ rodando a operação EPSG exata.

Precisa do Python do QGIS (qgis.core, pyproj, GDAL); pulado em outros interpretadores.
"""

import json
import math
import os
import subprocess  # nosec B404 - roda o próprio interpretador do teste
import sys
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from qgis.core import (
        QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
        QgsCoordinateTransformContext, QgsDatumTransform, QgsFeature, QgsGeometry,
        QgsPointXY, QgsProjUtils, QgsRasterLayer, QgsRectangle, QgsVectorLayer)
    from pyproj import Transformer
    from osgeo import gdal, osr
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None
_MAPPING = {'nome_field': None, 'descricao_field': None, 'tipo': 'local',
            'sub_tipo': 'outroLocal', 'situation': 'Ativo'}
MANAUS = (-60.02, -3.1)        # SAD69: dentro da grade do IBGE
SANTIAGO = (-70.65, -33.45)    # PSAD56: Chile centro


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], False)
    _APP.initQgis()


def _ref(code, lon, lat):
    """(lon, lat) em WGS84 pela operação EPSG `code`, no PROJ."""
    la, lo = Transformer.from_pipeline(
        f'urn:ogc:def:coordinateOperation:EPSG::{code}').transform(lat, lon)
    return lo, la


def _metros(a, b):
    dx = (a[0] - b[0]) * 111320.0 * math.cos(math.radians(a[1]))
    return math.hypot(dx, (a[1] - b[1]) * 110574.0)


def _no_src(epsg, geog, lon, lat):
    """O ponto (lon, lat) do datum `geog` escrito no SRC `epsg` (mesmo datum: sem deslocamento)."""
    return QgsCoordinateTransform(
        QgsCoordinateReferenceSystem(f'EPSG:{geog}'), QgsCoordinateReferenceSystem(f'EPSG:{epsg}'),
        QgsCoordinateTransformContext()).transform(QgsPointXY(lon, lat))


def _camada(epsg, geog, lon, lat):
    camada = QgsVectorLayer(f'Point?crs=EPSG:{epsg}&field=nome:string', 'pontos', 'memory')
    feicao = QgsFeature(camada.fields())
    feicao.setGeometry(QgsGeometry.fromPointXY(_no_src(epsg, geog, lon, lat)))
    camada.dataProvider().addFeatures([feicao])
    camada.updateExtents()
    return camada


def _padrao_qgis(epsg, geog, lon, lat):
    """Para onde o QGIS levaria o ponto sozinho, sem entrada no contexto."""
    p = QgsCoordinateTransform(QgsCoordinateReferenceSystem(f'EPSG:{epsg}'),
                               QgsCoordinateReferenceSystem('EPSG:4326'),
                               QgsCoordinateTransformContext()).transform(_no_src(epsg, geog, lon, lat))
    return p.x(), p.y()


class _Retorno(object):
    def __init__(self):
        self.avisos = []

    def push_info(self, texto):
        self.avisos.append(texto)

    def report_error(self, texto, fatal=False):
        self.avisos.append(texto)

    def set_progress(self, _p):
        pass

    def set_progress_text(self, _t):
        pass

    def is_canceled(self):
        return False


class _Escritor(object):
    conn = None

    def __init__(self):
        self.pontos = []

    def insertVectorLayer(self, *_args):
        pass

    def insertFeature(self, *args):
        self.pontos.append(tuple(float(v) for v in args[6].split()))


class _Mapa(object):
    map_id = 'map-de-teste'
    nome = 'Expedição de teste'

    def role_for(self, _uid):
        return 'owner'


def _exportado(camada, contexto=None):
    from tairu_core.vector_export import export_vector_layers
    escritor = _Escritor()
    export_vector_layers(escritor, [camada], contexto, _Retorno())
    return escritor.pontos[0]


def _enviado(camada):
    from tairu_sync.push import build_push_plan
    plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1')
    ponto = json.loads(plano.items[0].record.geometry_points_json)[0]
    return ponto['lo'], ponto['la']


class TestOperacaoDoApp(unittest.TestCase):

    def test_sad69_utm23s_em_manaus_exporta_e_envia_pela_grade_do_ibge(self):
        esperado = _ref(5542, *MANAUS)
        # O teste distingue: sozinho, o QGIS poria o ponto a mais de 5 m dali.
        self.assertGreater(_metros(_padrao_qgis(29193, 4618, *MANAUS), esperado), 5.0)
        camada = _camada(29193, 4618, *MANAUS)
        self.assertLess(_metros(_exportado(camada), esperado), 0.05)
        self.assertLess(_metros(_enviado(camada), esperado), 0.05)

    def test_psad56_em_santiago_pela_6972_e_nao_pela_media_1201(self):
        esperado = _ref(6972, *SANTIAGO)
        self.assertGreater(_metros(_ref(1201, *SANTIAGO), esperado), 50.0)
        self.assertGreater(_metros(_padrao_qgis(24879, 4248, *SANTIAGO), esperado), 50.0)
        camada = _camada(24879, 4248, *SANTIAGO)
        self.assertLess(_metros(_exportado(camada), esperado), 0.05)
        self.assertLess(_metros(_enviado(camada), esperado), 0.05)
        # A área de interesse (to_wgs84) também.
        from tairu_core.tile_math import to_wgs84
        area = to_wgs84(QgsGeometry.fromPointXY(_no_src(24879, 4248, *SANTIAGO)),
                        QgsCoordinateReferenceSystem('EPSG:24879'),
                        QgsCoordinateReferenceSystem('EPSG:4326'),
                        QgsCoordinateTransformContext()).asPoint()
        self.assertLess(_metros((area.x(), area.y()), esperado), 0.05)

    def test_entrada_explicita_do_projeto_prevalece(self):
        from tairu_core.datum_context import datum_context
        src = QgsCoordinateReferenceSystem('EPSG:29193')
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        escolha = next(op.proj for op in QgsDatumTransform.operations(src, wgs84)
                       if any(d.code == '1864' for d in op.operationDetails))
        projeto = QgsCoordinateTransformContext()
        projeto.addCoordinateOperation(src, wgs84, escolha)
        camada = _camada(29193, 4618, *MANAUS)

        contexto = datum_context(projeto, [camada])
        self.assertEqual(contexto.calculateCoordinateOperation(src, wgs84), escolha)
        # O par que o usuário não fixou recebe a do app (a grade).
        self.assertIn('br_ibge_SAD69_003', contexto.calculateCoordinateOperation(
            src, QgsCoordinateReferenceSystem('EPSG:3857')))
        self.assertLess(_metros(_exportado(camada, projeto), _ref(1864, *MANAUS)), 0.05)


class TestTileRenderizado(unittest.TestCase):
    """O pixel de um GeoTIFF PSAD56 cai no tile onde a EPSG:6972 manda."""

    ZOOM = 18   # ~0,5 m por pixel em Santiago: os 57 m da 1201 viram ~114 px

    def test_marcador_do_geotiff_psad56(self):
        from tairu_core.generator import GenerationSpec, TileRenderEngine
        n = 2 ** self.ZOOM
        lon, lat = _ref(6972, *SANTIAGO)
        tx = int((lon + 180.0) / 360.0 * n)
        ty = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        # Marcador no CENTRO do tile, levado de volta ao PSAD56 pela própria 6972.
        c_lon = (tx + 0.5) / n * 360.0 - 180.0
        c_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 0.5) / n))))
        m_lat, m_lon = Transformer.from_pipeline(
            'urn:ogc:def:coordinateOperation:EPSG::6972').transform(c_lat, c_lon, direction='INVERSE')
        marco = _no_src(24879, 4248, m_lon, m_lat)

        caminho = os.path.join(tempfile.mkdtemp(prefix='datum-'), 'marcador.tif')
        ds = gdal.GetDriverByName('GTiff').Create(caminho, 101, 101, 4, gdal.GDT_Byte)
        ds.SetGeoTransform((marco.x() - 50.5, 1.0, 0.0, marco.y() + 50.5, 0.0, -1.0))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(24879)
        ds.SetProjection(srs.ExportToWkt())
        for banda in range(1, 5):   # branco opaco só no bloco 3x3 do meio
            ds.GetRasterBand(banda).WriteRaster(49, 49, 3, 3, b'\xff' * 9)
        ds = None
        camada = QgsRasterLayer(caminho, 'marcador', 'gdal')
        self.assertEqual(camada.crs().authid(), 'EPSG:24879')

        spec = GenerationSpec(
            output_file='', layers=[camada], region_tiles={0: [(tx, ty)]},
            filtered_tiles=[(tx, ty)], bounds_list=[], wgs84_extent=QgsRectangle(),
            max_zoom=self.ZOOM, transform_context=QgsCoordinateTransformContext())
        motor = TileRenderEngine(spec, _Retorno())
        motor.meta_tiles = [motor.create_individual_metatile(self.ZOOM, tx, ty, n)]
        motor.process_metatile = lambda _job: None
        motor.start_jobs()
        job = next(iter(motor.renderer_jobs))
        job.waitForFinished()
        imagem = job.renderedImage()

        opacos = [(x + 0.5, y + 0.5) for y in range(imagem.height()) for x in range(imagem.width())
                  if (imagem.pixel(x, y) >> 24) & 0xFF > 128]
        self.assertTrue(opacos, 'o marcador não apareceu no tile')
        cx = sum(p[0] for p in opacos) / len(opacos)
        cy = sum(p[1] for p in opacos) / len(opacos)
        # Centro do tile = onde a 6972 põe o marcador; a 1201 o poria ~114 px ao lado.
        self.assertLess(math.hypot(cx - imagem.width() / 2.0, cy - imagem.height() / 2.0), 1.5)


_SEM_GRADE = r'''
import json
from qgis.core import QgsApplication
app = QgsApplication([], False)
app.initQgis()
from tairu_core.vector_export import export_vector_layers
from tairu_sync.push import build_push_plan
from test.test_datum_context import MANAUS, _MAPPING, _Escritor, _Mapa, _Retorno, _camada
camada = _camada(29193, 4618, *MANAUS)
retorno, escritor = _Retorno(), _Escritor()
export_vector_layers(escritor, [camada], None, retorno)
plano = build_push_plan(camada, _MAPPING, _Mapa(), 'u1')
print(json.dumps({'ponto': escritor.pontos[0], 'avisos': retorno.avisos,
                  'previa': plano.datum_warning}))
'''


class TestGradeAusente(unittest.TestCase):

    def test_sem_a_grade_do_ibge_cai_no_padrao_e_avisa(self):
        origem = next((p for p in QgsProjUtils.searchPaths()
                       if os.path.exists(os.path.join(p, 'proj.db'))), None)
        if origem is None:
            self.skipTest('proj.db não encontrado')
        # PROJ_LIB com tudo, menos as grades do IBGE (links, não cópia: 1 GB).
        pasta = tempfile.mkdtemp(prefix='proj-sem-ibge-')
        for nome in os.listdir(origem):
            if not nome.startswith('br_ibge'):
                os.symlink(os.path.join(origem, nome), os.path.join(pasta, nome))
        env = dict(os.environ, PROJ_LIB=pasta, PROJ_DATA=pasta, PROJ_NETWORK='OFF',
                   PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        saida = subprocess.run([sys.executable, '-c', _SEM_GRADE], env=env, cwd=raiz,  # nosec B603
                               capture_output=True, text=True, timeout=120, check=True)
        resultado = json.loads(saida.stdout.strip().splitlines()[-1])

        self.assertLess(_metros(resultado['ponto'], _ref(1864, *MANAUS)), 0.05)
        self.assertEqual(len(resultado['avisos']), 1)
        self.assertIn('br_ibge_SAD69_003.tif', resultado['avisos'][0])
        self.assertIn('EPSG:1864', resultado['avisos'][0])
        self.assertIn('br_ibge_SAD69_003.tif', resultado['previa'])


if __name__ == '__main__':
    unittest.main()
