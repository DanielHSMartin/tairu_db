# -*- coding: utf-8 -*-

"""
Progress/cancel abstraction so the generation core can be driven either by the
QGIS Processing framework (QgsProcessingFeedback) or by plugin widgets.
"""


class FeedbackAdapter:
    """No-op base implementation of the feedback interface."""

    def set_progress(self, value):
        pass

    def set_progress_text(self, text):
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
