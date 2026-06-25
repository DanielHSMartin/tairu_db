# -*- coding: utf-8 -*-

"""
Tile rendering engine for .tairudb generation, extracted from
tairu_db_algorithm during the 2.0 refactor so it can be driven both by the
Processing algorithm and by the dock widget's raster wizard.

IMPORTANT: TileRenderEngine uses QgsMapRendererSequentialJob plus a
QCoreApplication.processEvents() polling loop, so run() MUST be called from
the main (GUI) thread — the same constraint expressed by FlagNoThreading in
the Processing algorithm. Never call run() from a QgsTask worker thread.
"""

import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from qgis.PyQt.QtCore import QSize, QBuffer, QByteArray, QCoreApplication
from qgis.PyQt.QtGui import QImage
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsRectangle,
)

try:
    from ..compat import _OPEN_WRITE_ONLY, _FMT_ARGB32
    from .tairudb_writer import TairuDBWriter, MetaTile
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _OPEN_WRITE_ONLY, _FMT_ARGB32
    from tairu_core.tairudb_writer import TairuDBWriter, MetaTile

# Debug mode - set to True for detailed logging, False for production
DEBUG_MODE = False


@dataclass
class GenerationSpec:
    """Everything TileRenderEngine needs to produce a .tairudb file."""
    output_file: str
    layers: list                       # visible raster layers to render
    region_tiles: dict                 # region index -> list[(tx, ty)] (XYZ)
    filtered_tiles: list               # unique (tx, ty) across regions
    bounds_list: list                  # one "lon lat, ..." ring string per region
    wgs84_extent: QgsRectangle         # union bbox of all regions
    max_zoom: int
    tile_format: str = "JPG"           # PNG | JPG | WEBP
    jpg_quality: int = 90
    transform_context: object = None
    threads_number: int = 4
    dpi: int = 96
    tile_width: int = 256
    tile_height: int = 256
    filter_empty_tiles: bool = True
    antialias: bool = True
    include_attribution: bool = False
    attribution_text: str = "© TairuDB contributors"
    name: Optional[str] = None         # metadata name; defaults to output file basename


@dataclass
class EstimateResult:
    """Dry-run statistics for a prospective generation."""
    total_tiles: int = 0
    region_tile_counts: dict = field(default_factory=dict)
    fmt: str = "JPG"
    quality: int = 90
    avg_kb: float = 0.0
    avg_mb: float = 0.0
    lo_mb: float = 0.0
    hi_mb: float = 0.0
    secs: float = 0.0
    time_str: str = ""
    area_km2: float = 0.0
    res_label: str = ""
    max_zoom: int = 18
    threads_number: int = 4
    warnings: list = field(default_factory=list)


_ZOOM_TO_LABEL = {
    18: "Altíssima (0,5 m/px) — zoom 18",
    17: "Alta (1 m/px) — zoom 17",
    16: "Médio Alta (2 m/px) — zoom 16",
    15: "Média (4 m/px) — zoom 15",
    14: "Médio Baixa (8 m/px) — zoom 14",
    13: "Baixa (16 m/px) — zoom 13",
    12: "Muito Baixa (32 m/px) — zoom 12",
}


