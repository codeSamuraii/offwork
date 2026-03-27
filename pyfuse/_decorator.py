from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pyfuse._graph import FuseGraph

_F = TypeVar("_F", bound=Callable[..., object])


def trace(func: _F) -> _F:
    """Register a function in the default pyfuse dependency graph."""
    FuseGraph.default().register(func)
    return func
