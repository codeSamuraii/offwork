"""Public API for away — remote Python function execution."""

import inspect
from typing import Any
from collections.abc import Callable

from away.core.task import Task
from away.core.errors import (
    Error,
    RemoteError,
    TaskStalled,
    WorkerError,
    PairingError,
    TaskCancelled,
    SignatureError,
    DependencyError,
    ThrottleError,
    WorkerOnlyError,
)
from away.core.models import ImportInfo, FunctionNode
from away.graph.graph import Graph
from away.graph.store import Store, MergeResult
from away.worker.deps import install_package_as, worker_only_import
from away.core.pairing import (
    PairingResult,
    generate_pin,
    load_shared_key,
    save_shared_key,
    clear_shared_key,
    initiate_pairing,
    respond_to_pairing,
)
from away.core.signing import (
    sign_json,
    derive_key,
    verify_signature,
    compute_signature,
    verify_and_load_json,
)
from away.core.token import (
    load_token,
    save_token,
    clear_token,
    generate_token,
    resolve_signing_key,
)
from away.core.version import _VERSION
from away.core.progress import ProgressInfo
from away.core.progress import progress as progress
from away.worker.remote import serve, connect, disconnect
from away.worker.result import Result, ResultEnvelope
from away.worker.worker import Worker
from away.worker.worker import execute as execute
from away.worker.sandbox import DockerSandbox
from away.worker.schedule import ScheduleHandle
from away.graph.decorator import trace
from away.worker.backends.base import Backend


def get_graph() -> Graph:
    """Return the default dependency graph.

    The returned :class:`Graph` has :meth:`~Graph.to_mermaid` for
    visualization::

        print(away.get_graph().to_mermaid())           # full graph
        print(away.get_graph().to_mermaid(my_func))    # subgraph of my_func
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


__version__: str = _VERSION

__all__ = [
    "__version__",
    # Primary API
    "trace",
    "connect",
    "disconnect",
    "serve",
    "install_package_as",
    "worker_only_import",
    "progress",
    # Serialization
    "serialize",
    "reconstruct",
    "pack",
    "execute",
    # Result
    "Result",
    "ProgressInfo",
    # Errors
    "Error",
    "WorkerError",
    "DependencyError",
    "RemoteError",
    "TaskStalled",
    "TaskCancelled",
    "ThrottleError",
    "SignatureError",
    "PairingError",
    "WorkerOnlyError",
    # Graph
    "get_graph",
    "Graph",
    # Signing
    "compute_signature",
    "verify_signature",
    "sign_json",
    "verify_and_load_json",
    "derive_key",
    # Token
    "generate_token",
    "save_token",
    "load_token",
    "clear_token",
    "resolve_signing_key",
    # Pairing
    "generate_pin",
    "save_shared_key",
    "load_shared_key",
    "clear_shared_key",
    "initiate_pairing",
    "respond_to_pairing",
    "PairingResult",
    # Power-user
    "Task",
    "Worker",
    "Backend",
    # Scheduling
    "ScheduleHandle",
    # Sandbox
    "DockerSandbox",
]
