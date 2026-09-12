# -*- coding: utf-8 -*-

"""Push em três etapas: escolher camadas, conferir feições, agrupar e enviar."""

from qgis.PyQt.QtCore import QCoreApplication, QDateTime, QEventLoop, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit,
    QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QDateTimeEdit, QWidget,
)
from qgis.core import QgsProject, QgsVectorLayer

try:
    from ..tairu_core.layer_tree import layer_is_visible
    from ..tairu_sync.record_convert import FOLDER_PROPERTY as _FOLDER_PROPERTY
    from ..tairu_sync.record_convert import layer_origin_map_id
    from ..tairu_firebase.models import RECORD_TYPES, RECORD_SUBTYPES, SUBTYPES_BY_TYPE, SITUATIONS_BY_TYPE
    from ..tairu_sync.push import (
        apply_group_to_plan, batch_summary, build_push_plan, execute_push,
        group_candidate_count, record_group_id,
    )
    from ..tairu_core.vector_types import has_elevation_attribute
    from .style import (
        apply_cell_combo_style, apply_table_style, apply_tairu_style, set_control_enabled,
        set_info_banner, set_muted, set_plain_button, set_primary_button, set_section_title,
    )
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_core.layer_tree import layer_is_visible
    from tairu_sync.record_convert import FOLDER_PROPERTY as _FOLDER_PROPERTY
    from tairu_sync.record_convert import layer_origin_map_id
    from tairu_firebase.models import RECORD_TYPES, RECORD_SUBTYPES, SUBTYPES_BY_TYPE, SITUATIONS_BY_TYPE
    from tairu_sync.push import (
        apply_group_to_plan, batch_summary, build_push_plan, execute_push,
        group_candidate_count, record_group_id,
    )
    from tairu_core.vector_types import has_elevation_attribute
    from tairu_ui.style import (
        apply_cell_combo_style, apply_table_style, apply_tairu_style, set_control_enabled,
        set_info_banner, set_muted, set_plain_button, set_primary_button, set_section_title,
    )

_ACTION_LABELS = {
    'new': 'Novo', 'update': 'Atualizar', 'unchanged': 'Inalterado',
    'forbidden': 'Sem permissão', 'delete': 'Excluir',
    'remote_changed': 'Mudou no Tairu', 'conflict': 'Conflito',
}
_GEOMETRY_LABELS = {
    'none': 'Sem geometria',
    'point': 'Ponto',
    'line': 'Linha',
    'polygon': 'Polígono',
    'circle': 'Círculo',
}
_DATA_COLUMNS = [
    ('nome', 'Nome', 'nome', 'text'),
    ('descricao', 'Descrição', 'descricao', 'text'),
    ('tipoRegistro', 'Tipo', 'tipo_registro', 'type'),
    ('subTipo', 'Subtipo', 'sub_tipo', 'subtype'),
    ('situation', 'Situação', 'situation', 'situation'),
    ('endereco', 'Endereço', 'endereco', 'text'),
    ('owner', 'Responsável', 'owner', 'text'),
    ('plateTag', 'Placa/Tag', 'plate_tag', 'text'),
    ('brand', 'Marca', 'brand', 'text'),
    ('model', 'Modelo', 'model', 'text'),
    ('year', 'Ano', 'year', 'int'),
    ('color', 'Cor', 'color', 'text'),
    ('size', 'Tamanho', 'size', 'float'),
    ('valueEstimate', 'Valor estim.', 'value_estimate', 'float'),
    ('eventDateTime', 'Data evento', 'event_date_time', 'datetime'),
    ('circleRadius', 'Raio (m)', 'circle_radius', 'float_optional'),
    ('geometrySize', 'Tamanho geom.', 'geometry_size', 'float_optional'),
]
_FIELD_LABELS = {key: label for key, label, _attr, _kind in _DATA_COLUMNS}
_FIELD_LABELS.update({
    'geometryType': 'Geometria',
    'geometryPoints': 'Pontos',
    'geometryBounds': 'Limites',
    'geometryColorValue': 'Cor geometria',
    'geometryBackgroundColorValue': 'Fundo geometria',
    'groupId': 'Grupo',
    'lastModified': 'Última alteração',
})
# Enviar (caixa) | Camada | Ação | ...campos... | Geometria | Detalhes
_SEND_COL = 0
_LAYER_COL = 1
_ACTION_COL = 2
_FIRST_DATA_COL = 3
_HEADERS = ['Enviar', 'Camada', 'Ação'] + [label for _key, label, _attr, _kind in _DATA_COLUMNS] + [
    'Geometria', 'Detalhes',
]
_COLUMN_BY_KEY = {key: _FIRST_DATA_COL + index
                  for index, (key, _label, _attr, _kind) in enumerate(_DATA_COLUMNS)}
