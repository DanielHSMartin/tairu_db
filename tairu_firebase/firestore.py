# -*- coding: utf-8 -*-

"""
Firestore REST client scoped to what the plugin needs: list the user's maps,
read/write records, and register tairudb files on the map document.

Every call is shaped to satisfy firestore.rules:
- maps are queried with ARRAY_CONTAINS on integrantIdList (the only allowed list);
- record updates never touch createdBy;
- map-doc patches only touch tairuDBRemoteFiles/lastModified/serverTimestamp.
"""

import datetime
import urllib.parse

try:
    from .http import AuthorizedSession
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_firebase.http import AuthorizedSession

_FIRESTORE_HOST = 'https://firestore.googleapis.com/v1'


# ------------------------------------------------------------- value codec

def to_value(py):
    """Encode a Python value as a Firestore Value.

    NOTE: bool must be tested before int (bool is an int subclass), and all
    Python ints become integerValue strings — the app stores millis/ARGB as
    integers and would break on doubleValue.
    """
    if py is None:
        return {'nullValue': None}
    if isinstance(py, bool):
        return {'booleanValue': py}
    if isinstance(py, int):
        return {'integerValue': str(py)}
    if isinstance(py, float):
        return {'doubleValue': py}
    if isinstance(py, str):
        return {'stringValue': py}
    if isinstance(py, datetime.datetime):
        return {'timestampValue': py.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}
    if isinstance(py, (list, tuple)):
        return {'arrayValue': {'values': [to_value(v) for v in py]}}
    if isinstance(py, dict):
        return {'mapValue': {'fields': {k: to_value(v) for k, v in py.items()}}}
    raise TypeError(f'Cannot encode {type(py)} as Firestore value')


def from_value(value):
    """Decode a Firestore Value into a Python value (tolerant of variants)."""
    if value is None:
        return None
    if 'nullValue' in value:
        return None
    if 'booleanValue' in value:
        return bool(value['booleanValue'])
    if 'integerValue' in value:
        return int(value['integerValue'])
    if 'doubleValue' in value:
        return float(value['doubleValue'])
    if 'stringValue' in value:
        return value['stringValue']
    if 'timestampValue' in value:
        return value['timestampValue']  # ISO 8601 string
    if 'arrayValue' in value:
        return [from_value(v) for v in (value['arrayValue'].get('values') or [])]
    if 'mapValue' in value:
        return {k: from_value(v) for k, v in (value['mapValue'].get('fields') or {}).items()}
    if 'referenceValue' in value:
        return value['referenceValue']
    if 'geoPointValue' in value:
        return value['geoPointValue']
    if 'bytesValue' in value:
        return value['bytesValue']
    return None


def fields_to_dict(fields):
    return {k: from_value(v) for k, v in (fields or {}).items()}


def dict_to_fields(py_dict):
    return {k: to_value(v) for k, v in py_dict.items()}


def doc_id_from_name(name):
    """Last path segment of a full document resource name."""
    return name.rsplit('/', 1)[-1]


# ------------------------------------------------------------------ client

