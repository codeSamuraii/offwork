"""Remote execution orchestration: connect, serve, and submit tasks."""

import os
import sys
import json
import time
import uuid
import atexit
import signal
import asyncio
import inspect
import logging
import contextlib
from typing import TYPE_CHECKING, Any
from collections.abc import Callable, Awaitable

from offwork.core.task import Task
from offwork.core.token import resolve_root_token
from offwork.core.version import _VERSION
from offwork.core.clients import KnownClients
from offwork.core.signing import NonceLRU
from offwork.core.envelope import (
    DEFAULT_CLOCK_SKEW,
    build_signed_envelope,
    verify_task_envelope,
)
from offwork.core.identity import get_client_id, get_identity_seed, get_public_key
from offwork.core.progress import _progress_callback
from offwork.core.log_capture import TaskLogHandler, _log_callback
from offwork.worker.result import Result, ResultEnvelope
from offwork.worker.worker import Worker
from offwork.worker.schedule import ScheduleHandle
from offwork.worker.backends.base import Backend

if TYPE_CHECKING:
    from offwork.worker.sandbox import DockerSandbox

logger = logging.getLogger(__name__)

_active_backend: Backend | None = None
_atexit_registered = False

_ENV_VAR = "OFFWORK_BACKEND"


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
        from offwork.worker.backends.redis import RedisBackend

        return RedisBackend(url, **kwargs)
    if scheme == "local":
        from offwork.worker.backends.local import LocalBackend

        return LocalBackend(url, **kwargs)
    if scheme in ("amqp", "amqps"):
        from offwork.worker.backends.rabbitmq import RabbitMQBackend

        return RabbitMQBackend(url, **kwargs)
    if scheme in ("ws", "wss"):
        from offwork.worker.backends.ws import WebSocketBackend

        role = kwargs.pop("role", "client")
        return WebSocketBackend(url, role=role, **kwargs)
    raise ValueError(
        f"Unknown backend scheme: {scheme!r}. "
        f"Supported: redis://, rediss://, local://, amqp://, amqps://, ws://, wss://"
    )


class _ConnectionContext:
    """Return type of :func:`connect`.

    Supports both simple one-liner usage and explicit async context-manager
    usage for deterministic cleanup:

    .. code-block:: python

        # Simple: connection lives for the script's lifetime (atexit closes it)
        offwork.connect("redis://localhost:6379")

        # Explicit: closed deterministically on exit (even on exception)
        async with offwork.connect("redis://localhost:6379") as backend:
            result = await my_task.run(42)

    The object also proxies attribute access to the underlying
    :class:`~offwork.Backend` so the return value can be used directly::

        backend = offwork.connect("redis://localhost:6379")
        await backend.submit(task_json)   # proxied to the real backend
    """

    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    @property
    def backend(self) -> Backend:
        """The underlying :class:`~offwork.Backend` instance."""
        return self._backend

    def __getattr__(self, name: str) -> Any:
        # Proxy attribute access so callers can treat this as a Backend.
        return getattr(self._backend, name)

    async def __aenter__(self) -> Backend:
        return self._backend

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await disconnect()


