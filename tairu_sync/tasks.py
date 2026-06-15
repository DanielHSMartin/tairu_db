# -*- coding: utf-8 -*-

"""
QgsTask wrapper for Firebase network operations.

Rules of the road:
- network code runs in QgsTask.run() (worker thread) and never touches widgets;
- results come back on the GUI thread through queued Qt signals;
- a module-level registry keeps Python references alive while tasks run
  (the classic QgsTask garbage-collection pitfall).
"""

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsTask, QgsMessageLog, Qgis

try:
    from ..tairu_firebase.http import FirebaseError
    from ..tairu_firebase.storage import CanceledError
except ImportError:  # standalone usage with the plugin dir on sys.path
    from tairu_firebase.http import FirebaseError
    from tairu_firebase.storage import CanceledError

_ACTIVE_TASKS = set()


class TaskReporter(QObject):
    finishedOk = pyqtSignal(object)
    failed = pyqtSignal(str)
    progressed = pyqtSignal(float, str)


class FirebaseTask(QgsTask):
    """Runs `fn(task)` on a worker thread; fn may call task.report(pct, msg)
    and check task.isCanceled(). Exceptions become readable failure messages."""

    def __init__(self, description, fn):
        super().__init__(description, QgsTask.CanCancel)
        self._fn = fn
        self.reporter = TaskReporter()
        self._result = None
        self._error = None

    # Called by fn from the worker thread
    def report(self, fraction, message=''):
        self.setProgress(max(0.0, min(100.0, fraction * 100.0)))
        self.reporter.progressed.emit(fraction, message)

    def run(self):
        try:
            self._result = self._fn(self)
            return not self.isCanceled()
        except CanceledError:
            return False
        except FirebaseError as e:
            QgsMessageLog.logMessage(
                f'[{self.description()}] FirebaseError: code={e.code!r} '
                f'http_status={e.http_status} message={e.message!r}',
                'Tairu Maps', Qgis.MessageLevel.Warning)
            self._error = e.user_message()
            return False
        except Exception as e:
            QgsMessageLog.logMessage(
                f'[{self.description()}] Unexpected error: {e}',
                'Tairu Maps', Qgis.MessageLevel.Critical)
            self._error = f'Erro inesperado: {e}'
            return False

    def finished(self, ok):
        _ACTIVE_TASKS.discard(self)
        if ok:
            self.reporter.finishedOk.emit(self._result)
        else:
            self.reporter.failed.emit(self._error or 'Operação cancelada')


def run_task(description, fn, on_success=None, on_error=None, on_progress=None):
    """Create, register and start a FirebaseTask. Returns the task."""
    task = FirebaseTask(description, fn)
    if on_success:
        task.reporter.finishedOk.connect(on_success)
    if on_error:
        task.reporter.failed.connect(on_error)
    if on_progress:
        task.reporter.progressed.connect(on_progress)
    _ACTIVE_TASKS.add(task)
    QgsApplication.taskManager().addTask(task)
    return task


def cancel_all_tasks():
    for task in list(_ACTIVE_TASKS):
        try:
            task.cancel()
        except Exception:
            pass
