from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any

from pyfuse.worker.deps import ensure_dependencies
from pyfuse.core.errors import WorkerError
from pyfuse.graph.store import Store
from pyfuse.core.task import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildInfo:
    """Metadata about how a function was resolved for execution."""

    cache_hit: bool
    installed_packages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _CachedFunction:
    namespace: dict[str, Any]
    func: Any  # callable
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
    simple_name = target_node.name

    if target_node.owner_class:
        class_name = target_node.owner_class.rsplit(".", 1)[-1]
        cls = namespace.get(class_name)
        if cls is None:
            raise WorkerError(
                f"Class '{class_name}' not found in reconstructed namespace"
            )
        func = getattr(cls, simple_name, None)
        if func is None:
            raise WorkerError(
                f"Method '{simple_name}' not found on class '{class_name}'"
            )
        return func

    func = namespace.get(simple_name)
    if func is None:
        raise WorkerError(
            f"Function '{simple_name}' not found in reconstructed namespace"
        )
    return func


class Worker:
    """Execute functions from serialized pyfuse graphs with caching.

    Parameters
    ----------
    import_to_package
        Extra import-name → pip-package-name mappings (merged with defaults).
    auto_install
        Automatically install missing third-party dependencies via pip.
    """

    def __init__(
        self,
        import_to_package: dict[str, str] | None = None,
        auto_install: bool = True,
    ) -> None:
        self._import_to_package = import_to_package
        self._auto_install = auto_install
        self._cache: dict[str, _CachedFunction] = {}
        self._local = threading.local()

    def _get_cached(self, json_str: str, function_name: str) -> _CachedFunction:
        """Return the cached (or freshly built) function for *function_name*."""
        store = Store.from_json(json_str)
        key = _compute_subgraph_key(store, function_name)
        if key not in self._cache:
            self._cache[key] = self._build(store, function_name, key)
        else:
            self._local.last_build_info = BuildInfo(cache_hit=True)
        return self._cache[key]

    def execute(
        self,
        json_str: str,
        function_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Deserialize, reconstruct, and execute *function_name*.

        Cached by subgraph content hash — repeated calls with identical
        graphs skip reconstruction entirely.
        """
        cached = self._get_cached(json_str, function_name)
        logger.info("Executing %s (cache key: %s)", function_name, cached.subgraph_key)
        return cached.func(*args, **kwargs)

    async def execute_async(
        self,
        json_str: str,
        function_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Like :meth:`execute` but ``await``s the target coroutine."""
        cached = self._get_cached(json_str, function_name)
        logger.info("Executing async %s (cache key: %s)", function_name, cached.subgraph_key)
        return await cached.func(*args, **kwargs)

    def run(self, task: Task) -> Any:
        """Execute a :class:`Task`, resolving serialized object arguments."""
        from pyfuse.core.task import resolve_args

        cached = self._get_cached(task.graph_json, task.function_name)
        args, kwargs = resolve_args(task.args, task.kwargs, cached.namespace)
        logger.info("Executing %s (cache key: %s)", task.function_name, cached.subgraph_key)
        return cached.func(*args, **kwargs)

    async def run_async(self, task: Task) -> Any:
        """Execute a :class:`Task` asynchronously, resolving serialized object arguments."""
        from pyfuse.core.task import resolve_args

        cached = self._get_cached(task.graph_json, task.function_name)
        args, kwargs = resolve_args(task.args, task.kwargs, cached.namespace)
        logger.info("Executing async %s (cache key: %s)", task.function_name, cached.subgraph_key)
        return await cached.func(*args, **kwargs)

    def run_with_policy(self, task: Task) -> Any:
        """Execute a :class:`Task` with retry and timeout enforcement.

        Reads ``task.retries``, ``task.timeout``, and ``task.retry_delay``
        to apply exponential-backoff retries and per-attempt timeouts.
        """
        last_exc: Exception | None = None
        for attempt in range(1 + task.retries):
            try:
                if task.timeout is not None:
                    return self._run_with_timeout(task, task.timeout)
                return self.run(task)
            except Exception as exc:
                last_exc = exc
                if attempt < task.retries:
                    delay = task.retry_delay * (2 ** attempt)
                    logger.warning(
                        "Task %s attempt %d/%d failed, retrying in %.1fs: %s",
                        task.task_id, attempt + 1, task.retries, delay, exc,
                    )
                    time.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def _run_with_timeout(self, task: Task, timeout: float) -> Any:
        """Execute a task with a timeout enforced via a thread pool."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.run, task)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeoutError:
                raise TimeoutError(
                    f"Task {task.task_id} timed out after {timeout}s"
                ) from None

    def cache_info(self) -> dict[str, Any]:
        """Return cache statistics."""
        return {"size": len(self._cache), "keys": list(self._cache.keys())}

    def clear_cache(self) -> None:
        """Drop all cached functions."""
        self._cache.clear()

    # -- internals -------------------------------------------------------------

    def last_build_info(self) -> BuildInfo | None:
        """Return metadata about the most recent execution's build phase."""
        return getattr(self._local, "last_build_info", None)

    def _build(
        self, store: Store, function_name: str, key: str
    ) -> _CachedFunction:
        installed_packages: list[str] = []
        if self._auto_install:
            install_result = ensure_dependencies(
                store, function_name, self._import_to_package
            )
            installed_packages = install_result.installed

        self._local.last_build_info = BuildInfo(
            cache_hit=False,
            installed_packages=installed_packages,
        )

        source = store.reconstruct(function_name)
        logger.debug("Reconstructed source for %s:\n%s", function_name, source)

        code = compile(source, f"<pyfuse:{function_name}>", "exec")
        namespace: dict[str, Any] = {}
        exec(code, namespace)  # noqa: S102

        func = _extract_target_callable(namespace, store, function_name)
        return _CachedFunction(
            namespace=namespace, func=func, subgraph_key=key, source=source
        )


def execute(
    json_str_or_task: str | Task,
    function_name: str | None = None,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """One-shot execution of a function from a serialized pyfuse graph.

    Accepts either a JSON string + function name, or a :class:`Task`::

        execute(json_str, "my_func", arg1, arg2)
        execute(task)
    """
    worker = Worker()
    if isinstance(json_str_or_task, Task):
        return worker.run(json_str_or_task)
    if function_name is None:
        raise TypeError("function_name is required when passing a JSON string")
    return worker.execute(json_str_or_task, function_name, *args, **kwargs)
