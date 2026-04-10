from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar, overload

from pyfuse.graph.graph import Graph

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., object])


@overload
def trace(func: _F) -> _F: ...
@overload
def trace(*, timeout: float | None = ..., retries: int = ..., retry_delay: float = ...) -> Callable[[_F], _F]: ...


def trace(
    func: _F | None = None,
    *,
    timeout: float | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
) -> _F | Callable[[_F], _F]:
    """Enable a function for serialization and remote execution.

    The decorated function works normally when called directly.
    Call ``func.run(...)`` to submit it to a remote worker.

    Can be used with or without arguments::

        @trace
        def fast(x): ...

        @trace(timeout=30, retries=3)
        def flaky(x): ...
    """
    if func is not None:
        return _apply_trace(func, timeout=timeout, retries=retries, retry_delay=retry_delay)

    def decorator(f: _F) -> _F:
        return _apply_trace(f, timeout=timeout, retries=retries, retry_delay=retry_delay)

    return decorator


def _apply_trace(
    func: _F,
    *,
    timeout: float | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
) -> _F:
    logger.debug("@trace applied to %s", func.__qualname__)
    graph = Graph.default()
    graph.register(func)
    wrapper = graph.create_wrapper(func)
    wrapper.__pyfuse_options__ = {  # type: ignore[attr-defined]
        "timeout": timeout,
        "retries": retries,
        "retry_delay": retry_delay,
    }
    return wrapper
