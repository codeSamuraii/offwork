class PyFuseError(Exception):
    """Raised when pyfuse cannot trace or analyze a function."""


class WorkerError(PyFuseError):
    """Raised when worker execution fails."""


class DependencyError(PyFuseError):
    """Raised when dependency installation fails."""


class RemoteError(WorkerError):
    """Raised on the client side when a remote execution fails."""