def connect(url: str | None = None, **kwargs: Any) -> _ConnectionContext:
    """Configure the global transport backend and return a connection handle.

    The returned :class:`_ConnectionContext` can be used in three ways:

    .. code-block:: python

        # 1. Simple: atexit handles cleanup automatically
        offwork.connect("redis://localhost:6379")

        # 2. Proxy access to the backend
        ctx = offwork.connect("redis://localhost:6379")
        await ctx.submit(task_json)

        # 3. Explicit cleanup via async context manager
        async with offwork.connect("redis://localhost:6379") as backend:
            result = await my_task.run(42)

    Parameters
    ----------
    url
        Backend URL.  Supported schemes:

        - ``redis://`` / ``rediss://`` — :class:`RedisBackend`
        - ``local://`` — :class:`LocalBackend` (same-machine IPC)
        - ``amqp://`` / ``amqps://`` — :class:`RabbitMQBackend`
        - ``ws://`` / ``wss://`` — :class:`WebSocketBackend`

        When ``None``, the ``OFFWORK_BACKEND`` environment variable is used.

    **kwargs
        Passed to the backend constructor.

    Returns
    -------
    _ConnectionContext
        A handle that wraps the backend and optionally acts as an
        ``async with`` context manager.
    """
    global _active_backend, _atexit_registered
    resolved = _resolve_url(url)
    _active_backend = _create_backend(resolved, **kwargs)
    if not _atexit_registered:
        atexit.register(_sync_disconnect)
        _atexit_registered = True
    logger.debug("Connected to backend: %s", resolved)
    return _ConnectionContext(_active_backend)


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
    ``OFFWORK_BACKEND`` environment variable is checked and used
    to auto-connect.
    """
    global _active_backend
    if _active_backend is None:
        env_url = os.environ.get(_ENV_VAR)
        if env_url:
            connect(env_url)
        else:
            raise RuntimeError(
                "No backend connected. Call offwork.connect('redis://...') or offwork.connect('wss://...') "
                f"or set the {_ENV_VAR} environment variable."
            )
    return _active_backend  # type: ignore[return-value]


def _encode_task(task: Task, root_token: bytes | None) -> str:
    """Return the wire JSON for *task*, signed if *root_token* is set."""
    if root_token is None:
        return task.to_json()
    return build_signed_envelope(
        task,
        root_token=root_token,
        client_id=get_client_id(),
        identity_seed=get_identity_seed(),
        public_key=get_public_key(),
    )


def _resolve_backend(_backend: str | Backend | None) -> Backend:
    """Resolve a backend selector: explicit instance > URL > global default."""
    if isinstance(_backend, Backend):
        return _backend
    if isinstance(_backend, str):
        return _create_backend(_backend)
    return get_backend()


def _prepare_submission(
    func: Callable[..., object],
    wrapper: Callable[..., object],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    _backend: str | Backend | None,
    _root_token: bytes | None,
    **task_overrides: Any,
) -> tuple[Backend, Task, bytes | None]:
    """Shared submission prep: resolve backend, serialize graph, build :class:`Task`."""
    from offwork.graph.graph import Graph  # circular

    backend = _resolve_backend(_backend)
    if _root_token is None:
        _root_token = resolve_root_token("client")

    unwrapped = inspect.unwrap(func)
    function_name = f"{unwrapped.__module__}.{unwrapped.__qualname__}"
    logger.debug("Serializing graph for %s", function_name)
    graph_json = Graph.default().serialize(wrapper)

    opts = getattr(wrapper, "__offwork_options__", {})
    task = Task(
        graph_json=graph_json,
        function_name=function_name,
        args=args,
        kwargs=kwargs,
        timeout=opts.get("timeout"),
        retries=opts.get("retries", 0),
        retry_delay=opts.get("retry_delay", 1.0),
        throttle=opts.get("throttle"),
        **task_overrides,
    )
    return backend, task, _root_token


async def submit_remote(
    func: Callable[..., object],
    wrapper: Callable[..., object],
    *args: Any,
    _backend: str | Backend | None = None,
    _root_token: bytes | None = None,
    **kwargs: Any,
) -> Result:
    """Pack and submit a function to the remote backend.

    Called internally by ``traced_func.run(...)``.

    Parameters
    ----------
    _root_token
        When provided (or auto-loaded), the envelope is signed with a
        per-client HMAC plus an Ed25519 signature bound to this
        machine's stable identity.
    """
    backend, task, root_token = _prepare_submission(
        func, wrapper, args, kwargs, _backend, _root_token,
    )
    logger.debug("Submitting task %s → %s", task.task_id[:8], task.function_name)
    await backend.submit(_encode_task(task, root_token))
    logger.info("Submitted task %s for %s", task.task_id, task.function_name)
    return Result(task.task_id, backend)


async def submit_remote_scheduled(
    func: Callable[..., object],
    wrapper: Callable[..., object],
    *args: Any,
    _backend: str | Backend | None = None,
    _root_token: bytes | None = None,
    _scheduled_at: float | None = None,
    **kwargs: Any,
) -> Result:
    """Submit a task scheduled for future execution."""
    backend, task, root_token = _prepare_submission(
        func, wrapper, args, kwargs, _backend, _root_token,
        scheduled_at=_scheduled_at,
    )
    logger.debug(
        "Submitting scheduled task %s → %s (at %.3f)",
        task.task_id[:8], task.function_name, _scheduled_at or 0,
    )
    await backend.submit(_encode_task(task, root_token))
    logger.info(
        "Submitted scheduled task %s for %s (at %.0f)",
        task.task_id, task.function_name, _scheduled_at or 0,
    )
    return Result(task.task_id, backend)


async def submit_recurring(
    func: Callable[..., object],
    wrapper: Callable[..., object],
    *args: Any,
    _backend: str | Backend | None = None,
    _root_token: bytes | None = None,
    _interval: float = 0,
    _start_at: float | None = None,
    _run_for: float | None = None,
    _max_runs: int | None = None,
    **kwargs: Any,
) -> ScheduleHandle:
    """Submit a recurring task and return a :class:`ScheduleHandle`."""
    schedule_id = uuid.uuid4().hex[:12]
    scheduled_at = _start_at or time.time()
    recur_deadline = scheduled_at + _run_for if _run_for is not None else None

    backend, task, root_token = _prepare_submission(
        func, wrapper, args, kwargs, _backend, _root_token,
        scheduled_at=scheduled_at,
        recur_interval=_interval,
        recur_deadline=recur_deadline,
        recur_remaining=_max_runs,
        schedule_id=schedule_id,
    )

    logger.debug(
        "Submitting recurring task %s → %s (every %.1fs, schedule=%s)",
        task.task_id[:8], task.function_name, _interval, schedule_id,
    )
    await backend.submit(_encode_task(task, root_token))
    logger.info(
        "Submitted recurring task %s for %s (every %.1fs, schedule=%s)",
        task.task_id, task.function_name, _interval, schedule_id,
    )
    return ScheduleHandle(schedule_id, backend)


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


_HEARTBEAT_INTERVAL = 5.0


async def _heartbeat_loop(
    backend: Backend,
    task_id: str,
    cancel_event: asyncio.Event,
    exec_task: asyncio.Task[Any] | None = None,
) -> None:
    """Send periodic heartbeats and check for cancellation.

    Backends override ``heartbeat_and_check_cancel`` to combine both
    into a single round-trip (e.g. an HTTP POST whose response carries
    the cancel flag). When *exec_task* is provided and the backend
    reports the task as cancelled, the execution task is cancelled via
    :meth:`asyncio.Task.cancel`, which raises :class:`CancelledError`
    at the next ``await`` in async user functions.
    """
    while not cancel_event.is_set():
        try:
            if exec_task is not None:
                if await backend.heartbeat_and_check_cancel(task_id):
                    exec_task.cancel()
                    return
            else:
                await backend.send_heartbeat(task_id)
        except Exception:
            logger.debug("Heartbeat send failed for task %s", task_id, exc_info=True)
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

    async def _send(data_json: str) -> None:
        try:
            await backend.send_progress(task_id, data_json)
        except Exception:
            logger.debug("Progress send failed for task %s", task_id, exc_info=True)

    def _do_send(data_json: str) -> None:
        if state["flushed"]:
            return
        state["task"] = asyncio.create_task(_send(data_json))

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
        # Do not cancel an in-flight backend RPC on the shared channel.
        # Wait for it, then send the authoritative final state.
        t: asyncio.Task[None] | None = state.get("task")
        if t is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        # Always send the authoritative final state
        if state["latest"] is not None:
            data_json = json.dumps(state["latest"], separators=(",", ":"))
            await _send(data_json)

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


async def _send_envelope(backend: Backend, envelope: ResultEnvelope) -> None:
    """Send a result envelope and notify result-waiters."""
    await backend.send_result(envelope.task_id, envelope.to_json())
    await backend.notify_result(envelope.task_id)


def _extract_task_id(task_json: str) -> str | None:
    """Best-effort task_id extraction from a malformed/rejected task payload."""
    try:
        data = json.loads(task_json)
    except Exception:
        return None
    task_id = data.get("id")
    return task_id if isinstance(task_id, str) and task_id else None


async def _handle_task(
    worker: Worker,
    backend: Backend,
    task_json: str,
    root_token: bytes | None = None,
    known_clients: KnownClients | None = None,
    nonce_lru: NonceLRU | None = None,
    clock_skew: float = DEFAULT_CLOCK_SKEW,
) -> None:
    """Process a single task: deserialize, execute with policy, send result.

    Parameters
    ----------
    root_token
        When provided, every task must carry a valid signed envelope
        (per-client HMAC + Ed25519).  Unsigned or invalid envelopes are
        rejected with an error result.
    """
    try:
        if root_token is not None:
            assert known_clients is not None and nonce_lru is not None
            task = verify_task_envelope(
                task_json,
                root_token=root_token,
                known_clients=known_clients,
                nonce_lru=nonce_lru,
                clock_skew=clock_skew,
            )
        else:
            task = Task.from_json(task_json)
    except Exception as exc:
        # If we can extract a task_id, send an error envelope so the
        # client gets feedback instead of hanging forever.
        logger.warning("Task rejected: %s", exc)
        task_id = _extract_task_id(task_json)
        if task_id is not None:
            await _send_envelope(backend, ResultEnvelope.failure(task_id, exc))
        return

    logger.debug("Received task %s: %s", task.task_id, task.function_name)

    # Wait for scheduled time
    if task.scheduled_at is not None:
        delay = task.scheduled_at - time.time()
        if delay > 0:
            logger.debug("Task %s scheduled in %.1fs", task.task_id, delay)
            await asyncio.sleep(delay)

    # Any failure in the backend checks below must still surface to the
    # client, otherwise it would hang forever polling for a result.
    try:
        cancelled = await backend.is_cancelled(task.task_id)
    except Exception as exc:
        logger.exception("is_cancelled failed for task %s", task.task_id)
        await _send_envelope(backend, ResultEnvelope.failure(task.task_id, exc))
        return

    if cancelled:
        envelope = ResultEnvelope.cancelled(task.task_id)
        _log_task_result(task, envelope, 0, worker)
        return

    # Check throttle
    if task.throttle is not None:
        try:
            allowed = await backend.check_throttle(task.function_name)
        except Exception as exc:
            logger.exception(
                "check_throttle failed for task %s (%s)",
                task.task_id, task.function_name,
            )
            await _send_envelope(backend, ResultEnvelope.failure(task.task_id, exc))
            return

        if not allowed:
            await _send_envelope(backend, ResultEnvelope.throttled(task.task_id))
            logger.info(
                "%-40s          %s  throttled",
                task.function_name, task.task_id[:8],
            )
            return

    # Set up rate-limited progress callback
    loop = asyncio.get_running_loop()
    progress_cb, flush = _make_progress_callback(backend, task.task_id, loop)
    token = _progress_callback.set(progress_cb)

    # Set up per-task log capture: route logging records to send_log_line.
    # Mirror the progress callback pattern: schedule on the event loop
    # from either the loop thread (async tasks) or an executor thread
    # (sync tasks wrapped in run_in_executor).
    _log_send_tasks: set[asyncio.Task[None]] = set()

    async def _do_send_log(line: str) -> None:
        try:
            await backend.send_log_line(task.task_id, line)
        except Exception:
            logger.warning("Log line send failed for task %s", task.task_id, exc_info=True)

    def _fire_log_send(line: str) -> None:
        t = asyncio.create_task(_do_send_log(line))
        _log_send_tasks.add(t)
        t.add_done_callback(_log_send_tasks.discard)

    def _log_cb(line: str) -> None:
        try:
            asyncio.get_running_loop()
            _fire_log_send(line)
        except RuntimeError:
            loop.call_soon_threadsafe(_fire_log_send, line)

    log_token = _log_callback.set(_log_cb)
    log_handler = TaskLogHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)

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
        root_logger.removeHandler(log_handler)
        _log_callback.reset(log_token)
        # Drain in-flight log sends so the server-side buffer is
        # complete before put_result flushes it to MongoDB.
        if _log_send_tasks:
            await asyncio.gather(*_log_send_tasks, return_exceptions=True)
            _log_send_tasks.clear()
        _progress_callback.reset(token)
        cancel_event.set()
        # Do not hb_task.cancel() — cancelling can interrupt an
        # in-flight AMQP RPC (e.g. Queue.Declare) on the shared
        # channel, causing it to close and preventing send_result
        # from delivering the result.  The cancel_event already
        # signals the loop to exit promptly.
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task

    elapsed_ms = (time.monotonic() - t0) * 1000

    # Flush any pending progress, then send the result unconditionally.
    # If the client cancelled mid-execution, it already stored a cancelled
    # result envelope (via Result.cancel) which the client reads first.
    await flush()
    try:
        result_json = envelope.to_json()
    except Exception as exc:
        # Result serialization failed (e.g. unsupported return type).
        # Surface the failure as an error envelope rather than letting
        # the task hang forever from the client's point of view.
        logger.exception(
            "Failed to serialize result for task %s", task.task_id,
        )
        envelope = ResultEnvelope.failure(task.task_id, exc)
        result_json = envelope.to_json()
    await backend.send_result(task.task_id, result_json)
    await backend.notify_result(task.task_id)

    _log_task_result(task, envelope, elapsed_ms, worker)

    # Record throttle cooldown after successful execution
    if task.throttle is not None and envelope.status == "ok":
        await backend.record_throttle(task.function_name, task.throttle)

    # Re-enqueue recurring task — worker re-signs with its own identity.
    if task.recur_interval is not None and task.schedule_id is not None:
        next_at = time.time() + task.recur_interval
        remaining = task.recur_remaining
        deadline_exceeded = task.recur_deadline is not None and next_at > task.recur_deadline
        runs_exhausted = remaining is not None and remaining <= 1
        if deadline_exceeded or runs_exhausted:
            await backend.cancel_schedule(task.schedule_id)
            logger.info(
                "Recurring schedule %s exhausted (%s)",
                task.schedule_id,
                "deadline" if deadline_exceeded else "max_runs",
            )
        elif not await backend.is_schedule_cancelled(task.schedule_id):
            next_task = Task(
                graph_json=task.graph_json,
                function_name=task.function_name,
                args=task.args,
                kwargs=task.kwargs,
                timeout=task.timeout,
                retries=task.retries,
                retry_delay=task.retry_delay,
                throttle=task.throttle,
                scheduled_at=next_at,
                recur_interval=task.recur_interval,
                recur_deadline=task.recur_deadline,
                recur_remaining=remaining - 1 if remaining is not None else None,
                schedule_id=task.schedule_id,
            )
            await backend.submit(_encode_task(next_task, root_token))
            logger.debug(
                "Re-enqueued recurring task %s (schedule=%s, next in %.1fs)",
                next_task.task_id, task.schedule_id, task.recur_interval,
            )


async def _worker_loop(
    worker: Worker,
    backend: Backend,
    concurrency: int,
    root_token: bytes | None = None,
    known_clients: KnownClients | None = None,
    nonce_lru: NonceLRU | None = None,
    clock_skew: float = DEFAULT_CLOCK_SKEW,
) -> None:
    """Consume tasks from *backend* and dispatch to *worker*.

    Supports graceful shutdown: on the first SIGINT/SIGTERM, stops
    accepting new tasks and waits for in-progress tasks to complete.
    On the second signal, cancels all in-progress tasks immediately.

    Parameters
    ----------
    root_token
        When provided, every incoming task must carry a valid signed
        envelope (per-client HMAC + Ed25519).
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
            await _handle_task(
                worker, backend, task_json,
                root_token=root_token,
                known_clients=known_clients,
                nonce_lru=nonce_lru,
                clock_skew=clock_skew,
            )

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
    sandbox: "DockerSandbox | bool | None" = None,
    require_signing: bool = False,
    clock_skew: float = DEFAULT_CLOCK_SKEW,
) -> None:
    """Start a worker loop that pops tasks from the backend and executes them.

    Parameters
    ----------
    url
        Backend URL (e.g. ``redis://localhost:6379``).
        When *None*, the ``OFFWORK_BACKEND`` environment variable is used.
    concurrency
        Number of concurrent tasks (default: 1).
    auto_install
        Automatically install missing third-party dependencies via pip.
    import_to_package
        Extra import-name to pip-package-name mappings.
    sandbox
        ``True`` or a :class:`~offwork.worker.sandbox.DockerSandbox`
        instance to execute tasks inside a Docker container.
    require_signing
        When ``True``, every incoming task must carry a valid signed
        envelope (per-client HMAC + Ed25519).  Key material is loaded
        from the environment or ``~/.offwork``.
    clock_skew
        Maximum allowed ``|now - iat|`` for signed envelopes, in
        seconds.  Defaults to 300s.
    """
    resolved = _resolve_url(url)
    auto_tag = "on" if auto_install else "off"
    sandbox_tag = "docker" if sandbox else "off"
    signing_tag = "on" if require_signing else "off"
    logger.info(
        "offwork worker v%s  \u2502  %s  \u2502  concurrency=%d  \u2502  "
        "auto_install=%s  \u2502  sandbox=%s  \u2502  signing=%s",
        _VERSION, resolved, concurrency, auto_tag, sandbox_tag, signing_tag,
    )

    root_token: bytes | None = None
    known_clients: KnownClients | None = None
    nonce_lru: NonceLRU | None = None
    if require_signing:
        root_token = resolve_root_token("worker")
        if root_token is None:
            logger.error(
                "Signing is enabled but no key material found. "
                "Set OFFWORK_SIGNING_TOKEN, run 'offwork token generate', "
                "or run 'offwork pair' to pair with a client."
            )
            sys.exit(1)
        known_clients = KnownClients()
        nonce_lru = NonceLRU()
        logger.info(
            "Task signing enabled — only valid envelopes will be executed "
            "(known_clients=%s)",
            known_clients.path,
        )

    scheme = resolved.split("://", 1)[0].lower()
    connect_kwargs: dict[str, Any] = {"role": "worker"} if scheme in {"ws", "wss"} else {}
    try:
        backend = connect(resolved, **connect_kwargs).backend
    except Exception as exc:
        logger.error("Could not connect to %s: %s", resolved, exc)
        sys.exit(1)

    worker = Worker(
        auto_install=auto_install,
        import_to_package=import_to_package,
        sandbox=sandbox,
    )

    # Boot the sandbox container before accepting tasks so the first
    # execution doesn't pay the startup cost.
    if worker.sandboxed:
        assert worker._sandbox is not None
        await worker._sandbox.start()

    logger.info("Listening for tasks \u2014 Ctrl+C to stop.")

    try:
        await _worker_loop(
            worker, backend, concurrency,
            root_token=root_token,
            known_clients=known_clients,
            nonce_lru=nonce_lru,
            clock_skew=clock_skew,
        )
    finally:
        if worker.sandboxed:
            assert worker._sandbox is not None
            await worker._sandbox.stop()
        await disconnect()
        logger.info("Worker stopped.")
