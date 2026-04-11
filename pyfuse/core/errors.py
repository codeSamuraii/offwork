class Error(Exception):
    """Raised when pyfuse cannot trace or analyze a function."""


class WorkerError(Error):
    """Raised when worker execution fails."""


class DependencyError(Error):
    """Raised when dependency installation fails."""


class RemoteError(WorkerError):
    """Raised on the client side when a remote execution fails."""


class TaskStalled(Error):
    """Raised when a worker stops sending heartbeats for a task."""
