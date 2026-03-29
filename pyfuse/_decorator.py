from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from pyfuse._graph import FuseGraph

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., object])


def trace(func: _F) -> _F:
    """Register a function in the default pyfuse dependency graph and wrap it
    to record runtime caller-callee edges."""
    logger.debug("@trace applied to %s", func.__qualname__)
    graph = FuseGraph.default()
    graph.register(func)
    return graph.create_wrapper(func)