def estimate(region_result, max_zoom, tile_format, jpg_quality, threads_number):
    """Estimate tile count, file size and processing time without rendering."""
    est = EstimateResult()
    est.total_tiles = region_result.total_tiles
    est.region_tile_counts = {rid: len(tiles) for rid, tiles in region_result.region_tiles.items()}
    est.max_zoom = max_zoom
    est.threads_number = threads_number
    est.quality = jpg_quality

    # Average tile sizes in KB by format (typical QGIS raster rendering)
    size_kb = {'PNG': 70, 'JPG': 28, 'WEBP': 20}
    min_kb = {'PNG': 20, 'JPG': 8, 'WEBP': 6}
    max_kb = {'PNG': 180, 'JPG': 70, 'WEBP': 50}

    fmt = tile_format.upper()
    est.fmt = fmt
    # Scale JPG/WebP estimate by quality relative to baseline of 90
    if fmt in ('JPG', 'WEBP') and jpg_quality != 90:
        q = jpg_quality / 90.0
        est.avg_kb = max(1, size_kb[fmt] * q)
        lo_kb = max(1, min_kb[fmt] * q)
        hi_kb = max(1, max_kb[fmt] * q)
    else:
        est.avg_kb = size_kb.get(fmt, 28)
        lo_kb = min_kb.get(fmt, 8)
        hi_kb = max_kb.get(fmt, 70)

    est.avg_mb = (est.total_tiles * est.avg_kb) / 1024
    est.lo_mb = (est.total_tiles * lo_kb) / 1024
    est.hi_mb = (est.total_tiles * hi_kb) / 1024

    # Rendering time estimate: ~0.15 s/tile single-thread
    est.secs = est.total_tiles * 0.15 / max(1, threads_number)
    if est.secs < 60:
        est.time_str = f"~{est.secs:.0f} seg"
    elif est.secs < 3600:
        est.time_str = f"~{est.secs/60:.0f} min"
    else:
        est.time_str = f"~{est.secs/3600:.1f} h"

    # Coverage area from WGS84 bounding box
    bbox = region_result.wgs84_extent
    clat = math.radians((bbox.yMinimum() + bbox.yMaximum()) / 2)
    w_km = abs(bbox.xMaximum() - bbox.xMinimum()) * 111.32 * math.cos(clat)
    h_km = abs(bbox.yMaximum() - bbox.yMinimum()) * 110.574
    est.area_km2 = w_km * h_km

    est.res_label = _ZOOM_TO_LABEL.get(max_zoom, f"zoom {max_zoom}")

    if est.total_tiles > 10000:
        est.warnings.append("Mais de 10.000 tiles. Reduza a área ou use resolução menor.")
    elif est.total_tiles > 5000:
        est.warnings.append("Mais de 5.000 tiles. Verifique se área/resolução são adequadas.")
    if est.avg_mb > 1024:
        est.warnings.append("Estimativa acima de 1 GB. Pode impactar desempenho no dispositivo.")
    elif est.avg_mb > 500:
        est.warnings.append("Estimativa acima de 500 MB. Verifique o espaço no dispositivo.")

    return est


