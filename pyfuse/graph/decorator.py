"""The ``@trace`` decorator for marking functions for remote execution."""

import logging
from typing import TypeVar, ParamSpec, overload
from collections.abc import Callable

from pyfuse.typing import TraceDecorator, TracedFunction
from pyfuse.graph.graph import Graph

logger = logging.getLogger(__name__)

_R = TypeVar("_R")
_P = ParamSpec("_P")


@overload
def trace(func: Callable[_P, _R]) -> TracedFunction[_P, _R]: ...
@overload
def trace(*, timeout: float | None = ..., retries: int = ..., retry_delay: float = ...) -> TraceDecorator: ...


def trace(
    func: Callable[..., object] | None = None,
    *,
    timeout: float | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
) -> object:
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

    def decorator(f: Callable[_P, _R]) -> object:
        return _apply_trace(f, timeout=timeout, retries=retries, retry_delay=retry_delay)

    return decorator


def _apply_trace(
    func: Callable[_P, _R],
    *,
    timeout: float | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
) -> TracedFunction[_P, _R]:
    if timeout is not None and timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")
    if retries < 0:
        raise ValueError(f"retries must be non-negative, got {retries}")
    if retry_delay < 0:
        raise ValueError(f"retry_delay must be non-negative, got {retry_delay}")

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
