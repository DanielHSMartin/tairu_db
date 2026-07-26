# -*- coding: utf-8 -*-
"""Stable identity for an exported .tairudb map.

The app needs to know whether an incoming file is a NEW map or a newer version
of one it already has. File name cannot answer that: messengers rename a second
copy to "mapa (1).tairudb", and two unrelated maps can legitimately share a name
— matching by name would silently replace the wrong one.

So the exporter stamps `metadata.map_uuid`, minted once and then reused. It is
kept as a QGIS **project** custom property, which is saved inside the .qgs/.qgz,
so it survives closing QGIS and travels with the project.

Keyed by the chosen OUTPUT NAME within the project, not by the project alone:
one project routinely exports several distinct maps (different areas, different
layer sets), and a single per-project id would make the app treat them as
versions of each other. The name is the user's statement of "this is the same
map"; the uuid is what actually travels and what the app matches on — so
renaming the file in transit no longer breaks the link.

Degradation: exporting from a project that was never saved has nowhere to
persist the property, so each export mints a fresh id and the app falls back to
its previous name-based behaviour. No worse than before, never wrong.
"""

import os
import re
import uuid

try:
    from qgis.core import QgsProject
except ImportError:  # pragma: no cover - allows importing outside QGIS
    QgsProject = None


_SCOPE = 'tairu_db'
_KEY_PREFIX = 'export_uuid'


def _slug(value):
    """Filesystem-independent key for an output name.

    The stored key must not change when the same map is written to another
    folder, so only the base name matters, lowercased, with anything unusual
    folded to '_'.
    """
    base = os.path.basename(value or '')
    base = os.path.splitext(base)[0]
    return re.sub(r'[^a-z0-9]+', '_', base.lower()).strip('_') or 'mapa'


def feature_uuid_for(layer, feature):
    """Stable uuid for a source feature, so re-exporting yields the SAME id.

    Without this every export minted a fresh uuid4, so a re-exported map had
    nothing in common with the previous one — and any feature the user had
    already incorporated into the expedition rendered TWICE: once as their
    record, once as the "new" file feature.

    Derived from the layer id (assigned by QGIS at layer creation and saved in
    the project, so it survives renaming the layer and reopening the project)
    plus the provider feature id (rowid / primary key for GeoPackage, SpatiaLite
    and PostGIS; record index for shapefile).

    Falls back to uuid4 — i.e. exactly the old behaviour — whenever the key
    can't be trusted: a negative fid (feature not yet committed to the
    provider), a layer with no id, or any API surprise.
    """
    try:
        layer_id = layer.id()
        fid = feature.id()
    except Exception:
        return str(uuid.uuid4())
    if not layer_id or fid is None or fid < 0:
        return str(uuid.uuid4())
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'tairudb://feature/{layer_id}/{fid}'))


def map_uuid_for_output(output_name, project=None):
    """Return the stable map uuid for [output_name], minting it on first use.

    Returns a fresh (unpersisted) uuid when there is no project to store it in,
    so callers always get a usable value.
    """
    key = f'{_KEY_PREFIX}/{_slug(output_name)}'

    instance = project
    if instance is None and QgsProject is not None:
        try:
            instance = QgsProject.instance()
        except Exception:
            instance = None
    if instance is None:
        return str(uuid.uuid4())

    try:
        existing, ok = instance.readEntry(_SCOPE, key, '')
        if ok and existing:
            return existing
    except Exception:
        # A project that cannot be read is not a reason to fail an export.
        return str(uuid.uuid4())

    minted = str(uuid.uuid4())
    try:
        instance.writeEntry(_SCOPE, key, minted)
    except Exception:
        # Not persisted: the next export mints another one and the app falls
        # back to matching by name, exactly as it did before this existed.
        pass
    return minted
