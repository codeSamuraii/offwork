"""Exception hierarchy for offwork."""

import sys
import types


class Error(Exception):
    """Raised when offwork cannot trace or analyze a function."""


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


class SignatureError(Error):
    """Raised when signature verification of a task fails.

    Base class for all envelope-level rejections.  Existing callers
    that ``except SignatureError`` continue to catch every flavour of
    rejection produced by the per-client signing protocol.
    """


class ReplayError(SignatureError):
    """Raised when a task nonce has already been seen by the worker."""


class StaleTaskError(SignatureError):
    """Raised when a task's ``iat`` is outside the allowed clock-skew window."""


class ClientRevokedError(SignatureError):
    """Raised when a task originates from a revoked client_id."""


class IdentityMismatchError(SignatureError):
    """Raised when a known client_id submits with a different public key."""


class PairingError(Error):
    """Raised when the PIN-based pairing protocol fails."""


class ThrottleError(Error):
    """Raised when a task is rejected due to rate limiting."""


class WorkerOnlyError(Error):
    """Raised when a worker-only import stub is used on the client."""


# ---------------------------------------------------------------------------
# Custom excepthook: suppress client traceback for RemoteError
# ---------------------------------------------------------------------------

_original_excepthook = sys.excepthook


def _offwork_excepthook(
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


if sys.excepthook is not _offwork_excepthook:
    sys.excepthook = _offwork_excepthook
