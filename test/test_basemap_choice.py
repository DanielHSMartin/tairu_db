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


def _file_raster(name):
    """Ortofoto sintética georreferenciada (provider gdal)."""
    from osgeo import gdal, osr
    path = os.path.join(tempfile.mkdtemp(prefix='orto-'), '%s.tif' % name)
    ds = gdal.GetDriverByName('GTiff').Create(path, 400, 400, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((-47.94, 0.0001, 0, -15.77, 0, -0.0001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
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
