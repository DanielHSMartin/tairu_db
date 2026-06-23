import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _QgsApplicationStub:
    settings_dir = ''

    @staticmethod
    def qgisSettingsDirPath():
        return _QgsApplicationStub.settings_dir


def _install_qgis_stub(settings_dir):
    _QgsApplicationStub.settings_dir = settings_dir
    qgis_mod = types.ModuleType('qgis')
    core_mod = types.ModuleType('qgis.core')
    core_mod.QgsApplication = _QgsApplicationStub
    sys.modules['qgis'] = qgis_mod
    sys.modules['qgis.core'] = core_mod


class TestFirestoreCache(unittest.TestCase):
    def test_v1_sync_state_is_invalidated_but_records_are_preserved(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as settings_dir:
            _install_qgis_stub(settings_dir)
            from tairu_core.firestore_cache import FirestoreCache, RECORDS_COLLECTION

            cache = FirestoreCache('prod', 'user-a')
            cache.store_records(
                'map-a',
                [('rec-a', {'recordId': 'rec-a', 'isDeleted': False, 'serverTimestamp': 1000})],
                1000,
                full_snapshot=True,
            )
            cache.save_sync_state('map-a', RECORDS_COLLECTION, 1000, full_snapshot=True)

            with sqlite3.connect(cache.db_path) as conn:
                conn.execute(
                    "UPDATE cache_meta SET value = '1' WHERE key = 'schema_version';"
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sync_state(
                        env_key, user_id, map_id, collection_path,
                        high_watermark_ms, last_full_sync_ms, last_delta_sync_ms,
                        cache_schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ('prod', 'user-a', 'map-a', 'maps/map-a/records', 9999, 9999, 0, 1),
                )

            self.assertEqual(
                cache.load_sync_state('map-a', RECORDS_COLLECTION),
                {'high_watermark_ms': 0, 'last_full_sync_ms': 0, 'last_delta_sync_ms': 0},
            )
            self.assertEqual(cache.load_records('map-a')[0][0], 'rec-a')

    def test_counts_only_use_full_snapshots_and_support_zero_records(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as settings_dir:
            _install_qgis_stub(settings_dir)
            from tairu_core.firestore_cache import FirestoreCache, RECORDS_COLLECTION

            cache = FirestoreCache('prod', 'user-a')
            cache.store_records(
                'partial',
                [('rec-a', {'recordId': 'rec-a', 'isDeleted': False, 'serverTimestamp': 1000})],
                1000,
                full_snapshot=False,
            )
            cache.save_sync_state('partial', RECORDS_COLLECTION, 1000, full_snapshot=False)
            self.assertEqual(cache.record_counts(), {})

            cache.store_records('empty-full', [], 2000, full_snapshot=True)
            cache.save_sync_state('empty-full', RECORDS_COLLECTION, 2000, full_snapshot=True)
            self.assertEqual(cache.record_counts()['empty-full'], 0)

            cache.store_records(
                'full',
                [('rec-a', {'recordId': 'rec-a', 'isDeleted': False, 'serverTimestamp': 3000})],
                3000,
                full_snapshot=True,
            )
            cache.save_sync_state('full', RECORDS_COLLECTION, 3000, full_snapshot=True)
            self.assertEqual(cache.record_counts()['full'], 1)

            cache.store_records(
                'full',
                [('rec-a', {'recordId': 'rec-a', 'isDeleted': True, 'serverTimestamp': 4000})],
                4000,
                full_snapshot=False,
            )
            self.assertEqual(cache.record_counts()['full'], 0)


if __name__ == '__main__':
    unittest.main()
