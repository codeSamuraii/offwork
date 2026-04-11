import inspect
from collections.abc import Callable
from typing import Any

from pyfuse.worker.backends.base import Backend
from pyfuse.graph.decorator import trace
from pyfuse.worker.deps import install_package_as
from pyfuse.core.errors import Error, DependencyError, RemoteError, WorkerError
from pyfuse.graph.graph import Graph
from pyfuse.core.models import FunctionNode, ImportInfo
from pyfuse.worker.remote import connect, disconnect, serve
from pyfuse.worker.result import Result, ResultEnvelope
from pyfuse.graph.store import Store, MergeResult
from pyfuse.core.task import Task
from pyfuse.worker.worker import Worker
from pyfuse.worker.worker import execute as execute


def get_graph() -> Graph:
    """Return the default dependency graph.

    The returned :class:`Graph` has :meth:`~Graph.to_mermaid` for
    visualization::

        print(pyfuse.get_graph().to_mermaid())           # full graph
        print(pyfuse.get_graph().to_mermaid(my_func))    # subgraph of my_func

    Also accessible as ``pyfuse.graph``.
    """
    return Graph.default()



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
    "install_package_as",
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
    "Graph",
    # Power-user
    "Task",
    "Worker",
    "Backend",
]
