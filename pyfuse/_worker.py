from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from pyfuse._deps import ensure_dependencies
from pyfuse._errors import WorkerError
from pyfuse._store import FuseStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CachedFunction:
    namespace: dict[str, Any]
    func: Any  # callable
    subgraph_key: str
    source: str


def _compute_subgraph_key(store: FuseStore, function_name: str) -> str:
    """Compute a cache key from all content hashes in the function's subgraph."""
    root_hash = store._resolve_function_hash(function_name)
    all_hashes = sorted(store.walk(root_hash))
    return hashlib.sha256(":".join(all_hashes).encode()).hexdigest()[:16]


def _extract_target_callable(
    namespace: dict[str, Any],
    store: FuseStore,
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


class FuseWorker:
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
        store = FuseStore.from_json(json_str)
        key = _compute_subgraph_key(store, function_name)

        if key not in self._cache:
            self._cache[key] = self._build(store, function_name, key)

        cached = self._cache[key]
        logger.info("Executing %s (cache key: %s)", function_name, key)
        return cached.func(*args, **kwargs)

    async def execute_async(
        self,
        json_str: str,
        function_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Like :meth:`execute` but ``await``s the target coroutine."""
        store = FuseStore.from_json(json_str)
        key = _compute_subgraph_key(store, function_name)

        if key not in self._cache:
            self._cache[key] = self._build(store, function_name, key)

        cached = self._cache[key]
        logger.info("Executing async %s (cache key: %s)", function_name, key)
        return await cached.func(*args, **kwargs)

    def cache_info(self) -> dict[str, Any]:
        """Return cache statistics."""
        return {"size": len(self._cache), "keys": list(self._cache.keys())}

    def clear_cache(self) -> None:
        """Drop all cached functions."""
        self._cache.clear()

    # -- internals -------------------------------------------------------------

    def _build(
        self, store: FuseStore, function_name: str, key: str
    ) -> _CachedFunction:
        if self._auto_install:
            ensure_dependencies(
                store, function_name, self._import_to_package
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
    json_str: str, function_name: str, *args: Any, **kwargs: Any
) -> Any:
    """One-shot execution of a function from a serialized pyfuse graph."""
    return FuseWorker().execute(json_str, function_name, *args, **kwargs)
