"""Request id: generated per request, attached to logs, the X-Request-ID header and audit rows.

A client-supplied X-Request-ID is accepted only if it is a UUID. Anything else is replaced, so a
client can't inject text into log lines or audit rows through it.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def new_request_id(client_value: str | None) -> str:
    """The client's id if it is a valid UUID, else a fresh uuid4."""
    if client_value:
        try:
            return str(uuid.UUID(client_value.strip()))
        except ValueError:
            pass
    return str(uuid.uuid4())


class RequestIdFilter(logging.Filter):
    """Adds ``record.request_id`` for the log format."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get()
        return True


def install_log_filter() -> None:
    """Attach the filter to every root handler (idempotent)."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())
