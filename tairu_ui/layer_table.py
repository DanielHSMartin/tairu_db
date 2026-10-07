# -*- coding: utf-8 -*-

"""A tabela de camadas vetoriais do envio e da geração do .tairudb.

Caixa | Camada | Geometria | Feições | SRC | Origem: a camada tem dados que decidem a
escolha, e o nome leva a pasta, porque com a árvore de grupos uma expedição tem várias
camadas "Pontos". As duas telas montam a lista daqui para não divergirem — a etapa do
assistente era uma coluna de caixas só com o nome, na ordem dos ids.
"""

from qgis.PyQt.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem
from qgis.core import QgsProject, QgsVectorLayer

try:
    from ..compat import _CHECKED, _ITEM_IS_CHECKABLE, _ITEM_IS_EDITABLE, _ITEM_IS_ENABLED, _UNCHECKED
    from ..tairu_core.i18n import tr
    from ..tairu_core.layer_tree import layer_is_visible
    from ..tairu_sync.record_convert import is_record_sync_layer, layer_origin_map_id
    from .style import apply_table_style
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _CHECKED, _ITEM_IS_CHECKABLE, _ITEM_IS_EDITABLE, _ITEM_IS_ENABLED, _UNCHECKED
    from tairu_core.i18n import tr
    from tairu_core.layer_tree import layer_is_visible
    from tairu_sync.record_convert import is_record_sync_layer, layer_origin_map_id
    from tairu_ui.style import apply_table_style

CHECK_COL = 0
NAME_COL = 1
# QgsVectorLayer.geometryType() -> Qgis.GeometryType (Point/Line/Polygon/Unknown/Null).
GEOMETRY_LABELS = {
    0: tr('Ponto'), 1: tr('Linha'), 2: tr('Polígono'), 3: tr('Desconhecida'), 4: tr('Sem geometria'),
}

__all__ = ['CHECK_COL', 'NAME_COL', 'checked_rows', 'fill_layer_table', 'is_record_sync_layer',
           'layer_display_name', 'new_layer_table', 'visible_vector_layers']


def layer_display_name(layer):
    """Nome da camada com a pasta em que ela está.

    Com a árvore de grupos a mesma expedição tem várias camadas chamadas "Pontos": sem
    o nome da pasta a tabela de escolha vira uma lista de repetidos e não há como saber
    o que se está escolhendo.
    """
    name = layer.name()
    try:
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        parent = node.parent() if node is not None else None
        parent_name = parent.name() if parent is not None else ''
    except Exception:
        parent_name = ''
    return f'{parent_name} / {name}' if parent_name else name


def _origin_label(layer, map_id):
    """QGIS, Tairu Maps ou Outra expedição — esta só quando há uma expedição de destino."""
    if layer.fields().indexOf('recordId') < 0:
        return 'QGIS'
    origin = layer_origin_map_id(layer)
    return tr('Outra expedição') if origin and map_id and origin != map_id else 'Tairu Maps'


def _row_values(layer, map_id):
    """Camada | Geometria | Feições | SRC | Origem."""
    try:
        geometry = GEOMETRY_LABELS.get(int(layer.geometryType()), '—')
    except (TypeError, ValueError):
        geometry = '—'
    # featureCount() devolve -1 quando o provedor não sabe contar sem varrer.
    count = layer.featureCount()
    count_text = str(count) if count is not None and count >= 0 else '—'
    return [layer_display_name(layer), geometry, count_text, layer.crs().authid() or '—',
            _origin_label(layer, map_id)]


