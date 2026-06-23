# -*- coding: utf-8 -*-

"""Shared Tairu Maps visual style for the QGIS plugin."""

PRIMARY = '#006A43'
ON_PRIMARY = '#FFFFFF'
PRIMARY_CONTAINER = '#008656'
ON_PRIMARY_CONTAINER = '#F0FFF6'
INVERSE_PRIMARY = '#69DCA1'

SECONDARY = '#3E6750'
SECONDARY_CONTAINER = '#C0EDD0'
ON_SECONDARY_CONTAINER = '#446D56'

TERTIARY = '#9C3C3F'
ERROR = '#BA1A1A'
ERROR_CONTAINER = '#FFDAD6'
ON_ERROR_CONTAINER = '#93000A'

SURFACE = '#F4FBF4'
ON_SURFACE = '#171D19'
ON_SURFACE_VARIANT = '#3E4A41'
SURFACE_CONTAINER_LOWEST = '#FFFFFF'
SURFACE_CONTAINER_LOW = '#EFF5EE'
SURFACE_CONTAINER = '#E9F0E8'
SURFACE_CONTAINER_HIGH = '#E3EAE3'
OUTLINE = '#6D7A71'
OUTLINE_VARIANT = '#BDCABF'

INFO = '#0D47A1'
INFO_CONTAINER = '#E3F2FD'

WARNING = '#6D4C00'
WARNING_CONTAINER = '#FFF4D6'


