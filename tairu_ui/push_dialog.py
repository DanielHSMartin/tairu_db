# -*- coding: utf-8 -*-

"""Push dialog: pick a vector layer, preview records, edit values, then send."""

from qgis.PyQt.QtCore import QDateTime, QTimer, Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QDateTimeEdit,
)
from qgis.gui import QgsMapLayerComboBox

try:
    from ..compat import _VECTOR_LAYER_FILTER, _exec_dialog
    from ..tairu_firebase.models import RECORD_TYPES, RECORD_SUBTYPES, SUBTYPES_BY_TYPE, SITUATIONS_BY_TYPE
    from ..tairu_sync.push import build_push_plan, execute_push
    from .style import (
        apply_combo_popup_style, apply_table_style, apply_tairu_style,
        set_info_banner, set_muted, set_plain_button, set_primary_button,
    )
except ImportError:  # standalone usage with the plugin dir on sys.path
    from compat import _VECTOR_LAYER_FILTER, _exec_dialog
    from tairu_firebase.models import RECORD_TYPES, RECORD_SUBTYPES, SUBTYPES_BY_TYPE, SITUATIONS_BY_TYPE
    from tairu_sync.push import build_push_plan, execute_push
    from tairu_ui.style import (
        apply_combo_popup_style, apply_table_style, apply_tairu_style,
        set_info_banner, set_muted, set_plain_button, set_primary_button,
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
    'lastModified': 'Última alteração',
})
_HEADERS = ['Ação'] + [label for _key, label, _attr, _kind in _DATA_COLUMNS] + [
    'Geometria', 'Detalhes',
]
_COLUMN_BY_KEY = {key: index + 1 for index, (key, _label, _attr, _kind) in enumerate(_DATA_COLUMNS)}
_GEOMETRY_COL = len(_DATA_COLUMNS) + 1
_DETAILS_COL = len(_DATA_COLUMNS) + 2
_MAX_PREVIEW_ROWS = 500

try:
    _ITEM_IS_EDITABLE = Qt.ItemFlag.ItemIsEditable
except AttributeError:
    _ITEM_IS_EDITABLE = Qt.ItemIsEditable

try:
    _EDIT_TRIGGERS = (
        QTableWidget.EditTrigger.DoubleClicked |
        QTableWidget.EditTrigger.EditKeyPressed |
        QTableWidget.EditTrigger.AnyKeyPressed
    )
except AttributeError:
    _EDIT_TRIGGERS = (
        QTableWidget.DoubleClicked |
        QTableWidget.EditKeyPressed |
        QTableWidget.AnyKeyPressed
    )


