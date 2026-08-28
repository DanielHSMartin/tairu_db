# -*- coding: utf-8 -*-

"""Self-check for the per-feature attributes a push carries to /records.

The source layer's own columns are what the app shows in the record's "Atributos"
tab. They used to be sent ONLY when the layer's labels referenced a non-name field,
so practically every pushed layer produced records with no attributes at all — the
columns existed in QGIS and never left it.

Pinned here:
1) a plain layer (no labels) still yields every genuine user column;
2) the record-mirror columns a pushed/pulled layer carries (recordId, nome,
   descricao, situation, color, size, tairuSync*) never leak in as "attributes";
3) an UPDATE puts `attributes` in the mask when the candidate has some, and never
   when it hasn't — a pulled layer has no user columns, and an empty candidate
   means "not seen", never "clear what the app has".

QGIS is stubbed: _feature_attributes_json and build_writes are pure Python.
"""

import importlib.abc
import importlib.machinery
import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Any:
    def __getattr__(self, n):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __mro_entries__(self, bases):
        return (object,)


def _stub_qgis():
    class _AnyModule(types.ModuleType):
        __path__ = []

        def __getattr__(self, name):
            return _Any()

    class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path, target=None):
            if fullname == 'qgis' or fullname.startswith('qgis.'):
                return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
            return None

        def create_module(self, spec):
            return _AnyModule(spec.name)

        def exec_module(self, module):
            pass

    sys.meta_path.insert(0, _Finder())


_stub_qgis()

from tairu_firebase.models import TairuRecord  # noqa: E402
from tairu_sync.push import (  # noqa: E402
    build_writes, PushPlan, PushItem, _feature_attributes_json,
)


class _Field:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _Fields:
    def __init__(self, names):
        self._names = list(names)

    def __iter__(self):
        return iter(_Field(n) for n in self._names)

    def indexOf(self, name):  # noqa: N802 - QGIS API spelling
        return self._names.index(name) if name in self._names else -1


class _Feature:
    """The slice of QgsFeature that _feature_attributes_json touches."""

    def __init__(self, values):
        self._values = dict(values)
        self.fields_ = _Fields(values.keys())

    def fields(self):
        return self.fields_

    def attribute(self, idx):
        return list(self._values.values())[idx]


class TestFeatureAttributes(unittest.TestCase):
    def test_plain_user_columns_are_all_kept(self):
        feature = _Feature({'especie': 'ipê', 'dap_cm': 42, 'saudavel': True})
        self.assertEqual(
            json.loads(_feature_attributes_json(feature)),
            {'especie': 'ipê', 'dap_cm': 42, 'saudavel': True},
        )

    def test_record_mirror_columns_never_leak(self):
        # What ensure_record_layer_fields stamps on a pushed/pulled layer. None of
        # it is user data: it is the record's own fields coming back.
        feature = _Feature({
            'recordId': 'r1', 'nome': 'Ponto 1', 'descricao': 'x',
            'tipoRegistro': 'local', 'subTipo': 'outroLocal', 'situation': 'Ativo',
            'endereco': '', 'owner': '', 'plateTag': '', 'brand': '', 'model': '',
            'year': 0, 'color': '', 'valueEstimate': 0.0, 'size': 0.0,
            'eventDateTime': None, 'geometryColor': '#FFFFFFFF',
            'geometryBackgroundColor': '', 'geometrySize': 3.0, 'circleRadius': None,
            'isDeleted': 0, 'createdBy': 'u', 'createdAt': None, 'lastModified': None,
            'tairuSyncHash': 'abc', 'tairuSyncLastModified': '1',
        })
        self.assertIsNone(_feature_attributes_json(feature))

    def test_user_columns_survive_alongside_the_mirror_columns(self):
        # Second push of a source layer: write-back added the record columns to it.
        feature = _Feature({
            'recordId': 'r1', 'nome': 'Ponto 1', 'tairuSyncHash': 'abc',
            'especie': 'ipê',
        })
        self.assertEqual(json.loads(_feature_attributes_json(feature)),
                         {'especie': 'ipê'})


class _FakeFs:
    def build_update_write(self, path, fields, mask):
        return {'path': path, 'fields': dict(fields), 'mask': list(mask)}

    def build_create_write(self, path, fields):
        return {'path': path, 'fields': dict(fields)}


class TestUpdateMask(unittest.TestCase):
    """`attributes` is neither a layer column nor part of the sync hash, so it never
    reaches changed_fields — build_writes has to add it itself."""

    def _write_for(self, rec, changed_fields=()):
        plan = PushPlan(map_id='m1')
        plan.items.append(PushItem('update', rec, feature_id=1,
                                   changed_fields=list(changed_fields)))
        return build_writes(_FakeFs(), plan, 'uid1')[0]

    def _record(self, attributes=None):
        return TairuRecord(record_id='r1', geometry_type='point',
                           geometry_points_json='[{"la":1.0,"lo":2.0,"ts":0}]',
                           attributes=attributes)

    def test_added_to_mask_even_when_nothing_else_changed(self):
        # The group-promotion path: changed_fields is just ['groupId'].
        w = self._write_for(self._record('{"especie":"ipê"}'), ['groupId'])
        self.assertIn('attributes', w['mask'])
        self.assertEqual(w['fields']['attributes'], '{"especie":"ipê"}')

    def test_absent_candidate_never_clears_the_cloud_value(self):
        w = self._write_for(self._record(None), ['nome'])
        self.assertNotIn('attributes', w['mask'])
        self.assertNotIn('attributes', w['fields'])

    def test_not_duplicated_when_already_in_changed_fields(self):
        w = self._write_for(self._record('{"a":1}'), ['attributes'])
        self.assertEqual(w['mask'].count('attributes'), 1)


if __name__ == '__main__':
    unittest.main()
