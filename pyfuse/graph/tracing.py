from __future__ import annotations

import builtins
import contextvars
import functools
import inspect
import logging
import os
import sys
import sysconfig
import threading
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from pyfuse.worker.backends.base import Backend

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., object])

_BUILTIN_NAMES = set(dir(builtins))


def _make_run_method(
    wrapper: Callable[..., object], func: Callable[..., object]
) -> Callable[..., object]:
    """Create the ``.run()`` / ``.delay()`` method attached to a traced wrapper."""

    def run(*args: object, backend: str | Backend | None = None, **kwargs: object) -> object:
        from pyfuse.worker.remote import submit_remote

        return submit_remote(func, wrapper, *args, _backend=backend, **kwargs)

    return run


def _make_map_method(
    run_method: Callable[..., object],
) -> Callable[..., object]:
    """Create the ``.map()`` method for batch submission."""

    def map(args_list: list[tuple[object, ...]], **kwargs: object) -> list[object]:
        return [run_method(*args, **kwargs) for args in args_list]

    return map


def _get_stdlib_dirs() -> list[str]:
    dirs: list[str] = []
    for key in ("stdlib", "platstdlib"):
        val = sysconfig.get_path(key)
        if val:
            dirs.append(str(Path(val).resolve()))
    return dirs


_STDLIB_DIRS = _get_stdlib_dirs()


def _is_user_function(func: Callable[..., object]) -> bool:
    """Return True if func is user-defined (not stdlib or third-party)."""
    if not inspect.isfunction(func):
        return False
    top_module = func.__module__.split(".")[0]
    if hasattr(sys, "stdlib_module_names") and top_module in sys.stdlib_module_names:
        return False
    try:
        source_file = inspect.getfile(func)
    except (TypeError, OSError):
        return False
    resolved = str(Path(source_file).resolve())
    for stdlib_dir in _STDLIB_DIRS:
        if resolved.startswith(stdlib_dir):
            return False
    # Heuristic: anything under a site-packages directory is third-party
    if f"{os.sep}site-packages{os.sep}" in resolved:
        return False
    return True


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
        """Wrap func to record runtime caller-callee edges."""
        qualified_name = f"{func.__module__}.{func.__qualname__}"

        if inspect.isasyncgenfunction(func):
            logger.debug("Creating async generator wrapper for %s", qualified_name)

            @functools.wraps(func)
            def async_gen_wrapper(*args: object, **kwargs: object) -> object:
                self._record_edge(self._get_call_stack(), qualified_name)
                async_gen = func(*args, **kwargs)
                return self._proxy_async_generator(async_gen, qualified_name)

            async_gen_wrapper.__pyfuse_traced__ = True  # type: ignore[attr-defined]
            async_gen_wrapper.run = _make_run_method(async_gen_wrapper, func)  # type: ignore[attr-defined]
            async_gen_wrapper.delay = async_gen_wrapper.run  # type: ignore[attr-defined]
            async_gen_wrapper.map = _make_map_method(async_gen_wrapper.run)  # type: ignore[attr-defined]
            return async_gen_wrapper  # type: ignore[return-value]

        if inspect.iscoroutinefunction(func):
            logger.debug("Creating async wrapper for %s", qualified_name)

            @functools.wraps(func)
            async def async_wrapper(*args: object, **kwargs: object) -> object:
                stack = self._ensure_isolated_stack()
                self._record_edge(stack, qualified_name)
                stack.append(qualified_name)
                try:
                    return await func(*args, **kwargs)
                finally:
                    stack.pop()

            async_wrapper.__pyfuse_traced__ = True  # type: ignore[attr-defined]
            async_wrapper.run = _make_run_method(async_wrapper, func)  # type: ignore[attr-defined]
            async_wrapper.delay = async_wrapper.run  # type: ignore[attr-defined]
            async_wrapper.map = _make_map_method(async_wrapper.run)  # type: ignore[attr-defined]
            return async_wrapper  # type: ignore[return-value]

        if inspect.isgeneratorfunction(func):
            logger.debug("Creating generator wrapper for %s", qualified_name)

            @functools.wraps(func)
            def gen_wrapper(*args: object, **kwargs: object) -> object:
                self._record_edge(self._get_call_stack(), qualified_name)
                gen = func(*args, **kwargs)
                return self._proxy_generator(gen, qualified_name)

            gen_wrapper.__pyfuse_traced__ = True  # type: ignore[attr-defined]
            gen_wrapper.run = _make_run_method(gen_wrapper, func)  # type: ignore[attr-defined]
            gen_wrapper.delay = gen_wrapper.run  # type: ignore[attr-defined]
            gen_wrapper.map = _make_map_method(gen_wrapper.run)  # type: ignore[attr-defined]
            return gen_wrapper  # type: ignore[return-value]

        logger.debug("Creating wrapper for %s", qualified_name)

        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            stack = self._get_call_stack()
            self._record_edge(stack, qualified_name)
            stack.append(qualified_name)
            try:
                return func(*args, **kwargs)
            finally:
                stack.pop()

        wrapper.__pyfuse_traced__ = True  # type: ignore[attr-defined]
        wrapper.run = _make_run_method(wrapper, func)  # type: ignore[attr-defined]
        wrapper.delay = wrapper.run  # type: ignore[attr-defined]
        wrapper.map = _make_map_method(wrapper.run)  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

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
