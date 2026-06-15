#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Processing algorithm wrapper for the TairuDB generator.

Since plugin 2.0 the actual work lives in the tairu_core package
(tile_math, generator, vector_export, tairudb_writer); this module only
parses Processing parameters and delegates, so the same engine can also be
driven by the Tairu Maps dock widget. The algorithm id, parameter names and
outputs are unchanged from 1.x ('Tairu:tairudbgenerator').

TairuDBWriter, MetaTile and qvariant_to_python are re-exported here for
backward compatibility (geopdf_converter.py and the test suite import them
from this module).
"""

import os

from qgis.core import QgsProcessingException  # type: ignore
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterBoolean,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsProcessingParameterMultipleLayers,
)

try:
    from .compat import _RASTER_LAYER_TYPE, _FLAG_NO_THREADING
    from .tairu_core.feedback import ProcessingFeedbackAdapter
    from .tairu_core.tairudb_writer import TairuDBWriter, MetaTile  # noqa: F401 (re-export)
    from .tairu_core.tile_math import compute_region_tiles
    from .tairu_core.generator import (
        GenerationSpec,
        TileRenderEngine,
        estimate,
        format_estimate_report,
    )
    from .tairu_core.vector_export import qvariant_to_python, export_vector_layers  # noqa: F401 (re-export)
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _RASTER_LAYER_TYPE, _FLAG_NO_THREADING
    from tairu_core.feedback import ProcessingFeedbackAdapter
    from tairu_core.tairudb_writer import TairuDBWriter, MetaTile  # noqa: F401
    from tairu_core.tile_math import compute_region_tiles
    from tairu_core.generator import (
        GenerationSpec,
        TileRenderEngine,
        estimate,
        format_estimate_report,
    )
    from tairu_core.vector_export import qvariant_to_python, export_vector_layers  # noqa: F401


def TairuDBAlgorithm():
    """
    This algorithm generates XYZ tiles of map canvas content with
    enhanced features and saves them as a TairuDB file, and can also export selected vector layers.
    """

    # Define constants for parameter names
    EXTENT_POLYGON = 'EXTENT_POLYGON'
    MAP_RESOLUTION = 'MAP_RESOLUTION'
    TILE_FORMAT = 'TILE_FORMAT'
    QUALITY = 'QUALITY'
    VECTOR_LAYERS = 'VECTOR_LAYERS'
    DRY_RUN = 'DRY_RUN'
    OUTPUT_FILE = 'OUTPUT_FILE'

    class Algorithm(QgsProcessingAlgorithm):
        def __init__(self):
            super().__init__()
            self.wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")

            try:
                cpu_count = os.cpu_count() or 4
            except Exception:
                cpu_count = 4
            self.threads_number = min(cpu_count, 4)  # Limit concurrent jobs to prevent memory issues

            self.max_zoom = 18
            self.tile_format = "PNG"
            self.jpg_quality = 75
            self.layers = []
            self.selected_vector_layers = []
            self.transform_context = None
            self.region_result = None

        def name(self):
            return "tairudbgenerator"

        def displayName(self):
            return self.tr("TairuDB")

        def group(self):
            return ""

        def shortHelpString(self):
            return self.tr("Gera um arquivo TairuDB com dos dados do projeto atual e exporta camadas vetoriais selecionadas.")

        def createInstance(self):
            return TairuDBAlgorithm()

        def flags(self):
            return _FLAG_NO_THREADING

        def tr(self, text):
            return text

        def initAlgorithm(self, configuration=None):  # pylint: disable=unused-argument
            self.addParameter(QgsProcessingParameterBoolean(
                DRY_RUN,
                self.tr("Simulação — calcular estatísticas sem gerar arquivo"),
                defaultValue=False,
                optional=False,
            ))

            self.addParameter(QgsProcessingParameterFeatureSource(
                EXTENT_POLYGON,
                self.tr("Área de interesse (polígono)"),
                [QgsProcessing.TypeVectorPolygon],
                optional=False
            ))

            map_resolutions = [
                self.tr("Altíssima (0,5 m/px)"),
                self.tr("Alta (1 m/px)"),
                self.tr("Médio Alta (2 m/px)"),
                self.tr("Média (4 m/px)"),
                self.tr("Médio Baixa (8 m/px)"),
                self.tr("Baixa (16 m/px)"),
                self.tr("Muito Baixa (32 m/px)"),
            ]

            self.addParameter(QgsProcessingParameterEnum(
                MAP_RESOLUTION,
                self.tr("Resolução do mapa (metros/pixel)"),
                options=map_resolutions,
                defaultValue=0,
                optional=False
            ))

            tile_formats = [self.tr("PNG"), self.tr("JPG"), self.tr("WebP")]
            self.addParameter(QgsProcessingParameterEnum(
                TILE_FORMAT,
                self.tr("Formato da imagem"),
                options=tile_formats,
                defaultValue=1,
                optional=False
            ))

            self.addParameter(QgsProcessingParameterNumber(
                QUALITY,
                self.tr("Qualidade (apenas JPG/WebP)"),
                QgsProcessingParameterNumber.Integer,
                90,
                False,
                1,
                100
            ))

            self.addParameter(QgsProcessingParameterMultipleLayers(
                VECTOR_LAYERS,
                self.tr("Camadas vetoriais para exportar"),
                layerType=QgsProcessing.TypeVectorAnyGeometry,
                optional=True,
            ))

            self.addParameter(QgsProcessingParameterFileDestination(
                OUTPUT_FILE,
                self.tr("Arquivo de saída"),
                fileFilter="Arquivo TairuDB (*.tairudb)",
                optional=True,
            ))

        def prepareAlgorithm(self, parameters, context, feedback):
            self.region_result = None

            if feedback.isCanceled():
                return False

            # Get vector layers to export
            self.selected_vector_layers = []
            vector_layer_ids = self.parameterAsLayerList(parameters, VECTOR_LAYERS, context)
            if vector_layer_ids:
                if all(hasattr(l, "isValid") for l in vector_layer_ids):
                    self.selected_vector_layers = [l for l in vector_layer_ids if l.isValid()]
                else:
                    all_layers = QgsProject.instance().mapLayers()
                    for lid in vector_layer_ids:
                        if feedback.isCanceled():
                            return False
                        lyr = all_layers.get(lid)
                        if lyr and lyr.isValid():
                            self.selected_vector_layers.append(lyr)

            # --- Get polygon geometry for extent ---
            source = self.parameterAsSource(parameters, EXTENT_POLYGON, context)
            features = list(source.getFeatures())
            if not features:
                feedback.reportError(self.tr("Nenhuma feição encontrada na camada de entrada."))
                return False

            if feedback.isCanceled():
                return False

            # Get parameters
            map_resolution_idx = self.parameterAsEnum(parameters, MAP_RESOLUTION, context)
            tile_format_idx = self.parameterAsEnum(parameters, TILE_FORMAT, context)
            self.jpg_quality = self.parameterAsInt(parameters, QUALITY, context)

            # Set zoom level based on map resolution
            map_resolution_formats = [18, 17, 16, 15, 14, 13, 12]
            self.max_zoom = map_resolution_formats[map_resolution_idx]
            feedback.pushInfo(self.tr(f"Zoom máximo selecionado: {self.max_zoom}"))

            # Set tile format
            tile_formats = ["PNG", "JPG", "WEBP"]
            self.tile_format = tile_formats[tile_format_idx]

            # Get layers from current project
            self.layers = [layer for layer in QgsProject.instance().mapLayers().values()
               if QgsProject.instance().layerTreeRoot().findLayer(layer.id()) and
               QgsProject.instance().layerTreeRoot().findLayer(layer.id()).isVisible() and
               layer.type() in [_RASTER_LAYER_TYPE]]

            if not self.layers:
                feedback.reportError(self.tr("Nenhuma camada encontrada para renderizar."))
                return False

            self.transform_context = context.transformContext()
            source_crs = source.sourceCrs() if hasattr(source, "sourceCrs") else context.project().crs()
            src2wgs = QgsCoordinateTransform(source_crs, self.wgs84_crs, self.transform_context)

            feedback.pushInfo(self.tr(f"CRS do polígono: {source_crs.authid()}"))

            # Transform each feature's polygon to WGS84 and compute its tile set
            polygons_wgs84 = []
            for feature in features:
                if feedback.isCanceled():
                    return False
                polygon_geom = feature.geometry()
                if polygon_geom is None or polygon_geom.isEmpty():
                    polygons_wgs84.append(QgsGeometry())  # reported invalid downstream
                    continue
                polygon_geom_wgs84 = QgsGeometry(polygon_geom)
                polygon_geom_wgs84.transform(src2wgs)
                polygons_wgs84.append(polygon_geom_wgs84)

            self.region_result = compute_region_tiles(
                polygons_wgs84, self.max_zoom, ProcessingFeedbackAdapter(feedback)
            )
            if self.region_result is None:  # canceled
                return False

            total_region_tiles = sum(len(tiles) for tiles in self.region_result.region_tiles.values())
            feedback.pushInfo(self.tr(f"Encontrados {total_region_tiles} tiles em {len(self.region_result.region_tiles)} regiões"))
            feedback.pushInfo(self.tr(f"Encontrados {self.region_result.total_tiles} tiles únicos que intersectam com os polígonos selecionados."))

            for region_id, tiles in self.region_result.region_tiles.items():
                feedback.pushInfo(self.tr(f"Região {region_id}: {len(tiles)} tiles"))

            feedback.pushInfo(self.tr(f"Bounds criados: {len(self.region_result.bounds_list)} entradas"))

            if not self.region_result.filtered_tiles:
                feedback.reportError(self.tr("Nenhum tile intersecta a extensão do polígono selecionado."))
                return False

            return True

        def processAlgorithm(self, parameters, context, feedback):
            if feedback.isCanceled():
                return {}

            fb = ProcessingFeedbackAdapter(feedback)
            dry_run = self.parameterAsBool(parameters, DRY_RUN, context)

            if dry_run:
                # Collect vector feature count without rendering
                num_vector_layers = len(self.selected_vector_layers)
                vector_feature_count = sum(
                    lyr.featureCount() for lyr in self.selected_vector_layers if lyr.isValid()
                )
                est = estimate(
                    self.region_result,
                    self.max_zoom,
                    self.tile_format,
                    self.jpg_quality,
                    self.threads_number,
                )
                format_estimate_report(est, fb, num_vector_layers, vector_feature_count)
                feedback.setProgress(100)
                return {}

            output_file = self.parameterAsString(parameters, OUTPUT_FILE, context)
            if not output_file:
                raise QgsProcessingException(
                    self.tr("Informe o arquivo de saída ou ative a opção 'Simulação (Dry Run)'.")
                )

            spec = GenerationSpec(
                output_file=output_file,
                layers=self.layers,
                region_tiles=self.region_result.region_tiles,
                filtered_tiles=self.region_result.filtered_tiles,
                bounds_list=self.region_result.bounds_list,
                wgs84_extent=self.region_result.wgs84_extent,
                max_zoom=self.max_zoom,
                tile_format=self.tile_format,
                jpg_quality=self.jpg_quality,
                transform_context=self.transform_context,
                threads_number=self.threads_number,
            )

            engine = TileRenderEngine(spec, fb)
            if not engine.run():
                if engine.canceled:
                    return {}
                raise QgsProcessingException(self.tr(engine.error_message or "Falha na geração do arquivo TairuDB"))

            # --- Write vector layers into database ---
            if feedback.isCanceled():
                engine.cleanup_resources()
                feedback.pushInfo(self.tr("Operação cancelada pelo usuário antes da exportação de camadas vetoriais"))
                return {}

            export_vector_layers(engine.writer, self.selected_vector_layers, self.transform_context, fb)

            # Final cancellation check
            if feedback.isCanceled():
                engine.cleanup_resources()
                feedback.pushInfo(self.tr("Operação cancelada pelo usuário antes da finalização"))
                return {}

            engine.finalize()
            feedback.pushInfo(self.tr("Processamento concluído com sucesso!"))
            return {OUTPUT_FILE: output_file}

    return Algorithm()
