"""Runtime call-stack tracing for dependency edge recording via contextvars."""

import os
import sys
import time as _time
import asyncio
import inspect
import logging
import builtins
import functools
import sysconfig
import threading
import contextvars
from typing import Any, TypeVar, ParamSpec, cast
from pathlib import Path
from datetime import datetime, timedelta
from collections.abc import Callable, Awaitable, Generator, AsyncGenerator

from offwork.typing import TracedFunction
from offwork.worker.backends.base import Backend

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., object])
_P = ParamSpec("_P")
_R = TypeVar("_R")

_BUILTIN_NAMES = set(dir(builtins))


def _make_submit_method(
    wrapper: Callable[..., object], func: Callable[..., object]
) -> Callable[..., object]:
    """Create the ``.submit()`` async method.

    Submits the function to a remote worker and returns a :class:`Result`
    handle (or a :class:`ScheduleHandle` when *run_every* is set).

    Scheduling keywords (all optional, at most one of ``run_at`` /
    ``run_in`` / ``run_every`` may be given):

    ``run_at``
        :class:`~datetime.datetime` — run once at a specific point in time.
    ``run_in``
        :class:`~datetime.timedelta` or ``float`` (seconds) — run once after
        a delay.
    ``run_every``
        :class:`~datetime.timedelta` or ``float`` (seconds) — repeat at this
        interval.  Returns a :class:`ScheduleHandle` instead of a
        :class:`Result`.

    Additional keywords for recurring schedules:

    ``_start_at``
        :class:`~datetime.datetime` — first occurrence for *run_every*.
    ``run_for``
        :class:`~datetime.timedelta` or ``float`` (seconds) — stop recurring
        after this wall-clock duration.
    ``max_runs``
        ``int`` — stop recurring after this many executions.
    ``backend``
        Override the global backend for this submission.
    """

    async def submit(
        *args: Any,
        run_at: datetime | None = None,
        run_in: timedelta | float | None = None,
        run_every: timedelta | float | None = None,
        _start_at: datetime | None = None,
        run_for: timedelta | float | None = None,
        max_runs: int | None = None,
        backend: str | Backend | None = None,
        **kwargs: Any,
    ) -> object:
        if sum(x is not None for x in (run_at, run_in, run_every)) > 1:
            raise ValueError(
                "At most one of run_at, run_in, run_every may be specified"
            )

        if run_every is not None:
            from offwork.worker.remote import submit_recurring  # circular

            interval = (
                run_every.total_seconds()
                if isinstance(run_every, timedelta)
                else float(run_every)
            )
            start_ts: float | None = None
            if _start_at is not None:
                start_ts = (
                    _start_at.timestamp()
                    if isinstance(_start_at, datetime)
                    else float(_start_at)
                )
            if run_for is None and max_runs is None:
                run_for = timedelta(hours=1)
            run_for_seconds: float | None = None
            if run_for is not None:
                run_for_seconds = (
                    run_for.total_seconds()
                    if isinstance(run_for, timedelta)
                    else float(run_for)
                )
                if run_for_seconds <= 0:
                    raise ValueError(f"run_for must be positive, got {run_for}")
            if max_runs is not None and max_runs <= 0:
                raise ValueError(f"max_runs must be positive, got {max_runs}")
            return await submit_recurring(
                func, wrapper, *args,
                _backend=backend, _interval=interval, _start_at=start_ts,
                _run_for=run_for_seconds, _max_runs=max_runs,
                **kwargs,
            )

        if run_at is not None or run_in is not None:
            from offwork.worker.remote import submit_remote_scheduled  # circular

            if run_at is not None:
                scheduled_at = (
                    run_at.timestamp()
                    if isinstance(run_at, datetime)
                    else float(run_at)
                )
            else:
                assert run_in is not None
                delay = (
                    run_in.total_seconds()
                    if isinstance(run_in, timedelta)
                    else float(run_in)
                )
                scheduled_at = _time.time() + delay
            return await submit_remote_scheduled(
                func, wrapper, *args,
                _backend=backend, _scheduled_at=scheduled_at,
                **kwargs,
            )

        from offwork.worker.remote import submit_remote  # circular

        return await submit_remote(func, wrapper, *args, _backend=backend, **kwargs)

    return submit


def _make_run_method(
    submit_method: Callable[..., object],
    is_streaming: bool,
) -> Callable[..., object]:
    """Create the ``.run()`` async method that submits and awaits the result."""

    async def run(*args: object, **kwargs: object) -> object:
        if is_streaming:
            raise TypeError(
                "This task is an async generator; use '.stream(...)' and "
                "iterate with 'async for', not '.run(...)'."
            )
        result = await submit_method(*args, **kwargs)  # type: ignore[misc]
        return await result

    return run


