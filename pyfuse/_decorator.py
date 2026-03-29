from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pyfuse._graph import FuseGraph

_F = TypeVar("_F", bound=Callable[..., object])


def trace(func: _F) -> _F:
    """Register a function in the default pyfuse dependency graph and wrap it
    to record runtime caller-callee edges."""
    graph = FuseGraph.default()
    graph.register(func)
    return graph.create_wrapper(func)
