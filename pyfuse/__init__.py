import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyfuse._backend import Backend
from pyfuse._decorator import trace
from pyfuse._deps import install_package_as
from pyfuse._errors import Error, DependencyError, RemoteError, WorkerError
from pyfuse._graph import Graph
from pyfuse._models import FunctionNode, ImportInfo
from pyfuse._remote import connect, disconnect, serve
from pyfuse._result import Result, ResultEnvelope
from pyfuse._store import Store, MergeResult
from pyfuse._task import Task
from pyfuse._worker import Worker
from pyfuse._worker import execute as execute

# Backward-compat aliases
PyFuseError = Error
FuseResult = Result
FuseWorker = Worker
FuseGraph = Graph
FuseStore = Store


def get_graph() -> Graph:
    """Return the default dependency graph.

    The returned :class:`Graph` has :meth:`~Graph.to_mermaid` for
    visualization::

        print(pyfuse.get_graph().to_mermaid())           # full graph
        print(pyfuse.get_graph().to_mermaid(my_func))    # subgraph of my_func

    Also accessible as ``pyfuse.graph``.
    """
    return Graph.default()


if TYPE_CHECKING:
    graph: Graph


def __getattr__(name: str) -> object:
    if name == "graph":
        return get_graph()
    raise AttributeError(f"module 'pyfuse' has no attribute {name!r}")


def serialize(*funcs: Callable[..., object] | str) -> str:
    """Serialize the default graph (or a subgraph) to JSON."""
    return Graph.default().serialize(*funcs)


def reconstruct(json_str: str, function_name: str) -> str:
    """Reconstruct a Python script from serialized JSON for the given function."""
    return Graph.reconstruct(json_str, function_name)


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
    graph_json = Graph.default().serialize(func)
    return Task(
        graph_json=graph_json,
        function_name=function_name,
        args=args,
        kwargs=kwargs,
    )


__all__ = [
    # Primary API
    "trace",
    "connect",
    "disconnect",
    "serve",
    # Serialization
    "serialize",
    "reconstruct",
    "pack",
    "execute",
    # Result
    "Result",
    # Errors
    "Error",
    "RemoteError",
    # Graph
    "get_graph",
    "graph",
    "Graph",
    # Power-user
    "Task",
    "Worker",
    "Backend",
]