def _make_stream_method(
    wrapper: Callable[..., object],
    func: Callable[..., object],
    is_streaming: bool,
) -> Callable[..., object]:
    """Create the ``.stream()`` method returning a streaming handle.

    The returned object can be used directly with ``async for`` (it
    submits lazily on first iteration) or ``await``-ed to obtain the
    underlying :class:`~offwork.worker.result.Stream`.
    """

    def stream(*args: Any, backend: Any = None, **kwargs: Any) -> object:
        if not is_streaming:
            raise TypeError(
                "This task is not an async generator; '.stream(...)' is only "
                "available for 'async def ... yield' tasks. Use '.run(...)' or "
                "'.submit(...)' instead."
            )
        from offwork.worker.result import _StreamSubmission  # circular

        return _StreamSubmission(func, wrapper, args, kwargs, backend)

    return stream


def _make_map_method(
    submit_method: Callable[..., object],
) -> Callable[..., object]:
    """Create the ``.map()`` async method for batch submission and collection."""

    async def map(args_list: list[tuple[object, ...]], **kwargs: object) -> list[object]:
        coros: list[Awaitable[object]] = [
            cast(Awaitable[object], submit_method(*args, **kwargs))
            for args in args_list
        ]
        results = await asyncio.gather(*coros)
        awaitables: list[Awaitable[object]] = [
            cast(Awaitable[object], r) for r in results
        ]
        return list(await asyncio.gather(*awaitables))

    return map


def _attach_traced_attrs(
    wrapper: Callable[..., object], func: Callable[..., object]
) -> None:
    """Attach offwork metadata and remote-execution methods to a traced wrapper."""
    unwrapped = inspect.unwrap(func)
    is_streaming = inspect.isasyncgenfunction(unwrapped)
    wrapper.__offwork_traced__ = True  # type: ignore[attr-defined]
    wrapper.is_streaming = is_streaming  # type: ignore[attr-defined]
    submit = _make_submit_method(wrapper, func)
    wrapper.submit = submit  # type: ignore[attr-defined]
    wrapper.run = _make_run_method(submit, is_streaming)  # type: ignore[attr-defined]
    wrapper.map = _make_map_method(submit)  # type: ignore[attr-defined]
    wrapper.stream = _make_stream_method(wrapper, func, is_streaming)  # type: ignore[attr-defined]


def _get_stdlib_dirs() -> list[str]:
    dirs: list[str] = []
    for key in ("stdlib", "platstdlib"):
        val = sysconfig.get_path(key)
        if val:
            dirs.append(str(Path(val).resolve()))
    return dirs


_STDLIB_DIRS = _get_stdlib_dirs()

# Derive our own top-level package name so we never inline offwork internals.
_SELF_TOP_PACKAGE = (__package__ or __name__).split(".")[0]


def _is_stdlib_module(module: str) -> bool:
    """Return True if *module* belongs to the standard library."""
    top_module = module.split(".")[0]
    return hasattr(sys, "stdlib_module_names") and top_module in sys.stdlib_module_names


def _is_user_source_file(source_file: str) -> bool:
    """Return True if *source_file* is user code (not stdlib/site-packages)."""
    resolved = str(Path(source_file).resolve())
    if any(resolved.startswith(d) for d in _STDLIB_DIRS):
        return False
    return f"{os.sep}site-packages{os.sep}" not in resolved


def _is_user_defined(obj: object) -> bool:
    """Return True if *obj* (function or class) is user-defined."""
    if inspect.isfunction(obj):
        module = obj.__module__
    elif inspect.isclass(obj):
        module = obj.__module__
    else:
        return False
    if _is_stdlib_module(module):
        return False
    if module.split(".")[0] == _SELF_TOP_PACKAGE:
        return False
    try:
        source_file = inspect.getfile(obj)
    except (TypeError, OSError):
        return False
    return _is_user_source_file(source_file)


def _is_user_function(func: Callable[..., object]) -> bool:
    """Return True if func is user-defined (not stdlib or third-party)."""
    return inspect.isfunction(func) and _is_user_defined(func)


def _is_user_class(cls: type) -> bool:
    """Return True if cls is user-defined (not stdlib or third-party)."""
    return inspect.isclass(cls) and _is_user_defined(cls)


