# -*- coding: utf-8 -*-

"""Helpers for mapping source vector layers to TairuDB feature types."""

CONTOUR_LINE_TYPE = "contourLine"
ELEVATION_FIELD_NAME = "ELEV"


def has_elevation_attribute(field_names):
    """Return True when a layer attribute table exposes an ELEV field."""
    return any(str(name).strip().upper() == ELEVATION_FIELD_NAME for name in field_names)


def tairudb_type_for_fields(default_type, field_names):
    """Use contourLine for layers carrying ELEV attributes."""
    if has_elevation_attribute(field_names):
        return CONTOUR_LINE_TYPE
    return default_type
