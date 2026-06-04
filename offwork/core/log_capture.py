"""Per-task log capture helpers.

A single ContextVar holds a callback that is set by the worker's
``_handle_task`` loop for the duration of one task execution.  A
``logging.Handler`` subclass calls the callback; the callback ships
lines to the backend via ``send_log_line``.
"""

import logging
import contextvars
from collections.abc import Callable


_log_callback: contextvars.ContextVar[Callable[[str], None] | None] = (
    contextvars.ContextVar("offwork_log_callback", default=None)
)


def emit_log_line(line: str) -> None:
    """Forward *line* to the active task log callback, if one is set."""
    cb = _log_callback.get(None)
    if cb is not None:
        cb(line)


class TaskLogHandler(logging.Handler):
    """Logging handler that routes records to the active task's log stream."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            emit_log_line(self.format(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)
