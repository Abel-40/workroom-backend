"""Logging filter injecting the current request id (see utils/middleware.py)
into every log record, so LOGGING's formatter can include it uniformly."""

import logging

from .middleware import get_current_request_id


class RequestIDFilter(logging.Filter):
    def filter(self, record):
        record.request_id = get_current_request_id()
        return True