TAIRU_STYLE_SHEET = f"""
QWidget {{
    background-color: {SURFACE};
    color: {ON_SURFACE};
    font-size: 13px;
}}

QLabel {{
    background: transparent;
}}

QLabel#TairuTitle {{
    color: {ON_SURFACE};
    font-size: 20px;
    font-weight: 800;
}}

QLabel#TairuSectionTitle {{
    color: {ON_SURFACE};
    font-size: 15px;
    font-weight: 700;
}}

QLabel#TairuSubtitle,
QLabel#TairuMuted {{
    color: {ON_SURFACE_VARIANT};
}}

QLabel#TairuStatusError {{
    color: {ERROR};
}}

QLabel#TairuStatusInfo {{
    color: {ON_SURFACE_VARIANT};
}}

QLabel#TairuStatusSuccess {{
    color: {PRIMARY};
}}

QLabel#TairuInfoBanner {{
    color: {INFO};
    background-color: {INFO_CONTAINER};
    border: 1px solid #BBDEFB;
    border-radius: 6px;
    padding: 8px 10px;
}}

QLabel#TairuWarningBanner {{
    color: {WARNING};
    background-color: {WARNING_CONTAINER};
    border: 1px solid #F2DFA3;
    border-radius: 6px;
    padding: 8px 10px;
}}

QPushButton {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    color: {ON_SURFACE};
    border: 1px solid {OUTLINE_VARIANT};
    border-radius: 6px;
    padding: 7px 11px;
    font-weight: 600;
    min-height: 24px;
    outline: none;
}}

QPushButton:focus {{
    outline: none;
}}

QPushButton:hover {{
    background-color: {SURFACE_CONTAINER_LOW};
    border-color: {OUTLINE};
}}

QPushButton:pressed {{
    background-color: {SURFACE_CONTAINER};
}}

QPushButton:disabled {{
    background-color: {SURFACE_CONTAINER_HIGH};
    color: {OUTLINE};
    border-color: {OUTLINE_VARIANT};
}}

QPushButton#TairuPrimaryButton {{
    background-color: {PRIMARY};
    color: {ON_PRIMARY};
    border-color: {PRIMARY};
}}

QPushButton#TairuPrimaryButton:hover {{
    background-color: #005B39;
    border-color: #005B39;
}}

QPushButton#TairuPrimaryButton:disabled {{
    background-color: {OUTLINE_VARIANT};
    color: {SURFACE_CONTAINER_LOWEST};
    border-color: {OUTLINE_VARIANT};
}}

QPushButton#TairuSecondaryButton {{
    background-color: {SECONDARY_CONTAINER};
    color: {ON_SECONDARY_CONTAINER};
    border-color: {SECONDARY_CONTAINER};
}}

QPushButton#TairuPlainButton {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    color: {ON_SURFACE};
    border: 1px solid {OUTLINE_VARIANT};
    border-radius: 6px;
    padding: 7px 11px;
    font-weight: 600;
    min-height: 24px;
    outline: none;
}}

QPushButton#TairuPlainButton:hover {{
    background-color: {SURFACE_CONTAINER_LOW};
    border-color: {OUTLINE};
}}

QPushButton#TairuPlainButton:pressed {{
    background-color: {SURFACE_CONTAINER};
}}

QPushButton#TairuPlainButton:disabled {{
    background-color: {SURFACE_CONTAINER_HIGH};
    color: {OUTLINE};
    border-color: {OUTLINE_VARIANT};
}}

QPushButton#TairuActionButton {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    color: {ON_SURFACE};
    border: 1px solid {OUTLINE_VARIANT};
    border-left: 5px solid {PRIMARY};
    border-radius: 8px;
    padding: 12px 14px;
    min-height: 38px;
    text-align: left;
    font-weight: 700;
}}

QPushButton#TairuActionButton:hover {{
    background-color: {ON_PRIMARY_CONTAINER};
    border-color: {PRIMARY};
    color: {PRIMARY};
}}

QPushButton#TairuActionButton:pressed {{
    background-color: {SECONDARY_CONTAINER};
    border-color: {PRIMARY};
}}

QPushButton#TairuActionButton:disabled {{
    background-color: {SURFACE_CONTAINER_HIGH};
    color: {OUTLINE};
    border-color: {OUTLINE_VARIANT};
    border-left-color: {OUTLINE_VARIANT};
}}

QPushButton#TairuLinkButton {{
    background-color: transparent;
    color: {PRIMARY};
    border: none;
    padding: 6px 2px;
}}

QGroupBox {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    border: 1px solid {OUTLINE_VARIANT};
    border-radius: 8px;
    margin-top: 12px;
    padding: 14px 10px 10px 10px;
    font-weight: 700;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {SECONDARY};
}}

QLineEdit,
QComboBox,
QDateTimeEdit,
QPlainTextEdit,
QTextEdit {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    color: {ON_SURFACE};
    border: 1px solid {OUTLINE_VARIANT};
    border-radius: 6px;
    padding: 5px 7px;
    selection-background-color: {PRIMARY_CONTAINER};
    selection-color: {ON_PRIMARY};
}}

QLineEdit:focus,
QComboBox:focus,
QDateTimeEdit:focus,
QPlainTextEdit:focus,
QTextEdit:focus {{
    border-color: {PRIMARY};
}}

QCheckBox,
QRadioButton {{
    color: {ON_SURFACE};
    spacing: 8px;
}}

QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}

QListWidget::item {{
    background: {SURFACE_CONTAINER_LOWEST};
    border: 1px solid {OUTLINE_VARIANT};
    border-radius: 6px;
    margin: 2px;
    color: {ON_SURFACE};
}}

QListWidget::item:hover {{
    border-color: {OUTLINE};
}}

QListWidget::item:selected {{
    background: {SECONDARY_CONTAINER};
    border-color: {PRIMARY};
    color: {ON_SURFACE};
}}

QTableWidget {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    alternate-background-color: {SURFACE_CONTAINER_LOW};
    gridline-color: {OUTLINE_VARIANT};
    border: 1px solid {OUTLINE_VARIANT};
    border-radius: 6px;
}}

QHeaderView::section {{
    background-color: {SURFACE_CONTAINER};
    color: {ON_SURFACE_VARIANT};
    border: 0px;
    border-right: 1px solid {OUTLINE_VARIANT};
    border-bottom: 1px solid {OUTLINE_VARIANT};
    padding: 6px;
    font-weight: 700;
}}

QHeaderView::section:checked,
QHeaderView::section:pressed {{
    background-color: {SURFACE_CONTAINER_HIGH};
    color: {ON_SURFACE};
}}

QTableCornerButton::section {{
    background-color: {SURFACE_CONTAINER};
    border: 0px;
    border-right: 1px solid {OUTLINE_VARIANT};
    border-bottom: 1px solid {OUTLINE_VARIANT};
}}

QScrollBar:vertical {{
    background: {SURFACE_CONTAINER_LOW};
    width: 12px;
    margin: 0px;
    border: 0px;
}}

QScrollBar::handle:vertical {{
    background: {OUTLINE_VARIANT};
    min-height: 28px;
    border-radius: 5px;
    margin: 2px;
}}

QScrollBar::handle:vertical:hover {{
    background: {OUTLINE};
}}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: transparent;
    border: 0px;
    height: 0px;
}}

QScrollBar:horizontal {{
    background: {SURFACE_CONTAINER_LOW};
    height: 12px;
    margin: 0px;
    border: 0px;
}}

QScrollBar::handle:horizontal {{
    background: {OUTLINE_VARIANT};
    min-width: 28px;
    border-radius: 5px;
    margin: 2px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {OUTLINE};
}}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: transparent;
    border: 0px;
    width: 0px;
}}

QProgressBar {{
    background-color: {SURFACE_CONTAINER};
    border: 1px solid {OUTLINE_VARIANT};
    border-radius: 6px;
    text-align: center;
    color: {ON_SURFACE};
}}

QProgressBar::chunk {{
    background-color: {PRIMARY};
    border-radius: 5px;
}}

QFrame#TairuSeparator {{
    color: {OUTLINE_VARIANT};
}}
"""