def _fmt_size(mb):
    return f"{mb/1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def format_estimate_report(est, feedback, num_vector_layers=0, vector_feature_count=0,
                           dry_run_footer=True, contour_enabled=False,
                           contour_source_label='', contour_interval=10,
                           contour_smoothing='Médio',
                           grg_enabled=False, grg_type_label=''):
    """Push the dry-run report through a feedback adapter."""
    num_regions = len(est.region_tile_counts)
    quality_str = f" (qualidade {est.quality})" if est.fmt in ('JPG', 'WEBP') else ""
    sep = "─" * 34

    def line(text=""):
        feedback.push_info(text)

    line()
    line("[ SIMULAÇÃO] Nenhum arquivo foi gerado")
    line("=" * 34)
    line()
    line("O ARQUIVO CONTERÁ")
    line(sep)
    line((f"  Tiles raster    : {est.total_tiles:,} tiles · zoom {est.max_zoom} · "
          f"{est.fmt}{quality_str} · ~{_fmt_size(est.avg_mb)}").replace(',', '.'))
    if num_vector_layers > 0:
        feat_s = 'ões' if vector_feature_count != 1 else 'ão'
        line((f"  Vetoriais       : {num_vector_layers} camada{'s' if num_vector_layers != 1 else ''} "
              f"· {vector_feature_count:,} feição{feat_s}").replace(',', '.'))
    if contour_enabled:
        line(f"  Curvas de nível : {contour_source_label} · {contour_interval} m · {contour_smoothing}")
    if grg_enabled:
        line(f"  Grade GRG       : {grg_type_label}")
    line()
    line("CONFIGURAÇÃO")
    line(sep)
    line(f"  Resolução  : {est.res_label}")
    line(f"  Formato    : {est.fmt}{quality_str}")
    line(f"  Regiões    : {num_regions} polígono{'s' if num_regions != 1 else ''}")
    line(f"  Área (bbox): {est.area_km2:.1f} km²")
    line()
    line("TILES RASTER")
    line(sep)
    line(f"  Total de tiles : {est.total_tiles:,}".replace(',', '.'))
    for rid, count in sorted(est.region_tile_counts.items()):
        line(f"    Região {rid + 1}: {count:,} tiles".replace(',', '.'))
    line()
    line("TAMANHO ESTIMADO")
    line(sep)
    line(f"  Estimativa : {_fmt_size(est.avg_mb)}  (~{est.avg_kb:.0f} KB/tile)")
    line(f"  Intervalo  : {_fmt_size(est.lo_mb)} – {_fmt_size(est.hi_mb)}")
    line()
    line("TEMPO ESTIMADO")
    line(sep)
    line(f"  {est.threads_number} thread{'s' if est.threads_number != 1 else ''} paralela{'s' if est.threads_number != 1 else ''} : {est.time_str}")
    line("  (~0,15 s/tile em hardware típico)")
    if num_vector_layers > 0:
        line()
        line("CAMADAS VETORIAIS")
        line(sep)
        line(f"  Camadas  : {num_vector_layers}")
        line(f"  Feições  : {vector_feature_count:,}".replace(',', '.'))
    if contour_enabled:
        master_interval = contour_interval * 5
        line()
        line("CURVAS DE NÍVEL")
        line(sep)
        line(f"  Fonte       : {contour_source_label}")
        if 'INPE' in contour_source_label:
            line("  Cobertura   : Brasil (6°N–34°S, 75°W–34.5°W)")
        else:
            line("  Cobertura   : Global")
        line(f"  Intervalo   : {contour_interval} m  ·  Curvas mestras: {master_interval} m")
        line(f"  Suavização  : {contour_smoothing}")
        line("  Requer internet. Tiles DEM são salvos em cache localmente.")
        line("  Tempo de download não incluído nesta estimativa.")
    line()

    if est.warnings:
        line("AVISOS")
        line(sep)
        for w in est.warnings:
            feedback.report_error(f"  ⚠  {w}", False)
        line()

    if dry_run_footer:
        line("Desmarque 'Dry Run' e execute novamente para gerar o arquivo.")
        line()


