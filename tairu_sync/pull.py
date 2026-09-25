# -*- coding: utf-8 -*-

"""
Pull flows: records → GeoPackage layers, and tairudb file → MBTiles raster.

Network + sqlite conversion run in a FirebaseTask; everything that touches
QgsProject / widgets happens back on the GUI thread in the success handlers.
"""

import contextlib
import os

from qgis.core import QgsMessageLog

try:
    from ..compat import _MSG_WARNING
    from ..tairu_core.i18n import tr
    from ..tairu_core.firestore_cache import (
        FirestoreCache, RECORDS_COLLECTION, RECORD_GROUPS_COLLECTION)
    from ..tairu_core.reentrancy_guard import run_or_defer
    from ..tairu_core.mbtiles import tairudb_to_mbtiles
    from ..tairu_core.workspace import (
        map_workspace, mark_record_schema_generation,
        record_schema_generation, save_last_pull_ts)
    from ..tairu_firebase.config import TAIRUDB_OBJECT_PATH
    from ..tairu_firebase.models import (
        TairuRecord, TairuRecordGroup, now_millis, parse_millis)
    from .record_convert import (
        RECORD_SCHEMA_GENERATION, apply_pull, add_record_layers_to_project,
        add_raster_to_project)
    from .tasks import run_task
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _MSG_WARNING
    from tairu_core.i18n import tr
    from tairu_core.firestore_cache import (
        FirestoreCache, RECORDS_COLLECTION, RECORD_GROUPS_COLLECTION)
    from tairu_core.reentrancy_guard import run_or_defer
    from tairu_core.mbtiles import tairudb_to_mbtiles
    from tairu_core.workspace import (
        map_workspace, mark_record_schema_generation,
        record_schema_generation, save_last_pull_ts)
    from tairu_firebase.config import TAIRUDB_OBJECT_PATH
    from tairu_firebase.models import (
        TairuRecord, TairuRecordGroup, now_millis, parse_millis)
    from tairu_sync.record_convert import (
        RECORD_SCHEMA_GENERATION, apply_pull, add_record_layers_to_project,
        add_raster_to_project)
    from tairu_sync.tasks import run_task

# Safety margin subtracted from the Firestore serverTimestamp cursor to absorb
# precision loss and races around the last observed commit.
_PULL_CLOCK_SKEW_MS = 30_000


def _rows_server_watermark(rows, empty_fallback_ms=0):
    """Highest serverTimestamp in fetched rows, or the fallback when none is usable.

    The fallback also covers rows that exist but ALL lack a serverTimestamp (a
    fully-legacy map): on a full pull the caller passes pull_started_at, so we save a
    positive watermark and the snapshot flag sticks. Returning 0 here would leave
    last_full_sync_ms=0 and force a full re-read of the whole collection on EVERY open
    — the exact unnecessary-reads cost this cache exists to avoid. The incremental
    caller passes empty_fallback_ms=0 and does `... or since_millis`, so its behaviour
    is unchanged.
    """
    high = 0
    for _record_id, fields in rows or []:
        high = max(high, parse_millis((fields or {}).get('serverTimestamp')))
    if high > 0:
        return high
    return int(empty_fallback_ms or 0)


