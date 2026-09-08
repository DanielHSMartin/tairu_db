# -*- coding: utf-8 -*-

"""Self-check for a ordem da lista de expedições do painel.

A lista era alfabética; agora é "uso mais recente primeiro", a mesma definição do
aplicativo (TairuDataProvider.lastActivityMillisFor): a última vez que ESTA máquina
abriu a expedição e, para a que nunca foi aberta aqui, o lastModified do documento.
Sem a reserva, uma instalação nova mostraria todas empatadas em zero e cairia de
volta na ordem alfabética — o que este teste também fixa, no desempate.

Precisa do Python do QGIS (qgis.core); pulado em outros interpretadores.
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from qgis.core import QgsApplication
except ImportError:  # pragma: no cover - no QGIS on this interpreter
    QgsApplication = None

_APP = None


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], True)
    _APP.initQgis()


class TestMapsOrder(unittest.TestCase):

    def _ordered(self, maps, opened):
        from tairu_ui import maps_page as page_module

        original = page_module.map_last_opened_ms
        # Sem tocar no QgsSettings do usuário: a regra sob teste é a ordenação.
        page_module.map_last_opened_ms = lambda _env, map_id: opened.get(map_id, 0)
        try:
            page = page_module.MapsPage()
            page.set_maps(maps, 'uid1', 'testenv')
            return [m.nome for m in sorted(page._maps.values(), key=page._order_key)]
        finally:
            page_module.map_last_opened_ms = original

    def _maps(self):
        from tairu_firebase.models import TairuMap
        return [
            TairuMap(map_id='m-zebra', nome='Zebra', last_modified=1000),
            TairuMap(map_id='m-alfa', nome='Alfa', last_modified=3000),
            TairuMap(map_id='m-beta', nome='Beta', last_modified=2000),
            TairuMap(map_id='m-i2', nome='Mesmo nome'),
            TairuMap(map_id='m-i1', nome='Mesmo nome'),
        ]

    def test_never_opened_here_falls_back_to_the_map_last_change(self):
        self.assertEqual(
            self._ordered(self._maps(), {}),
            ['Alfa', 'Beta', 'Zebra', 'Mesmo nome', 'Mesmo nome'])

    def test_last_opened_wins_over_the_document_timestamp(self):
        self.assertEqual(
            self._ordered(self._maps(), {'m-zebra': 9_000_000, 'm-beta': 9_100_000}),
            ['Beta', 'Zebra', 'Alfa', 'Mesmo nome', 'Mesmo nome'])

    def test_ties_break_by_name_then_id(self):
        from tairu_firebase.models import TairuMap
        maps = [TairuMap(map_id='m-i2', nome='Mesmo nome'),
                TairuMap(map_id='m-i1', nome='Mesmo nome')]
        page_ids = self._ordered(maps, {})
        self.assertEqual(page_ids, ['Mesmo nome', 'Mesmo nome'])
        from tairu_ui.maps_page import MapsPage
        page = MapsPage()
        page.set_maps(maps, 'uid1', 'testenv')
        self.assertEqual([m.map_id for m in sorted(maps, key=page._order_key)],
                         ['m-i1', 'm-i2'])


if __name__ == '__main__':
    unittest.main()
