# -*- coding: utf-8 -*-

"""Helpers for mapping source vector layers to TairuDB feature types."""

ELEVATION_FIELD_NAME = "ELEV"


def has_elevation_attribute(field_names):
    """Return True when a layer attribute table exposes an ELEV field."""
    return any(str(name).strip().upper() == ELEVATION_FIELD_NAME for name in field_names)


def tairudb_type_for_fields(default_type, field_names):
    """Contour layers (ELEV field present) map to 'line'; other layers use default_type."""
    if has_elevation_attribute(field_names):
        return "line"
    return default_type
