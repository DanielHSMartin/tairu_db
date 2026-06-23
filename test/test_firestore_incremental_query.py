import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tairu_firebase.firestore import FirestoreClient, timestamp_value_from_millis


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request_json(self, method, url, json_body=None):
        self.calls.append((method, url, json_body))
        return self.response


class TestFirestoreIncrementalQuery(unittest.TestCase):
    def test_timestamp_cursor_uses_firestore_timestamp_value(self):
        self.assertEqual(
            timestamp_value_from_millis(1234),
            {'timestampValue': '1970-01-01T00:00:01.234Z'},
        )

    def test_list_records_since_queries_server_timestamp(self):
        client = FirestoreClient.__new__(FirestoreClient)
        client._base = 'https://example.test/v1/projects/p/databases/(default)/documents'
        client._session = _FakeSession([
            {
                'document': {
                    'name': 'projects/p/databases/(default)/documents/maps/map-a/records/rec-a',
                    'fields': {
                        'recordId': {'stringValue': 'rec-a'},
                        'serverTimestamp': {
                            'timestampValue': '2026-06-23T10:00:00.000Z',
                        },
                    },
                },
            },
        ])

        rows = client.list_records_since('map-a', 1234)

        self.assertEqual(rows[0][0], 'rec-a')
        method, url, body = client._session.calls[0]
        self.assertEqual(method, 'POST')
        self.assertTrue(url.endswith('/maps/map-a:runQuery'))
        query = body['structuredQuery']
        field_filter = query['where']['fieldFilter']
        self.assertEqual(field_filter['field']['fieldPath'], 'serverTimestamp')
        self.assertEqual(field_filter['value'], {'timestampValue': '1970-01-01T00:00:01.234Z'})
        self.assertEqual(query['orderBy'][0]['field']['fieldPath'], 'serverTimestamp')


if __name__ == '__main__':
    unittest.main()
