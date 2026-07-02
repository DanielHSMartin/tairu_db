# -*- coding: utf-8 -*-

"""
Progress/cancel abstraction so the generation core can be driven either by the
QGIS Processing framework (QgsProcessingFeedback) or by plugin widgets.
"""


class FeedbackAdapter:
    """No-op base implementation of the feedback interface."""

    def set_progress(self, value):
        pass

    def reset_progress(self):
        """Start a new progress phase at 0%. set_progress is forward-only within a
        phase (avoids backward jitter); call this between phases (prefetch, render,
        DEM download, …) so each one's bar can grow from 0 again."""
        pass

    def set_progress_text(self, text):
        pass

    def heartbeat(self, text):
        """Frequent, throwaway status update (e.g. a live 'X/N tiles' counter driven
        by a timer). Unlike push_info it must NOT accumulate — implementations should
        overwrite in place (a status label / progress-bar text), never append."""
        pass

    def push_info(self, text):
        pass

    def report_error(self, text, fatal=False):
        pass

    def is_canceled(self):
        return False


class ProcessingFeedbackAdapter(FeedbackAdapter):
    """Bridges the interface to a QgsProcessingFeedback instance."""

    def __init__(self, feedback):
        self._feedback = feedback

    def set_progress(self, value):
        self._feedback.setProgress(value)

    def set_progress_text(self, text):
        self._feedback.setProgressText(text)

    def push_info(self, text):
        self._feedback.pushInfo(text)

    def report_error(self, text, fatal=False):
        self._feedback.reportError(text, fatal)

    def is_canceled(self):
        return self._feedback.isCanceled()
