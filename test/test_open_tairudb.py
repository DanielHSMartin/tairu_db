# -*- coding: utf-8 -*-

"""Abrir um .tairudb no QGIS: raster, vetores e GRG como camadas, como o app desenha.

Monta um .tairudb sintetico com o que o leitor precisa tratar — camada do app com
pontos, linhas e poligonos juntos, icone embutido, poligono com furo so no WKB, poligono
sem preenchimento, atributos que colidem com as colunas do GeoPackage, GRG e raster — e
um '#' no nome do arquivo, que cortava a URI do sqlite.

Precisa do Python do QGIS (qgis.core); pulado em outros interpretadores.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from qgis.core import (
        QgsApplication, QgsCategorizedSymbolRenderer, QgsGeometry, QgsProject,
        QgsSingleSymbolRenderer, QgsVectorLayer)
    from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice
    from qgis.PyQt.QtGui import QColor, QImage
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None
_ICON_URI = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=='  # noqa: E501
_HOLED = ('POLYGON((10 10, 11 10, 11 11, 10 11, 10 10),'
          '(10.2 10.2, 10.4 10.2, 10.4 10.4, 10.2 10.4, 10.2 10.2))')


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], True)   # offscreen; as paginas do assistente sao widgets
    _APP.initQgis()


def _png_tile():
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 128, 0))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(data)


def _build(path):
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE vector_layers (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT UNIQUE NOT NULL,
            type TEXT, name TEXT, description TEXT);
        CREATE TABLE features (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT UNIQUE NOT NULL,
            layer_id TEXT, type TEXT, name TEXT, attributes TEXT, color TEXT, size INTEGER,
            iconType TEXT, points TEXT, style TEXT, wkb BLOB);
        CREATE TABLE regions (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT UNIQUE NOT NULL,
            name TEXT, minzoom INTEGER, maxzoom INTEGER, bounds TEXT);
        CREATE TABLE tiles_region_0 (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER,
            tile_data BLOB);
    ''')
    grg_config = {'grid_type': 'alphanumeric', 'line_color': '#FF0000', 'line_opacity': 0.5,
                  'line_width': 2, 'line_style': 'solid', 'font_color': '#FFFFFF', 'font_size': 14}
    conn.executemany('INSERT INTO metadata VALUES (?, ?)', [
        ('name', 'Mapa de teste'), ('format', 'png'), ('icon:0', _ICON_URI),
        ('grg_config', json.dumps(grg_config))])
    conn.executemany('INSERT INTO vector_layers (uuid, type, name, description) VALUES (?, ?, ?, ?)', [
        ('L1', 'vector', 'Importado', ''), ('L2', 'polygon', 'Áreas', ''), ('G', 'line', '__grg__', '')])

    def feature(uuid, layer, kind, name, points, attributes=None, color='#FF00FF00', size=None,
                style=None, wkb=None, icon='locationOn'):
        conn.execute('INSERT INTO features (uuid, layer_id, type, name, attributes, color, size, '
                     'iconType, points, style, wkb) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (uuid, layer, kind, name, json.dumps(attributes or {}), color, size, icon,
                      points, json.dumps(style) if style else None, wkb))

    # Camada do app: tres tipos numa camada so.
    feature('p1', 'L1', 'point', 'Poço', '-47.9 -15.8',
            attributes={'name': 'Poço', 'fid': 'x-1', 'Profundidade': 12, 'obs': 'raso'},
            style={'v': 1, 'base': {'icon': 'tairu:icon:0'}, 'label': {'field': 'obs', 'show': True}})
    feature('p2', 'L1', 'point', 'Casa', '-47.8 -15.7',
            attributes={'name': 'Casa', 'fid': 'x-2', 'Profundidade': 2.5})
    feature('l1', 'L1', 'line', 'Trilha', '-47.9 -15.8, -47.8 -15.7; -47.7 -15.6, -47.6 -15.5')
    feature('a1', 'L1', 'polygon', 'Lote', '-47.9 -15.8, -47.8 -15.8, -47.8 -15.7')
    # Poligono com furo (so o WKB o carrega) e com preenchimento explicito.
    feature('a2', 'L2', 'polygon', 'Reserva', '10 10, 11 10, 11 11, 10 11',
            style={'v': 1, 'base': {'bgColor': 0x800000FF}},
            wkb=QgsGeometry.fromWkt(_HOLED).asWkb().data())
    # Coordenada invalida: descartada como no app.
    feature('bad', 'L2', 'polygon', 'Lixo', '500 500, 600 600, 700 700')
    # GRG 2x1: tres limites de coluna, dois de linha, e os pontos de rotulo nos centros.
    grg = {'grid_type': 'alphanumeric'}
    for n, x in enumerate((0.0, 1.0, 2.0)):
        feature(f'gc{n}', 'G', 'line', 'grg_col', f'{x} 0, {x} 1', attributes=dict(grg, label=''),
                icon='grg_line_col')
    for n, y in enumerate((1.0, 0.0)):
        feature(f'gr{n}', 'G', 'line', 'grg_row', f'0 {y}, 2 {y}', attributes=dict(grg, label=''),
                icon='grg_line_row')
    feature('gla', 'G', 'point', 'grg_col_A', '0.5 0.5', attributes=dict(grg, label='A'),
            icon='grg_label_col')
    feature('glb', 'G', 'point', 'grg_col_B', '1.5 0.5', attributes=dict(grg, label='B'),
            icon='grg_label_col')
    feature('gl1', 'G', 'point', 'grg_row_1', '1 0.5', attributes=dict(grg, label='1'),
            icon='grg_label_row')

    conn.execute("INSERT INTO regions (uuid, name, minzoom, maxzoom, bounds) VALUES "
                 "('R1', 'Satélite', 0, 0, '-180 -85, 180 -85, 180 85, -180 85')")
    conn.execute('INSERT INTO tiles_region_0 VALUES (0, 0, 0, ?)', (_png_tile(),))
    conn.commit()
    conn.close()


class _Bar:
    def __init__(self):
        self.messages = []

    def pushMessage(self, title, text, level, duration):
        self.messages.append(text)


class _Iface:
    def __init__(self):
        self.bar = _Bar()

    def messageBar(self):
        return self.bar


class OpenTairuDBTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from tairu_ui.open_tairudb import convert
        cls.tmp = tempfile.TemporaryDirectory()
        cls.source = os.path.join(cls.tmp.name, 'mapa #1.tairudb')
        _build(cls.source)
        cls.out_dir = os.path.join(cls.tmp.name, 'conv')
        cls.manifest = convert(cls.source, cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        QgsProject.instance().clear()
        cls.tmp.cleanup()

    def _entry(self, name):
        return next(e for e in self.manifest['entries'] if e['name'] == name)

    def _layer(self, name):
        entry = self._entry(name)
        layer = QgsVectorLayer(f'{os.path.join(self.out_dir, entry["file"])}|layername={entry["table"]}',
                               name, 'ogr')
        self.assertTrue(layer.isValid(), name)
        return layer

    def test_entries_in_panel_order(self):
        self.assertEqual(self.manifest['title'], 'Mapa de teste')
        self.assertEqual([e['name'] for e in self.manifest['entries']], [
            'GRG (rótulos)', 'GRG', 'Importado (pontos)', 'Importado (linhas)',
            'Importado (polígonos)', 'Áreas', 'Satélite'])

    def test_mixed_layer_split_by_geometry(self):
        self.assertEqual(self._layer('Importado (pontos)').featureCount(), 2)
        self.assertEqual(self._layer('Importado (linhas)').featureCount(), 1)
        geometry = next(self._layer('Importado (linhas)').getFeatures()).geometry()
        self.assertEqual(geometry.constGet().numGeometries(), 2)   # ';' = duas partes

    def test_columns(self):
        points = self._layer('Importado (pontos)')
        names = points.fields().names()
        # 'name' so repetia o nome da feicao; 'fid' colidiria com a chave do GeoPackage.
        self.assertNotIn('name', names)
        self.assertIn('fid_2', names)
        values = {f['nome']: f for f in points.getFeatures()}
        self.assertEqual(values['Poço']['fid_2'], 'x-1')
        self.assertEqual(values['Casa']['Profundidade'], 2.5)   # int e float -> double
        self.assertEqual(values['Poço']['tairu_uuid'], 'p1')

    def test_hole_comes_from_wkb_and_invalid_is_dropped(self):
        areas = self._layer('Áreas')
        self.assertEqual(areas.featureCount(), 1)
        geometry = next(areas.getFeatures()).geometry()   # vivo: constGet() aponta para dentro
        self.assertEqual(geometry.constGet().geometryN(0).numInteriorRings(), 1)

    def test_styles_follow_the_app(self):
        points = self._entry('Importado (pontos)')
        icons = sorted(s['icon'] for s in points['styles'])
        self.assertEqual(icons, ['img:icon:0', 'locationOn'])   # iconType da coluna ignorado
        self.assertIn('icon:0', self.manifest['icons'])
        self.assertIn('"obs"', points['label']['expression'])
        # Sem bgColor o poligono do app nao tem preenchimento; com bgColor, tem.
        self.assertIsNone(self._entry('Importado (polígonos)')['styles'][0]['bg'])
        self.assertEqual(self._entry('Áreas')['styles'][0]['bg'], '#800000FF')

    def test_grg_labels_sit_outside_the_grid(self):
        marks = {f['rotulo']: f.geometry().asPoint() for f in self._layer('GRG (rótulos)').getFeatures()}
        self.assertEqual(set(marks), {'A', 'B', '1'})
        self.assertEqual((marks['A'].x(), marks['A'].y()), (0.5, 1.0))   # borda norte
        self.assertEqual((marks['1'].x(), marks['1'].y()), (0.0, 0.5))   # borda oeste
        self.assertEqual(self._entry('GRG')['grg']['color'], '#80FF0000')   # opacidade 0,5

    def test_reopen_reuses_conversion(self):
        from tairu_ui.open_tairudb import convert
        gpkg = os.path.join(self.out_dir, 'vetores.gpkg')
        before = os.stat(gpkg).st_mtime_ns
        self.assertEqual(convert(self.source, self.out_dir), self.manifest)
        self.assertEqual(os.stat(gpkg).st_mtime_ns, before)

    def test_add_to_project(self):
        from tairu_ui.open_tairudb import add_to_project
        QgsProject.instance().clear()
        iface = _Iface()
        add_to_project(iface, self.source, self.out_dir, self.manifest)
        group = QgsProject.instance().layerTreeRoot().children()[0]
        self.assertEqual(group.name(), 'Mapa de teste')
        layers = {node.name(): node.layer() for node in group.findLayers()}
        self.assertEqual(len(layers), 7, iface.bar.messages)
        self.assertTrue(layers['Satélite'].isValid())
        self.assertIsInstance(layers['Importado (pontos)'].renderer(), QgsCategorizedSymbolRenderer)
        self.assertIsInstance(layers['Áreas'].renderer(), QgsSingleSymbolRenderer)
        self.assertTrue(layers['Importado (pontos)'].labelsEnabled())
        self.assertTrue(layers['GRG (rótulos)'].labelsEnabled())
        self.assertTrue(layers['Áreas'].readOnly())
        fill = layers['Importado (polígonos)'].renderer().symbol().symbolLayer(0).fillColor()
        self.assertEqual(fill.alpha(), 0)
        # Abrir de novo o mesmo arquivo nao duplica o grupo.
        add_to_project(iface, self.source, self.out_dir, self.manifest)
        titles = [n.name() for n in QgsProject.instance().layerTreeRoot().children()]
        self.assertEqual(titles.count('Mapa de teste'), 1)
        self.assertIn('já está aberto', iface.bar.messages[-1])

    def test_regenerated_file_replaces_the_open_group(self):
        """O arquivo regravado ganha outra pasta de conversao; o grupo velho sai."""
        import shutil
        from tairu_ui.open_tairudb import add_to_project
        QgsProject.instance().clear()
        iface = _Iface()
        add_to_project(iface, self.source, self.out_dir, self.manifest)
        newer = self.out_dir + '-nova'
        shutil.copytree(self.out_dir, newer, dirs_exist_ok=True)
        add_to_project(iface, self.source, newer, self.manifest)
        groups = [n for n in QgsProject.instance().layerTreeRoot().children()
                  if n.name() == 'Mapa de teste']
        self.assertEqual(len(groups), 1)
        sources = {node.layer().source() for node in groups[0].findLayers()}
        self.assertTrue(all(newer in s for s in sources), sources)
        self.assertEqual(len(QgsProject.instance().mapLayers()), 7)   # as velhas saíram

    def test_open_layers_never_feed_the_next_generation(self):
        """O resultado aberto no projeto nao e fonte: nem na lista, nem no export."""
        from tairu_core.layer_tree import is_tairudb_view
        from tairu_core.vector_export import export_vector_layers
        from tairu_ui.local_generate_wizard import VectorLayersPage
        from tairu_ui.open_tairudb import add_to_project
        QgsProject.instance().clear()
        add_to_project(_Iface(), self.source, self.out_dir, self.manifest)
        layers = list(QgsProject.instance().mapLayers().values())
        self.assertTrue(layers and all(is_tairudb_view(layer) for layer in layers))

        page = VectorLayersPage(None)
        page.initializePage()
        self.assertEqual(page._vector_checkboxes, {})

        class _Writer:
            conn = None

            def insertVectorLayer(self, *args):
                raise AssertionError('camada aberta de .tairudb foi reexportada')

        class _Feedback:
            info = []

            def push_info(self, text):
                self.info.append(text)

            def is_canceled(self):
                return False

        vectors = [layer for layer in layers if isinstance(layer, QgsVectorLayer)]
        feedback = _Feedback()
        export_vector_layers(_Writer(), vectors, None, feedback)
        self.assertIn('.tairudb', feedback.info[-1])

    def test_not_a_tairudb(self):
        from tairu_ui.open_tairudb import convert
        junk = os.path.join(self.tmp.name, 'lixo.tairudb')
        with open(junk, 'w') as f:
            f.write('isto não é sqlite')
        with self.assertRaises(ValueError):
            convert(junk, os.path.join(self.tmp.name, 'conv-lixo'))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'conv-lixo.parcial')))


class OpenAfterGenerateTest(unittest.TestCase):
    """Caixa "Abrir o arquivo no QGIS ao terminar" do assistente de arquivo local."""

    def setUp(self):
        from qgis.PyQt.QtCore import QSettings
        # Configuracao isolada numa pasta temporaria: o teste nao pode mexer nas
        # preferencias reais do QGIS (e o QGIS headless grava na pasta do perfil).
        self._settings_dir = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                          self._settings_dir.name)

    def tearDown(self):
        self._settings_dir.cleanup()

    def _page(self, upload):
        from types import SimpleNamespace
        from tairu_ui.local_generate_wizard import DestinationPage
        return DestinationPage(SimpleNamespace(is_upload_mode=upload, tmap=None))

    def test_checked_by_default_and_remembered(self):
        page = self._page(upload=False)
        self.assertTrue(page.open_after())
        page.open_check.setChecked(False)
        self.assertFalse(self._page(upload=False).open_after())   # lembrado na proxima vez

    def test_not_offered_when_the_file_goes_to_an_expedition(self):
        page = self._page(upload=True)
        self.assertIsNone(page.open_check)
        self.assertFalse(page.open_after())


if __name__ == '__main__':
    unittest.main()
