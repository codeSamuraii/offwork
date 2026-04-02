from collections.abc import Callable

from pyfuse._decorator import trace
from pyfuse._errors import PyFuseError
from pyfuse._graph import FuseGraph
from pyfuse._models import FunctionNode, ImportInfo
from pyfuse._store import FuseStore, MergeResult


def analyze() -> FuseGraph:
    """Return the default graph of all traced functions."""
    return FuseGraph.default()


def serialize(*funcs: Callable[..., object] | str) -> str:
    """Serialize the default graph (or a subgraph) to JSON."""
    return FuseGraph.default().serialize(*funcs)


def reconstruct(json_str: str, function_name: str) -> str:
    """Reconstruct a Python script from serialized JSON for the given function."""
    return FuseGraph.reconstruct(json_str, function_name)


__all__ = [
    "trace",
    "analyze",
    "serialize",
    "reconstruct",
    "FuseGraph",
    "FunctionNode",
    "ImportInfo",
    "PyFuseError",
    "FuseStore",
    "MergeResult",
]
