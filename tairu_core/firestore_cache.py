# -*- coding: utf-8 -*-

"""
Firestore-shaped local cache for QGIS sync.

The cache stores each document as the same plain field dict used by the plugin's
Firestore REST codec, plus queryable metadata. GeoPackage layers remain the
editable QGIS surface; this cache is the durable canonical mirror that survives
QGIS restarts and can later be shared with app-side snapshot/export tooling.
"""

import hashlib
import json
import sqlite3
from contextlib import closing

try:
    from .workspace import firestore_cache_path
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_core.workspace import firestore_cache_path

try:
    from ..tairu_firebase.models import parse_millis
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_firebase.models import parse_millis


SCHEMA_VERSION = 2
MAPS_COLLECTION = 'maps'
RECORDS_COLLECTION = 'records'


def _collection_path(map_id=None, collection=None):
    if not map_id:
        return MAPS_COLLECTION
    if not collection:
        return f'maps/{map_id}'
    return f'maps/{map_id}/{collection}'


def _canonical_json(fields):
    return json.dumps(fields or {}, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _sync_hash(fields):
    return hashlib.sha256(_canonical_json(fields).encode('utf-8')).hexdigest()


def _is_deleted(fields):
    return 1 if bool((fields or {}).get('isDeleted')) else 0


def _last_modified_ms(fields):
    return parse_millis((fields or {}).get('lastModified'))


def _server_timestamp_ms(fields):
    return parse_millis((fields or {}).get('serverTimestamp'))


class FirestoreCache:
    """Small SQLite cache scoped by Firebase env and user id."""

    def __init__(self, env_key, user_id):
        self.env_key = env_key or ''
        self.user_id = user_id or ''
        self.db_path = firestore_cache_path(self.env_key)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA foreign_keys=ON;')
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        existing_version = 0
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key = 'schema_version';"
        ).fetchone()
        if row:
            try:
                existing_version = int(row[0] or 0)
            except (TypeError, ValueError):
                existing_version = 0
        conn.execute("""
            CREATE TABLE IF NOT EXISTS firestore_entities (
                env_key TEXT NOT NULL,
                user_id TEXT NOT NULL,
                map_id TEXT NOT NULL DEFAULT '',
                collection_path TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                last_modified_ms INTEGER NOT NULL DEFAULT 0,
                server_timestamp_ms INTEGER NOT NULL DEFAULT 0,
                sync_hash TEXT NOT NULL,
                fetched_at_ms INTEGER NOT NULL DEFAULT 0,
                origin TEXT NOT NULL DEFAULT 'firestore',
                PRIMARY KEY (env_key, user_id, collection_path, doc_id)
            );
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_firestore_entities_map_collection
            ON firestore_entities(env_key, user_id, map_id, collection_path, is_deleted);
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_firestore_entities_last_modified
            ON firestore_entities(env_key, user_id, collection_path, last_modified_ms);
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_firestore_entities_server_timestamp
            ON firestore_entities(env_key, user_id, collection_path, server_timestamp_ms);
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                env_key TEXT NOT NULL,
                user_id TEXT NOT NULL,
                map_id TEXT NOT NULL DEFAULT '',
                collection_path TEXT NOT NULL,
                high_watermark_ms INTEGER NOT NULL DEFAULT 0,
                last_full_sync_ms INTEGER NOT NULL DEFAULT 0,
                last_delta_sync_ms INTEGER NOT NULL DEFAULT 0,
                cache_schema_version INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (env_key, user_id, map_id, collection_path)
            );
        """)
        if 0 < existing_version < 2:
            # v1 used the local QGIS pull time and a lastModified integer query
            # as the incremental cursor. That can miss app-side edits, so all
            # v1 cursors must be rebuilt from a safe full pull.
            conn.execute("DELETE FROM sync_state;")
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES ('schema_version', ?);",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    def load_maps(self):
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT doc_id, payload_json
                FROM firestore_entities
                WHERE env_key = ?
                  AND user_id = ?
                  AND collection_path = ?
                  AND is_deleted = 0
                ORDER BY doc_id ASC
                """,
                (self.env_key, self.user_id, MAPS_COLLECTION),
            ).fetchall()
        decoded = [(doc_id, json.loads(payload_json)) for doc_id, payload_json in rows]
        return sorted(decoded, key=lambda row: (row[1].get('nome') or '').lower())

    def store_maps(self, rows, fetched_at_ms):
        incoming = set()
        with closing(self._connect()) as conn:
            with conn:
                for doc_id, fields in rows:
                    doc_id = str(doc_id or fields.get('mapId') or '')
                    if not doc_id:
                        continue
                    incoming.add(doc_id)
                    self._put_entity(
                        conn,
                        map_id=doc_id,
                        collection_path=MAPS_COLLECTION,
                        doc_id=doc_id,
                        fields=fields,
                        fetched_at_ms=fetched_at_ms,
                    )
                cached = conn.execute(
                    """
                    SELECT doc_id FROM firestore_entities
                    WHERE env_key = ? AND user_id = ? AND collection_path = ?
                    """,
                    (self.env_key, self.user_id, MAPS_COLLECTION),
                ).fetchall()
                for (doc_id,) in cached:
                    if doc_id not in incoming:
                        conn.execute(
                            """
                            UPDATE firestore_entities
                            SET is_deleted = 1, fetched_at_ms = ?
                            WHERE env_key = ?
                              AND user_id = ?
                              AND collection_path = ?
                              AND doc_id = ?
                            """,
                            (fetched_at_ms, self.env_key, self.user_id, MAPS_COLLECTION, doc_id),
                        )

    def load_records(self, map_id, include_deleted=False):
        collection_path = _collection_path(map_id, RECORDS_COLLECTION)
        params = [self.env_key, self.user_id, collection_path]
        # Two static queries avoid f-string SQL construction (flagged by security scanners).
        if include_deleted:
            sql = (
                "SELECT doc_id, payload_json"
                " FROM firestore_entities"
                " WHERE env_key = ? AND user_id = ? AND collection_path = ?"
                " ORDER BY last_modified_ms ASC, doc_id ASC"
            )
        else:
            sql = (
                "SELECT doc_id, payload_json"
                " FROM firestore_entities"
                " WHERE env_key = ? AND user_id = ? AND collection_path = ? AND is_deleted = 0"
                " ORDER BY last_modified_ms ASC, doc_id ASC"
            )
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(doc_id, json.loads(payload_json)) for doc_id, payload_json in rows]

    def store_records(self, map_id, rows, fetched_at_ms, full_snapshot=False):
        collection_path = _collection_path(map_id, RECORDS_COLLECTION)
        incoming = set()
        with closing(self._connect()) as conn:
            with conn:
                for doc_id, fields in rows:
                    doc_id = str(doc_id or fields.get('recordId') or fields.get('targetId') or '')
                    if not doc_id:
                        continue
                    incoming.add(doc_id)
                    self._put_entity(
                        conn,
                        map_id=map_id,
                        collection_path=collection_path,
                        doc_id=doc_id,
                        fields=fields,
                        fetched_at_ms=fetched_at_ms,
                    )
                if full_snapshot:
                    cached = conn.execute(
                        """
                        SELECT doc_id FROM firestore_entities
                        WHERE env_key = ?
                          AND user_id = ?
                          AND collection_path = ?
                        """,
                        (self.env_key, self.user_id, collection_path),
                    ).fetchall()
                    for (doc_id,) in cached:
                        if doc_id not in incoming:
                            conn.execute(
                                """
                                DELETE FROM firestore_entities
                                WHERE env_key = ?
                                  AND user_id = ?
                                  AND collection_path = ?
                                  AND doc_id = ?
                                """,
                                (self.env_key, self.user_id, collection_path, doc_id),
                            )

    def store_record_models(self, map_id, records, fetched_at_ms):
        rows = []
        for rec in records:
            if not rec or not getattr(rec, 'record_id', ''):
                continue
            rows.append((rec.record_id, rec.to_fields()))
        self.store_records(map_id, rows, fetched_at_ms, full_snapshot=False)

    def record_counts(self):
        collection_suffix = '/records'
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT s.map_id, COUNT(e.doc_id)
                FROM sync_state s
                LEFT JOIN firestore_entities e
                  ON e.env_key = s.env_key
                 AND e.user_id = s.user_id
                 AND e.map_id = s.map_id
                 AND e.collection_path = s.collection_path
                 AND e.is_deleted = 0
                WHERE s.env_key = ?
                  AND s.user_id = ?
                  AND s.collection_path LIKE ?
                  AND s.last_full_sync_ms > 0
                GROUP BY s.map_id
                """,
                (self.env_key, self.user_id, f'maps/%{collection_suffix}'),
            ).fetchall()
        return {map_id: count for map_id, count in rows}

    def load_sync_state(self, map_id, collection):
        collection_path = _collection_path(map_id, collection)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT high_watermark_ms, last_full_sync_ms, last_delta_sync_ms
                FROM sync_state
                WHERE env_key = ?
                  AND user_id = ?
                  AND map_id = ?
                  AND collection_path = ?
                """,
                (self.env_key, self.user_id, map_id, collection_path),
            ).fetchone()
        if not row:
            return {
                'high_watermark_ms': 0,
                'last_full_sync_ms': 0,
                'last_delta_sync_ms': 0,
            }
        return {
            'high_watermark_ms': int(row[0] or 0),
            'last_full_sync_ms': int(row[1] or 0),
            'last_delta_sync_ms': int(row[2] or 0),
        }

    def load_high_watermark(self, map_id, collection):
        return self.load_sync_state(map_id, collection)['high_watermark_ms']

    def save_sync_state(self, map_id, collection, high_watermark_ms, full_snapshot=False):
        collection_path = _collection_path(map_id, collection)
        last_full = int(high_watermark_ms) if full_snapshot else 0
        last_delta = 0 if full_snapshot else int(high_watermark_ms)
        with closing(self._connect()) as conn:
            with conn:
                existing = conn.execute(
                    """
                    SELECT last_full_sync_ms, last_delta_sync_ms
                    FROM sync_state
                    WHERE env_key = ?
                      AND user_id = ?
                      AND map_id = ?
                      AND collection_path = ?
                    """,
                    (self.env_key, self.user_id, map_id, collection_path),
                ).fetchone()
                if existing:
                    last_full = last_full or int(existing[0] or 0)
                    last_delta = last_delta or int(existing[1] or 0)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sync_state(
                        env_key, user_id, map_id, collection_path,
                        high_watermark_ms, last_full_sync_ms, last_delta_sync_ms,
                        cache_schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.env_key,
                        self.user_id,
                        map_id,
                        collection_path,
                        int(high_watermark_ms),
                        last_full,
                        last_delta,
                        SCHEMA_VERSION,
                    ),
                )

    def _put_entity(self, conn, map_id, collection_path, doc_id, fields, fetched_at_ms):
        fields = dict(fields or {})
        payload_json = _canonical_json(fields)
        conn.execute(
            """
            INSERT OR REPLACE INTO firestore_entities(
                env_key, user_id, map_id, collection_path, doc_id,
                payload_json, is_deleted, last_modified_ms, server_timestamp_ms,
                sync_hash, fetched_at_ms, origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.env_key,
                self.user_id,
                map_id or '',
                collection_path,
                doc_id,
                payload_json,
                _is_deleted(fields),
                _last_modified_ms(fields),
                _server_timestamp_ms(fields),
                _sync_hash(fields),
                int(fetched_at_ms or 0),
                'firestore',
            ),
        )