class TileRenderEngine:
    """Renders the tile set described by a GenerationSpec into a .tairudb file."""

    def __init__(self, spec, feedback):
        self.spec = spec
        self.feedback = feedback

        self.wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        self.mercator_crs = QgsCoordinateReferenceSystem("EPSG:3857")
        self.wgs2mercator = QgsCoordinateTransform(
            self.wgs84_crs, self.mercator_crs, spec.transform_context
        )

        self.writer: Optional[TairuDBWriter] = None
        self.error_message: Optional[str] = None
        self.canceled = False

        # Processing state
        self.total_tiles = len(spec.filtered_tiles)
        self.processed_tiles = 0
        self.failed_tiles = 0
        self.retried_tiles = 0
        self.meta_tiles = []
        self.renderer_jobs = {}
        self.max_retries = 3
        self.retry_queue = []
        self.failed_tiles_info = []
        self._completion_reported = False

    def debug_log(self, message):
        """Log debug messages only if DEBUG_MODE is enabled"""
        if DEBUG_MODE:
            self.feedback.push_info(f"[DEBUG] {message}")

    # ------------------------------------------------------------------ run

    def run(self):
        """Render all tiles and write them to the output file.

        Returns True on success; on failure/cancellation returns False with
        either self.canceled set or self.error_message populated. The writer
        is left open on success so vector layers can still be exported —
        callers must invoke finalize() (or cleanup() on abort).
        """
        spec = self.spec

        self.writer = TairuDBWriter(spec.output_file)
        if not self.writer.create():
            self.error_message = f"Falha ao criar o arquivo GeoDB {spec.output_file}"
            return False

        if self.feedback.is_canceled():
            self.cleanup_resources()
            self.canceled = True
            return False

        self._write_metadata_and_regions()

        self.feedback.set_progress_text(
            f"Preparando para renderizar {len(spec.filtered_tiles)} tiles..."
        )

        self.meta_tiles = []
        self.processed_tiles = 0

        z = spec.max_zoom
        n = 2.0 ** spec.max_zoom

        for i, (tx, ty) in enumerate(spec.filtered_tiles):
            if self.feedback.is_canceled():
                self.cleanup_resources()
                self.canceled = True
                return False

            # Update progress during meta tile creation
            if i % 50 == 0:
                self.feedback.set_progress(10 + (20 * i / len(spec.filtered_tiles)))

            self.meta_tiles.append(self.create_individual_metatile(z, tx, ty, n))

        self.feedback.set_progress_text(f"Renderizando {len(self.meta_tiles)} tiles...")

        # Start rendering jobs
        self.start_jobs()

        # Fast polling loop - process events until all tiles complete
        if self.meta_tiles or self.renderer_jobs:
            while self.renderer_jobs or self.meta_tiles or self.retry_queue:
                # Process Qt events to handle finished signals
                QCoreApplication.processEvents()
                if self.feedback.is_canceled():
                    self.debug_log("Cancelamento detectado durante o polling")
                    self.cleanup_resources()
                    self.canceled = True
                    self.feedback.push_info("Operação cancelada pelo usuário")
                    return False

        # Final event processing to ensure all signals handled
        QCoreApplication.processEvents()

        if self.feedback.is_canceled():
            self.cleanup_resources()
            self.canceled = True
            self.feedback.push_info("Operação cancelada pelo usuário")
            return False

        self._report_summary()
        return True

    def finalize(self):
        """Commit, VACUUM and close the output database."""
        if self.writer:
            self.writer.finalize()

    # ----------------------------------------------------------- internals

    def _write_metadata_and_regions(self):
        spec = self.spec
        writer = self.writer

        format_lower = spec.tile_format.lower()
        writer.setMetadataValue("format", format_lower)
        base_name = spec.name or os.path.splitext(os.path.basename(spec.output_file))[0]
        writer.setMetadataValue("name", base_name)
        writer.setMetadataValue("description", base_name)
        writer.setMetadataValue("version", "1.2")
        writer.setMetadataValue("type", "overlay")
        writer.setMetadataValue("minzoom", str(spec.max_zoom))
        writer.setMetadataValue("maxzoom", str(spec.max_zoom))

        for idx, bound_str in enumerate(spec.bounds_list):
            region_name = f"Região {idx + 1}"
            writer.insertRegion(
                region_name,
                spec.max_zoom,  # minzoom
                spec.max_zoom,  # maxzoom
                bound_str       # bounds
            )

        self.feedback.push_info(f"Regiões criadas na tabela de regiões: {len(spec.bounds_list)}")

        center_x = (spec.wgs84_extent.xMinimum() + spec.wgs84_extent.xMaximum()) / 2
        center_y = (spec.wgs84_extent.yMinimum() + spec.wgs84_extent.yMaximum()) / 2
        center_str = f"{center_x},{center_y},{spec.max_zoom}"
        writer.setMetadataValue("center", center_str)

        if spec.include_attribution and spec.attribution_text:
            writer.setMetadataValue("attribution", spec.attribution_text)

        writer.setMetadataValue("generator", "GeoPDB Generator")
        writer.setMetadataValue("created", datetime.now().isoformat())

    def _report_summary(self):
        self.feedback.set_progress(100)
        total_expected = len(self.spec.filtered_tiles)
        success_rate = ((total_expected - self.failed_tiles) / total_expected * 100) if total_expected > 0 else 0

        self.feedback.push_info("Resumo do processamento de tiles:")
        self.feedback.push_info(f"- Tiles esperados: {total_expected}")
        self.feedback.push_info(f"- Tiles processados com sucesso: {self.processed_tiles}")
        self.feedback.push_info(f"- Tiles falhados: {self.failed_tiles}")
        self.feedback.push_info(f"- Tiles reprocessados: {self.retried_tiles}")
        self.feedback.push_info(f"- Taxa de sucesso: {success_rate:.1f}%")

        if self.failed_tiles_info and len(self.failed_tiles_info) <= 10:
            self.feedback.push_info("Detalhes dos tiles falhados:")
            for fail_info in self.failed_tiles_info:
                self.feedback.push_info(
                    f"  - Tile {fail_info['x']},{fail_info['y']}: {fail_info['reason']}"
                )
        elif len(self.failed_tiles_info) > 10:
            self.feedback.push_info(f"({len(self.failed_tiles_info)} failed tiles - too many to list)")

    def cleanup_resources(self):
        """Enhanced cleanup with better error handling"""
        try:
            self.debug_log("cleanup_resources: Iniciando limpeza")
            self.feedback.push_info("Limpando recursos...")

            # Cancel and cleanup renderer jobs AGGRESSIVELY
            jobs_count = len(self.renderer_jobs)
            self.debug_log(f"cleanup_resources: Cancelando {jobs_count} jobs")
            for job in list(self.renderer_jobs.keys()):
                try:
                    # Disconnect finished signal BEFORE cancel/delete so that
                    # process_metatile is never called on an already-deleted job.
                    job.finished.disconnect()
                    job.cancelWithoutBlocking()
                    job.deleteLater()
                except Exception:
                    pass  # Ignore cleanup errors
            self.renderer_jobs.clear()

            # Force process events to handle deleteLater() immediately
            if jobs_count > 0:
                for _ in range(10):
                    QCoreApplication.processEvents()

            # Clear tile queues COMPLETELY
            self.debug_log(f"cleanup_resources: Limpando {len(self.meta_tiles)} meta_tiles")
            self.meta_tiles.clear()
            self.debug_log(f"cleanup_resources: Limpando {len(self.retry_queue)} retry_queue")
            self.retry_queue.clear()

            # Close database connection with proper cleanup
            if self.writer and self.writer.conn:
                try:
                    self.debug_log("cleanup_resources: Fechando conexão do banco de dados")
                    # Try to commit any pending changes before closing
                    try:
                        self.writer.conn.commit()
                    except Exception:
                        pass
                    try:
                        self.writer.conn.close()
                    except Exception:
                        pass
                    self.writer.conn = None
                except Exception as e:
                    self.feedback.push_info(f"Aviso ao fechar banco de dados: {str(e)}")

            # Process more events to ensure cleanup is complete
            self.debug_log("cleanup_resources: Processando eventos finais")
            for _ in range(20):
                QCoreApplication.processEvents()

            self.debug_log("cleanup_resources: Limpeza concluída")

        except Exception as e:
            self.feedback.push_info(f"Aviso durante a limpeza: {str(e)}")

    def create_individual_metatile(self, z, tx, ty, n):
        try:
            x1 = tx * 360.0 / n - 180.0
            y1 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
            x2 = (tx + 1) * 360.0 / n - 180.0
            y2 = 180.0 / math.pi * (math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
            meta_tile = MetaTile()
            meta_tile.zoom = z
            meta_tile.tx = tx
            meta_tile.ty = ty
            meta_tile.metatile_size = 1
            meta_tile.actual_size_x = 1
            meta_tile.actual_size_y = 1
            meta_tile.retry_count = 0  # Track retry attempts

            # Add error handling for coordinate transformation
            if self.wgs2mercator:
                p1 = self.wgs2mercator.transform(x1, y1)
                p2 = self.wgs2mercator.transform(x2, y2)
                meta_tile.extent = QgsRectangle(
                    min(p1.x(), p2.x()),
                    min(p1.y(), p2.y()),
                    max(p1.x(), p2.x()),
                    max(p1.y(), p2.y())
                )
            else:
                # Fallback to WGS84 coordinates if transformation fails
                meta_tile.extent = QgsRectangle(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

            # Validate the created meta tile
            if not meta_tile.is_valid():
                self.feedback.push_info(f"Aviso: Metatile inválido criado para {tx},{ty}")

            return meta_tile
        except Exception as e:
            self.feedback.push_info(f"Erro ao criar metatile {tx},{ty}: {str(e)}")
            # Return a basic metatile to avoid complete failure
            meta_tile = MetaTile()
            meta_tile.zoom = z
            meta_tile.tx = tx
            meta_tile.ty = ty
            meta_tile.metatile_size = 1
            meta_tile.actual_size_x = 1
            meta_tile.actual_size_y = 1
            meta_tile.retry_count = 0
            meta_tile.extent = QgsRectangle(-180, -85, 180, 85)  # Fallback extent
            return meta_tile

    def start_jobs(self):
        """Start rendering jobs for pending tiles - optimized for speed"""
        spec = self.spec

        while (self.meta_tiles or self.retry_queue) and len(self.renderer_jobs) < spec.threads_number:
            # Prioritize retries over new tiles
            meta_tile = None
            if self.retry_queue:
                meta_tile = self.retry_queue.pop(0)
            elif self.meta_tiles:
                meta_tile = self.meta_tiles.pop(0)
            else:
                break

            if not meta_tile:
                continue

            # Validate meta tile before processing
            if hasattr(meta_tile, 'is_valid') and not meta_tile.is_valid():
                self.feedback.push_info(f"Pulando metatile inválido {meta_tile.tx},{meta_tile.ty}")
                continue

            try:
                size_x = meta_tile.actual_size_x if meta_tile.actual_size_x > 0 else meta_tile.metatile_size
                size_y = meta_tile.actual_size_y if meta_tile.actual_size_y > 0 else meta_tile.metatile_size
                actual_tile_width = spec.tile_width * size_x
                actual_tile_height = spec.tile_height * size_y

                if actual_tile_width <= 0 or actual_tile_height <= 0:
                    self.feedback.push_info(f"Tamanho de tile inválido para tile {meta_tile.tx},{meta_tile.ty}")
                    continue

                # Validate extent
                if meta_tile.extent.isEmpty() or not meta_tile.extent.isFinite():
                    self.feedback.push_info(f"Extensão inválida para tile {meta_tile.tx},{meta_tile.ty}")
                    continue

                # Create map settings with error checking
                map_settings = QgsMapSettings()

                if not spec.layers:
                    self.feedback.push_info("Nenhuma camada disponível para renderização")
                    continue

                map_settings.setLayers(spec.layers)
                map_settings.setOutputDpi(spec.dpi)
                map_settings.setOutputSize(QSize(actual_tile_width, actual_tile_height))
                map_settings.setExtent(meta_tile.extent)
                map_settings.setDestinationCrs(self.mercator_crs)
                map_settings.setFlag(Qgis.MapSettingsFlag.Antialiasing, spec.antialias)  # type: ignore

                # Additional map settings for better rendering
                map_settings.setFlag(Qgis.MapSettingsFlag.RenderMapTile, True)  # type: ignore
                map_settings.setFlag(Qgis.MapSettingsFlag.DrawLabeling, True)  # type: ignore
                map_settings.setFlag(Qgis.MapSettingsFlag.UseAdvancedEffects, False)  # Disable for stability  # type: ignore

                # Create and start job
                job = QgsMapRendererSequentialJob(map_settings)
                self.renderer_jobs[job] = meta_tile
                job.finished.connect(lambda job=job: self.process_metatile(job))  # type: ignore
                job.start()

            except Exception as e:
                self.feedback.push_info(f"Erro ao iniciar trabalho para tile {meta_tile.tx},{meta_tile.ty}: {str(e)}")

                # Add to failed tiles if not already retrying
                if meta_tile.retry_count < self.max_retries:
                    meta_tile.retry_count += 1
                    self.retry_queue.append(meta_tile)
                else:
                    self.failed_tiles_info.append({
                        'x': meta_tile.tx,
                        'y': meta_tile.ty,
                        'zoom': meta_tile.zoom,
                        'reason': f'Erro ao iniciar trabalho: {str(e)}'
                    })
                    self.failed_tiles += 1

    def process_metatile(self, job):
        """Process completed rendering job - optimized for speed"""
        try:
            meta_tile = self.renderer_jobs.get(job)
            if not meta_tile:
                # Job was already cleaned up by cleanup_resources (cancel path).
                try:
                    job.deleteLater()
                except RuntimeError:
                    pass
                return

            metatile_image = job.renderedImage()

            # Check for rendering failures and implement retry logic
            if metatile_image.isNull() or metatile_image.width() == 0 or metatile_image.height() == 0:
                self.failed_tiles += 1

                # Add to retry queue if we haven't exceeded max retries
                if meta_tile.retry_count < self.max_retries:
                    meta_tile.retry_count += 1
                    self.retry_queue.append(meta_tile)
                    self.retried_tiles += 1
                    self.feedback.push_info(
                        f"Tentando tile novamente {meta_tile.tx},{meta_tile.ty} (tentativa {meta_tile.retry_count}/{self.max_retries})"
                    )
                else:
                    # Max retries reached, log as failed
                    self.failed_tiles_info.append({
                        'x': meta_tile.tx,
                        'y': meta_tile.ty,
                        'zoom': meta_tile.zoom,
                        'reason': 'Falha ao renderizar após tentativas máximas'
                    })
                    self.feedback.push_info(
                        f"Tile {meta_tile.tx},{meta_tile.ty} falhou após {self.max_retries} tentativas"
                    )

                del self.renderer_jobs[job]
                job.deleteLater()
                self.check_completion()
                return

            # Successfully rendered, process the tile
            self.save_metatile_data(meta_tile, metatile_image)

        except Exception as e:
            # Handle any unexpected errors during tile processing
            self.feedback.push_info(f"Erro ao processar tile: {str(e)}")

            meta_tile = self.renderer_jobs.get(job)
            if meta_tile:
                self.failed_tiles_info.append({
                    'x': meta_tile.tx,
                    'y': meta_tile.ty,
                    'zoom': meta_tile.zoom,
                    'reason': f'Erro ao processar tile: {str(e)}'
                })
                self.failed_tiles += 1
        finally:
            # Cleanup job. Guard against the case where cleanup_resources()
            # already disconnected and deleted this job (cancel during render).
            if job in self.renderer_jobs:
                del self.renderer_jobs[job]
            try:
                job.deleteLater()
            except RuntimeError:
                pass  # C++ object already deleted by cleanup_resources

            self.check_completion()

    def save_metatile_data(self, meta_tile, metatile_image):
        """Save individual tiles from a metatile image - optimized for speed"""
        spec = self.spec
        size_x = meta_tile.actual_size_x if meta_tile.actual_size_x > 0 else meta_tile.metatile_size
        size_y = meta_tile.actual_size_y if meta_tile.actual_size_y > 0 else meta_tile.metatile_size
        max_tile_index = int(math.pow(2, meta_tile.zoom))

        for i in range(size_x):
            if self.feedback.is_canceled():
                return

            tile_x = meta_tile.tx + i
            if tile_x >= max_tile_index:
                continue

            for j in range(size_y):
                if self.feedback.is_canceled():
                    return

                tile_y = meta_tile.ty + j
                if tile_y >= max_tile_index:
                    continue

                # Extract tile from metatile
                x_offset = i * spec.tile_width
                y_offset = j * spec.tile_height

                # Validate offsets
                if (x_offset >= metatile_image.width() or
                        y_offset >= metatile_image.height() or
                        x_offset + spec.tile_width > metatile_image.width() or
                        y_offset + spec.tile_height > metatile_image.height()):
                    continue

                tile_image = metatile_image.copy(x_offset, y_offset, spec.tile_width, spec.tile_height)

                # Improved empty tile detection
                if spec.filter_empty_tiles and self.is_tile_empty(tile_image):
                    self.processed_tiles += 1
                    continue

                # Convert and save tile
                if self.convert_and_save_tile(tile_image, meta_tile, tile_x, tile_y, max_tile_index):
                    self.processed_tiles += 1
                else:
                    # Failed to save tile
                    self.failed_tiles_info.append({
                        'x': tile_x,
                        'y': tile_y,
                        'zoom': meta_tile.zoom,
                        'reason': 'Falha ao converter ou salvar dados do tile'
                    })
                    self.failed_tiles += 1

    def is_tile_empty(self, tile_image):
        """Improved empty tile detection with better sampling"""
        if tile_image.isNull():
            return True

        # Sample more points for better accuracy
        sample_points = [
            (0, 0), (tile_image.width()//4, tile_image.height()//4),
            (tile_image.width()//2, tile_image.height()//2),
            (3*tile_image.width()//4, 3*tile_image.height()//4),
            (tile_image.width()-1, tile_image.height()-1)
        ]

        first_pixel = None
        for x, y in sample_points:
            if x < tile_image.width() and y < tile_image.height():
                pixel = tile_image.pixel(x, y)
                if first_pixel is None:
                    first_pixel = pixel
                elif pixel != first_pixel:
                    return False  # Found different pixels, not empty

        # Additional check with alpha channel
        if tile_image.hasAlphaChannel():
            if tile_image.format() != _FMT_ARGB32:
                tile_image = tile_image.convertToFormat(_FMT_ARGB32)

            # Check if all pixels are transparent
            for x, y in sample_points:
                if x < tile_image.width() and y < tile_image.height():
                    pixel = tile_image.pixel(x, y)
                    alpha = (pixel >> 24) & 0xFF
                    if alpha > 0:  # Not fully transparent
                        return False

        return True

    def convert_and_save_tile(self, tile_image, meta_tile, tile_x, tile_y, max_tile_index):
        """Convert tile image to specified format and save to database"""
        spec = self.spec
        try:
            quality = spec.jpg_quality
            tile_data = QByteArray()
            buffer = QBuffer(tile_data)
            buffer.open(_OPEN_WRITE_ONLY)

            success = False
            if spec.tile_format == "PNG":
                success = tile_image.save(buffer, "PNG")
            elif spec.tile_format == "JPG":
                success = tile_image.save(buffer, "JPG", quality)
            elif spec.tile_format == "WEBP":
                formats = QImage.supportedImageFormats()
                webp_format = b'WEBP'
                if webp_format in formats:
                    success = tile_image.save(buffer, "WEBP", quality)
                else:
                    # Fallback to PNG if WebP is not supported
                    success = tile_image.save(buffer, "PNG")
                    self.feedback.push_info("WebP não suportado, usando PNG em vez disso")

            if not success or tile_data.isEmpty():
                return False

            # Convert to TMS Y coordinate
            tms_y = max_tile_index - 1 - tile_y

            # Save to database for all regions that contain this tile
            tile_coord = (tile_x, tile_y)
            saved_to_regions = 0

            # Check which regions should contain this tile
            containing_regions = []
            for region_id, region_tiles in spec.region_tiles.items():
                if tile_coord in region_tiles:
                    containing_regions.append(region_id)

            if not containing_regions:
                self.feedback.push_info(f"Aviso: Tile {tile_x},{tile_y} não foi encontrado em nenhuma região")

            for region_id in containing_regions:
                if self.writer and self.writer.saveTile(meta_tile.zoom, tile_x, tms_y, tile_data, region_id):
                    saved_to_regions += 1
                else:
                    self.feedback.push_info(f"Falha ao salvar tile {tile_x},{tile_y} na região {region_id}")

            # Periodic commit every 100 tiles for data integrity
            if saved_to_regions > 0 and self.processed_tiles % 100 == 0:
                if self.writer:
                    self.writer.periodicCommit()

            return saved_to_regions > 0

        except Exception as e:
            self.feedback.push_info(f"Erro ao converter/salvar tile {tile_x},{tile_y}: {str(e)}")
            return False

    def check_completion(self):
        """Check if processing is complete and handle retries - optimized for speed"""
        # Update progress
        if self.total_tiles > 0:
            progress = min(99, 100.0 * self.processed_tiles / self.total_tiles)
            self.feedback.set_progress(progress)

        # Process retry queue
        if self.retry_queue and len(self.renderer_jobs) < self.spec.threads_number:
            self.debug_log(f"check_completion: Processando retry - {len(self.retry_queue)} na fila")
            retry_tile = self.retry_queue.pop(0)
            self.meta_tiles.insert(0, retry_tile)  # Priority to retries
            self.start_jobs()
            return

        # Start new jobs if available
        if self.meta_tiles:
            self.debug_log(f"check_completion: Iniciando novos jobs - {len(self.meta_tiles)} tiles aguardando")
            self.start_jobs()
        elif not self.renderer_jobs and not self.retry_queue:
            # All processing complete - only report once
            if not self._completion_reported:
                self._completion_reported = True
                self.feedback.push_info("Todos os tiles processados, finalizando renderização...")
                if self.failed_tiles > 0:
                    self.feedback.push_info(
                        f"Processamento completo. {self.failed_tiles} tiles falharam, {self.retried_tiles} tiles tentados novamente"
                    )
