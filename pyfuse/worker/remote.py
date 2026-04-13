from __future__ import annotations

import asyncio
import atexit
import contextlib
import inspect
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pyfuse.core.progress import _progress_callback
from pyfuse.core.task import Task
from pyfuse.core.version import _VERSION
from pyfuse.worker.backends.base import Backend
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
        from pyfuse.worker.backends.redis import RedisBackend

        return RedisBackend(url, **kwargs)
    if scheme == "local":
        from pyfuse.worker.backends.local import LocalBackend

        return LocalBackend(url, **kwargs)
    if scheme in ("amqp", "amqps"):
        from pyfuse.worker.backends.rabbitmq import RabbitMQBackend

        return RabbitMQBackend(url, **kwargs)
    raise ValueError(
        f"Unknown backend scheme: {scheme!r}. "
        f"Supported: redis://, rediss://, local://, amqp://, amqps://"
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
    exec_task: asyncio.Task[Any] | None = None,
) -> None:
    """Send periodic heartbeats and check for cancellation.

    When *exec_task* is provided and the backend reports the task as
    cancelled, the execution task is cancelled via
    :meth:`asyncio.Task.cancel`, which raises :class:`CancelledError`
    at the next ``await`` in async user functions.
    """
    while not cancel_event.is_set():
        try:
            await backend.send_heartbeat(task_id)
        except Exception:
            pass  # best-effort
        if exec_task is not None:
            try:
                if await backend.is_cancelled(task_id):
                    exec_task.cancel()
                    return
            except Exception:
                pass
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=_HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            pass


_PROGRESS_MIN_INTERVAL = 0.05  # 50ms rate limit


def _make_progress_callback(
    backend: Backend,
    task_id: str,
    loop: asyncio.AbstractEventLoop,
) -> tuple[
    Callable[[float, float | None, str | None], None],
    Callable[[], Awaitable[None]],
]:
    """Create a rate-limited progress callback.

    Returns ``(callback, flush)``.  The callback stores the latest
    progress locally and only sends to the backend when at least 50 ms
    have elapsed since the last send.  Call ``await flush()`` after
    execution to guarantee the final state is delivered.
    """
    state: dict[str, Any] = {
        "latest": None,          # latest progress dict (always kept)
        "last_sent": 0.0,        # monotonic time of last send
        "task": None,            # most recent fire-and-forget Task
        "flushed": False,        # set by flush() to block late sends
    }

    def _do_send(data_json: str) -> None:
        if state["flushed"]:
            return
        state["task"] = asyncio.create_task(
            backend.send_progress(task_id, data_json),
        )

    def _on_progress(
        current: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        if state["flushed"]:
            return
        d: dict[str, Any] = {"current": current}
        if total is not None:
            d["total"] = total
        if message is not None:
            d["message"] = message
        state["latest"] = d

        now = time.monotonic()
        if now - state["last_sent"] >= _PROGRESS_MIN_INTERVAL:
            data_json = json.dumps(d, separators=(",", ":"))
            state["last_sent"] = now
            try:
                asyncio.get_running_loop()
                _do_send(data_json)
            except RuntimeError:
                loop.call_soon_threadsafe(_do_send, data_json)

    async def _flush() -> None:
        state["flushed"] = True
        # Cancel any in-flight fire-and-forget send to prevent stale overwrites
        t: asyncio.Task[None] | None = state.get("task")
        if t is not None and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        # Always send the authoritative final state
        if state["latest"] is not None:
            data_json = json.dumps(state["latest"], separators=(",", ":"))
            await backend.send_progress(task_id, data_json)

    return _on_progress, _flush


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
    elif envelope.status == "cancelled":
        logger.info(
            "\u2718  %-40s          %s  cancelled",
            task.function_name, short_id,
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

    # Check cancellation before execution
    if await backend.is_cancelled(task.task_id):
        envelope = ResultEnvelope.cancelled(task.task_id)
        _log_task_result(task, envelope, 0, worker)
        return

    # Set up rate-limited progress callback
    loop = asyncio.get_running_loop()
    progress_cb, flush = _make_progress_callback(backend, task.task_id, loop)
    token = _progress_callback.set(progress_cb)

    # Run execution as a task so the heartbeat loop can cancel it
    exec_task: asyncio.Task[Any] = asyncio.create_task(worker.run_with_policy(task))

    cancel_event = asyncio.Event()
    hb_task = asyncio.create_task(
        _heartbeat_loop(backend, task.task_id, cancel_event, exec_task),
    )

    t0 = time.monotonic()
    try:
        result = await exec_task
        envelope = ResultEnvelope.success(task.task_id, result)
    except asyncio.CancelledError:
        envelope = ResultEnvelope.cancelled(task.task_id)
    except Exception as exc:
        logger.debug("Task %s failed", task.task_id, exc_info=True)
        envelope = ResultEnvelope.failure(task.task_id, exc)
    finally:
        _progress_callback.reset(token)
        cancel_event.set()
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task

    elapsed_ms = (time.monotonic() - t0) * 1000

    # Flush any pending progress, then send the result unconditionally.
    # If the client cancelled mid-execution, it already stored a cancelled
    # result envelope (via Result.cancel) which the client reads first.
    await flush()
    await backend.send_result(task.task_id, envelope.to_json())
    await backend.notify_result(task.task_id)

    _log_task_result(task, envelope, elapsed_ms, worker)


async def _worker_loop(
    worker: Worker,
    backend: Backend,
    concurrency: int,
) -> None:
    """Consume tasks from *backend* and dispatch to *worker*.

    Supports graceful shutdown: on the first SIGINT/SIGTERM, stops
    accepting new tasks and waits for in-progress tasks to complete.
    On the second signal, cancels all in-progress tasks immediately.
    """
    shutdown = asyncio.Event()
    pending: set[asyncio.Task[None]] = set()
    sem = asyncio.Semaphore(concurrency)

    _got_first_signal = False

    def _on_shutdown_signal() -> None:
        nonlocal _got_first_signal
        if not _got_first_signal:
            _got_first_signal = True
            shutdown.set()
        else:
            logger.warning("Forced shutdown — cancelling %d task(s).", len(pending))
            for t in pending:
                t.cancel()

    # Install signal handlers (not available on Windows)
    loop = asyncio.get_running_loop()
    _signals_installed = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_shutdown_signal)
            _signals_installed = True
        except (NotImplementedError, RuntimeError):
            pass

    async def bounded_handle(task_json: str) -> None:
        async with sem:
            await _handle_task(worker, backend, task_json)

    async def _listen() -> None:
        async for task_json in backend.listen():
            if shutdown.is_set():
                return
            task = asyncio.create_task(bounded_handle(task_json))
            pending.add(task)
            task.add_done_callback(pending.discard)

    listen_task = asyncio.create_task(_listen())

    try:
        await shutdown.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        shutdown.set()

    # Stop accepting new tasks
    listen_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await listen_task

    # Wait for in-flight tasks to complete
    if pending:
        logger.info(
            "Graceful shutdown: waiting for %d task(s) to complete... "
            "(Ctrl+C to force quit)",
            len(pending),
        )
        try:
            await asyncio.gather(*pending, return_exceptions=True)
        except (KeyboardInterrupt, asyncio.CancelledError):
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # Clean up signal handlers
    if _signals_installed:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)


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
    finally:
        await disconnect()
        logger.info("Worker stopped.")