def start_pull(dock, tmap):
    """Fetch the map's records and merge them into the project layers.

    On the first pull (or after a forced full-sync) fetches every record in the
    map. On subsequent pulls only records committed after the last observed
    Firestore serverTimestamp are transferred — the delta is typically tiny.
    """
    fs = dock.fs
    page = dock.detail_page
    paths = map_workspace(dock.env.key, tmap.map_id)
    cache = FirestoreCache(dock.env.key, dock.tokens.uid)
    pull_started_at = now_millis()
    try:
        cache_state = cache.load_sync_state(tmap.map_id, RECORDS_COLLECTION)
    except Exception:
        cache_state = {}
    cache_since_millis = int(cache_state.get('high_watermark_ms') or 0)
    has_full_cache_snapshot = int(cache_state.get('last_full_sync_ms') or 0) > 0
    # Migration safety: legacy last_pull.json and delta-only cache state are not
    # enough to prove the SQLite cache contains the whole record collection.
    since_millis = cache_since_millis if has_full_cache_snapshot else 0
    # Migracao do grupo: o pull normal e INCREMENTAL e o delta nao traz registro
    # inalterado, entao quem ja tinha a expedicao baixada ganharia a coluna vazia em todo
    # mundo e veria a arvore de grupos inteira nascer vazia, sem uma mensagem. Um pull
    # completo, uma unica vez por expedicao, preenche.
    backfilling = bool(since_millis) and (
        record_schema_generation(dock.env.key, tmap.map_id) < RECORD_SCHEMA_GENERATION)
    if backfilling:
        since_millis = 0
    is_incremental = since_millis > 0

    page.set_busy(True, tr('Baixando registros…'))

    def fetch(task):
        # Os grupos vem SEMPRE inteiros, nunca por delta: sao dezenas de documentos
        # minusculos, e a arvore precisa da lista completa para saber o que sumiu.
        group_rows = fs.list_record_groups(tmap.map_id, cancel_cb=task.isCanceled)
        if is_incremental:
            query_since = max(0, since_millis - _PULL_CLOCK_SKEW_MS)
            rows = fs.list_records_since(tmap.map_id, query_since, cancel_cb=task.isCanceled)
            task.report(1.0, tr('{n} registros recebidos (delta)').format(n=len(rows)))
        else:
            rows = fs.list_records(tmap.map_id, cancel_cb=task.isCanceled)
            task.report(1.0, tr('{n} registros recebidos').format(n=len(rows)))
        return rows, group_rows

    # NOTE: apply_pull creates QgsVectorLayer / QgsVectorFileWriter and reads
    # QgsProject.instance() — those crash the C++ layer off the GUI thread (a worker
    # attempt hard-crashed QGIS), so the GeoPackage merge stays on the GUI thread here.
    # The heavy-map freeze is a known trade-off; a safe off-thread merge would need a
    # thread-confined OGR path that never touches QgsProject/QgsVectorLayer.
    def apply_rows(rows, group_rows=(), from_cache=False):
        groups = []
        for group_id, fields in group_rows or ():
            with contextlib.suppress(Exception):
                groups.append(TairuRecordGroup.from_fields(group_id, fields))
        records = []
        parse_errors = []
        for record_id, fields in rows:
            try:
                records.append(TairuRecord.from_fields(record_id, fields))
            except Exception as e:
                parse_errors.append((record_id, str(e)))

        try:
            result = apply_pull(
                paths['gpkg'],
                records,
                remove_missing=from_cache or not is_incremental,
                # No pull de migracao, NAO levar junto o que o usuario desenhou e ainda
                # nao enviou: o pull completo normal limpa essas feicoes de proposito
                # (o estado local e refeito), mas este aqui so precisa preencher a coluna
                # do grupo, e ate ontem o mesmo clique era incremental e inofensivo.
                keep_unpushed=backfilling,
            )
        except Exception as e:
            page.set_busy(False)
            page.set_status(tr('Falha ao gravar GeoPackage: {erro}').format(erro=e), error=True)
            return None

        # NEVER mutate QgsProject re-entrantly while a generation is pumping its nested
        # event loop — addMapLayer there fires the wizard's layer combo and crashes
        # QGIS. run_or_defer runs this immediately in normal operation, or defers it
        # until the generation finishes.
        run_or_defer(lambda: add_record_layers_to_project(
            paths['gpkg'], tmap.nome or tmap.map_id, tmap.map_id, groups))

        page.set_busy(False)
        if from_cache:
            prefix = tr('Cache local')
        else:
            prefix = tr('Delta') if is_incremental else tr('Registros')
        summary = tr('{origem}: {novos} novos, {atualizados} atualizados, {removidos} removidos.').format(
            origem=prefix, novos=result.added, atualizados=result.updated, removidos=result.removed)
        errors = result.errors + parse_errors
        if errors:
            summary += ' ' + tr('{n} com problema (ignorados).').format(n=len(errors))
            for record_id, reason in errors[:20]:
                QgsMessageLog.logMessage(f'Registro {record_id}: {reason}',
                                         'Tairu Maps', _MSG_WARNING)
            if len(errors) > 20:
                QgsMessageLog.logMessage(f'... e mais {len(errors) - 20} erros',
                                         'Tairu Maps', _MSG_WARNING)
            page.set_status(tr('{resumo}\nPrimeiro erro: {erro} '
                               '(detalhes no painel Mensagens de Log, aba "Tairu Maps")').format(
                                   resumo=summary, erro=errors[0][1]),
                            error=(result.added + result.updated == 0))
        else:
            page.set_status(summary)
        dock.notify(f'{tmap.nome}: {summary}')
        return result

    def on_success(payload):
        rows, group_rows = payload
        if is_incremental:
            sync_watermark = _rows_server_watermark(rows) or since_millis
        else:
            sync_watermark = _rows_server_watermark(
                rows,
                empty_fallback_ms=pull_started_at,
            )
        cache_stored = False
        with contextlib.suppress(Exception):
            cache.store_records(
                tmap.map_id,
                rows,
                pull_started_at,
                full_snapshot=not is_incremental,
            )
            cache_stored = True
        # Os grupos vao para o mesmo cache: sem eles, abrir a expedicao sem rede mostraria
        # a arvore vazia e todo registro em "Sem grupo", que parece perda de dado.
        with contextlib.suppress(Exception):
            cache.store_records(tmap.map_id, group_rows, pull_started_at,
                                full_snapshot=True, collection=RECORD_GROUPS_COLLECTION)
        result = apply_rows(rows, group_rows)
        if result is None:
            return
        # Advance the sync cursor ONLY when the cache actually captured this batch.
        # If store_records failed, the cache is now missing rows the GeoPackage has;
        # advancing the watermark would make the next pull skip past them, and a later
        # OFFLINE pull (which rebuilds the GeoPackage from the cache via remove_missing)
        # would delete those local records. Leaving the cursor put makes the next pull
        # re-fetch and re-attempt the cache write, healing the divergence.
        if not is_incremental:
            # So depois de um recebimento completo bem-sucedido: o marcador e o que impede
            # a migracao de rodar de novo, e tambem o que impede que ela seja cancelada
            # para sempre por um envio ter criado a coluna numa tabela.
            with contextlib.suppress(Exception):
                mark_record_schema_generation(dock.env.key, tmap.map_id,
                                              RECORD_SCHEMA_GENERATION)
        if cache_stored:
            with contextlib.suppress(Exception):
                cache.save_sync_state(
                    tmap.map_id,
                    RECORDS_COLLECTION,
                    sync_watermark,
                    full_snapshot=not is_incremental,
                )
            save_last_pull_ts(paths, sync_watermark)

    def on_error(message):
        cached_rows = []
        cached_groups = []
        cache_loaded = False
        if has_full_cache_snapshot:
            try:
                cached_rows = cache.load_records(tmap.map_id, include_deleted=True)
                cache_loaded = True
            except Exception:
                cached_rows = []
            with contextlib.suppress(Exception):
                cached_groups = cache.load_records(
                    tmap.map_id, collection=RECORD_GROUPS_COLLECTION)
        if cache_loaded:
            result = apply_rows(cached_rows, cached_groups, from_cache=True)
            if result is not None:
                page.set_status(
                    tr('Falha ao atualizar online. Usando cache local.\n{erro}').format(erro=message),
                    error=False,
                )
                dock.notify(tr('{expedicao}: registros carregados do cache local.').format(expedicao=tmap.nome))
                return
        page.set_busy(False)
        page.set_status(message, error=True)

    run_task(tr('Tairu Maps: registros de {expedicao}').format(expedicao=tmap.nome), fetch,
             on_success=on_success, on_error=on_error,
             on_progress=lambda f, m: page.set_progress(f, m))


