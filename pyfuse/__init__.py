import inspect
import warnings
from collections.abc import Callable
from typing import Any

from pyfuse._backend import Backend
from pyfuse._decorator import trace
from pyfuse._deps import install_package_as
from pyfuse._errors import DependencyError, PyFuseError, RemoteError, WorkerError
from pyfuse._graph import FuseGraph
from pyfuse._models import FunctionNode, ImportInfo
from pyfuse._remote import connect, disconnect, serve
from pyfuse._result import FuseResult, ResultEnvelope
from pyfuse._store import FuseStore, MergeResult
from pyfuse._task import Task
from pyfuse._worker import FuseWorker
from pyfuse._worker import execute as execute


def graph() -> FuseGraph:
    """Return the default graph of all traced functions."""
    return FuseGraph.default()


def analyze() -> FuseGraph:
    """Return the default graph of all traced functions.

    .. deprecated::
        Use :func:`graph` instead.
    """
    warnings.warn(
        "analyze() is deprecated, use graph() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return FuseGraph.default()


def serialize(*funcs: Callable[..., object] | str) -> str:
    """Serialize the default graph (or a subgraph) to JSON."""
    return FuseGraph.default().serialize(*funcs)


def reconstruct(json_str: str, function_name: str) -> str:
    """Reconstruct a Python script from serialized JSON for the given function."""
    return FuseGraph.reconstruct(json_str, function_name)


def pack(func: Callable[..., object], *args: Any, **kwargs: Any) -> Task:
    """Capture the subgraph and bundle into a Task for remote execution.

    Equivalent to::

        Task(
            graph_json=serialize(func),
            function_name=qualified_name,
            args=args,
            kwargs=kwargs,
        )
    """
    unwrapped = inspect.unwrap(func)
    function_name = f"{unwrapped.__module__}.{unwrapped.__qualname__}"
    graph_json = FuseGraph.default().serialize(func)
    return Task(
        graph_json=graph_json,
        function_name=function_name,
        args=args,
        kwargs=kwargs,
    )


__all__ = [
    # Core API
    "trace",
    "graph",
    "serialize",
    "reconstruct",
    "execute",
    "pack",
    # Remote execution
    "connect",
    "disconnect",
    "serve",
    "Backend",
    "FuseResult",
    "ResultEnvelope",
    # Task / Worker
    "Task",
    "FuseWorker",
    # Graph / Store
    "FuseGraph",
    "FuseStore",
    # Errors
    "PyFuseError",
    "WorkerError",
    "DependencyError",
    "RemoteError",
    # Utilities
    "install_package_as",
    # Deprecated
    "analyze",
    # Data models
    "FunctionNode",
    "ImportInfo",
    "MergeResult",
]