def visible_vector_layers(project=None, skip=None):
    """(camadas vetoriais válidas e visíveis, na ordem do painel; quantas estão ocultas).

    Camada desmarcada no painel de camadas não é desenhada no mapa e não é oferecida.
    `skip(camada)` tira a camada da lista sem contá-la como oculta.

    Os nós do painel, e não layerOrder(): esta é a ordem de DESENHO — segue a ordem
    personalizada do projeto e devolve o grupo "renderizado como camada" no lugar das
    camadas de dentro dele, que sumiam da lista sem aviso. Só camadas com geometria, como
    o layerOrder() já fazia: no envio, que vem todo marcado, uma tabela de apoio (um CSV
    do projeto) subiria como registros sem geometria.
    """
    project = project or QgsProject.instance()
    layers, hidden, seen = [], 0, set()
    for layer in (node.layer() for node in project.layerTreeRoot().findLayers()):
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid() or not layer.isSpatial():
            continue
        # A mesma camada em dois nós do painel viraria duas linhas — e, marcada, entraria
        # duas vezes no arquivo ou seria enviada duas vezes.
        if layer.id() in seen:
            continue
        seen.add(layer.id())
        if skip is not None and skip(layer):
            continue
        if not layer_is_visible(layer, project):
            hidden += 1
            continue
        layers.append(layer)
    return layers, hidden


def new_layer_table(check_header):
    """A tabela vazia, com o cabeçalho e o estilo do plugin."""
    headers = [check_header, tr('Camada'), tr('Geometria'), tr('Feições'), tr('SRC'), tr('Origem')]
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    apply_table_style(table)
    # O nome ocupa o que sobra e as outras colunas ficam do tamanho do conteúdo: no
    # assistente (680 px) as larguras somadas passavam da tabela e cortavam a Origem.
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(NAME_COL, QHeaderView.ResizeMode.Stretch)
    return table


def fill_layer_table(table, layers, map_id=None, checked=None, blocked=None):
    """Uma linha por camada.

    `checked(camada)` decide se a caixa começa marcada (sem ele, todas começam).
    `blocked(camada)` devolve o motivo de a linha ficar desligada — cinza, sem caixa, e o
    motivo na dica de cada célula — ou '' para uma linha normal.
    """
    table.blockSignals(True)
    table.setRowCount(len(layers))
    for row, layer in enumerate(layers):
        reason = blocked(layer) if blocked is not None else ''
        box = QTableWidgetItem('')
        if reason:
            # sem caixa: uma caixa em branco numa linha que não marca parece clicável
            box.setFlags(box.flags() & ~_ITEM_IS_EDITABLE & ~_ITEM_IS_ENABLED & ~_ITEM_IS_CHECKABLE)
        else:
            box.setFlags((box.flags() & ~_ITEM_IS_EDITABLE) | _ITEM_IS_CHECKABLE)
            box.setCheckState(_CHECKED if checked is None or checked(layer) else _UNCHECKED)
        cells = [box] + [QTableWidgetItem('' if text is None else str(text)) for text in _row_values(layer, map_id)]
        for col, cell in enumerate(cells):
            if col != CHECK_COL:
                cell.setFlags(cell.flags() & ~_ITEM_IS_EDITABLE & ~_ITEM_IS_ENABLED if reason
                              else cell.flags() & ~_ITEM_IS_EDITABLE)
            if col == NAME_COL:
                # o nome com a pasta pode sair cortado — e a linha desligada também
                # precisa dizer de que camada se trata
                cell.setToolTip(f'{cell.text()}\n{reason}' if reason else cell.text())
            elif reason:
                cell.setToolTip(reason)
            table.setItem(row, col, cell)
    # Sem célula atual: com o foco na tabela ao abrir a etapa, o quadro de foco da
    # primeira célula aparecia ao lado da caixa como uma segunda caixa vazia.
    table.setCurrentCell(-1, -1)
    table.blockSignals(False)


def checked_rows(table):
    """Linhas com a caixa marcada (as desligadas nunca estão)."""
    rows = []
    for row in range(table.rowCount()):
        cell = table.item(row, CHECK_COL)
        if cell is not None and cell.checkState() == _CHECKED and cell.flags() & _ITEM_IS_ENABLED:
            rows.append(row)
    return rows
