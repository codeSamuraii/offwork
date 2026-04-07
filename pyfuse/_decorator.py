from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from pyfuse._graph import Graph

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., object])


def trace(func: _F) -> _F:
    """Enable a function for serialization and remote execution.

    The decorated function works normally when called directly.
    Call ``func.run(...)`` to submit it to a remote worker.
    """
    logger.debug("@trace applied to %s", func.__qualname__)
    graph = Graph.default()
    graph.register(func)
    return graph.create_wrapper(func)