_GEOMETRY_COL = _FIRST_DATA_COL + len(_DATA_COLUMNS)
_DETAILS_COL = _GEOMETRY_COL + 1
_ROUNDTRIP_TEXT = (
    'Alguma camada escolhida veio do Tairu Maps: os atributos existentes serão '
    'preservados e os registros ausentes aparecerão como exclusões na prévia.')
_COPY_TEXT = (
    'Alguma camada escolhida veio de OUTRA expedição: os registros serão criados '
    'como novos em «{nome}», em seu nome. Nada é excluído lá, e reenviar a mesma '
    'camada depois atualiza as cópias.')

_MAX_PREVIEW_ROWS = 500
# Rows between event-loop turns while filling the preview table (see _fill_table).
_FILL_PUMP_EVERY = 50

_ITEM_IS_EDITABLE = Qt.ItemFlag.ItemIsEditable
_ITEM_IS_CHECKABLE = Qt.ItemFlag.ItemIsUserCheckable
_CHECKED = Qt.CheckState.Checked
_UNCHECKED = Qt.CheckState.Unchecked
_EDIT_TRIGGERS = (
    QTableWidget.EditTrigger.DoubleClicked |
    QTableWidget.EditTrigger.EditKeyPressed |
    QTableWidget.EditTrigger.AnyKeyPressed
)

# Etapa 1 — Enviar (caixa) | Camada | Geometria | Feições | SRC | Origem
_LAYER_HEADERS = ['Enviar', 'Camada', 'Geometria', 'Feições', 'SRC', 'Origem']
_LAYER_SEND_COL = 0
_LAYER_NAME_COL = 1
# QgsVectorLayer.geometryType() -> Qgis.GeometryType (Point/Line/Polygon/Unknown/Null).
_LAYER_GEOMETRY_LABELS = {
    0: 'Ponto', 1: 'Linha', 2: 'Polígono', 3: 'Desconhecida', 4: 'Sem geometria',
}

_STEP_LAYERS, _STEP_FEATURES, _STEP_GROUP = 0, 1, 2
_STEP_TITLES = [
    'Etapa 1 de 3 · Camadas',
    'Etapa 2 de 3 · Feições',
    'Etapa 3 de 3 · Grupo de registros',
]


def _layer_display_name(layer):
    """Nome da camada com a pasta em que ela esta.

    Com a arvore de grupos a mesma expedicao tem varias camadas chamadas "Pontos": sem
    o nome da pasta a tabela de escolha vira uma lista de repetidos e nao ha como saber
    o que se esta enviando.
    """
    name = layer.name()
    try:
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        parent = node.parent() if node is not None else None
        parent_name = parent.name() if parent is not None else ''
    except Exception:
        parent_name = ''
    return f'{parent_name} / {name}' if parent_name else name


class _PreviewAborted(Exception):
    """The preview being computed was superseded, or the dialog went away."""


def open_push_dialog(dock, tmap):
    dialog = PushDialog(dock, tmap)
    dialog.exec()
    # The dialog is parented to the dock, so returning from exec() does NOT destroy it:
    # every closed dialog stays alive hidden and keeps listening to the project.
    # Opening "Enviar Camadas" N times made the next pull do N scans on the GUI
    # thread — the freeze users reported.
    dialog.deleteLater()


def _default_subtype(tipo):
    options = SUBTYPES_BY_TYPE.get(tipo, [])
    return options[-1] if options else ''


def _default_situation(tipo):
    options = SITUATIONS_BY_TYPE.get(tipo, ['Ativo'])
    return options[0] if options else ''