class TracingMixin:
    """Runtime call-stack tracing for dependency edge recording.

    Expects the host class to provide:
    - ``self._call_stack``: ``contextvars.ContextVar[list[str]]``
    - ``self._runtime_deps``: ``dict[str, set[str]]``
    - ``self._lock``: ``threading.Lock``
    """

    _call_stack: contextvars.ContextVar[list[str]]
    _runtime_deps: dict[str, set[str]]
    _lock: threading.Lock

    def _get_call_stack(self) -> list[str]:
        try:
            return self._call_stack.get()
        except LookupError:
            stack: list[str] = []
            self._call_stack.set(stack)
            return stack

    def _ensure_isolated_stack(self) -> list[str]:
        """Return a call stack isolated for the current async context.

        ContextVar copies the reference to the list when a new Task is created,
        so async wrappers must call this to get a fresh copy, preventing
        mutations from leaking across tasks.
        """
        stack = list(self._get_call_stack())
        self._call_stack.set(stack)
        return stack

    def _record_edge(self, stack: list[str], qualified_name: str) -> None:
        """Record a runtime dependency edge if a caller is on the stack."""
        if stack and stack[-1] != qualified_name:
            with self._lock:
                self._runtime_deps.setdefault(stack[-1], set()).add(qualified_name)

    def create_wrapper(self, func: Callable[_P, _R]) -> TracedFunction[_P, _R]:
        """Wrap func to record runtime caller-callee edges.

        The wrapper preserves the original function signature via
        ``functools.wraps`` and adds ``.run()`` / ``.arun()`` /
        ``.map()`` / ``.amap()`` methods for remote submission.
        """
        qualified_name = f"{func.__module__}.{func.__qualname__}"
        # Each _wrap_* method returns Any because functools.wraps erases
        # the precise callable type.  The outer signature guarantees _F.
        if inspect.isasyncgenfunction(func):
            wrapper = self._wrap_async_generator(func, qualified_name)
        elif inspect.iscoroutinefunction(func):
            wrapper = self._wrap_coroutine(func, qualified_name)
        elif inspect.isgeneratorfunction(func):
            wrapper = self._wrap_generator(func, qualified_name)
        else:
            wrapper = self._wrap_sync(func, qualified_name)
        return cast(TracedFunction[_P, _R], wrapper)

    def _wrap_async_generator(self, func: Any, qualified_name: str) -> Any:
        logger.debug("Creating async generator wrapper for %s", qualified_name)

        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            self._record_edge(self._get_call_stack(), qualified_name)
            return self._proxy_async_generator(func(*args, **kwargs), qualified_name)

        _attach_traced_attrs(wrapper, func)
        return wrapper

    def _wrap_coroutine(self, func: Any, qualified_name: str) -> Any:
        logger.debug("Creating async wrapper for %s", qualified_name)

        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> object:
            stack = self._ensure_isolated_stack()
            self._record_edge(stack, qualified_name)
            stack.append(qualified_name)
            try:
                return await func(*args, **kwargs)
            finally:
                stack.pop()

        _attach_traced_attrs(wrapper, func)
        return wrapper

    def _wrap_generator(self, func: Any, qualified_name: str) -> Any:
        logger.debug("Creating generator wrapper for %s", qualified_name)

        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            self._record_edge(self._get_call_stack(), qualified_name)
            return self._proxy_generator(func(*args, **kwargs), qualified_name)

        _attach_traced_attrs(wrapper, func)
        return wrapper

    def _wrap_sync(self, func: Any, qualified_name: str) -> Any:
        logger.debug("Creating sync wrapper for %s", qualified_name)

        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            stack = self._get_call_stack()
            self._record_edge(stack, qualified_name)
            stack.append(qualified_name)
            try:
                return func(*args, **kwargs)
            finally:
                stack.pop()

        _attach_traced_attrs(wrapper, func)
        return wrapper

    def _proxy_generator(
        self,
        gen: Generator[object, object, object],
        qualified_name: str,
    ) -> Generator[object, object, object]:
        """Wrap a generator to maintain call stack context during iteration."""
        stack = self._get_call_stack()
        stack.append(qualified_name)
        try:
            value = next(gen)
        except StopIteration as e:
            return e.value
        finally:
            stack.pop()

        while True:
            try:
                sent = yield value
            except GeneratorExit:
                gen.close()
                return  # type: ignore[return-value]
            except BaseException as exc:
                stack = self._get_call_stack()
                stack.append(qualified_name)
                try:
                    value = gen.throw(exc)
                except StopIteration as e:
                    return e.value
                finally:
                    stack.pop()
            else:
                stack = self._get_call_stack()
                stack.append(qualified_name)
                try:
                    value = gen.send(sent)
                except StopIteration as e:
                    return e.value
                finally:
                    stack.pop()

    async def _proxy_async_generator(
        self,
        async_gen: AsyncGenerator[object, object],
        qualified_name: str,
    ) -> AsyncGenerator[object, object]:
        """Wrap an async generator to maintain call stack context during iteration."""
        # Isolate once at entry; subsequent calls reuse the same isolated stack.
        self._ensure_isolated_stack()
        stack = self._get_call_stack()
        stack.append(qualified_name)
        try:
            value = await async_gen.__anext__()
        except StopAsyncIteration:
            return
        finally:
            stack.pop()

        while True:
            try:
                sent = yield value
            except GeneratorExit:
                await async_gen.aclose()
                return
            except BaseException as exc:
                stack = self._get_call_stack()
                stack.append(qualified_name)
                try:
                    value = await async_gen.athrow(exc)
                except StopAsyncIteration:
                    return
                finally:
                    stack.pop()
            else:
                stack = self._get_call_stack()
                stack.append(qualified_name)
                try:
                    value = await async_gen.asend(sent)
                except StopAsyncIteration:
                    return
                finally:
                    stack.pop()