def start_tairudb_download(dock, tmap, file_name):
    """Download a tairudb file, convert to MBTiles and add as raster layers."""
    storage = dock.storage
    page = dock.detail_page
    paths = map_workspace(dock.env.key, tmap.map_id)
    local_path = os.path.join(paths['downloads'], file_name)
    object_path = TAIRUDB_OBJECT_PATH.format(map_id=tmap.map_id, file_name=file_name)

    page.set_busy(True, tr('Baixando {arquivo}…').format(arquivo=file_name))

    def fetch(task):
        def dl_progress(done, total):
            if total:
                task.report(0.7 * done / total, tr('Baixando {arquivo}… {feitos} de {total} MB').format(
                    arquivo=file_name, feitos=done // (1024*1024), total=total // (1024*1024)))

        storage.download(object_path, local_path,
                         progress_cb=dl_progress, cancel_cb=task.isCanceled)
        task.report(0.75, tr('Convertendo para MBTiles…'))
        results = tairudb_to_mbtiles(
            local_path, paths['mbtiles'],
            progress_cb=lambda f: task.report(0.75 + 0.25 * f))
        return results

    def on_success(results):
        # Same re-entrancy guard as the records pull: adding raster layers to the
        # project while a generation pumps its nested loop can crash QGIS.
        def add_layers():
            added = 0
            for mbtiles_path, region_label in results:
                name = f'{os.path.splitext(file_name)[0]} — {region_label}'
                if add_raster_to_project(mbtiles_path, name,
                                         tmap.nome or tmap.map_id, tmap.map_id):
                    added += 1
            page.set_status(tr('{arquivo}: {n} camada(s) raster adicionada(s).').format(arquivo=file_name, n=added))
        page.set_busy(False)
        run_or_defer(add_layers)
        dock.notify(tr('{arquivo} adicionado ao projeto.').format(arquivo=file_name))

    def on_error(message):
        page.set_busy(False)
        page.set_status(message, error=True)

    run_task(tr('Tairu Maps: download {arquivo}').format(arquivo=file_name), fetch,
             on_success=on_success, on_error=on_error,
             on_progress=lambda f, m: page.set_progress(f, m))
