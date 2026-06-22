# -*- coding: utf-8 -*-

"""
Compatibility wrapper for the logged-in TairuDB generation flow.

The implementation lives in local_generate_wizard.py so local file generation
and map upload share the same pages, validation, generation and vector export.
"""

try:
    from .local_generate_wizard import TairuDBGenerateWizard, open_raster_wizard
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_ui.local_generate_wizard import TairuDBGenerateWizard, open_raster_wizard


class RasterWizard(TairuDBGenerateWizard):

    def __init__(self, dock, tmap):
        super().__init__(dock.iface, dock=dock, tmap=tmap)