def open_push_dialog(dock, tmap):
    dialog = PushDialog(dock, tmap)
    _exec_dialog(dialog)


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
        self.plan = None
        self._row_items = []
        self._hidden_unchanged_count = 0
        self._truncated_preview_count = 0
        self._preview_generation = 0
        self._auto_preview_pending = False
        self.setWindowTitle(f'Enviar camadas vetoriais · {tmap.nome}')
        self.resize(1120, 640)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form = QFormLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(_VECTOR_LAYER_FILTER)
        apply_combo_popup_style(self.layer_combo)
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        form.addRow('Camada:', self.layer_combo)
        layout.addLayout(form)

        self.roundtrip_label = set_info_banner(QLabel(
            'Esta camada veio do Tairu Maps: os atributos existentes serão preservados '
            'e os registros ausentes aparecerão como exclusões na prévia.'))
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
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive
                                    if hasattr(QHeaderView, 'ResizeMode')
                                    else QHeaderView.Interactive)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.send_btn = set_primary_button(QPushButton('Enviar'))
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self._send)
        buttons.addWidget(self.send_btn)
        cancel_btn = set_plain_button(QPushButton('Cancelar'))
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)
        apply_tairu_style(self)

        self._on_layer_changed(self.layer_combo.currentLayer())

    # ------------------------------------------------------------- mapping

    def _on_layer_changed(self, layer):
        is_roundtrip = False
        if layer is not None:
            is_roundtrip = layer.fields().indexOf('recordId') >= 0
        self.roundtrip_label.setVisible(is_roundtrip)
        self._invalidate_plan()
        self._schedule_preview()

    def _mapping(self):
        tipo = 'local'
        return {
            'nome_field': None,
            'descricao_field': None,
            'tipo': tipo,
            'sub_tipo': _default_subtype(tipo),
            'situation': _default_situation(tipo),
        }

    def _invalidate_plan(self, *_args):
        self._preview_generation += 1
        self.plan = None
        self._row_items = []
        self._hidden_unchanged_count = 0
        self._truncated_preview_count = 0
        self.table.setRowCount(0)
        self.send_btn.setEnabled(False)

    def _schedule_preview(self):
        if self._auto_preview_pending:
            return
        self._auto_preview_pending = True
        QTimer.singleShot(0, self._run_scheduled_preview)

    def _run_scheduled_preview(self):
        self._auto_preview_pending = False
        self._compute_preview()

    # ------------------------------------------------------------- preview

    def _compute_preview(self):
        layer = self.layer_combo.currentLayer()
        if layer is None:
            self.summary_label.setText('Selecione uma camada vetorial.')
            return
        if layer.featureCount() == 0:
            self.summary_label.setText('A camada selecionada não possui feições.')
            return

        self._preview_generation += 1
        generation = self._preview_generation
        mapping = self._mapping()
        include_deletions = layer.fields().indexOf('recordId') >= 0
        self.summary_label.setText('Calculando prévia…')

        try:
            self.plan = build_push_plan(
                layer, mapping, self.tmap, self.dock.tokens.uid,
                propagate_deletions=include_deletions)
        except Exception as e:
            self.summary_label.setText(f'Falha ao montar prévia: {e}')
            return

        if generation != self._preview_generation:
            return

        self._fill_table()
        extras = []
        if self._hidden_unchanged_count:
            extras.append(f'{self._hidden_unchanged_count} inalterados ocultos')
        if self._truncated_preview_count:
            extras.append(f'{self._truncated_preview_count} itens além do limite da tabela')
        suffix = f' {"; ".join(extras)}.' if extras else ''
        self.summary_label.setText(f'Prévia: {self.plan.summary()}.{suffix}')
        self.send_btn.setEnabled(bool(self.plan.writable_items()))

    def _fill_table(self):
        items = self.plan.items if self.plan else []
        visible_items = [item for item in items if item.action != 'unchanged' or item.warning]
        shown = visible_items[:_MAX_PREVIEW_ROWS]
        self._row_items = shown
        self._hidden_unchanged_count = len(items) - len(visible_items)
        self._truncated_preview_count = max(0, len(visible_items) - len(shown))
        self.table.setRowCount(len(shown))
        for row, item in enumerate(shown):
            self.table.setItem(row, 0, self._readonly_item(_ACTION_LABELS.get(item.action, item.action)))
            for col_offset, (key, _label, attr, kind) in enumerate(_DATA_COLUMNS, start=1):
                self._set_data_cell(row, col_offset, item, key, attr, kind)
            geometry = _GEOMETRY_LABELS.get(item.record.geometry_type, item.record.geometry_type or '')
            self.table.setItem(row, _GEOMETRY_COL, self._readonly_item(geometry))
            self.table.setItem(row, _DETAILS_COL, self._readonly_item(self._details_text(item)))
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
        apply_combo_popup_style(combo)
        return combo

    def _subtype_combo(self, tipo, value, editable):
        combo = QComboBox()
        options = [(key, RECORD_SUBTYPES.get(key, key)) for key in SUBTYPES_BY_TYPE.get(tipo, [])]
        self._fill_combo(combo, options, value)
        combo.setEnabled(editable)
        apply_combo_popup_style(combo)
        return combo

    def _situation_combo(self, tipo, value, editable):
        combo = QComboBox()
        options = [(sit, sit) for sit in SITUATIONS_BY_TYPE.get(tipo, ['Ativo'])]
        self._fill_combo(combo, options, value)
        combo.setEnabled(editable)
        apply_combo_popup_style(combo)
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
        if not self.plan:
            return
        if not self._apply_table_to_plan():
            return
        layer = self.layer_combo.currentLayer()
        self.accept()
        execute_push(self.dock, self.tmap, self.plan, layer)