SCROLLBAR_STYLE = f"""
QScrollBar:vertical {{
    background: {SURFACE_CONTAINER_LOW};
    width: 12px;
    margin: 0px;
    border: 0px;
}}

QScrollBar::handle:vertical {{
    background: {OUTLINE_VARIANT};
    min-height: 28px;
    border-radius: 5px;
    margin: 2px;
}}

QScrollBar::handle:vertical:hover {{
    background: {OUTLINE};
}}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: transparent;
    border: 0px;
    height: 0px;
}}

QScrollBar:horizontal {{
    background: {SURFACE_CONTAINER_LOW};
    height: 12px;
    margin: 0px;
    border: 0px;
}}

QScrollBar::handle:horizontal {{
    background: {OUTLINE_VARIANT};
    min-width: 28px;
    border-radius: 5px;
    margin: 2px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {OUTLINE};
}}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: transparent;
    border: 0px;
    width: 0px;
}}
"""

COMBO_LOCAL_STYLE = f"""
QComboBox {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    color: {ON_SURFACE};
    border: 1px solid {OUTLINE_VARIANT};
    border-radius: 6px;
    padding: 5px 28px 5px 7px;
    selection-background-color: {PRIMARY};
    selection-color: {ON_PRIMARY};
}}

QComboBox:hover {{
    border-color: {OUTLINE};
}}

QComboBox:focus {{
    border-color: {PRIMARY};
}}

QComboBox::drop-down {{
    border: 0px;
    width: 24px;
}}

QComboBox QAbstractItemView,
QComboBox QListView,
QComboBox QTreeView {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    color: {ON_SURFACE};
    border: 1px solid {OUTLINE_VARIANT};
    outline: 0px;
    selection-background-color: {PRIMARY};
    selection-color: {ON_PRIMARY};
}}

QComboBox QAbstractItemView::item {{
    min-height: 28px;
    padding: 4px 8px;
}}

QComboBox QAbstractItemView::item:hover {{
    background-color: {SURFACE_CONTAINER_LOW};
    color: {ON_SURFACE};
}}

QComboBox QAbstractItemView::item:selected {{
    background-color: {PRIMARY};
    color: {ON_PRIMARY};
}}
""" + SCROLLBAR_STYLE

POPUP_VIEW_STYLE = f"""
QAbstractItemView,
QListView,
QTreeView {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    color: {ON_SURFACE};
    border: 1px solid {OUTLINE_VARIANT};
    outline: 0px;
    selection-background-color: {PRIMARY};
    selection-color: {ON_PRIMARY};
}}

QAbstractItemView::item,
QListView::item,
QTreeView::item {{
    min-height: 28px;
    padding: 4px 8px;
}}

QAbstractItemView::item:hover,
QListView::item:hover,
QTreeView::item:hover {{
    background-color: {SURFACE_CONTAINER_LOW};
    color: {ON_SURFACE};
}}

QAbstractItemView::item:selected,
QListView::item:selected,
QTreeView::item:selected {{
    background-color: {PRIMARY};
    color: {ON_PRIMARY};
}}
""" + SCROLLBAR_STYLE

