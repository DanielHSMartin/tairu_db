# -*- coding: utf-8 -*-

"""
/***************************************************************************
 TairuDB
                                 A QGIS plugin
 Gera arquivos .tairudb para o aplicativo Tairu Maps
                              -------------------
        begin                : 2025-05-19
        copyright            : (C) 2025 by Daniel Hulshof Saint Martin
        email                : danielhsmartin@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
import contextlib

__author__ = 'Daniel Hulshof Saint Martin'
__date__ = '2025-05-19'
__copyright__ = '(C) 2025 by Daniel Hulshof Saint Martin'
__revision__ = '$Format:%H$'

import os
import sys
import inspect

from qgis.core import QgsApplication
from qgis.gui import QgsCustomDropHandler
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from .tairu_db_provider import TairuDBProvider
from .tairu_core.i18n import tr

cmd_folder = os.path.split(inspect.getfile(inspect.currentframe()))[0]

if cmd_folder not in sys.path:
    sys.path.insert(0, cmd_folder)


class _TairuDBDropHandler(QgsCustomDropHandler):
    """Arrastar um .tairudb para o QGIS abre o arquivo como camadas."""

    def __init__(self, open_file):
        super().__init__()
        self._open_file = open_file

    def handleFileDrop(self, file):
        if not str(file).lower().endswith('.tairudb'):
            return False
        self._open_file(file)
        return True


class TairuDBPlugin(object):

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None
        self.open_action = None
        self.drop_handler = None
        self.dock = None
        self.icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')

    def initProcessing(self):
        # proxy misconfiguration must never block plugin load
        with contextlib.suppress(Exception):
            from .qgis_proxy import install_qgis_proxy
            install_qgis_proxy()
        self.provider = TairuDBProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()

        self.action = QAction(
            QIcon(self.icon_path),
            tr('TairuDB'),
            self.iface.mainWindow()
        )
        self.action.setToolTip(tr('Abrir painel do Tairu Maps'))
        self.action.triggered.connect(self._show_dock)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('TairuDB', self.action)

        self.open_action = QAction(tr('Abrir arquivo .tairudb…'), self.iface.mainWindow())
        self.open_action.triggered.connect(self._open_dialog)
        self.iface.addPluginToMenu('TairuDB', self.open_action)
        self.drop_handler = _TairuDBDropHandler(self._open_file)
        self.iface.registerCustomDropHandler(self.drop_handler)

    def _open_dialog(self):
        from .tairu_ui.open_tairudb import open_tairudb_dialog
        open_tairudb_dialog(self.iface)

    def _open_file(self, path):
        from .tairu_ui.open_tairudb import open_tairudb
        open_tairudb(self.iface, path)

    def unload(self):
        with contextlib.suppress(Exception):
            from .tairu_ui.local_generate_wizard import close_open_wizards
            close_open_wizards()
        if self.drop_handler is not None:
            self.iface.unregisterCustomDropHandler(self.drop_handler)
            self.drop_handler = None
        self.iface.removePluginMenu('TairuDB', self.open_action)
        if self.dock is not None:
            with contextlib.suppress(Exception):
                self.dock.shutdown()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu('TairuDB', self.action)
        QgsApplication.processingRegistry().removeProvider(self.provider)
        self._forget_own_modules()

    def _forget_own_modules(self):
        """Devolve o plugin ao estado de antes de ser carregado, para poder recarregar.

        DUAS coisas, e as duas sao necessarias.

        1. Tirar os modulos da memoria. Ao desativar um complemento o QGIS descarta apenas
           os modulos cujo pacote RAIZ tem o nome dele (qgis/utils.py:
           `package_name = module_name.split('.')[0]` e `if package_name in
           available_plugins`). Os nossos sao de PRIMEIRO NIVEL — tairu_core, tairu_sync,
           tairu_firebase, tairu_ui, compat — porque o import la em cima poe a pasta do
           plugin no sys.path. Sem isto, desmarcar e marcar nao recarregava nada: o Python
           devolvia a versao velha e so reiniciar o QGIS adiantava.

        2. Tirar a pasta do sys.path. Ela fica em sys.path[0] e contem `tairu_db.py`;
           com os modulos fora da memoria, o `__import__('tairu_db')` do QGIS acharia o
           ARQUIVO antes do PACOTE e o carregamento morreria em "attempted relative import
           with no known parent package". Nao adianta tratar so o import deste arquivo: a
           falha se repete em cascata em tairu_db_provider.py e adiante. O import la em
           cima recoloca a pasta no proximo carregamento.
        """
        with contextlib.suppress(Exception):
            raiz = os.path.realpath(cmd_folder)
            alvos = []
            for nome, modulo in list(sys.modules.items()):
                if nome in ('__main__', '__mp_main__'):
                    continue     # o console do QGIS nao e nosso para descartar
                arquivo = getattr(modulo, '__file__', None)
                if not arquivo:
                    continue
                with contextlib.suppress(Exception):
                    if os.path.realpath(arquivo).startswith(raiz + os.sep):
                        alvos.append(nome)
            for nome in alvos:
                sys.modules.pop(nome, None)
            for entrada in [x for x in sys.path
                            if os.path.realpath(x) == raiz]:
                with contextlib.suppress(ValueError):
                    sys.path.remove(entrada)

    def _show_dock(self):
        with contextlib.suppress(Exception):
            from .qgis_proxy import install_qgis_proxy
            install_qgis_proxy()  # pick up proxy settings changed mid-session
        if self.dock is None:
            from .compat import _DOCK_RIGHT_AREA
            from .tairu_ui.dock_widget import TairuDockWidget
            self.dock = TairuDockWidget(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(_DOCK_RIGHT_AREA, self.dock)
        self.dock.show()
        self.dock.raise_()
