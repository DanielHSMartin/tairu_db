# -*- coding: utf-8 -*-

"""A MESMA transformação de datum do app Tairu, imposta ao QGIS.

O app guarda tudo em WGS84 e converte SAD69, Córrego Alegre, PSAD56, NAD27,
Bogotá 1975 e Campo Inchauspe pela operação EPSG mais precisa da região
(`datum_ops.py`, gerado junto com a tabela do app). O QGIS, sem entrada no
contexto de transformação, escolhe sozinho — e nem é coerente consigo mesmo:
SAD69 geográfico sai pela média continental (EPSG:1864) e SAD69 / UTM 23S pela
EPSG:5882; em Santiago o PSAD56 sai pela EPSG:1201, a 57 m da EPSG:6972 do app.
O dado exportado ou enviado pelo plugin caía longe de onde o app o põe.

`datum_context` devolve uma CÓPIA do contexto com a operação do app para cada
SRC envolvido, nos pares src→EPSG:4326 (área, exportação, envio) e
src→EPSG:3857 (render dos tiles): o contexto casa o PAR EXATO, um não vale pelo
outro. Entrada que o usuário pôs no projeto para o par prevalece.
"""

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsCsException,
    QgsDatumTransform,
    QgsMessageLog,
    QgsProject,
)

try:
    from .datum_ops import default_operation, operation_for
    from .i18n import tr
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_core.datum_ops import default_operation, operation_for
    from tairu_core.i18n import tr

_WGS84 = 'EPSG:4326'
_TARGETS = (_WGS84, 'EPSG:3857')

# (src, dst, código) -> TransformDetails ou None. operations() custa até 120 ms
# (NAD27 → 4326, medido no QGIS 3.40) e to_wgs84 roda uma vez por feição.
# ponytail: sem despejo — são poucos SRC por sessão.
_OPS = {}
_LOGGED = set()


def _log(text, level=Qgis.MessageLevel.Info):
    """Uma vez por sessão: to_wgs84 passa por aqui a cada feição."""
    if text not in _LOGGED:
        _LOGGED.add(text)
        QgsMessageLog.logMessage(text, 'TairuDB', level)


def _operation(src, dst, code):
    """A operação de src→dst que passa pela transformação EPSG `code`, ou None."""
    key = (src.authid() or src.toWkt(), dst.authid(), code)
    if key not in _OPS:
        # O código da transformação vem num dos passos: 'EPSG', 'INVERSE(EPSG)'
        # ou 'DERIVED_FROM(EPSG)' (a grade do IBGE vem assim).
        _OPS[key] = next((op for op in QgsDatumTransform.operations(src, dst)
                          if any(step.code == str(code) and 'EPSG' in step.authority
                                 for step in op.operationDetails)), None)
    return _OPS[key]


def _base_epsg(crs):
    """Código EPSG do SRC geográfico de `crs` (4618 para SAD69 / UTM 23S)."""
    authid = crs.geographicCrsAuthId() if crs.isValid() else ''
    return int(authid[5:]) if authid.upper().startswith('EPSG:') and authid[5:].isdigit() else None


def _chosen(crs, extent, epsg, base):
    """(código, região) do app para o centro de `extent` — em WGS84, como o app decide."""
    if extent is None or extent.isNull():
        return default_operation(epsg), None
    try:
        center = QgsCoordinateTransform(
            crs, QgsCoordinateReferenceSystem(_WGS84), base).transform(extent.center())
    except QgsCsException as exc:
        _log(f'Datum: centro de {crs.authid()} não reprojetou ({exc}); vale o padrão do datum.')
        return default_operation(epsg), None
    return operation_for(epsg, center.x(), center.y())


def datum_context(base, sources, warn=None):
    """Cópia de `base` com a operação do app para o SRC de cada item de `sources`.

    base: o contexto do projeto (ou do Processing); None = o do projeto.
    sources: camadas, ou pares (SRC, QgsRectangle naquele SRC).
    warn(texto): aviso VISÍVEL quando a grade da operação não está instalada.

    ponytail: uma operação por par de SRC — é o que o contexto do QGIS guarda.
    Duas camadas no mesmo SRC em regiões diferentes: vale a última.
    """
    if base is None:
        base = QgsProject.instance().transformContext()
    ctx = QgsCoordinateTransformContext(base)
    for source in sources:
        crs, extent = source if isinstance(source, tuple) else (source.crs(), source.extent())
        epsg = _base_epsg(crs)
        if epsg is None or default_operation(epsg) is None:
            continue
        wanted, region = _chosen(crs, extent, epsg, base)
        details = _operation(crs, QgsCoordinateReferenceSystem(_WGS84), wanted)
        # Grade ausente (isAvailable False): cai no padrão do datum e AVISA.
        missing = details is not None and not details.isAvailable
        code = default_operation(epsg) if missing or details is None else wanted
        if details is None:
            _log(f'Datum: EPSG:{wanted} não existe para {crs.authid()}; vale o padrão do datum.',
                 Qgis.MessageLevel.Warning)
        fell_back = False
        for target in _TARGETS:
            dst = QgsCoordinateReferenceSystem(target)
            pair = f'{crs.authid() or crs.description()} → {target}'
            op = _operation(crs, dst, code)
            if op is None:
                _log(f'Datum: EPSG:{code} não existe para {pair}; fica a escolha do QGIS.',
                     Qgis.MessageLevel.Warning)
                continue
            if base.hasTransform(crs, dst):
                if base.calculateCoordinateOperation(crs, dst) != op.proj:
                    _log(f'Datum: {pair} segue a transformação escolhida no projeto, '
                         f'não a do app (EPSG:{code}).')
                continue
            ctx.addCoordinateOperation(crs, dst, op.proj)
            fell_back = fell_back or missing
            _log(f'Datum: {pair} por EPSG:{code} ({region or "padrão do datum"}).')
        if fell_back:
            grids = [g for g in details.grids if not g.isAvailable]
            text = tr('A grade de transformação {grade} não está instalada neste QGIS: os dados '
                      'de «{origem}» foram convertidos pela transformação padrão do datum '
                      '(EPSG:{codigo}), e as posições podem divergir alguns metros das do app '
                      'Tairu, que usa a grade. A grade pode ser baixada em {url}.').format(
                          grade=', '.join(g.shortName for g in grids),
                          origem=source.name() if hasattr(source, 'name') else crs.authid(),
                          codigo=code, url=', '.join(g.url for g in grids))
            _log(text, Qgis.MessageLevel.Warning)
            if warn is not None:
                warn(text)
    return ctx
