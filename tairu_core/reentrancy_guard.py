# -*- coding: utf-8 -*-

"""
Re-entrancy guard for QgsProject mutations during a generation.

TairuDB generation keeps the UI responsive by pumping the event loop (a nested
QEventLoop for tile prefetch/render, processEvents() during DEM downloads). That
loop also dispatches UNRELATED QgsTask completions — e.g. a background records
pull whose on_success calls QgsProject.addMapLayer. Adding a layer while a nested
loop is running fires the wizard's QgsMapLayerComboBox -> completeChanged, which
re-enters QWizard mid-model-mutation and SEGFAULTs QGIS (observed on-device).

So: while a generation is active, don't mutate the project re-entrantly — defer it
until the outermost generation finishes, when it runs on a clean stack. Pure Python,
main-thread only (generation and task callbacks both run on the GUI thread).
"""
import contextlib

_depth = 0
_pending = []


def enter():
    """Mark that a generation (with its event-loop pumping) has started."""
    global _depth
    _depth += 1


def leave():
    """Mark a generation finished; when the outermost one ends, run deferred work."""
    global _depth
    _depth = max(0, _depth - 1)
    if _depth == 0 and _pending:
        pending = list(_pending)
        _pending.clear()
        for fn in pending:
            with contextlib.suppress(Exception):
                fn()


def active():
    return _depth > 0


def run_or_defer(fn):
    """Run fn now, or defer it until the outermost generation finishes. Used for
    QgsProject mutations that must never happen re-entrantly inside a generation's
    nested event loop."""
    if _depth > 0:
        _pending.append(fn)
    else:
        fn()
