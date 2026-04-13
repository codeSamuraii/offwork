from __future__ import annotations

import asyncio
import atexit
import contextlib
import inspect
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyfuse.core.task import Task
from pyfuse.core.version import _VERSION
from pyfuse.worker.backends.base import Backend
from pyfuse.worker.backends.redis import RedisBackend
from pyfuse.worker.result import Result, ResultEnvelope

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
    if scheme == "local":
        from pyfuse.worker.backends.local import LocalBackend

        return LocalBackend(url, **kwargs)
    raise ValueError(
        f"Unknown backend scheme: {scheme!r}. "
        f"Supported: redis://, rediss://, local://"
    )


def connect(url: str | None = None, **kwargs: Any) -> Backend:
    """Configure the global transport backend.

    Parameters
    ----------
    url
        Backend URL.  Supported schemes:

        - ``redis://`` / ``rediss://`` -- :class:`RedisBackend`
        - ``local://`` -- :class:`LocalBackend` (same-machine IPC)

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
        atexit.register(_sync_disconnect)
        _atexit_registered = True
    logger.debug("Connected to backend: %s", resolved)
    return _active_backend


async def disconnect() -> None:
    """Close and clear the global backend."""
    global _active_backend
    if _active_backend is not None:
        await _active_backend.close()
        _active_backend = None
        logger.info("Disconnected from backend")


def _sync_disconnect() -> None:
    """Synchronous atexit handler for disconnect."""
    global _active_backend
    if _active_backend is not None:
        try:
            asyncio.run(_active_backend.close())
        except RuntimeError:
            pass  # event loop already closed
        _active_backend = None


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


async def submit_remote(
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

    await backend.submit(task.to_json())
    logger.info("Submitted task %s for %s", task.task_id, function_name)
    return Result(task.task_id, backend)


def _build_detail_tags(worker: Worker) -> str:
    """Build a comma-separated detail string from the last build info."""
    build_info = worker.last_build_info()
    if build_info is not None and build_info.cache_hit:
        parts = ["cached"]
    else:
        parts = ["build"]
    if build_info is not None and build_info.installed_packages:
        parts.append("pip " + " ".join(build_info.installed_packages))
    return ", ".join(parts)


_HEARTBEAT_INTERVAL = 1.0


async def _heartbeat_loop(
    backend: Backend,
    task_id: str,
    cancel_event: asyncio.Event,
) -> None:
    """Send periodic heartbeats until *cancel_event* is set."""
    while not cancel_event.is_set():
        try:
            await backend.send_heartbeat(task_id)
        except Exception:
            pass  # best-effort
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=_HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            pass


def _log_task_result(
    task: Task,
    envelope: ResultEnvelope,
    elapsed_ms: float,
    worker: Worker,
) -> None:
    """Log the outcome of a completed task."""
    short_id = task.task_id[:8]
    details = _build_detail_tags(worker)
    if envelope.status == "ok":
        logger.info(
            "\u2713  %-40s %6.0fms  %s  %s",
            task.function_name, elapsed_ms, short_id, details,
        )
    else:
        error_msg = f"  {envelope.error_type}: {envelope.error_message}"
        logger.warning(
            "\u2717  %-40s %6.0fms  %s  %s%s",
            task.function_name, elapsed_ms, short_id, details, error_msg,
        )


async def _handle_task(
    worker: Worker,
    backend: Backend,
    task_json: str,
) -> None:
    """Process a single task: deserialize, execute with policy, send result."""
    task = Task.from_json(task_json)
    logger.debug("Received task %s: %s", task.task_id, task.function_name)

    cancel_event = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat_loop(backend, task.task_id, cancel_event))

    t0 = time.monotonic()
    try:
        result = await worker.run_with_policy(task)
        envelope = ResultEnvelope.success(task.task_id, result)
    except Exception as exc:
        logger.debug("Task %s failed", task.task_id, exc_info=True)
        envelope = ResultEnvelope.failure(task.task_id, exc)
    finally:
        cancel_event.set()
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task

    elapsed_ms = (time.monotonic() - t0) * 1000
    await backend.send_result(task.task_id, envelope.to_json())
    await backend.notify_result(task.task_id)
    _log_task_result(task, envelope, elapsed_ms, worker)


async def _worker_loop(
    worker: Worker,
    backend: Backend,
    concurrency: int,
) -> None:
    """Consume tasks from *backend* and dispatch to *worker*."""
    sem = asyncio.Semaphore(concurrency)

    async def bounded_handle(task_json: str) -> None:
        async with sem:
            await _handle_task(worker, backend, task_json)

    async with asyncio.TaskGroup() as tg:
        async for task_json in backend.listen():
            tg.create_task(bounded_handle(task_json))


async def serve(
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
        Number of concurrent tasks (default: 1).
    auto_install
        Automatically install missing third-party dependencies via pip.
    import_to_package
        Extra import-name to pip-package-name mappings.
    """
    from pyfuse.worker.worker import Worker

    resolved = _resolve_url(url)
    auto_tag = "on" if auto_install else "off"
    logger.info(
        "pyfuse worker v%s  \u2502  %s  \u2502  concurrency=%d  \u2502  auto_install=%s",
        _VERSION, resolved, concurrency, auto_tag,
    )

    try:
        backend = connect(resolved)
    except Exception as exc:
        logger.error("Could not connect to %s: %s", resolved, exc)
        sys.exit(1)

    worker = Worker(auto_install=auto_install, import_to_package=import_to_package)
    logger.info("Listening for tasks \u2014 Ctrl+C to stop.")

    try:
        await _worker_loop(worker, backend, concurrency)
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        logger.info("Worker stopped.")
    finally:
        await disconnect()