class FirestoreClient:

    def __init__(self, env, token_manager, app_check_manager=None):
        self.env = env
        self._session = AuthorizedSession(token_manager, scheme='Bearer',
                                          app_check_manager=app_check_manager)
        self._documents = f'projects/{env.project_id}/databases/(default)/documents'
        self._base = f'{_FIRESTORE_HOST}/{self._documents}'

    def full_name(self, relative_path):
        """'maps/x/records/y' -> full Firestore resource name."""
        return f'{self._documents}/{relative_path}'

    # ---------------------------------------------------------------- maps

    def list_user_maps(self, uid):
        """All map docs where the user is a member. Returns [(map_id, dict)].

        Uses exactly the filter the rules' `allow list` clause requires.
        """
        body = {
            'structuredQuery': {
                'from': [{'collectionId': 'maps'}],
                'where': {
                    'fieldFilter': {
                        'field': {'fieldPath': 'integrantIdList'},
                        'op': 'ARRAY_CONTAINS',
                        'value': {'stringValue': uid},
                    }
                },
            }
        }
        rows = self._session.request_json('POST', f'{self._base}:runQuery', json_body=body)
        result = []
        for row in rows:
            doc = row.get('document')
            if doc:
                result.append((doc_id_from_name(doc['name']), fields_to_dict(doc.get('fields'))))
        return result

    def get_map(self, map_id):
        doc = self._session.request_json('GET', f'{self._base}/maps/{map_id}')
        return fields_to_dict(doc.get('fields'))

    # ------------------------------------------------------------- records

    def list_records(self, map_id, page_size=300, cancel_cb=None):
        """All record docs of a map (paginated). Returns [(record_id, dict)]."""
        result = []
        page_token = None
        while True:
            if cancel_cb and cancel_cb():
                break
            url = f'{self._base}/maps/{map_id}/records?pageSize={page_size}'
            if page_token:
                url += f'&pageToken={urllib.parse.quote(page_token)}'
            data = self._session.request_json('GET', url)
            for doc in data.get('documents', []):
                result.append((doc_id_from_name(doc['name']), fields_to_dict(doc.get('fields'))))
            page_token = data.get('nextPageToken')
            if not page_token:
                break
        return result

    def list_records_since(self, map_id, since_millis, cancel_cb=None):
        """Records with lastModified > since_millis. Returns [(record_id, dict)].

        Includes isDeleted=True entries so incremental pull can remove soft-deleted
        records from the local GeoPackage. Uses a single-field index on lastModified
        (created automatically by Firestore — no manual index needed).
        """
        body = {
            'structuredQuery': {
                'from': [{'collectionId': 'records'}],
                'where': {
                    'fieldFilter': {
                        'field': {'fieldPath': 'lastModified'},
                        'op': 'GREATER_THAN',
                        'value': {'integerValue': str(int(since_millis))},
                    }
                },
                'orderBy': [{'field': {'fieldPath': 'lastModified'}, 'direction': 'ASCENDING'}],
            }
        }
        rows = self._session.request_json(
            'POST', f'{self._base}/maps/{map_id}:runQuery', json_body=body)
        result = []
        for row in rows:
            if cancel_cb and cancel_cb():
                break
            doc = row.get('document')
            if doc:
                result.append((doc_id_from_name(doc['name']),
                                fields_to_dict(doc.get('fields') or {})))
        return result

    def count_records(self, map_id):
        """COUNT aggregation over the records subcollection (best effort).

        Excludes soft-deleted documents so the count matches what pull() receives.
        """
        body = {
            'structuredAggregationQuery': {
                'structuredQuery': {
                    'from': [{'collectionId': 'records'}],
                    'where': {
                        'fieldFilter': {
                            'field': {'fieldPath': 'isDeleted'},
                            'op': 'EQUAL',
                            'value': {'booleanValue': False},
                        }
                    },
                },
                'aggregations': [{'count': {}, 'alias': 'total'}],
            }
        }
        rows = self._session.request_json(
            'POST', f'{self._base}/maps/{map_id}:runAggregationQuery', json_body=body
        )
        for row in rows:
            agg = row.get('result', {}).get('aggregateFields', {})
            if 'total' in agg:
                return from_value(agg['total'])
        return None

    # -------------------------------------------------------------- writes

    def commit(self, writes):
        """Atomic batch of writes (max 500; callers batch at 100)."""
        return self._session.request_json('POST', f'{self._base}:commit',
                                          json_body={'writes': writes})

    def build_create_write(self, relative_path, py_fields, server_timestamp_field='serverTimestamp'):
        """Write op creating a new document (fails if it already exists)."""
        write = {
            'update': {
                'name': self.full_name(relative_path),
                'fields': dict_to_fields(py_fields),
            },
            'currentDocument': {'exists': False},
        }
        if server_timestamp_field:
            write['updateTransforms'] = [
                {'fieldPath': server_timestamp_field, 'setToServerValue': 'REQUEST_TIME'}
            ]
        return write

    def build_update_write(self, relative_path, py_fields, mask_fields,
                           server_timestamp_field='serverTimestamp'):
        """Write op patching only mask_fields of an existing document."""
        write = {
            'update': {
                'name': self.full_name(relative_path),
                'fields': dict_to_fields(py_fields),
            },
            'updateMask': {'fieldPaths': list(mask_fields)},
            'currentDocument': {'exists': True},
        }
        if server_timestamp_field:
            write['updateTransforms'] = [
                {'fieldPath': server_timestamp_field, 'setToServerValue': 'REQUEST_TIME'}
            ]
        return write

    def build_array_append_write(self, relative_path, array_field, values, extra_py_fields=None,
                                 server_timestamp_field='serverTimestamp'):
        """Write op appending values to an array field via appendMissingElements.

        extra_py_fields are patched alongside (listed in the updateMask so
        nothing else on the document is touched).
        """
        extra_py_fields = extra_py_fields or {}
        write = {
            'update': {
                'name': self.full_name(relative_path),
                'fields': dict_to_fields(extra_py_fields),
            },
            'updateMask': {'fieldPaths': list(extra_py_fields.keys())},
            'currentDocument': {'exists': True},
            'updateTransforms': [
                {
                    'fieldPath': array_field,
                    'appendMissingElements': {'values': [to_value(v) for v in values]},
                }
            ],
        }
        if server_timestamp_field:
            write['updateTransforms'].append(
                {'fieldPath': server_timestamp_field, 'setToServerValue': 'REQUEST_TIME'}
            )
        return write
