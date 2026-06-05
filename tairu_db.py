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

__author__ = 'Daniel Hulshof Saint Martin'
__date__ = '2025-05-19'
__copyright__ = '(C) 2025 by Daniel Hulshof Saint Martin'
__revision__ = '$Format:%H$'

import os
import sys
import inspect

from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from .tairu_db_provider import TairuDBProvider

cmd_folder = os.path.split(inspect.getfile(inspect.currentframe()))[0]

if cmd_folder not in sys.path:
    sys.path.insert(0, cmd_folder)


class TairuDBPlugin(object):

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None
        self.icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')

    def initProcessing(self):
        self.provider = TairuDBProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()

        self.action = QAction(
            QIcon(self.icon_path),
            'TairuDB',
            self.iface.mainWindow()
        )
        self.action.setToolTip('Gerar arquivo TairuDB para o Tairu Maps')
        self.action.triggered.connect(self._run)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('TairuDB', self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu('TairuDB', self.action)
        QgsApplication.processingRegistry().removeProvider(self.provider)

    def _run(self):
        import processing
        processing.execAlgorithmDialog('Tairu:tairudbgenerator')
