# -*- coding: utf-8 -*-

"""Quem desenha o mapa: imagens de arquivo marcadas + o mapa de fundo do projeto.

A 2.0.18 tirou o mapa de fundo online da geração e não deixou nenhuma forma de
colocá-lo de volta: num projeto de satélite online e vetores — a maioria — o
assistente travava na Estimativa dizendo que não havia camada para desenhar.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QGIS_CUSTOM_CONFIG_PATH', tempfile.mkdtemp(prefix='qgis-test-'))

try:
    from qgis.core import (
        QgsApplication, QgsCoordinateReferenceSystem, QgsProject, QgsRasterLayer,
        QgsRectangle,
    )
    from qgis.gui import QgsMapCanvas
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    # Sem o prefixo, o registro de provedores fica sem o `wms` e uma camada XYZ
    # nasce inválida — o teste passaria a medir o ambiente, não o código.
    prefix = os.environ.get('QGIS_PREFIX_PATH')
    if prefix:
        QgsApplication.setPrefixPath(prefix, True)
    _APP = QgsApplication([], True)
    _APP.initQgis()


class _StubIface(object):

    def __init__(self, canvas):
        self._canvas = canvas

    def mapCanvas(self):
        return self._canvas

    def mainWindow(self):
        return None


def _xyz_layer(name):
    """Basemap XYZ — sem rede: só precisa existir como camada do projeto."""
    uri = 'type=xyz&url=https://example.invalid/%7Bz%7D/%7Bx%7D/%7By%7D.png'
    layer = QgsRasterLayer(uri, name, 'wms')
    if not layer.isValid():
        raise unittest.SkipTest('provedor wms indisponível neste interpretador')
    QgsProject.instance().addMapLayer(layer)
    return layer


def _patches_raster(name, patches=30, seed=7):
    """Máscara de manchas soltas sobre nodata (desmatamento, nuvem, talhões)."""
    import random
    from osgeo import gdal, osr
    path = os.path.join(tempfile.mkdtemp(prefix='manchas-'), '%s.tif' % name)
    ds = gdal.GetDriverByName('GTiff').Create(path, 1000, 1000, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((-47.94, 0.0001, 0, -15.77, 0, -0.0001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(255)
    band.Fill(255)
    sorteio = random.Random(seed)
    for _ in range(patches):
        lado = sorteio.randint(6, 20)
        x, y = sorteio.randint(0, 1000 - lado), sorteio.randint(0, 1000 - lado)
        band.WriteRaster(x, y, lado, lado, bytes([100]) * lado * lado)
    ds = None
    layer = QgsRasterLayer(path, name, 'gdal')
    QgsProject.instance().addMapLayer(layer)
    return layer


def _file_raster(name, nodata_holes=False):
    """Ortofoto sintética georreferenciada (provider gdal).

    Com `nodata_holes`: imagem só no miolo, um buraco de nodata com UM pixel de imagem
    no meio e um pixel de imagem solto longe do miolo (mais de dois tiles) — o que o
    nodata de uma ortofoto real deixa no contorno.
    """
    from osgeo import gdal, osr
    path = os.path.join(tempfile.mkdtemp(prefix='orto-'), '%s.tif' % name)
    ds = gdal.GetDriverByName('GTiff').Create(path, 400, 400, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((-47.94, 0.0001, 0, -15.77, 0, -0.0001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    if nodata_holes:
        band = ds.GetRasterBand(1)
        band.SetNoDataValue(255)
        band.Fill(255)
        band.WriteRaster(100, 100, 280, 280, bytes([100]) * 280 * 280)
        band.WriteRaster(180, 180, 40, 40, bytes([255]) * 40 * 40)
        band.WriteRaster(200, 200, 1, 1, bytes([100]))
        band.WriteRaster(20, 20, 1, 1, bytes([100]))
    ds = None
    layer = QgsRasterLayer(path, name, 'gdal')
    QgsProject.instance().addMapLayer(layer)
    return layer


def _build_wizard():
    from tairu_ui.local_generate_wizard import TairuDBGenerateWizard
    canvas = QgsMapCanvas()
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
    canvas.setExtent(QgsRectangle(-47.95, -15.80, -47.90, -15.76))
    wizard = TairuDBGenerateWizard(_StubIface(canvas))
    wizard._canvas = canvas  # mantém vivo enquanto o assistente existir
    wizard.extent_page.initializePage()
    wizard.params_page.initializePage()
    return wizard


def _fresh_project():
    project = QgsProject.instance()
    project.clear()
    project.setCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
    return project


class TestBasemapChoice(unittest.TestCase):

    def test_online_only_project_can_still_draw_the_map(self):
        _fresh_project()
        _xyz_layer('Satélite')
        wizard = _build_wizard()
        # Sem imagem em disco o fundo é a única coisa capaz de desenhar: vem
        # marcado, e o assistente não trava na Estimativa.
        self.assertTrue(wizard.params_page.basemap_check.isChecked())
        self.assertEqual([lyr.name() for lyr in wizard.visible_basemap_layers()],
                         ['Satélite'])
        self.assertIn('BAIXA', wizard.params_page.online_note.text())

    def test_file_images_win_and_the_online_basemap_stays_out_by_default(self):
        _fresh_project()
        _xyz_layer('Satélite')
        _file_raster('orto')
        wizard = _build_wizard()
        self.assertFalse(wizard.params_page.basemap_check.isChecked())
        self.assertEqual([lyr.name() for lyr in wizard.visible_basemap_layers()],
                         ['orto'])

    def test_marking_the_basemap_composes_it_under_the_file_image(self):
        _fresh_project()
        _xyz_layer('Satélite')
        _file_raster('orto')
        wizard = _build_wizard()
        wizard.params_page.basemap_check.setChecked(True)
        nomes = [lyr.name() for lyr in wizard.visible_basemap_layers()]
        self.assertEqual(sorted(nomes), ['Satélite', 'orto'])
        # Ordem de desenho do projeto, não a ordem em que foram escolhidas.
        ordem = [lyr.name() for lyr in QgsProject.instance().layerTreeRoot().layerOrder()]
        self.assertEqual(nomes, [n for n in ordem if n in nomes])

    def test_per_image_area_gives_the_basemap_its_own_region_under_the_image(self):
        # Antes: UMA região, com o fundo composto por baixo de um TIFF opaco —
        # baixado e invisível, sem como ligar um e desligar o outro no app.
        _fresh_project()
        _xyz_layer('Satélite')
        _file_raster('orto')
        wizard = _build_wizard()
        wizard.extent_page.raster_radio.setChecked(True)
        wizard.params_page.basemap_check.setChecked(True)
        polygons, layers, names = wizard.area_regions()
        self.assertEqual(len(polygons), 2)
        # O fundo vem PRIMEIRO: o app desenha a última região por cima.
        self.assertEqual(names, {0: 'Satélite', 1: 'orto'})
        self.assertEqual({i: [lyr.name() for lyr in lyrs] for i, lyrs in layers.items()},
                         {0: ['Satélite'], 1: ['orto']})
        # Na mesma área da imagem.
        fundo, orto = polygons[0].boundingBox(), polygons[1].boundingBox()
        for a, b in ((fundo.xMinimum(), orto.xMinimum()), (fundo.yMaximum(), orto.yMaximum()),
                     (fundo.xMaximum(), orto.xMaximum()), (fundo.yMinimum(), orto.yMinimum())):
            self.assertAlmostEqual(a, b, places=9)

    def test_per_image_area_without_the_basemap_names_each_image_region(self):
        _fresh_project()
        _xyz_layer('Satélite')
        orto = _file_raster('orto')
        wizard = _build_wizard()
        wizard.extent_page.raster_radio.setChecked(True)
        polygons, layers, names = wizard.area_regions()
        self.assertEqual((len(polygons), layers, names), (1, {0: [orto]}, {0: 'orto'}))

    def test_each_image_region_draws_only_its_image_bottom_first(self):
        # Composta, a região "orto_2024" mostrava a orto_2023 na sobreposição: desligar
        # uma no app nunca revelava a outra. O app desenha a ÚLTIMA região por cima, então
        # a imagem de cima do painel vai por último.
        _fresh_project()
        _xyz_layer('Satélite')
        baixo = _file_raster('orto_2023')
        cima = _file_raster('orto_2024')   # adicionada depois: no topo do painel
        wizard = _build_wizard()
        wizard.extent_page.raster_radio.setChecked(True)
        wizard.params_page.basemap_check.setChecked(True)
        polygons, layers, names = wizard.area_regions()
        self.assertEqual(names, {0: 'Satélite', 1: 'orto_2023', 2: 'orto_2024'})
        self.assertEqual({i: [lyr.name() for lyr in lyrs] for i, lyrs in layers.items()},
                         {0: ['Satélite'], 1: ['orto_2023'], 2: ['orto_2024']})
        self.assertEqual(len(polygons), 3)   # as duas na mesma área: um fundo só
        self.assertIs(layers[1][0], baixo)
        self.assertIs(layers[2][0], cima)

    def test_basemap_area_has_no_holes_nor_specks(self):
        # O nodata deixa buracos e ilhas de um pixel no contorno; cada ilha virava uma
        # região de fundo de um tile quase transparente (dez numa ortofoto de drone).
        _fresh_project()
        _xyz_layer('Satélite')
        _file_raster('orto', nodata_holes=True)
        wizard = _build_wizard()
        wizard.extent_page.raster_radio.setChecked(True)
        wizard.params_page.basemap_check.setChecked(True)
        polygons, layers, names = wizard.area_regions()
        self.assertEqual(names, {0: 'Satélite', 1: 'orto'})
        fundo = polygons[0]
        self.assertFalse(fundo.isMultipart())
        self.assertEqual(len(fundo.asPolygon()), 1, 'o buraco de nodata é onde o fundo aparece')
        # a imagem continua com o próprio contorno, buraco e ilhas inclusive
        self.assertTrue(polygons[1].isMultipart())

    def test_scattered_patches_give_one_basemap_region(self):
        # Uma região de fundo por mancha: 139 "Google Satellite" no app, para ligar e
        # desligar uma a uma. Com mais de quatro pedaços, o contorno convexo da imagem.
        _fresh_project()
        _xyz_layer('Satélite')
        _patches_raster('alertas')
        wizard = _build_wizard()
        wizard.extent_page.raster_radio.setChecked(True)
        wizard.params_page.basemap_check.setChecked(True)
        polygons, layers, names = wizard.area_regions()
        self.assertEqual(names, {0: 'Satélite', 1: 'alertas'})
        self.assertGreater(len(polygons[1].asGeometryCollection()), 4)   # a imagem segue em manchas

    def test_stacking_follows_the_panel_when_generating(self):
        # A tabela de imagens é montada quando o assistente abre; reordenar o painel
        # depois não pode inverter o empilhamento no app.
        _fresh_project()
        _xyz_layer('Satélite')
        a = _file_raster('orto_2023')
        _file_raster('orto_2024')   # no topo
        wizard = _build_wizard()
        wizard.extent_page.raster_radio.setChecked(True)
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(a.id())
        root.insertChildNode(0, node.clone())   # a 2023 vai para o topo do painel
        node.parent().removeChildNode(node)
        _polygons, _layers, names = wizard.area_regions()
        self.assertEqual(names, {0: 'orto_2024', 1: 'orto_2023'})   # a última fica por cima

    def test_aborted_run_redoes_the_estimate(self):
        # O QWizard só inicializa uma página indo para a frente: sem refazer a Estimativa
        # aqui, "volte à Estimativa" mostrava a conta velha e Avançar abortava de novo.
        _fresh_project()
        _xyz_layer('Satélite')
        _file_raster('orto')
        wizard = _build_wizard()
        wizard.region_layer_ids = {0: ['camada_que_saiu_do_projeto']}
        chamadas = []
        wizard.estimate_page.initializePage = lambda: chamadas.append(1)
        wizard.run_page._run_generation()
        self.assertEqual(chamadas, [1])
        self.assertIn('Estimativa foi refeita', wizard.run_page.log.toPlainText())
        self.assertFalse(wizard.run_page._running)

    def test_run_with_no_layer_left_stops_instead_of_writing_an_empty_file(self):
        # Fora de "Cada imagem" não há ids por região: ocultar a única imagem depois da
        # Estimativa gerava um arquivo sem tile nenhum e dizia "Concluído!".
        _fresh_project()
        orto = _file_raster('orto')
        wizard = _build_wizard()   # área visível do mapa
        QgsProject.instance().layerTreeRoot().findLayer(orto.id()).setItemVisibilityChecked(False)
        chamadas = []
        wizard.estimate_page.initializePage = lambda: chamadas.append(1)
        wizard.run_page._run_generation()
        self.assertEqual(chamadas, [1])

    def test_image_inside_a_group_rendered_as_a_layer_still_generates(self):
        # layerOrder() troca a imagem pelo grupo "renderizado como grupo": resolvida
        # por ele, a geração abortava em toda tentativa.
        from qgis.core import QgsGroupLayer
        _fresh_project()
        _xyz_layer('Satélite')
        orto = _file_raster('orto')
        wizard = _build_wizard()
        wizard.extent_page.raster_radio.setChecked(True)
        wizard.params_page.basemap_check.setChecked(True)
        _p, layers, _n = wizard.area_regions()
        wizard.region_layer_ids = {rid: [lyr.id() for lyr in lyrs] for rid, lyrs in layers.items()}
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        node = root.findLayer(orto.id())
        grupo = root.addGroup('Grupo')
        grupo.addChildNode(node.clone())
        node.parent().removeChildNode(node)
        project.addMapLayer(grupo.convertToGroupLayer(
            QgsGroupLayer.LayerOptions(project.transformContext())), False)
        self.assertNotIn(orto, root.layerOrder())
        self.assertIsNotNone(wizard.region_layers_now())

    def test_layers_are_resolved_by_id_when_generating(self):
        from tairu_ui.local_generate_wizard import _resolve_region_layers
        _fresh_project()
        fundo = _xyz_layer('Satélite')
        orto = _file_raster('orto')
        ids = {0: [fundo.id()], 1: [orto.id()]}
        self.assertEqual(_resolve_region_layers(ids, [orto, fundo]), {0: [fundo], 1: [orto]})
        # removida ou ocultada depois da Estimativa: a geração para e manda voltar
        self.assertIsNone(_resolve_region_layers(ids, [orto]))

    def test_other_areas_keep_the_basemap_composed_under_the_image(self):
        _fresh_project()
        _xyz_layer('Satélite')
        _file_raster('orto')
        wizard = _build_wizard()
        wizard.params_page.basemap_check.setChecked(True)
        polygons, layers, names = wizard.area_regions()   # área visível do mapa
        self.assertEqual((len(polygons), layers, names), (1, {}, {}))

    def test_no_basemap_layer_hides_the_choice(self):
        _fresh_project()
        _file_raster('orto')
        wizard = _build_wizard()
        self.assertFalse(wizard.params_page.basemap_check.isVisible())
        self.assertEqual(wizard.params_page.extra_basemap_layers(), [])

    def test_hidden_file_image_is_named_instead_of_denied(self):
        project = _fresh_project()
        layer = _file_raster('orto')
        project.layerTreeRoot().findLayer(layer.id()).setItemVisibilityChecked(False)
        wizard = _build_wizard()
        page = wizard.extent_page
        # Antes: "nenhum arquivo de imagem neste projeto" — falso, e sem dizer
        # onde mexer. A imagem existe; está apenas desmarcada no painel.
        self.assertEqual(page.hidden_images, ['orto'])
        self.assertIn('orto', page.hidden_images_label.text())
        self.assertIn('ocultas no painel', page.raster_radio.text())
        self.assertFalse(page.has_usable_images())

    def test_hiding_the_basemap_after_marking_it_drops_it(self):
        _fresh_project()
        layer = _xyz_layer('Satélite')
        wizard = _build_wizard()
        self.assertTrue(wizard.visible_basemap_layers())
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        node.setItemVisibilityChecked(False)
        self.assertEqual(wizard.visible_basemap_layers(), [])


if __name__ == '__main__':
    unittest.main()
