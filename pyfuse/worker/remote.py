from __future__ import annotations

import atexit
import inspect
import logging
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyfuse.core.version import _VERSION
from pyfuse.worker.backends.base import Backend
from pyfuse.worker.backends.redis import RedisBackend
from pyfuse.worker.result import Result, ResultEnvelope
from pyfuse.core.task import Task

if TYPE_CHECKING:
    from pyfuse.worker.worker import Worker

logger = logging.getLogger(__name__)

_active_backend: Backend | None = None
_atexit_registered = False

_ENV_VAR = "PYFUSE_BACKEND"


def _resolve_url(url: str | None) -> str:
    """Return *url* if given, otherwise read from the environment variable."""
    if url is not None:
        return url
    env_url = os.environ.get(_ENV_VAR)
    if env_url:
        return env_url
    raise ValueError(
        "No backend URL provided. Pass a URL or set the "
        f"{_ENV_VAR} environment variable."
    )


def _create_backend(url: str, **kwargs: Any) -> Backend:
    """Create a backend instance from a URL."""
    scheme = url.split("://", 1)[0].lower()
    if scheme in ("redis", "rediss"):
        return RedisBackend(url, **kwargs)
    if scheme == "shm":
        from pyfuse.worker.backends.shm import SharedMemoryBackend

        return SharedMemoryBackend(url, **kwargs)
    raise ValueError(
        f"Unknown backend scheme: {scheme!r}. "
        f"Supported: redis://, rediss://, shm://"
    )


def connect(url: str | None = None, **kwargs: Any) -> Backend:
    """Configure the global transport backend.

    Parameters
    ----------
    url
        Backend URL.  Supported schemes:

        - ``redis://`` / ``rediss://`` — :class:`RedisBackend`
        - ``shm://`` — :class:`SharedMemoryBackend` (same-machine IPC)

        When *None*, the ``PYFUSE_BACKEND`` environment variable is used.

    **kwargs
        Passed to the backend constructor.

    Returns
    -------
    Backend
        The connected backend instance.
    """
    global _active_backend, _atexit_registered
    resolved = _resolve_url(url)
    _active_backend = _create_backend(resolved, **kwargs)
    if not _atexit_registered:
        atexit.register(disconnect)
        _atexit_registered = True
    logger.info("Connected to backend: %s", resolved)
    return _active_backend


def disconnect() -> None:
    """Close and clear the global backend."""
    global _active_backend
    if _active_backend is not None:
        _active_backend.close()
        _active_backend = None
        logger.info("Disconnected from backend")


def get_backend() -> Backend:
    """Return the active backend, or raise if none is configured.

    If no backend has been configured via :func:`connect`, the
    ``PYFUSE_BACKEND`` environment variable is checked and used
    to auto-connect.
    """
    global _active_backend
    if _active_backend is None:
        env_url = os.environ.get(_ENV_VAR)
        if env_url:
            connect(env_url)
        else:
            raise RuntimeError(
                "No backend connected. Call pyfuse.connect('redis://...') "
                f"or set the {_ENV_VAR} environment variable."
            )
    return _active_backend  # type: ignore[return-value]


def submit_remote(
    func: Callable[..., object],
    wrapper: Callable[..., object],
    *args: Any,
    _backend: str | Backend | None = None,
    **kwargs: Any,
) -> Result:
    """Pack and submit a function to the remote backend.

    Called internally by ``traced_func.run(...)``.
    """
    from pyfuse.graph.graph import Graph

    if isinstance(_backend, str):
        backend = _create_backend(_backend)
    elif isinstance(_backend, Backend):
        backend = _backend
    else:
        backend = get_backend()

    unwrapped = inspect.unwrap(func)
    function_name = f"{unwrapped.__module__}.{unwrapped.__qualname__}"
    graph_json = Graph.default().serialize(wrapper)

    opts = getattr(wrapper, "__pyfuse_options__", {})
    task = Task(
        graph_json=graph_json,
        function_name=function_name,
        args=args,
        kwargs=kwargs,
        timeout=opts.get("timeout"),
        retries=opts.get("retries", 0),
        retry_delay=opts.get("retry_delay", 1.0),
    )

    backend.submit(task.to_json())
    logger.info("Submitted task %s for %s", task.task_id, function_name)
    return Result(task.task_id, backend)


def _handle_task(
    worker: Worker,
    backend: Backend,
    task_json: str,
) -> None:
    """Process a single task: deserialize, execute with policy, send result."""
    task = Task.from_json(task_json)
    logger.info("Received task %s: %s", task.task_id, task.function_name)

    error_msg = ""
    t0 = time.monotonic()
    try:
        result = worker.run_with_policy(task)
        envelope = ResultEnvelope.success(task.task_id, result)
    except Exception as exc:
        logger.debug("Task %s failed", task.task_id, exc_info=True)
        envelope = ResultEnvelope.failure(task.task_id, exc)
        error_msg = f"  {type(exc).__name__}: {exc}"
    elapsed_ms = (time.monotonic() - t0) * 1000

    backend.send_result(task.task_id, envelope.to_json())

    build_info = worker.last_build_info()
    if build_info is not None and build_info.cache_hit:
        detail = "cached"
    elif build_info is not None and build_info.installed_packages:
        pkgs = ", ".join(build_info.installed_packages)
        detail = f"build + install {pkgs}"
    else:
        detail = "build"

    status = "ok" if envelope.status == "ok" else "err"
    msg = (
        f"[{status:<3}] {task.function_name:<40} "
        f"{elapsed_ms:>6.0f}ms  {task.task_id}  ({detail}){error_msg}"
    )
    if envelope.status == "ok":
        logger.info(msg)
    else:
        logger.warning(msg)


def serve(
    url: str | None = None,
    *,
    concurrency: int = 1,
    auto_install: bool = True,
    import_to_package: dict[str, str] | None = None,
) -> None:
    """Start a worker loop that pops tasks from the backend and executes them.

    Parameters
    ----------
    url
        Backend URL (e.g. ``redis://localhost:6379``).
        When *None*, the ``PYFUSE_BACKEND`` environment variable is used.
    concurrency
        Number of concurrent worker threads (default: 1).
    auto_install
        Automatically install missing third-party dependencies via pip.
    import_to_package
        Extra import-name to pip-package-name mappings.
    """
    import sys
    from concurrent.futures import ThreadPoolExecutor

    from pyfuse.worker.worker import Worker

    resolved = _resolve_url(url)

    logger.info("pyfuse v%s", _VERSION)
    logger.info("  backend:      %s", resolved)
    logger.info("  concurrency:  %d", concurrency)
    logger.info("  auto_install: %s", "on" if auto_install else "off")

    try:
        backend = connect(resolved)
    except Exception as exc:
        logger.error("Could not connect to %s: %s", resolved, exc)
        sys.exit(1)

    worker = Worker(
        auto_install=auto_install,
        import_to_package=import_to_package,
    )

    logger.info("Listening for tasks. Press Ctrl+C to stop.")

    try:
        if concurrency > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                for task_json in backend.listen():
                    pool.submit(_handle_task, worker, backend, task_json)
        else:
            for task_json in backend.listen():
                _handle_task(worker, backend, task_json)
    except KeyboardInterrupt:
        logger.info("Worker stopped.")
    finally:
        disconnect()
