"""Public API for offwork — remote Python function execution."""

import inspect
from typing import Any
from collections.abc import Callable

from offwork.core.task import Task
from offwork.core.errors import (
    Error,
    RemoteError,
    ReplayError,
    TaskStalled,
    WorkerError,
    PairingError,
    StaleTaskError,
    TaskCancelled,
    SignatureError,
    DependencyError,
    ThrottleError,
    WorkerOnlyError,
    ClientRevokedError,
    IdentityMismatchError,
)
from offwork.core.models import ImportInfo, FunctionNode
from offwork.graph.graph import Graph
from offwork.graph.store import Store, MergeResult
from offwork.worker.deps import install_package_as, worker_only_import
from offwork.core.pairing import (
    PairingResult,
    generate_pin,
    load_shared_key,
    save_shared_key,
    clear_shared_key,
    initiate_pairing,
    respond_to_pairing,
)
from offwork.core.signing import (
    NonceLRU,
    derive_key,
    verify_signature,
    compute_signature,
)
from offwork.core.token import (
    load_token,
    save_token,
    clear_token,
    generate_token,
    resolve_root_token,
)
from offwork.core.identity import (
    get_client_id,
    get_public_key,
    clear_identity,
    get_identity_seed,
    get_identity_fingerprint,
)
from offwork.core.clients import KnownClients, ClientEntry
from offwork.core.envelope import (
    build_signed_envelope,
    verify_task_envelope,
)
from offwork.core.version import _VERSION
from offwork.core.progress import ProgressInfo
from offwork.core.progress import progress as progress
from offwork.worker.remote import serve, connect, disconnect
from offwork.worker.result import Result, ResultEnvelope
from offwork.worker.worker import Worker
from offwork.worker.worker import execute as execute
from offwork.worker.sandbox import DockerSandbox
from offwork.worker.schedule import ScheduleHandle
from offwork.graph.decorator import task
from offwork.worker.backends.base import Backend


def get_graph() -> Graph:
    """Return the default dependency graph.

    The returned :class:`Graph` has :meth:`~Graph.to_mermaid` for
    visualization::

        print(offwork.get_graph().to_mermaid())           # full graph
        print(offwork.get_graph().to_mermaid(my_func))    # subgraph of my_func
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
    "task",
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
    "ReplayError",
    "StaleTaskError",
    "ClientRevokedError",
    "IdentityMismatchError",
    "PairingError",
    "WorkerOnlyError",
    # Graph
    "get_graph",
    "Graph",
    # Signing
    "compute_signature",
    "verify_signature",
    "derive_key",
    "NonceLRU",
    "build_signed_envelope",
    "verify_task_envelope",
    # Token
    "generate_token",
    "save_token",
    "load_token",
    "clear_token",
    "resolve_root_token",
    # Identity / clients
    "get_client_id",
    "get_identity_seed",
    "get_public_key",
    "get_identity_fingerprint",
    "clear_identity",
    "KnownClients",
    "ClientEntry",
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
