# -*- coding: utf-8 -*-

"""Copiar registros de uma expedição para outra.

O tairuSyncHash gravado nas feições é a fotografia do registro NA EXPEDIÇÃO DE ONDE ELE
VEIO. Comparar com ele ao enviar para OUTRA expedição responde à pergunta errada: todos os
registros saíam como "inalterados" — prévia vazia e botão Enviar desligado — quando no
destino eles nem existem. E um membro comum copiando registros de outra pessoa via tudo
como "sem permissão", porque a autoria da origem era restaurada no candidato.

Precisa do Python do QGIS; pulado em outros interpretadores.
"""

import json
import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
# Perfil QGIS descartável: map_workspace() escreve dentro do perfil, e o teste precisa do
# layout real ({...}/tairu_workspace/{env}/{mapId}/records.gpkg) para exercitar a detecção.
os.environ.setdefault('QGIS_CUSTOM_CONFIG_PATH', tempfile.mkdtemp(prefix='qgis-test-'))

try:
    from qgis.core import QgsApplication, QgsProject
except ImportError:  # pragma: no cover - sem QGIS neste interpretador
    QgsApplication = None

_APP = None
_ORIGEM = 'expedicao-origem'
_DESTINO = 'expedicao-destino'
_AUTOR = 'uid-autor-original'
_COPIADOR = 'uid-quem-copia'


def setUpModule():
    global _APP
    if QgsApplication is None:
        raise unittest.SkipTest('QGIS Python bindings not available')
    _APP = QgsApplication([], False)
    _APP.initQgis()


class _Mapa:
    def __init__(self, map_id, papel='owner'):
        self.map_id = map_id
        self.nome = map_id
        self._papel = papel

    def role_for(self, _uid):
        return self._papel


_MAPPING = {'nome_field': None, 'descricao_field': None, 'tipo': 'local',
            'sub_tipo': 'outroLocal', 'situation': 'Ativo'}


def _pulled_layer(map_id, quantidade=4):
    """A camada exatamente como o pull a deixa: GeoPackage no workspace + estilo."""
    from tairu_firebase.models import TairuRecord, now_millis
    from tairu_core.workspace import map_workspace
    from tairu_sync.record_convert import apply_pull, add_record_layers_to_project

    gpkg = map_workspace('prod', map_id)['gpkg']
    agora = now_millis()
    registros = [
        TairuRecord(
            record_id=f'rec{i}', nome=f'Ponto {i}', tipo_registro='local',
            sub_tipo='outroLocal', situation='Ativo', geometry_type='point',
            geometry_points_json=json.dumps([{'la': -15.0 - i * 0.01, 'lo': -47.0, 'ts': agora}]),
            created_by=_AUTOR, created_at=agora, last_modified=agora, event_date_time=agora)
        for i in range(quantidade)
    ]
    apply_pull(gpkg, registros)
    add_record_layers_to_project(gpkg, map_id)
    return [layer for layer in QgsProject.instance().mapLayers().values()
            if layer.source().startswith(gpkg) and layer.name().endswith('Pontos')][0]