class PushDialog(QDialog):

    def __init__(self, dock, tmap):
        super().__init__(dock)
        self.dock = dock
        self.tmap = tmap
        # [(PushPlan, camada)] — um par por camada marcada na etapa 1.
        self.entries = []
        self._row_items = []
        self._empty_layers = []
        self._group_name_touched = False
        self._hidden_unchanged_count = 0
        self._truncated_preview_count = 0
        self._preview_generation = 0
        self.setWindowTitle(f'Enviar camadas vetoriais · {tmap.nome}')
        self.resize(1120, 640)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.step_label = set_section_title(QLabel(_STEP_TITLES[_STEP_LAYERS]))
        layout.addWidget(self.step_label)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_layers_page())
        self.pages.addWidget(self._build_features_page())
        self.pages.addWidget(self._build_group_page())
        layout.addWidget(self.pages, 1)

        buttons = QHBoxLayout()
        self.back_btn = set_plain_button(QPushButton('Voltar'))
        self.back_btn.clicked.connect(self._go_back)
        buttons.addWidget(self.back_btn)
        buttons.addStretch(1)
        self.next_btn = set_primary_button(QPushButton('Avançar'))
        self.next_btn.clicked.connect(self._go_next)
        buttons.addWidget(self.next_btn)
        cancel_btn = set_plain_button(QPushButton('Cancelar'))
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)
        apply_tairu_style(self)

        self._fill_layer_table()
        self._update_buttons()

    # ----------------------------------------------------------- etapa 1

    def _build_layers_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        hint = set_muted(QLabel(
            'Marque as camadas vetoriais a enviar. Todas as camadas visíveis já '
            'vêm marcadas.'))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Mesma tabela da etapa de feições: a camada também tem dados que decidem
        # a escolha (geometria, quantidade de feições, SRC, de onde ela veio).
        self._layer_ids = []
        self.layer_table = QTableWidget(0, len(_LAYER_HEADERS))
        self.layer_table.setHorizontalHeaderLabels(_LAYER_HEADERS)
        self.layer_table.setAlternatingRowColors(True)
        self.layer_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.layer_table.verticalHeader().setVisible(False)
        apply_table_style(self.layer_table)
        self.layer_table.itemChanged.connect(self._update_buttons)
        self.layer_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.layer_table, 1)

        self.hidden_label = set_muted(QLabel(''))
        self.hidden_label.setWordWrap(True)
        self.hidden_label.hide()
        layout.addWidget(self.hidden_label)
        return page

    def _fill_layer_table(self):
        """Camadas vetoriais visíveis, todas marcadas.

        O diálogo é modal, então a lista não muda enquanto ele está aberto — basta
        montá-la na abertura. Camada desmarcada no painel de camadas não é
        desenhada no mapa e não é oferecida aqui.
        """
        project = QgsProject.instance()
        hidden = 0
        rows = []
        self._layer_ids = []
        for layer in project.layerTreeRoot().layerOrder():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            if not layer_is_visible(layer, project):
                hidden += 1
                continue
            rows.append(layer)

        self.layer_table.blockSignals(True)
        self.layer_table.setRowCount(len(rows))
        for row, layer in enumerate(rows):
            self._layer_ids.append(layer.id())
            send = QTableWidgetItem('')
            send.setFlags((send.flags() & ~_ITEM_IS_EDITABLE) | _ITEM_IS_CHECKABLE)
            send.setCheckState(_CHECKED)
            self.layer_table.setItem(row, _LAYER_SEND_COL, send)
            for col, text in enumerate(self._layer_row_values(layer), start=_LAYER_NAME_COL):
                self.layer_table.setItem(row, col, self._readonly_item(text))
        self.layer_table.blockSignals(False)
        self.layer_table.resizeColumnsToContents()

        if not rows and hidden:
            self.hidden_label.setText(
                'Nenhuma camada vetorial visível. Camadas desmarcadas no painel de '
                'camadas não são enviadas — marque a camada no painel e abra esta '
                'janela de novo.')
            self.hidden_label.show()
        elif not rows:
            self.hidden_label.setText('Nenhuma camada vetorial neste projeto.')
            self.hidden_label.show()
        elif hidden:
            plural = 's' if hidden > 1 else ''
            self.hidden_label.setText(
                f'{hidden} camada{plural} oculta{plural} no painel de camadas '
                f'não {"são" if hidden > 1 else "é"} listada{plural}.')
            self.hidden_label.show()

    def _layer_row_values(self, layer):
        """Camada | Geometria | Feições | SRC | Origem."""
        try:
            geometry = _LAYER_GEOMETRY_LABELS.get(int(layer.geometryType()), '—')
        except (TypeError, ValueError):
            geometry = '—'
        # featureCount() devolve -1 quando o provedor não sabe contar sem varrer.
        count = layer.featureCount()
        count_text = str(count) if count is not None and count >= 0 else '—'
        crs = layer.crs().authid() or '—'
        return [_layer_display_name(layer), geometry, count_text, crs,
                self._layer_origin_label(layer)]

    def _layer_origin_label(self, layer):
        if layer.fields().indexOf('recordId') < 0:
            return 'QGIS'
        origin = layer_origin_map_id(layer)
        return 'Outra expedição' if origin and origin != self.tmap.map_id else 'Tairu Maps'

    def selected_layers(self):
        """Camadas marcadas aqui E ainda válidas no projeto, na ordem da tabela."""
        project = QgsProject.instance()
        layers = []
        for row, layer_id in enumerate(self._layer_ids):
            cell = self.layer_table.item(row, _LAYER_SEND_COL)
            if cell is None or cell.checkState() != _CHECKED:
                continue
            layer = project.mapLayer(layer_id)
            if layer is not None and layer.isValid():
                layers.append(layer)
        return layers

    # ----------------------------------------------------------- etapa 2

    def _build_features_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.roundtrip_label = set_info_banner(QLabel(_ROUNDTRIP_TEXT))
        self.roundtrip_label.setWordWrap(True)
        self.roundtrip_label.hide()
        layout.addWidget(self.roundtrip_label)

        self.summary_label = set_muted(QLabel('Calculando prévia…'))
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(_EDIT_TRIGGERS)
        apply_table_style(self.table)
        # One stylesheet for every combo the preview puts in a cell (see the helper):
        # applying it per widget was most of the time the table took to appear.
        apply_cell_combo_style(self.table)
        self.table.itemChanged.connect(self._on_table_item_changed)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        layout.addWidget(self.table, 1)
        return page

    # ----------------------------------------------------------- etapa 3

    def _build_group_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.group_check = QCheckBox('Reunir em um grupo os registros que ainda não têm um')
        # Marcada por padrão: reunir o lote é o que quase todo envio quer, e o
        # nome já vem preenchido. Desmarcar desliga o campo de nome.
        self.group_check.setChecked(True)
        self.group_check.toggled.connect(self._on_group_toggled)
        layout.addWidget(self.group_check)

        hint = set_muted(QLabel(
            'O grupo aparece na aba Registros do aplicativo e pode ser renomeado '
            'lá depois. Reenviar as mesmas camadas com o mesmo nome reaproveita o '
            'grupo em vez de criar outro. Entram só os registros deste envio que '
            'ainda não pertencem a um grupo — o plugin nunca tira um registro do '
            'grupo em que o aplicativo já o colocou.'))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        name_row = QHBoxLayout()
        self.group_name_label = QLabel('Nome do grupo:')
        name_row.addWidget(self.group_name_label)
        self.group_name_edit = QLineEdit()
        self.group_name_edit.setMaxLength(80)
        self.group_name_edit.textChanged.connect(self._update_buttons)
        # textEdited (não textChanged) só dispara com digitação: é o que separa
        # "o usuário escolheu este nome" de "nós preenchemos o padrão".
        self.group_name_edit.textEdited.connect(self._on_group_name_edited)
        name_row.addWidget(self.group_name_edit, 1)
        layout.addLayout(name_row)

        self.group_summary_label = set_muted(QLabel(''))
        self.group_summary_label.setWordWrap(True)
        layout.addWidget(self.group_summary_label)
        layout.addStretch(1)

        # Os botões do rodapé ainda não existem neste ponto da construção: só o
        # estado dos campos é ajustado aqui.
        self._set_group_name_enabled(self.group_check.isChecked())
        return page

    def _on_group_name_edited(self, _text):
        self._group_name_touched = True

    def _set_group_name_enabled(self, enabled):
        """Liga/desliga o campo de nome junto com a caixa de agrupar.

        set_control_enabled, e não setEnabled: o TAIRU_STYLE_SHEET não tem regra
        `:disabled` para QLineEdit/QLabel, então um setEnabled(False) sozinho fica
        com a MESMA aparência de um campo ativo — desabilitado e sem parecer.
        """
        set_control_enabled(self.group_name_label, enabled)
        set_control_enabled(self.group_name_edit, enabled)

    def _on_group_toggled(self, checked):
        self._set_group_name_enabled(checked)
        self._refresh_group_summary()
        self._update_buttons()

    def _refresh_group_summary(self):
        plans = [plan for plan, _layer in self.entries]
        if not plans:
            self.group_summary_label.setText('')
            return
        text = f'Serão enviados: {batch_summary(plans)}.'
        if self.group_check.isChecked():
            # Dizer o número evita as duas surpresas do modelo antigo: a de reunir mais
            # do que o usuário vê marcado, e a de marcar a caixa e nada acontecer porque
            # todo registro escolhido já tem grupo no aplicativo.
            entram = sum(group_candidate_count(plan) for plan in plans)
            if entram:
                text += (f' Entram no grupo {entram} '
                         f'{"registro" if entram == 1 else "registros"}.')
            else:
                text += (' Nenhum registro entra no grupo: os que serão enviados já têm '
                         'grupo no aplicativo, e o plugin não desfaz essa organização.')
        else:
            text += ' Cada registro mantém o grupo em que já está no aplicativo.'
        self.group_summary_label.setText(text)

    def _default_group_name(self):
        # layer.name(), NUNCA o rotulo com a pasta: este texto vira o nome do grupo e,
        # por record_group_id(map_id, uid, nome), tambem o ID do documento — que existe
        # para reenviar a mesma camada cair no MESMO grupo. Fazendo-o depender da pasta do
        # painel, arrastar a camada entre dois envios forjaria um segundo grupo no app.
        names = [plan.layer_name for plan, _layer in self.entries if plan.layer_name]
        return names[0] if len(names) == 1 else 'Camadas do QGIS'

    def _all_layers_already_grouped(self):
        """True quando TODA camada escolhida ja e uma pasta da arvore desta expedicao.

        Nesse caso os registros ja carregam o grupo deles, entao a caixa comeca
        desmarcada: marca-la nao faria nada (apply_group_to_plan pula quem ja tem grupo)
        e so deixaria um grupo vazio no aplicativo.
        """
        if not self.entries:
            return False
        prefix = f'{self.tmap.map_id}|'
        return all(str(layer.customProperty(_FOLDER_PROPERTY, '') or '').startswith(prefix)
                   for _plan, layer in self.entries)

    def selected_group(self):
        """(group_id, nome) escolhido na etapa 3, ou None."""
        if not self.group_check.isChecked():
            return None
        name = self.group_name_edit.text().strip()
        if not name:
            return None
        return record_group_id(self.tmap.map_id, self.dock.tokens.uid, name), name

    # ------------------------------------------------------- navegação

    def _go_back(self):
        if self.pages.currentIndex() > _STEP_LAYERS:
            self._show_step(self.pages.currentIndex() - 1)

    def _go_next(self):
        step = self.pages.currentIndex()
        if step == _STEP_LAYERS:
            self._show_step(_STEP_FEATURES)
            self._compute_preview()
        elif step == _STEP_FEATURES:
            if not self._apply_table_to_plan():
                return
            if not self._group_name_touched:
                self.group_name_edit.setText(self._default_group_name())
                self.group_check.setChecked(not self._all_layers_already_grouped())
            self._refresh_group_summary()
            self._show_step(_STEP_GROUP)
        else:
            self._send()

    def _show_step(self, step):
        self.pages.setCurrentIndex(step)
        self.step_label.setText(_STEP_TITLES[step])
        self._update_buttons()

    def _update_buttons(self, *_args):
        step = self.pages.currentIndex()
        self.back_btn.setEnabled(step > _STEP_LAYERS)
        self.next_btn.setText('Enviar' if step == _STEP_GROUP else 'Avançar')
        if step == _STEP_LAYERS:
            self.next_btn.setEnabled(bool(self.selected_layers()))
        elif step == _STEP_FEATURES:
            self.next_btn.setEnabled(
                any(plan.writable_items() for plan, _layer in self.entries))
        else:
            self.next_btn.setEnabled(
                not self.group_check.isChecked()
                or bool(self.group_name_edit.text().strip()))

    # ------------------------------------------------------------- preview

    def _mapping(self, layer):
        tipo = 'local'
        sub_tipo = None
        if layer is not None and has_elevation_attribute(layer.fields().names()):
            tipo = 'curvaNivel'
            sub_tipo = 'curvaNormal'
        return {
            'nome_field': None,
            'descricao_field': None,
            'tipo': tipo,
            'sub_tipo': sub_tipo if sub_tipo is not None else _default_subtype(tipo),
            'situation': _default_situation(tipo),
        }

    def _compute_preview(self):
        self._preview_generation += 1
        generation = self._preview_generation
        self.entries = []
        self._row_items = []
        self.table.setRowCount(0)
        self._update_roundtrip_banner()

        layers = self.selected_layers()
        self._empty_layers = []
        empty = self._empty_layers
        for layer in layers:
            if layer.featureCount() == 0:
                empty.append(layer.name())
                continue
            include_deletions = layer.fields().indexOf('recordId') >= 0
            try:
                plan = build_push_plan(
                    layer, self._mapping(layer), self.tmap, self.dock.tokens.uid,
                    propagate_deletions=include_deletions,
                    progress=self._preview_progress(generation, layer.name()))
            except _PreviewAborted:
                return
            except Exception as e:
                # Prévia incompleta não pode habilitar o Enviar: o que já foi
                # calculado não é o que a tabela (vazia) está mostrando.
                self.entries = []
                self.summary_label.setText(f'Falha ao montar prévia de «{layer.name()}»: {e}')
                self._update_buttons()
                return
            self.entries.append((plan, layer))

        try:
            self._fill_table(generation)
        except _PreviewAborted:
            return
        self._refresh_summary()
        self._update_buttons()

    def _update_roundtrip_banner(self):
        roundtrip = False
        copied = False
        for layer in self.selected_layers():
            if layer.fields().indexOf('recordId') < 0:
                continue
            roundtrip = True
            origin = layer_origin_map_id(layer)
            if origin and origin != self.tmap.map_id:
                copied = True
        if copied:
            # O texto padrão prometeria exclusões que não vão acontecer: nada é apagado
            # na expedição de destino, onde estes registros nem existem ainda.
            self.roundtrip_label.setText(_COPY_TEXT.format(nome=self.tmap.nome))
        else:
            self.roundtrip_label.setText(_ROUNDTRIP_TEXT)
        self.roundtrip_label.setVisible(roundtrip)

    def _refresh_summary(self):
        extras = []
        if self._hidden_unchanged_count:
            extras.append(f'{self._hidden_unchanged_count} inalterados ocultos')
        if self._truncated_preview_count:
            extras.append(f'{self._truncated_preview_count} itens além do limite da tabela '
                          f'(enviados assim mesmo)')
        for name in self._empty_layers:
            extras.append(f'«{name}» não possui feições')
        suffix = f' {"; ".join(extras)}.' if extras else ''
        plans = [plan for plan, _layer in self.entries]
        if not plans:
            self.summary_label.setText(f'Nada a enviar.{suffix}')
            return
        self.summary_label.setText(f'Prévia: {batch_summary(plans)}.{suffix}')

    def _preview_progress(self, generation, layer_name):
        """Keep the window painted while the plan is built.

        The scan is Python-heavy (~0.3 ms/feature measured on QGIS 3.40 LTR) and runs on
        the GUI thread, so a layer with thousands of features used to block before the
        dialog's first paint: a blank window titled "(Não está respondendo)".
        User input stays excluded — pumping it here would let a click re-enter the
        preview (or the send) in the middle of the scan.
        """
        def report(done, total, phase='Calculando prévia'):
            if total:
                self.summary_label.setText(
                    f'{phase} de «{layer_name}»… {done} de {total} feições')
            else:
                self.summary_label.setText(f'{phase} de «{layer_name}»… {done} feições')
            self._pump(generation)
        return report

    def _pump(self, generation):
        """Give the event loop a turn, then bail out if this preview was superseded."""
        if generation != self._preview_generation:
            raise _PreviewAborted()
        QCoreApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        # A pull finishing mid-scan adds layers; re-check after pumping, never
        # after the fact.
        if generation != self._preview_generation:
            raise _PreviewAborted()

    def _fill_table(self, generation=None):
        items = [(item, plan) for plan, _layer in self.entries for item in plan.items]
        visible = [(item, plan) for item, plan in items
                   if item.action != 'unchanged' or item.warning]
        shown = visible[:_MAX_PREVIEW_ROWS]
        self._row_items = [item for item, _plan in shown]
        self._hidden_unchanged_count = len(items) - len(visible)
        self._truncated_preview_count = max(0, len(visible) - len(shown))
        self.table.blockSignals(True)
        self.table.setRowCount(len(shown))
        for row, (item, plan) in enumerate(shown):
            # Each row is ~4 cell widgets; 500 of them measured 1.5 s in one blocked
            # chunk. Same treatment as the scan: hand the loop back regularly.
            if generation is not None and row and row % _FILL_PUMP_EVERY == 0:
                self.summary_label.setText(f'Montando prévia… {row} de {len(shown)} linhas')
                self._pump(generation)
            self.table.setItem(row, _SEND_COL, self._send_item(item))
            self.table.setItem(row, _LAYER_COL, self._readonly_item(plan.layer_name))
            self.table.setItem(row, _ACTION_COL,
                               self._readonly_item(_ACTION_LABELS.get(item.action, item.action)))
            for col_offset, (key, _label, attr, kind) in enumerate(_DATA_COLUMNS,
                                                                   start=_FIRST_DATA_COL):
                self._set_data_cell(row, col_offset, item, key, attr, kind)
            geometry = _GEOMETRY_LABELS.get(item.record.geometry_type, item.record.geometry_type or '')
            self.table.setItem(row, _GEOMETRY_COL, self._readonly_item(geometry))
            self.table.setItem(row, _DETAILS_COL, self._readonly_item(self._details_text(item)))
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(_COLUMN_BY_KEY['descricao'], 220)
        self.table.setColumnWidth(_DETAILS_COL, 220)

    def _details_text(self, item):
        details = ''
        if item.action in ('update', 'remote_changed', 'conflict'):
            labels = [_FIELD_LABELS.get(field, field) for field in item.changed_fields]
            details = ', '.join(labels)
        elif item.action == 'delete':
            details = 'ausente na camada'
        if item.warning:
            details = f'{details} ⚠ {item.warning}'.strip()
        return details

    def _send_item(self, item):
        """Caixa "Enviar" da linha, só para itens graváveis.

        Item não gravável (inalterado, sem permissão) fica com um traço em vez de
        uma caixa desmarcada: uma caixa desligada convida a ligá-la, e o estado
        dela não diria a verdade — um inalterado ENTRA no envio se a etapa 3
        escolher um grupo.
        """
        if item.action not in ('new', 'update', 'delete'):
            cell = self._readonly_item('—')
            cell.setToolTip('Este item não é enviado por si só.')
            return cell
        cell = QTableWidgetItem('')
        cell.setFlags((cell.flags() & ~_ITEM_IS_EDITABLE) | _ITEM_IS_CHECKABLE)
        cell.setCheckState(_CHECKED if item.send else _UNCHECKED)
        return cell

    def _on_table_item_changed(self, cell):
        if cell.column() != _SEND_COL:
            return
        row = cell.row()
        if row >= len(self._row_items):
            return
        item = self._row_items[row]
        if item.action not in ('new', 'update', 'delete'):
            return
        item.send = cell.checkState() == _CHECKED
        self._refresh_summary()
        self._update_buttons()

    def _readonly_item(self, text):
        item = QTableWidgetItem('' if text is None else str(text))
        item.setFlags(item.flags() & ~_ITEM_IS_EDITABLE)
        return item

    def _editable_item(self, text):
        return QTableWidgetItem('' if text is None else str(text))

    def _set_data_cell(self, row, col, item, key, attr, kind):
        record = item.record
        value = getattr(record, attr)
        editable = item.action in ('new', 'update', 'unchanged')
        if item.action == 'forbidden' or item.action == 'delete':
            editable = False
        if key == 'circleRadius' and record.geometry_type != 'circle':
            editable = False
            value = ''
        if item.action == 'new' and kind == 'subtype':
            allowed = SUBTYPES_BY_TYPE.get(record.tipo_registro, [])
            if value not in allowed:
                value = _default_subtype(record.tipo_registro)
        if item.action == 'new' and kind == 'situation':
            allowed = SITUATIONS_BY_TYPE.get(record.tipo_registro, ['Ativo'])
            if value not in allowed:
                value = _default_situation(record.tipo_registro)

        if kind == 'type':
            combo = self._type_combo(value, editable)
            combo.currentIndexChanged.connect(lambda _idx, r=row: self._refresh_type_dependents(r))
            self.table.setCellWidget(row, col, combo)
        elif kind == 'subtype':
            self.table.setCellWidget(row, col, self._subtype_combo(record.tipo_registro, value, editable))
        elif kind == 'situation':
            self.table.setCellWidget(row, col, self._situation_combo(record.tipo_registro, value, editable))
        elif kind == 'datetime':
            editor = QDateTimeEdit()
            editor.setDisplayFormat('yyyy-MM-dd HH:mm')
            editor.setCalendarPopup(True)
            editor.setDateTime(QDateTime.fromMSecsSinceEpoch(int(value or 0)))
            editor.setEnabled(editable)
            self.table.setCellWidget(row, col, editor)
        else:
            text = self._format_value(value, kind)
            self.table.setItem(row, col, self._editable_item(text) if editable else self._readonly_item(text))

    def _format_value(self, value, kind):
        if value is None:
            return ''
        if kind == 'int':
            return str(int(value or 0))
        if kind in ('float', 'float_optional'):
            if kind == 'float_optional' and value is None:
                return ''
            return ('%f' % float(value or 0.0)).rstrip('0').rstrip('.')
        return str(value)

    def _type_combo(self, value, editable):
        combo = QComboBox()
        self._fill_combo(combo, [(key, label) for key, label in RECORD_TYPES.items()], value)
        combo.setEnabled(editable)
        return combo

    def _subtype_combo(self, tipo, value, editable):
        combo = QComboBox()
        options = [(key, RECORD_SUBTYPES.get(key, key)) for key in SUBTYPES_BY_TYPE.get(tipo, [])]
        self._fill_combo(combo, options, value)
        combo.setEnabled(editable)
        return combo

    def _situation_combo(self, tipo, value, editable):
        combo = QComboBox()
        options = [(sit, sit) for sit in SITUATIONS_BY_TYPE.get(tipo, ['Ativo'])]
        self._fill_combo(combo, options, value)
        combo.setEnabled(editable)
        return combo

    def _fill_combo(self, combo, options, value):
        combo.blockSignals(True)
        combo.clear()
        keys = []
        for key, label in options:
            combo.addItem(label, key)
            keys.append(key)
        if value not in (None, '') and value not in keys:
            combo.addItem(str(value), value)
        index = combo.findData(value)
        if index < 0 and combo.count():
            index = 0
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _refresh_type_dependents(self, row):
        type_combo = self.table.cellWidget(row, _COLUMN_BY_KEY['tipoRegistro'])
        subtype_combo = self.table.cellWidget(row, _COLUMN_BY_KEY['subTipo'])
        situation_combo = self.table.cellWidget(row, _COLUMN_BY_KEY['situation'])
        if type_combo is None:
            return
        tipo = type_combo.currentData()
        if subtype_combo is not None:
            current = subtype_combo.currentData()
            options = [(key, RECORD_SUBTYPES.get(key, key)) for key in SUBTYPES_BY_TYPE.get(tipo, [])]
            if current not in SUBTYPES_BY_TYPE.get(tipo, []):
                current = _default_subtype(tipo)
            self._fill_combo(subtype_combo, options, current)
        if situation_combo is not None:
            current = situation_combo.currentData()
            options = [(sit, sit) for sit in SITUATIONS_BY_TYPE.get(tipo, ['Ativo'])]
            allowed = [key for key, _label in options]
            if current not in allowed:
                current = _default_situation(tipo)
            self._fill_combo(situation_combo, options, current)

    def _combo_value(self, row, key):
        combo = self.table.cellWidget(row, _COLUMN_BY_KEY[key])
        return combo.currentData() if combo is not None else ''

    def _cell_text(self, row, key):
        item = self.table.item(row, _COLUMN_BY_KEY[key])
        return item.text() if item is not None else ''

    def _datetime_value(self, row, key):
        editor = self.table.cellWidget(row, _COLUMN_BY_KEY[key])
        return int(editor.dateTime().toMSecsSinceEpoch()) if editor is not None else 0

    def _parse_int(self, text, label):
        text = (text or '').strip()
        if not text:
            return 0
        try:
            return int(float(text.replace(',', '.')))
        except ValueError:
            raise ValueError(f'{label}: valor inteiro inválido.')

    def _parse_float(self, text, label, optional=False):
        text = (text or '').strip()
        if not text:
            return None if optional else 0.0
        try:
            return float(text.replace(',', '.'))
        except ValueError:
            raise ValueError(f'{label}: valor numérico inválido.')

    def _apply_table_to_plan(self):
        try:
            for row, item in enumerate(self._row_items):
                if item.action not in ('new', 'update', 'unchanged'):
                    continue
                for key, label, attr, kind in _DATA_COLUMNS:
                    if key == 'circleRadius' and item.record.geometry_type != 'circle':
                        continue
                    if kind == 'type':
                        value = self._combo_value(row, key)
                    elif kind in ('subtype', 'situation'):
                        value = self._combo_value(row, key)
                    elif kind == 'datetime':
                        value = self._datetime_value(row, key)
                    elif kind == 'int':
                        value = self._parse_int(self._cell_text(row, key), label)
                    elif kind == 'float':
                        value = self._parse_float(self._cell_text(row, key), label)
                    elif kind == 'float_optional':
                        value = self._parse_float(self._cell_text(row, key), label, optional=True)
                    else:
                        value = self._cell_text(row, key)
                    self._set_record_value(item, key, attr, value)
        except ValueError as e:
            self.summary_label.setText(str(e))
            return False
        return True

    def _set_record_value(self, item, field_key, attr, value):
        current = getattr(item.record, attr)
        if self._same_value(current, value):
            return
        setattr(item.record, attr, value)
        if item.action == 'unchanged':
            item.action = 'update'
        if item.action == 'update' and field_key not in item.changed_fields:
            item.changed_fields.append(field_key)

    def _same_value(self, left, right):
        if isinstance(left, float) or isinstance(right, float):
            return abs(float(left or 0.0) - float(right or 0.0)) <= 1e-9
        return (left or None) == (right or None) or (left or '') == (right or '')

    # ---------------------------------------------------------------- send

    def _send(self):
        if not self.entries:
            return
        group = self.selected_group()
        if group is not None:
            entraram = sum(apply_group_to_plan(plan, group[0]) for plan, _layer in self.entries)
            if not entraram:
                # Sem ninguem para reunir, criar o RecordGroup so deixaria uma pasta vazia
                # na aba Registros do aplicativo.
                group = None
        entries = list(self.entries)
        self.accept()
        execute_push(self.dock, self.tmap, entries, group=group)
