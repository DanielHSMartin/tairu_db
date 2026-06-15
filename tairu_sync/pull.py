# -*- coding: utf-8 -*-

"""
Pull flows: records → GeoPackage layers, and tairudb file → MBTiles raster.

Network + sqlite conversion run in a FirebaseTask; everything that touches
QgsProject / widgets happens back on the GUI thread in the success handlers.
"""

import os

from qgis.core import QgsMessageLog

try:
    from ..compat import _MSG_WARNING
    from ..tairu_core.mbtiles import tairudb_to_mbtiles
    from ..tairu_core.workspace import map_workspace
    from ..tairu_firebase.config import TAIRUDB_OBJECT_PATH
    from ..tairu_firebase.models import TairuRecord
    from .record_convert import apply_pull, add_record_layers_to_project, add_raster_to_project
    from .tasks import run_task
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _MSG_WARNING
    from tairu_core.mbtiles import tairudb_to_mbtiles
    from tairu_core.workspace import map_workspace
    from tairu_firebase.config import TAIRUDB_OBJECT_PATH
    from tairu_firebase.models import TairuRecord
    from tairu_sync.record_convert import apply_pull, add_record_layers_to_project, add_raster_to_project
    from tairu_sync.tasks import run_task


def start_pull(dock, tmap):
    """Fetch the map's records and merge them into the project layers."""
    fs = dock.fs
    page = dock.detail_page
    page.set_busy(True, 'Baixando registros…')

    def fetch(task):
        rows = fs.list_records(tmap.map_id, cancel_cb=task.isCanceled)
        task.report(1.0, f'{len(rows)} registros recebidos')
        return rows

    def on_success(rows):
        records = []
        parse_errors = []
        for record_id, fields in rows:
            try:
                records.append(TairuRecord.from_fields(record_id, fields))
            except Exception as e:
                parse_errors.append((record_id, str(e)))

        paths = map_workspace(dock.env.key, tmap.map_id)
        try:
            result = apply_pull(paths['gpkg'], records, remove_missing=True)
        except Exception as e:
            page.set_busy(False)
            page.set_status(f'Falha ao gravar GeoPackage: {e}', error=True)
            return
        add_record_layers_to_project(paths['gpkg'], tmap.nome or tmap.map_id)

        page.set_busy(False)
        summary = (f'Registros: {result.added} novos, {result.updated} atualizados, '
                   f'{result.removed} removidos.')
        errors = result.errors + parse_errors
        if errors:
            summary += f' {len(errors)} com problema (ignorados).'
            for record_id, reason in errors[:20]:
                QgsMessageLog.logMessage(f'Registro {record_id}: {reason}',
                                         'Tairu Maps', _MSG_WARNING)
            if len(errors) > 20:
                QgsMessageLog.logMessage(f'... e mais {len(errors) - 20} erros',
                                         'Tairu Maps', _MSG_WARNING)
            page.set_status(f'{summary}\nPrimeiro erro: {errors[0][1]} '
                            '(detalhes no painel Mensagens de Log, aba "Tairu Maps")',
                            error=(result.added + result.updated == 0))
        else:
            page.set_status(summary)
        dock.notify(f'{tmap.nome}: {summary}')

    def on_error(message):
        page.set_busy(False)
        page.set_status(message, error=True)

    run_task(f'Tairu Maps: registros de {tmap.nome}', fetch,
             on_success=on_success, on_error=on_error,
             on_progress=lambda f, m: page.set_progress(f, m))


def start_tairudb_download(dock, tmap, file_name):
    """Download a tairudb file, convert to MBTiles and add as raster layers."""
    storage = dock.storage
    page = dock.detail_page
    paths = map_workspace(dock.env.key, tmap.map_id)
    local_path = os.path.join(paths['downloads'], file_name)
    object_path = TAIRUDB_OBJECT_PATH.format(map_id=tmap.map_id, file_name=file_name)

    page.set_busy(True, f'Baixando {file_name}…')

    def fetch(task):
        def dl_progress(done, total):
            if total:
                task.report(0.7 * done / total, f'Baixando {file_name}… '
                            f'{done // (1024*1024)} de {total // (1024*1024)} MB')

        storage.download(object_path, local_path,
                         progress_cb=dl_progress, cancel_cb=task.isCanceled)
        task.report(0.75, 'Convertendo para MBTiles…')
        results = tairudb_to_mbtiles(
            local_path, paths['mbtiles'],
            progress_cb=lambda f: task.report(0.75 + 0.25 * f))
        return results

    def on_success(results):
        added = 0
        for mbtiles_path, region_label in results:
            name = f'{os.path.splitext(file_name)[0]} — {region_label}'
            if add_raster_to_project(mbtiles_path, name, tmap.nome or tmap.map_id):
                added += 1
        page.set_busy(False)
        page.set_status(f'{file_name}: {added} camada(s) raster adicionada(s).')
        dock.notify(f'{file_name} adicionado ao projeto.')

    def on_error(message):
        page.set_busy(False)
        page.set_status(message, error=True)

    run_task(f'Tairu Maps: download {file_name}', fetch,
             on_success=on_success, on_error=on_error,
             on_progress=lambda f, m: page.set_progress(f, m))
