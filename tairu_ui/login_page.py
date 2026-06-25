# -*- coding: utf-8 -*-

"""Login page of the Tairu Maps dock: browser login and local generation."""

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QCheckBox, QFrame,
)

try:
    from .style import (
        apply_tairu_style, set_muted, set_primary_button, set_title,
        status_style, success_style,
    )
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_ui.style import (
        apply_tairu_style, set_muted, set_primary_button, set_title,
        status_style, success_style,
    )


class LoginPage(QWidget):

    browserLogin = pyqtSignal(bool)         # remember
    pasteCodeLogin = pyqtSignal(str, bool)  # refresh token, remember
    generateLocalRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = set_title(QLabel('Tairu Maps'))
        subtitle = set_muted(QLabel(
            'Entre com sua conta do Tairu Maps para acessar suas expedições.'))
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.remember_check = QCheckBox('Manter conectado')
        self.remember_check.setChecked(True)
        layout.addWidget(self.remember_check)

        self.login_btn = set_primary_button(QPushButton('Entrar pelo navegador'))
        self.login_btn.clicked.connect(
            lambda: self.browserLogin.emit(self.remember_check.isChecked()))
        layout.addWidget(self.login_btn)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(status_style(True))
        layout.addWidget(self.status_label)

        separator = QFrame()
        separator.setObjectName('TairuSeparator')
        separator.setFrameShape(QFrame.Shape.HLine if hasattr(QFrame, 'Shape') else QFrame.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken if hasattr(QFrame, 'Shadow') else QFrame.Sunken)
        layout.addWidget(separator)

        local_title = set_title(QLabel('TairuDB'))
        layout.addWidget(local_title)

        local_desc = set_muted(QLabel('Gere um arquivo .tairudb diretamente no seu computador, sem necessidade de login.'))
        local_desc.setWordWrap(True)
        layout.addWidget(local_desc)

        self.generate_local_btn = set_primary_button(QPushButton('Gerar arquivo TairuDB'))
        self.generate_local_btn.clicked.connect(self.generateLocalRequested.emit)
        layout.addWidget(self.generate_local_btn)

        layout.addStretch(1)
        apply_tairu_style(self)

    # ------------------------------------------------------------------ api

    def set_email(self, email):
        pass  # no longer shown; kept so dock_widget callers don't break

    def set_status(self, message, error=True):
        self.status_label.setStyleSheet(status_style(True) if error else success_style())
        self.status_label.setText(message or '')

    def set_busy(self, busy, message=None):
        for w in (self.login_btn, self.remember_check):
            w.setEnabled(not busy)
        if message is not None:
            self.set_status(message, error=False)

    def clear_password(self):
        pass  # no password field; kept for call-site compatibility

    # ------------------------------------------------------------- internal
