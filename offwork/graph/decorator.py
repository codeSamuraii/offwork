"""The ``@offwork.task`` decorator for marking functions for remote execution."""

import logging
from datetime import timedelta
from typing import TypeVar, ParamSpec, overload
from collections.abc import Callable

from offwork.typing import TraceDecorator, TracedFunction
from offwork.graph.graph import Graph

logger = logging.getLogger(__name__)

_R = TypeVar("_R")
_P = ParamSpec("_P")


@overload
def task(func: Callable[_P, _R]) -> TracedFunction[_P, _R]: ...
@overload
def task(*, timeout: float | None = ..., retries: int = ..., retry_delay: float = ..., throttle: timedelta | float | None = ..., storage: bool = ...) -> TraceDecorator: ...


def task(
    func: Callable[..., object] | None = None,
    *,
    timeout: float | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
    throttle: timedelta | float | None = None,
    storage: bool = False,
) -> object:
    """Enable a function for serialization and remote execution.

    The decorated function works normally when called directly.
    Call ``func.run(...)`` to submit it to a remote worker.

    Can be used with or without arguments::

        @offwork.task
        def fast(x): ...

        @offwork.task(timeout=30, retries=3)
        def flaky(x): ...

        @offwork.task(storage=True)
        def cache_to_disk(x): ...
    """
    if func is not None:
        return _apply_trace(
            func, timeout=timeout, retries=retries, retry_delay=retry_delay,
            throttle=throttle, storage=storage,
        )

    def decorator(f: Callable[_P, _R]) -> object:
        return _apply_trace(
            f, timeout=timeout, retries=retries, retry_delay=retry_delay,
            throttle=throttle, storage=storage,
        )

    return decorator


def _apply_trace(
    func: Callable[_P, _R],
    *,
    timeout: float | None = None,
    retries: int = 0,
    retry_delay: float = 1.0,
    throttle: timedelta | float | None = None,
    storage: bool = False,
) -> TracedFunction[_P, _R]:
    if timeout is not None and timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")
    if retries < 0:
        raise ValueError(f"retries must be non-negative, got {retries}")
    if retry_delay < 0:
        raise ValueError(f"retry_delay must be non-negative, got {retry_delay}")

    throttle_seconds: float | None = None
    if throttle is not None:
        if isinstance(throttle, timedelta):
            throttle_seconds = throttle.total_seconds()
        else:
            throttle_seconds = float(throttle)
        if throttle_seconds <= 0:
            raise ValueError(f"throttle must be positive, got {throttle}")

    logger.debug("@offwork.task applied to %s", func.__qualname__)
    graph = Graph.default()
    graph.register(func)
    wrapper = graph.create_wrapper(func)
    wrapper.__offwork_options__ = {  # type: ignore[attr-defined]
        "timeout": timeout,
        "retries": retries,
        "retry_delay": retry_delay,
        "throttle": throttle_seconds,
        "storage": storage,
    }
    return wrapper