class TestCrossMapCopy(unittest.TestCase):

    def setUp(self):
        QgsProject.instance().clear()
        self.layer = _pulled_layer(_ORIGEM)

    def test_origin_is_recovered_from_the_pull_workspace_path(self):
        from tairu_sync.record_convert import layer_origin_map_id
        # parts[-2] é a expedição: .../tairu_workspace/{env}/{mapId}/records.gpkg
        self.assertEqual(layer_origin_map_id(self.layer), _ORIGEM)

    def test_same_expedition_still_detects_unchanged(self):
        from tairu_sync.push import build_push_plan
        plano = build_push_plan(self.layer, _MAPPING, _Mapa(_ORIGEM), _AUTOR,
                                propagate_deletions=True)
        self.assertEqual({i.action for i in plano.items}, {'unchanged'})
        self.assertEqual(plano.copied_from_map_id, '')

    def test_other_expedition_becomes_new_records_keeping_the_id(self):
        from tairu_sync.push import build_push_plan
        plano = build_push_plan(self.layer, _MAPPING, _Mapa(_DESTINO), _AUTOR,
                                propagate_deletions=True)
        self.assertEqual({i.action for i in plano.items}, {'new'})
        self.assertTrue(plano.writable_items(), 'o botão Enviar tem de ficar habilitado')
        self.assertEqual(plano.copied_from_map_id, _ORIGEM)
        self.assertEqual([i.record.record_id for i in plano.items],
                         [f'rec{i}' for i in range(4)])
        self.assertTrue(all(i.warning for i in plano.items), 'a prévia tem de explicar')
        # A prévia esconde 'unchanged' sem aviso — era essa a "lista vazia".
        visiveis = [i for i in plano.items if i.action != 'unchanged' or i.warning]
        self.assertEqual(len(visiveis), 4)

    def test_member_copying_someone_elses_records_is_not_forbidden(self):
        from tairu_sync.push import build_push_plan
        plano = build_push_plan(self.layer, _MAPPING, _Mapa(_DESTINO, papel='member'),
                                _COPIADOR, propagate_deletions=True)
        self.assertEqual({i.action for i in plano.items}, {'new'})
        # A cópia é de quem copiou: é o que as regras do servidor exigem no create.
        self.assertEqual({i.record.created_by for i in plano.items}, {_COPIADOR})

    def test_copy_never_propagates_deletions_from_the_origin_snapshot(self):
        from tairu_sync.push import build_push_plan
        plano = build_push_plan(self.layer, _MAPPING, _Mapa(_DESTINO), _AUTOR,
                                propagate_deletions=True)
        self.assertEqual(plano.count('delete'), 0)

    def test_copy_writes_upsert_so_the_second_send_updates_instead_of_failing(self):
        from tairu_sync.push import build_push_plan, build_writes

        class _FakeFS:
            def full_name(self, path):
                return f'projects/p/databases/(default)/documents/{path}'

            def build_create_write(self, path, fields, **kwargs):
                return {'kind': 'create', 'path': path}

            def build_update_write(self, path, fields, mask, require_existing=True, **kwargs):
                return {'kind': 'update', 'path': path, 'require_existing': require_existing}

        plano = build_push_plan(self.layer, _MAPPING, _Mapa(_DESTINO), _AUTOR)
        writes = build_writes(_FakeFS(), plano, _AUTOR)
        self.assertEqual(len(writes), 4)
        # create traz currentDocument.exists=false: no segundo envio o lote inteiro
        # falharia com "documento já existe".
        self.assertEqual({w['kind'] for w in writes}, {'update'})
        self.assertEqual({w['require_existing'] for w in writes}, {False})
        self.assertTrue(all(f'/{_DESTINO}/records/' in w['path'] for w in writes))

    def test_normal_push_of_an_ordinary_layer_still_uses_create(self):
        """Camada comum do usuário (sem recordId): continua sendo create, não upsert."""
        from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY
        from tairu_sync.push import build_push_plan, build_writes

        comum = QgsVectorLayer('Point?crs=EPSG:4326&field=nome:string', 'minha camada', 'memory')
        provider = comum.dataProvider()
        feicoes = []
        for i in range(3):
            feicao = QgsFeature(comum.fields())
            feicao.setAttribute('nome', f'p{i}')
            feicao.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(-47.0, -15.0 - i)))
            feicoes.append(feicao)
        provider.addFeatures(feicoes)
        comum.updateExtents()
        QgsProject.instance().addMapLayer(comum, True)

        class _FakeFS:
            def build_create_write(self, path, fields, **kwargs):
                return {'kind': 'create'}

            def build_update_write(self, path, fields, mask, require_existing=True, **kwargs):
                return {'kind': 'update', 'require_existing': require_existing}

        plano = build_push_plan(comum, _MAPPING, _Mapa(_DESTINO), _AUTOR)
        self.assertEqual(plano.copied_from_map_id, '')
        self.assertEqual({i.action for i in plano.items}, {'new'})
        writes = build_writes(_FakeFS(), plano, _AUTOR)
        self.assertEqual({w['kind'] for w in writes}, {'create'})


if __name__ == '__main__':
    unittest.main()
