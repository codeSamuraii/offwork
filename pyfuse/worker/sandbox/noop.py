"""No-op executor — runs code in the host process (current behaviour)."""

import asyncio
import contextvars
import functools
import inspect
from typing import Any

from pyfuse.worker.sandbox.base import SandboxExecutor


class NoopExecutor(SandboxExecutor):
    """Passthrough executor that compiles and runs code locally.

    This replicates the original ``exec``-based execution path so that
    the :class:`~pyfuse.worker.worker.Worker` can unconditionally use a
    :class:`SandboxExecutor` without paying any overhead when sandboxing
    is disabled.
    """

    async def execute(
        self,
        source: str,
        function_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        owner_class: str | None = None,
    ) -> Any:
        code = compile(source, f"<pyfuse:{function_name}>", "exec")
        namespace: dict[str, Any] = {}
        exec(code, namespace)  # noqa: S102

        func = _extract_callable(namespace, function_name, owner_class)

        if inspect.iscoroutinefunction(func):
            return await func(*args, **kwargs)

        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(
            None, ctx.run, functools.partial(func, *args, **kwargs),
        )


def _extract_callable(
    namespace: dict[str, Any],
    function_name: str,
    owner_class: str | None,
) -> Any:
    """Look up the target callable from an exec'd namespace."""
    if owner_class:
        class_name = owner_class.rsplit(".", 1)[-1]
        cls = namespace.get(class_name)
        if cls is None:
            raise RuntimeError(
                f"Class '{class_name}' not found in reconstructed namespace"
            )
        func = getattr(cls, function_name, None)
        if func is None:
            raise RuntimeError(
                f"Method '{function_name}' not found on class '{class_name}'"
            )
        return func

    func = namespace.get(function_name)
    if func is None:
        raise RuntimeError(
            f"Function '{function_name}' not found in reconstructed namespace"
        )
    return func
