# -*- coding: utf-8 -*-

"""Página de área: medida da origem marcada, origens impossíveis e tabela de imagens.

O tamanho da área só aparecia cinco telas adiante, na Estimativa — um basemap global
marcado por engano só se revelava lá. A medida usa `extent()` e `featureCount()`, que
não varrem feições: uma camada grande tem que responder na hora.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QGIS_CUSTOM_CONFIG_PATH', tempfile.mkdtemp(prefix='qgis-test-'))

try:
    from qgis.core import (
        QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,
        QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer,
    )
    from qgis.gui import QgsMapCanvas
    from qgis.PyQt.QtCore import Qt
    _UNCHECKED_STATE = Qt.CheckState.Unchecked
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], True)
    _APP.initQgis()


class _StubIface(object):
    """Só o que a ExtentPage usa do iface: o canvas."""

    def __init__(self, canvas):
        self._canvas = canvas

    def mapCanvas(self):
        return self._canvas


class _StubWizard(object):

    def __init__(self, iface):
        self.iface = iface
        self.hidden = False

    def hide(self):
        self.hidden = True

    def show(self):
        self.hidden = False

    def raise_(self):
        pass


def _polygon_layer(name, wkts):
    layer = QgsVectorLayer('Polygon?crs=EPSG:4326&field=id:integer', name, 'memory')
    provider = layer.dataProvider()
    for wkt in wkts:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        provider.addFeature(feature)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer, False)
    return layer


def _raster_layer(name, page):
    """Ortofoto sintética georreferenciada: 400x400 px cobrindo ~4,4 km."""
    from osgeo import gdal, osr
    path = os.path.join(tempfile.mkdtemp(prefix='orto-'), f'{name}.tif')
    ds = gdal.GetDriverByName('GTiff').Create(path, 400, 400, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((-47.94, 0.0001, 0, -15.77, 0, -0.0001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds = None
    layer = QgsRasterLayer(path, name, 'gdal')
    QgsProject.instance().addMapLayer(layer)
    return layer


def _xyz_layer(name):
    """Basemap XYZ — sem rede: só precisa existir como camada do projeto."""
    uri = 'type=xyz&url=https://example.invalid/%7Bz%7D/%7Bx%7D/%7By%7D.png'
    layer = QgsRasterLayer(uri, name, 'wms')
    QgsProject.instance().addMapLayer(layer)
    return layer


def _build_page():
    from tairu_ui.local_generate_wizard import ExtentPage
    project = QgsProject.instance()
    project.clear()
    project.setCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
    canvas = QgsMapCanvas()
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
    canvas.setExtent(QgsRectangle(-47.95, -15.80, -47.90, -15.76))
    page = ExtentPage(_StubWizard(_StubIface(canvas)))
    page._canvas = canvas  # mantém vivo enquanto a página existir
    return page


class TestExtentSummary(unittest.TestCase):

    def test_layer_counts_one_region_per_feature(self):
        page = _build_page()
        layer = _polygon_layer('areas', [
            'POLYGON((-47.95 -15.80, -47.90 -15.80, -47.90 -15.76, -47.95 -15.76, -47.95 -15.80))',
            'POLYGON((-47.80 -15.70, -47.78 -15.70, -47.78 -15.68, -47.80 -15.68, -47.80 -15.70))',
            'POLYGON((-47.70 -15.60, -47.68 -15.60, -47.68 -15.58, -47.70 -15.58, -47.70 -15.60))',
        ])
        page.layer_radio.setChecked(True)
        page.layer_combo.setLayer(layer)
        page._sync_controls()
        self.assertIn('3 regiões', page.layer_radio.text())
        self.assertIn('km', page.layer_radio.text())

    def test_canvas_is_a_single_region_sized_from_the_visible_extent(self):
        page = _build_page()
        page._sync_controls()
        text = page.canvas_radio.text()
        self.assertIn('1 região', text)
        # 0,04° de latitude ≈ 4,4 km. A largura não entra na asserção: o canvas
        # estica a extensão para a proporção do widget antes de devolvê-la.
        self.assertIn('× 4,4 km', text)

    def test_drawn_rectangle_says_so_before_anything_is_drawn(self):
        page = _build_page()
        page.draw_radio.setChecked(True)
        self.assertIn('nenhum definido ainda', page.draw_radio.text())
        page._on_extent_picked(QgsRectangle(-47.95, -15.80, -47.90, -15.76))
        self.assertIn('1 região', page.draw_radio.text())

    def test_unselected_sources_show_nothing_at_all(self):
        page = _build_page()
        _polygon_layer('areas', [
            'POLYGON((-47.95 -15.80, -47.90 -15.80, -47.90 -15.76, -47.95 -15.76, -47.95 -15.80))',
        ])
        _raster_layer('Ortofoto_2024', None)
        page.initializePage()
        page.layer_radio.setChecked(True)
        # Só controle de verdade ocupa altura; a medida mora no rótulo.
        for box in (page._draw_box, page._raster_box):
            self.assertFalse(box.isVisible())
        # A opção não marcada é só o rótulo base, sem sufixo nenhum.
        self.assertEqual(page.canvas_radio.text(), 'A área visível do mapa')
        self.assertEqual(page.raster_radio.text(), 'Cada imagem carregada no projeto')

    def test_the_four_sources_stay_mutually_exclusive(self):
        """Cada opção mora num recipiente próprio: sem QButtonGroup, dois marcados."""
        page = _build_page()
        page.draw_radio.setChecked(True)
        self.assertFalse(page.canvas_radio.isChecked())
        page.canvas_radio.setChecked(True)
        self.assertFalse(page.draw_radio.isChecked())

    def test_opens_on_the_visible_area_so_next_is_never_dead_on_arrival(self):
        page = _build_page()
        self.assertTrue(page.canvas_radio.isChecked())
        self.assertTrue(page.isComplete())

    def test_impossible_sources_are_disabled_with_the_reason_in_the_label(self):
        page = _build_page()
        page._sync_controls()
        self.assertFalse(page.layer_radio.isEnabled())
        self.assertIn('nenhuma camada de polígonos', page.layer_radio.text())
        self.assertFalse(page.raster_radio.isEnabled())
        self.assertIn('nenhum arquivo de imagem', page.raster_radio.text())

    def test_image_table_lists_metadata_and_checks_file_layers(self):
        page = _build_page()
        _raster_layer('Ortofoto_2024', page)
        _xyz_layer('Basemap online')
        page.initializePage()
        # O XYZ NAO entra: a caixinha dele aqui fazia o usuário concluir que ela
        # decidia se o mapa de fundo entrava no arquivo — quem decide é Parâmetros.
        self.assertEqual(page.raster_table.rowCount(), 1)
        self.assertEqual(page.raster_table.item(0, 1).text(), 'Ortofoto_2024')
        self.assertIn('km', page.raster_table.item(0, 2).text())
        self.assertIn('m/px', page.raster_table.item(0, 3).text())
        self.assertEqual(page.raster_table.item(0, 4).text(), 'EPSG:4326')
        self.assertTrue(page.raster_radio.isEnabled())
        self.assertEqual([lyr.name() for lyr in page.checked_raster_layers()],
                         ['Ortofoto_2024'])

    def test_selecting_the_rectangle_option_does_not_hijack_the_screen(self):
        page = _build_page()
        page.draw_radio.setChecked(True)
        # Marcar a opção NAO chama o desenho: quem chama é o botão.
        self.assertFalse(page._wizard.hidden)
        self.assertTrue(page.draw_btn.isVisible() or not page.isVisible())

    def test_giving_up_on_the_rectangle_brings_the_wizard_back(self):
        page = _build_page()
        page.draw_radio.setChecked(True)
        page._start_picker()
        self.assertTrue(page._wizard.hidden)
        page._picker.canceled.emit()
        self.assertFalse(page._wizard.hidden)
        self.assertIsNone(page._picker)

    def test_checking_an_unusable_source_falls_back_to_the_visible_area(self):
        page = _build_page()
        page.layer_radio.setChecked(True)
        self.assertTrue(page.canvas_radio.isChecked())
        self.assertTrue(page.isComplete())


class TestBasemapSelection(unittest.TestCase):
    """Uma lista só, a da primeira tela — e mapas de fundo online ficam de fora.

    Um XYZ ligado no QGIS fazia a geração baixar milhares de tiles da internet
    numa exportação que o usuário achava ser só das imagens locais dele.
    """

    def test_unchecking_an_image_removes_it_from_what_gets_generated(self):
        page = _build_page()
        _raster_layer('Ortofoto_2024', None)
        segunda = _raster_layer('Voo_Norte', None)
        page.initializePage()
        self.assertEqual({lyr.name() for lyr in page.checked_raster_layers()},
                         {'Ortofoto_2024', 'Voo_Norte'})

        row = [lid for lid, _u in page._raster_rows].index(segunda.id())
        page.raster_table.item(row, 0).setCheckState(_UNCHECKED_STATE)
        self.assertEqual([lyr.name() for lyr in page.checked_raster_layers()],
                         ['Ortofoto_2024'])

    def test_checked_images_keep_the_project_draw_order(self):
        page = _build_page()
        primeira = _raster_layer('fundo', None)
        _raster_layer('cima', None)
        page.initializePage()
        ordem_projeto = [lyr.name() for lyr in QgsProject.instance().layerTreeRoot().layerOrder()
                         if lyr.id() in {lid for lid, _u in page._raster_rows}]
        self.assertEqual([lyr.name() for lyr in page.checked_raster_layers()], ordem_projeto)
        self.assertIn(primeira.name(), ordem_projeto)

    def test_online_source_is_detected_from_the_url_not_from_the_provider(self):
        from tairu_ui.local_generate_wizard import _layer_origin

        class _Stub:
            def __init__(self, src):
                self._src = src

            def source(self):
                return self._src

        xyz = 'type=xyz&url=https://tile.exemplo.com/%7Bz%7D/%7Bx%7D/%7By%7D.png'
        # Mesmo provedor (`wms`/`vectortile`) serve XYZ remoto e .mbtiles local:
        # é a URL na fonte que separa "gerar" de "baixar".
        self.assertEqual(_layer_origin(_Stub(xyz)), 'internet')
        self.assertEqual(_layer_origin(_Stub('/dados/basemap.mbtiles')), 'arquivo local')
        self.assertEqual(_layer_origin(_Stub('/dados/orto.tif')), 'arquivo local')


if __name__ == '__main__':
    unittest.main()
