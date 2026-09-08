# -*- coding: utf-8 -*-

"""
Managed local workspace for cloud-synced data:
{QGIS settings dir}/tairu_workspace/{env}/
    firestore_cache.sqlite - Firestore-shaped canonical cache
{QGIS settings dir}/tairu_workspace/{env}/{mapId}/
    records.gpkg   — pulled records as editable layers
    downloads/     — .tairudb files fetched from Storage
    mbtiles/       — per-region MBTiles converted for display
    out/           — .tairudb files generated for upload
    last_pull.json — epoch-ms of last successful incremental pull
"""

import json
import os

from qgis.core import QgsApplication, QgsSettings


WORKSPACE_DIR_NAME = 'tairu_workspace'
GPKG_FILE_NAME = 'records.gpkg'


def workspace_root(env_key):
    root = os.path.join(QgsApplication.qgisSettingsDirPath(), WORKSPACE_DIR_NAME, env_key)
    os.makedirs(root, exist_ok=True)
    return root


def firestore_cache_path(env_key):
    """Path to the per-environment Firestore-shaped SQLite cache."""
    return os.path.join(workspace_root(env_key), 'firestore_cache.sqlite')


def map_workspace(env_key, map_id):
    """Returns the per-map directory paths, creating them on first use."""
    base = os.path.join(workspace_root(env_key), map_id)
    paths = {
        'base': base,
        'gpkg': os.path.join(base, GPKG_FILE_NAME),
        'downloads': os.path.join(base, 'downloads'),
        'mbtiles': os.path.join(base, 'mbtiles'),
        'out': os.path.join(base, 'out'),
    }
    for key in ('base', 'downloads', 'mbtiles', 'out'):
        os.makedirs(paths[key], exist_ok=True)
    return paths


_LAST_PULL_FILE = 'last_pull.json'


def load_last_pull_ts(paths):
    """Return epoch-ms of the last successful pull, or 0 if none recorded."""
    p = os.path.join(paths['base'], _LAST_PULL_FILE)
    try:
        with open(p, encoding='utf-8') as f:
            return int(json.load(f).get('ts', 0))
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def save_last_pull_ts(paths, ts):
    """Persist ts (epoch-ms) as the timestamp of the last successful pull."""
    p = os.path.join(paths['base'], _LAST_PULL_FILE)
    try:
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'ts': ts}, f)
    except OSError:
        pass


# Quando esta maquina abriu cada expedicao pela ultima vez — a ordem da lista,
# espelhando lastActivityMillisFor do app (ultimo acesso local, com o lastModified
# da expedicao como reserva para a que nunca foi aberta aqui). Fica em QgsSettings, e
# nao no workspace, porque ler um arquivo por expedicao criaria o diretorio de TODAS
# elas so para desenhar a lista.
_LAST_OPENED_KEY = 'tairu_db/lastOpened'


def mark_map_opened(env_key, map_id, ts):
    """Registra que a expedicao foi aberta agora (epoch-ms)."""
    if not env_key or not map_id:
        return
    QgsSettings().setValue(f'{_LAST_OPENED_KEY}/{env_key}/{map_id}', str(int(ts)))


def map_last_opened_ms(env_key, map_id):
    """Epoch-ms da ultima abertura desta expedicao nesta maquina, ou 0."""
    if not env_key or not map_id:
        return 0
    try:
        return int(QgsSettings().value(f'{_LAST_OPENED_KEY}/{env_key}/{map_id}', 0) or 0)
    except (TypeError, ValueError):
        return 0


def slugify_filename(name, fallback='arquivo'):
    """ASCII-safe object/file name for Storage paths."""
    import re
    import unicodedata
    normalized = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', normalized).strip('._-')
    return cleaned or fallback
