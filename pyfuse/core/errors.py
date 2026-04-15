import sys
import types


class Error(Exception):
    """Raised when pyfuse cannot trace or analyze a function."""


class WorkerError(Error):
    """Raised when worker execution fails."""


class DependencyError(Error):
    """Raised when dependency installation fails."""


class RemoteError(WorkerError):
    """Raised on the client side when a remote execution fails.

    Carries the worker-side traceback and formats it cleanly when the
    exception is printed, suppressing the noisy client-side frames.
    """

    remote_traceback: str | None

    def __init__(self, message: str, remote_traceback: str | None = None) -> None:
        super().__init__(message)
        self.remote_traceback = remote_traceback


class TaskStalled(Error):
    """Raised when a worker stops sending heartbeats for a task."""


class TaskCancelled(Error):
    """Raised when a task is cancelled before or during execution."""


class TrustError(Error):
    """Raised when a task fails signature verification.

    This indicates that the task was either unsigned, signed by an
    unknown client, or the signature is invalid.
    """


# ---------------------------------------------------------------------------
# Custom excepthook: suppress client traceback for RemoteError
# ---------------------------------------------------------------------------

_original_excepthook = sys.excepthook


def _pyfuse_excepthook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: types.TracebackType | None,
) -> None:
    if isinstance(exc_value, RemoteError) and exc_value.remote_traceback:
        tb = exc_value.remote_traceback
        # Replace "Traceback (most recent call last):" with our header
        tb = tb.replace(
            "Traceback (most recent call last):",
            "Worker traceback (most recent call last):",
            1,
        )
        sys.stderr.write(f"\n{tb}")
        return
    _original_excepthook(exc_type, exc_value, exc_tb)


sys.excepthook = _pyfuse_excepthook