TABLE_LOCAL_STYLE = f"""
QTableWidget {{
    background-color: {SURFACE_CONTAINER_LOWEST};
    alternate-background-color: {SURFACE_CONTAINER_LOW};
    color: {ON_SURFACE};
    gridline-color: {OUTLINE_VARIANT};
    border: 1px solid {OUTLINE_VARIANT};
    border-radius: 6px;
    selection-background-color: {SECONDARY_CONTAINER};
    selection-color: {ON_SURFACE};
}}

QTableWidget::item {{
    color: {ON_SURFACE};
    padding: 3px;
}}

QHeaderView::section {{
    background-color: {SURFACE_CONTAINER};
    color: {ON_SURFACE_VARIANT};
    border: 0px;
    border-right: 1px solid {OUTLINE_VARIANT};
    border-bottom: 1px solid {OUTLINE_VARIANT};
    padding: 6px;
    font-weight: 700;
}}

QHeaderView::section:checked,
QHeaderView::section:pressed {{
    background-color: {SURFACE_CONTAINER_HIGH};
    color: {ON_SURFACE};
}}

QTableCornerButton::section {{
    background-color: {SURFACE_CONTAINER};
    border: 0px;
    border-right: 1px solid {OUTLINE_VARIANT};
    border-bottom: 1px solid {OUTLINE_VARIANT};
}}
""" + SCROLLBAR_STYLE


def apply_tairu_style(widget):
    widget.setStyleSheet(TAIRU_STYLE_SHEET)


def apply_combo_popup_style(combo):
    combo.setStyleSheet(COMBO_LOCAL_STYLE)
    try:
        view = combo.view()
        view.setStyleSheet(POPUP_VIEW_STYLE)
        view.viewport().setStyleSheet(f'background-color: {SURFACE_CONTAINER_LOWEST};')
        view.verticalScrollBar().setStyleSheet(SCROLLBAR_STYLE)
        view.horizontalScrollBar().setStyleSheet(SCROLLBAR_STYLE)
    except Exception:
        pass


def apply_table_style(table):
    table.setStyleSheet(TABLE_LOCAL_STYLE)
    try:
        table.horizontalHeader().setStyleSheet(TABLE_LOCAL_STYLE)
        table.verticalHeader().setStyleSheet(TABLE_LOCAL_STYLE)
        table.horizontalScrollBar().setStyleSheet(SCROLLBAR_STYLE)
        table.verticalScrollBar().setStyleSheet(SCROLLBAR_STYLE)
        table.viewport().setStyleSheet(f'background-color: {SURFACE_CONTAINER_LOWEST};')
    except Exception:
        pass


def set_title(label):
    label.setObjectName('TairuTitle')
    return label


def set_section_title(label):
    label.setObjectName('TairuSectionTitle')
    return label


def set_muted(label):
    label.setObjectName('TairuMuted')
    return label


def set_info_banner(label):
    label.setObjectName('TairuInfoBanner')
    return label


def set_warning_banner(label):
    label.setObjectName('TairuWarningBanner')
    return label


def set_primary_button(button):
    button.setObjectName('TairuPrimaryButton')
    _remove_button_focus(button)
    _refresh_widget_style(button)
    return button


def set_secondary_button(button):
    button.setObjectName('TairuSecondaryButton')
    _remove_button_focus(button)
    _refresh_widget_style(button)
    return button


def set_action_button(button):
    button.setObjectName('TairuActionButton')
    _remove_button_focus(button)
    _refresh_widget_style(button)
    return button


def set_link_button(button):
    button.setObjectName('TairuLinkButton')
    button.setFlat(True)
    _remove_button_focus(button)
    _refresh_widget_style(button)
    return button


def set_plain_button(button):
    button.setObjectName('TairuPlainButton')
    _remove_button_focus(button)
    _refresh_widget_style(button)
    return button


def set_control_enabled(widget, enabled, disabled_opacity=0.58):
    widget.setEnabled(enabled)
    if enabled:
        widget.setGraphicsEffect(None)
        return
    try:
        from qgis.PyQt.QtWidgets import QGraphicsOpacityEffect
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(disabled_opacity)
        widget.setGraphicsEffect(effect)
    except Exception:
        pass


def _remove_button_focus(button):
    try:
        from qgis.PyQt.QtCore import Qt
        no_focus = Qt.FocusPolicy.NoFocus if hasattr(Qt, 'FocusPolicy') else Qt.NoFocus
        button.setFocusPolicy(no_focus)
    except Exception:
        pass


def _refresh_widget_style(widget):
    try:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()
    except Exception:
        pass


def status_style(error=False):
    return f'color: {ERROR if error else ON_SURFACE_VARIANT};'


def success_style():
    return f'color: {PRIMARY};'


def badge_style(color, background):
    return (
        'QLabel {'
        f'color: {color};'
        f'background: {background};'
        'border-radius: 4px;'
        'padding: 3px 7px;'
        'font-size: 12px;'
        'font-weight: 600;'
        '}'
    )
