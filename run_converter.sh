#!/bin/bash
# GeoPDF to TairuDB Converter Runner
# This script sets up the environment and runs the converter with QGIS's Python

# Set QGIS paths
export QGIS_PREFIX_PATH="/Applications/QGIS-LTR.app/Contents/MacOS"
export PYTHONPATH="${QGIS_PREFIX_PATH}/../Resources/python:${QGIS_PREFIX_PATH}/lib/python3.9/site-packages:$PYTHONPATH"
export DYLD_LIBRARY_PATH="${QGIS_PREFIX_PATH}/lib:$DYLD_LIBRARY_PATH"
export PATH="${QGIS_PREFIX_PATH}/bin:$PATH"

# Set PROJ database path for pyproj
export PROJ_LIB="/Applications/QGIS-LTR.app/Contents/Resources/proj"

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run the converter with QGIS's Python
"${QGIS_PREFIX_PATH}/bin/python3" "${SCRIPT_DIR}/geopdf_converter.py" "$@"
