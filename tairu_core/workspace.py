# -*- coding: utf-8 -*-

"""
Managed local workspace for cloud-synced data:
{QGIS settings dir}/tairu_workspace/{env}/{mapId}/
    records.gpkg   — pulled records as editable layers
    downloads/     — .tairudb files fetched from Storage
    mbtiles/       — per-region MBTiles converted for display
    out/           — .tairudb files generated for upload
"""

import os

from qgis.core import QgsApplication


def workspace_root(env_key):
    return os.path.join(QgsApplication.qgisSettingsDirPath(), 'tairu_workspace', env_key)


def map_workspace(env_key, map_id):
    """Returns the per-map directory paths, creating them on first use."""
    base = os.path.join(workspace_root(env_key), map_id)
    paths = {
        'base': base,
        'gpkg': os.path.join(base, 'records.gpkg'),
        'downloads': os.path.join(base, 'downloads'),
        'mbtiles': os.path.join(base, 'mbtiles'),
        'out': os.path.join(base, 'out'),
    }
    for key in ('base', 'downloads', 'mbtiles', 'out'):
        os.makedirs(paths[key], exist_ok=True)
    return paths


def slugify_filename(name, fallback='arquivo'):
    """ASCII-safe object/file name for Storage paths."""
    import re
    import unicodedata
    normalized = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', normalized).strip('._-')
    return cleaned or fallback
