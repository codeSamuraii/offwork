"""Worker: reconstruct functions from serialized stores, cache, and execute."""

import asyncio
import hashlib
import inspect
import logging
import functools
import contextvars
from typing import Any
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from offwork.core.task import Task, resolve_args
from offwork.core.errors import WorkerError
from offwork.core.models import FunctionNode
from offwork.graph.store import Store
from offwork.worker.deps import ensure_dependencies
from offwork.worker.sandbox import DockerSandbox

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildInfo:
    """Metadata about how a function was resolved for execution."""

    cache_hit: bool
    installed_packages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _CachedFunction:
    """A cached compiled function with its namespace and metadata."""

    namespace: dict[str, Any]
    func: Callable[..., Any]
    subgraph_key: str
    source: str


def _compute_subgraph_key(store: Store, function_name: str) -> str:
    """Compute a cache key from all content hashes in the function's subgraph."""
    root_hash = store._resolve_function_hash(function_name)
    all_hashes = sorted(store.walk(root_hash))
    return hashlib.sha256(":".join(all_hashes).encode()).hexdigest()[:16]


def _extract_target_callable(
    namespace: dict[str, Any],
    store: Store,
    function_name: str,
) -> Any:
    """Extract the target callable from an exec'd namespace."""
    target_qname, nodes = store.collect(function_name)
    target_node = nodes[target_qname]

    if target_node.owner_class:
        return _extract_method(namespace, target_node)
    return _extract_function(namespace, target_node)


def _extract_method(namespace: dict[str, Any], node: FunctionNode) -> Any:
    """Look up a method on a class in the reconstructed namespace."""
    assert node.owner_class is not None
    class_name = node.owner_class.rsplit(".", 1)[-1]
    cls = namespace.get(class_name)
    if cls is None:
        raise WorkerError(f"Class '{class_name}' not found in reconstructed namespace")
    func = getattr(cls, node.name, None)
    if func is None:
        raise WorkerError(f"Method '{node.name}' not found on class '{class_name}'")
    return func


def _extract_function(namespace: dict[str, Any], node: FunctionNode) -> Any:
    """Look up a standalone function in the reconstructed namespace."""
    func = namespace.get(node.name)
    if func is None:
        raise WorkerError(f"Function '{node.name}' not found in reconstructed namespace")
    return func


