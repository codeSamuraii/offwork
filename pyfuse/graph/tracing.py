from __future__ import annotations

import os
import sys
import inspect
import logging
import builtins
import functools
import sysconfig
import threading
import contextvars
from typing import TYPE_CHECKING, Any, TypeVar
from pathlib import Path
from collections.abc import Callable, Generator, AsyncGenerator

if TYPE_CHECKING:
    from pyfuse.worker.backends.base import Backend

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., object])

_BUILTIN_NAMES = set(dir(builtins))


def _make_start_method(
    wrapper: Callable[..., object], func: Callable[..., object]
) -> Callable[..., object]:
    """Create the ``.start()`` async method that submits and returns a Result."""

    async def start(*args: object, backend: str | Backend | None = None, **kwargs: object) -> object:
        from pyfuse.worker.remote import submit_remote

        return await submit_remote(func, wrapper, *args, backend=backend, **kwargs)

    return start


def _make_run_method(
    start_method: Callable[..., object],
) -> Callable[..., object]:
    """Create the ``.run()`` async method that submits and awaits the result."""

    async def run(*args: object, **kwargs: object) -> object:
        result = await start_method(*args, **kwargs)  # type: ignore[misc]
        return await result

    return run


def _make_map_method(
    start_method: Callable[..., object],
) -> Callable[..., object]:
    """Create the ``.map()`` async method for batch submission and collection."""

    async def map(args_list: list[tuple[object, ...]], **kwargs: object) -> list[object]:
        results = [await start_method(*args, **kwargs) for args in args_list]  # type: ignore[misc]
        return [await r for r in results]

    return map


def _attach_traced_attrs(
    wrapper: Callable[..., object], func: Callable[..., object]
) -> None:
    """Attach pyfuse metadata and .start()/.run()/.map() to a traced wrapper."""
    wrapper.__pyfuse_traced__ = True  # type: ignore[attr-defined]
    start = _make_start_method(wrapper, func)
    wrapper.start = start  # type: ignore[attr-defined]
    wrapper.run = _make_run_method(start)  # type: ignore[attr-defined]
    wrapper.map = _make_map_method(start)  # type: ignore[attr-defined]


def _get_stdlib_dirs() -> list[str]:
    dirs: list[str] = []
    for key in ("stdlib", "platstdlib"):
        val = sysconfig.get_path(key)
        if val:
            dirs.append(str(Path(val).resolve()))
    return dirs


_STDLIB_DIRS = _get_stdlib_dirs()

# Derive our own top-level package name so we never inline pyfuse internals.
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

    def create_wrapper(self, func: _F) -> _F:
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
        return wrapper  # type: ignore[no-any-return]

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