class Worker:
    """Execute functions from serialized offwork graphs with caching.

    Parameters
    ----------
    auto_install
        Automatically install missing third-party dependencies via pip.
    import_to_package
        Extra import-name -> pip-package-name mappings (merged with defaults).
    sandbox
        Optional :class:`~offwork.worker.sandbox.DockerSandbox` or
        ``True`` to create one with default settings.  When provided,
        function execution is delegated to the sandbox instead of
        running ``exec`` in the host process.
    """

    def __init__(
        self,
        auto_install: bool = True,
        import_to_package: dict[str, str] | None = None,
        sandbox: DockerSandbox | bool | None = None,
    ) -> None:
        self._import_to_package = import_to_package
        self._auto_install = auto_install
        self._cache: dict[str, _CachedFunction] = {}
        self._last_build_info: BuildInfo | None = None
        # When set, the current task already recorded its build info during
        # pre-execution inspection (``is_streaming``); later cache lookups
        # for the same task must not overwrite it with a spurious hit.
        self._build_info_locked = False

        if sandbox is True:
            self._sandbox: DockerSandbox | None = DockerSandbox()
        elif isinstance(sandbox, DockerSandbox):
            self._sandbox = sandbox
        else:
            self._sandbox = None

    async def _get_cached(self, json_str: str, function_name: str) -> _CachedFunction:
        """Return the cached (or freshly built) function for *function_name*."""
        store = Store.from_json(json_str)
        key = _compute_subgraph_key(store, function_name)
        if key not in self._cache:
            self._cache[key] = await self._build(store, function_name, key)
        elif not self._build_info_locked:
            self._last_build_info = BuildInfo(cache_hit=True)
        return self._cache[key]

    @property
    def sandboxed(self) -> bool:
        """Whether execution is delegated to a Docker sandbox."""
        return self._sandbox is not None

    async def run(self, task: Task) -> Any:
        """Execute a :class:`Task`, resolving serialized object arguments.

        When a sandbox is configured, the full source + args are sent to
        the sandbox executor.  Otherwise async functions are awaited
        directly and sync functions run in a thread executor.
        """
        cached = await self._get_cached(task.graph_json, task.function_name)
        self._build_info_locked = False
        logger.debug("Executing %s (cache key: %s)", task.function_name, cached.subgraph_key)

        if self.sandboxed:
            assert self._sandbox is not None
            # Determine owner_class for method dispatch inside the sandbox
            store = Store.from_json(task.graph_json)
            target_qname, nodes = store.collect(task.function_name)
            target_node = nodes[target_qname]
            return await self._sandbox.execute(
                cached.source,
                target_node.name,
                task.args,
                task.kwargs,
                owner_class=target_node.owner_class,
            )

        args, kwargs = resolve_args(task.args, task.kwargs, cached.namespace)

        if inspect.isasyncgenfunction(cached.func):
            raise WorkerError(
                f"Task {task.function_name!r} is an async generator. "
                "Streaming tasks must be executed via run_stream(), not run()."
            )
        if inspect.isgeneratorfunction(cached.func):
            raise WorkerError(
                f"Task {task.function_name!r} is a synchronous generator. "
                "Synchronous generators are not supported (planned for v2); "
                "use 'async def ... yield' for streaming tasks."
            )

        if inspect.iscoroutinefunction(cached.func):
            return await cached.func(*args, **kwargs)

        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(
            None, ctx.run, functools.partial(cached.func, *args, **kwargs),
        )

    async def is_streaming(self, task: Task) -> bool:
        """Return ``True`` if *task*'s target is an async generator.

        Raises :class:`WorkerError` for synchronous generators, which are
        not supported (planned for v2).

        This is the first cache lookup for a task dispatched through the
        worker loop, so the build info it records is authoritative.  The
        lock prevents the subsequent ``run``/``run_stream`` lookup from
        masking a fresh build with a spurious cache hit.
        """
        self._build_info_locked = False
        cached = await self._get_cached(task.graph_json, task.function_name)
        self._build_info_locked = True
        if inspect.isgeneratorfunction(cached.func):
            raise WorkerError(
                f"Task {task.function_name!r} is a synchronous generator. "
                "Synchronous generators are not supported (planned for v2); "
                "use 'async def ... yield' for streaming tasks."
            )
        return inspect.isasyncgenfunction(cached.func)

    async def run_stream(
        self,
        task: Task,
        on_yield: Callable[[int, Any], Awaitable[None]],
    ) -> int:
        """Execute an async-generator *task*, invoking *on_yield* per value.

        *on_yield* receives ``(seq, value)`` for each value the generator
        yields, with *seq* a zero-based monotonically increasing index.
        Returns the total number of values yielded.  Sandboxed execution
        of streaming tasks is not yet supported.
        """
        cached = await self._get_cached(task.graph_json, task.function_name)
        self._build_info_locked = False

        if self.sandboxed:
            assert self._sandbox is not None
            store = Store.from_json(task.graph_json)
            target_qname, nodes = store.collect(task.function_name)
            target_node = nodes[target_qname]
            return await self._sandbox.execute_stream(
                cached.source,
                target_node.name,
                task.args,
                task.kwargs,
                on_yield,
                owner_class=target_node.owner_class,
            )

        if not inspect.isasyncgenfunction(cached.func):
            raise WorkerError(
                f"Task {task.function_name!r} is not an async generator."
            )

        args, kwargs = resolve_args(task.args, task.kwargs, cached.namespace)
        agen = cached.func(*args, **kwargs)
        seq = 0
        try:
            async for value in agen:
                await on_yield(seq, value)
                seq += 1
        finally:
            await agen.aclose()
        return seq

    async def run_with_policy(self, task: Task) -> Any:
        """Execute a :class:`Task` with retry and timeout enforcement.

        Reads ``task.retries``, ``task.timeout``, and ``task.retry_delay``
        to apply exponential-backoff retries and per-attempt timeouts.
        """
        last_exc: Exception | None = None
        for attempt in range(1 + task.retries):
            try:
                if task.timeout is not None:
                    return await asyncio.wait_for(
                        self.run(task), timeout=task.timeout,
                    )
                return await self.run(task)
            except Exception as exc:
                last_exc = exc
                if attempt < task.retries:
                    delay = task.retry_delay * (2 ** attempt)
                    logger.warning(
                        "Task %s attempt %d/%d failed, retrying in %.1fs: %s",
                        task.task_id, attempt + 1, task.retries, delay, exc,
                    )
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def cache_info(self) -> dict[str, Any]:
        """Return cache statistics."""
        return {"size": len(self._cache), "keys": list(self._cache.keys())}

    def clear_cache(self) -> None:
        """Drop all cached functions."""
        self._cache.clear()

    # -- internals -------------------------------------------------------------

    def last_build_info(self) -> BuildInfo | None:
        """Return metadata about the most recent execution's build phase."""
        return self._last_build_info

    async def _build(
        self, store: Store, function_name: str, key: str
    ) -> _CachedFunction:
        installed_packages: list[str] = []
        if self._auto_install:
            install_result = await ensure_dependencies(
                store, function_name, self._import_to_package
            )
            installed_packages = install_result.installed

        self._last_build_info = BuildInfo(
            cache_hit=False,
            installed_packages=installed_packages,
        )

        source = store.reconstruct(function_name)
        logger.debug("Reconstructed source for %s:\n%s", function_name, source)

        code = compile(source, f"<offwork:{function_name}>", "exec")
        namespace: dict[str, Any] = {}
        exec(code, namespace)  # noqa: S102

        func = _extract_target_callable(namespace, store, function_name)
        return _CachedFunction(
            namespace=namespace, func=func, subgraph_key=key, source=source
        )


async def execute(
    json_str_or_task: str | Task,
    function_name: str | None = None,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """One-shot execution of a function from a serialized offwork graph.

    Accepts either a JSON string + function name, or a :class:`Task`::

        await execute(json_str, "my_func", arg1, arg2)
        await execute(task)
    """
    worker = Worker()
    if isinstance(json_str_or_task, Task):
        return await worker.run(json_str_or_task)
    if function_name is None:
        raise TypeError("function_name is required when passing a JSON string")

    cached = await worker._get_cached(json_str_or_task, function_name)
    resolved_args, resolved_kwargs = resolve_args(args, {}, cached.namespace)

    if inspect.iscoroutinefunction(cached.func):
        return await cached.func(*resolved_args, **resolved_kwargs)

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(
        None, ctx.run, functools.partial(cached.func, *resolved_args, **resolved_kwargs),
    )
