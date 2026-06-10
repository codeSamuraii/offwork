"""Result future and result envelope for remote task execution."""

import json
import time
import asyncio
import logging
import traceback as tb_mod
from typing import Any, Self
from dataclasses import dataclass
from collections.abc import AsyncGenerator, AsyncIterator, Generator

from offwork.core.task import _TaskEncoder, _resolve
from offwork.core.errors import RemoteError, TaskStalled, TaskCancelled, ThrottleError
from offwork.core.progress import ProgressInfo
from offwork.core._timeout import TimeoutIn, resolve_timeout
from offwork.worker.backends.base import Backend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultEnvelope:
    """Serializable envelope for a task result (success or error).

    Use the :meth:`success` and :meth:`failure` class methods to create
    instances.
    """

    task_id: str
    status: str  # "ok", "error", or "cancelled"
    result: Any = None
    error_type: str | None = None
    error_message: str | None = None
    error_traceback: str | None = None
    stream_yields: int | None = None

    @classmethod
    def success(cls, task_id: str, result: Any) -> Self:
        """Create an envelope for a successful result."""
        return cls(task_id=task_id, status="ok", result=result)

    @classmethod
    def stream_complete(cls, task_id: str, yield_count: int) -> Self:
        """Create the terminal envelope for a completed streaming task.

        A streaming (async-generator) task has no return value; the
        envelope's ``stream_yields`` records how many values were
        yielded so the client knows when it has drained the channel.
        """
        return cls(task_id=task_id, status="ok", stream_yields=yield_count)

    @classmethod
    def cancelled(cls, task_id: str) -> Self:
        """Create an envelope for a cancelled task."""
        return cls(task_id=task_id, status="cancelled")

    @classmethod
    def throttled(cls, task_id: str) -> Self:
        """Create an envelope for a throttled (rate-limited) task."""
        return cls(task_id=task_id, status="throttled")

    @classmethod
    def failure(cls, task_id: str, exc: BaseException) -> Self:
        """Create an envelope from an exception, capturing its traceback."""
        return cls(
            task_id=task_id,
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
            error_traceback="".join(tb_mod.format_exception(exc)),
        )

    def to_json(self) -> str:
        """Serialize to JSON string.

        Result payloads are encoded with the same sentinel-based encoder
        used for task arguments, so ``bytes``, ``datetime``, ``Decimal``,
        ``Path`` etc. round-trip transparently.
        """
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "status": self.status,
        }
        if self.status == "ok":
            if self.stream_yields is not None:
                d["stream_yields"] = self.stream_yields
            else:
                d["result"] = self.result
        elif self.status == "error":
            d["error_type"] = self.error_type
            d["error_message"] = self.error_message
            d["error_traceback"] = self.error_traceback
        return json.dumps(d, cls=_TaskEncoder)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Self:
        """Deserialize from a JSON string or bytes."""
        data = json.loads(raw)
        is_ok = data.get("status") == "ok"
        result = _resolve(data.get("result"), {}) if is_ok else None
        return cls(
            task_id=data["task_id"],
            status=data["status"],
            result=result,
            error_type=data.get("error_type"),
            error_message=data.get("error_message"),
            error_traceback=data.get("error_traceback"),
            stream_yields=data.get("stream_yields"),
        )


# ---------------------------------------------------------------------------
# _CancelAwaitable
# ---------------------------------------------------------------------------


class _CancelAwaitable:
    """Return value of :meth:`Result.cancel`.

    Supports both fire-and-forget and ``await``:

    .. code-block:: python

        fut.cancel()          # schedules cancellation in background
        await fut.cancel()    # waits for confirmation (up to *timeout*)
        await fut.cancel(timeout=False)  # waits indefinitely
    """

    def __init__(self, result: "Result", resolved: float | None) -> None:
        self._result = result
        self._resolved = resolved
        self._bg_task: asyncio.Task[bool] | None = None
        # If a running event loop exists, schedule the cancel immediately so
        # bare ``fut.cancel()`` (without await) still fires the request.
        try:
            loop = asyncio.get_running_loop()
            self._bg_task = loop.create_task(result._do_cancel(resolved))
        except RuntimeError:
            pass  # no running loop — will execute when awaited

    def __await__(self) -> Generator[Any, None, bool]:
        if self._bg_task is not None:
            return self._bg_task.__await__()
        return self._result._do_cancel(self._resolved).__await__()


# ---------------------------------------------------------------------------
# _ProgressAwaitable
# ---------------------------------------------------------------------------


class _ProgressAwaitable:
    """Return value of :meth:`Result.progress`.

    Supports both a single snapshot and async streaming:

    .. code-block:: python

        p = await fut.progress()          # latest snapshot or None
        async for p in fut.progress():    # each distinct update until done
            print(p.percent)
    """

    def __init__(self, result: "Result") -> None:
        self._result = result

    def __await__(self) -> "Generator[Any, None, ProgressInfo | None]":
        return self._fetch_once().__await__()

    async def _fetch_once(self) -> ProgressInfo | None:
        raw = await self._result._backend.get_progress(self._result._task_id)
        return ProgressInfo.from_json(raw) if raw else None

    def __aiter__(self) -> AsyncIterator[ProgressInfo]:
        return self._stream()

    async def _stream(self) -> AsyncGenerator[ProgressInfo, None]:
        """Yield each distinct progress update until the task finishes."""
        backend = self._result._backend
        task_id = self._result._task_id
        last_raw: str | None = None
        while True:
            # Fan out the two reads in parallel — they're independent
            # and would otherwise serialize one RTT per poll.
            check_raw, raw = await asyncio.gather(
                backend.try_get_result(task_id),
                backend.get_progress(task_id),
            )
            if check_raw is not None:
                self._result._envelope = ResultEnvelope.from_json(check_raw)

            if raw is not None and raw != last_raw:
                last_raw = raw
                yield ProgressInfo.from_json(raw)

            if self._result.done():
                return
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class Result:
    """Awaitable future-like handle for a remotely submitted task.

    Returned by :meth:`~offwork.typing.TracedFunction.submit`.

    State queries (synchronous, no I/O)
    ------------------------------------
    :meth:`done`
        ``True`` once the result envelope has been received.
    :meth:`cancelled`
        ``True`` if the task was cancelled.
    :meth:`exception`
        The exception the task raised, or ``None`` on success.
        Raises :class:`asyncio.InvalidStateError` while still pending.

    Waiting for completion
    ----------------------
    ``await result``
        Wait forever and return the task's return value (raises on error).
    :meth:`result`
        Same, but accepts explicit *timeout* and *stall_timeout* controls.
    :meth:`wait`
        Wait until done; returns ``self`` for chaining.
    :meth:`check`
        Non-blocking poll that updates internal state; returns ``self``.

    Cancellation
    ------------
    :meth:`cancel`
        Returns a :class:`_CancelAwaitable`: call without ``await`` to
        fire-and-forget, or ``await`` to wait for worker confirmation.

    Progress
    --------
    :meth:`progress`
        Returns a :class:`_ProgressAwaitable`: ``await`` for a snapshot,
        or ``async for`` to stream each update.
    """

    def __init__(self, task_id: str, backend: Backend) -> None:
        self._task_id = task_id
        self._backend = backend
        self._envelope: ResultEnvelope | None = None

    @property
    def task_id(self) -> str:
        """The unique identifier assigned to this task."""
        return self._task_id

    # -- internal helpers ------------------------------------------------------

    def _unwrap(self) -> Any:
        """Unwrap the cached envelope, raising on error / cancellation."""
        assert self._envelope is not None
        if self._envelope.status == "cancelled":
            raise TaskCancelled(f"Task {self._task_id} was cancelled") from None
        if self._envelope.status == "throttled":
            raise ThrottleError(
                f"Task {self._task_id} was throttled (rate-limited)"
            ) from None
        if self._envelope.status == "error":
            raise RemoteError(
                f"{self._envelope.error_type}: {self._envelope.error_message}",
                self._envelope.error_traceback,
            ) from None
        return self._envelope.result

    async def _wait_with_stall_detection(
        self,
        timeout: float | None,
        stall_timeout: float,
    ) -> None:
        """Wait for the result envelope using heartbeat-based stall detection.

        Polls the backend in bounded slices derived from *stall_timeout* so
        the stall is detected in time even with long-poll backends.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        slice_seconds = max(1.0, min(stall_timeout / 2, 30.0))
        last_hb_value: float | None = None
        last_hb_change: float | None = None

        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for result of task {self._task_id}"
                )
            wait_for = (
                slice_seconds if remaining is None else min(slice_seconds, remaining)
            )
            try:
                raw = await self._backend.get_result(self._task_id, timeout=wait_for)
            except TimeoutError:
                raw = None
            if raw is not None:
                self._envelope = ResultEnvelope.from_json(raw)
                logger.debug(
                    "Received result for task %s: status=%s",
                    self._task_id[:8],
                    self._envelope.status,
                )
                return

            hb = await self._backend.get_heartbeat(self._task_id)
            now = time.monotonic()
            if hb is not None and hb != last_hb_value:
                last_hb_value = hb
                last_hb_change = now
            if last_hb_change is not None and (now - last_hb_change) > stall_timeout:
                elapsed = now - last_hb_change
                raise TaskStalled(
                    f"Task {self._task_id} stalled: no heartbeat for "
                    f"{elapsed:.1f}s (threshold: {stall_timeout}s)"
                )

    async def _do_cancel(self, resolved: float | None) -> bool:
        """Send the cancel signal and optionally wait for worker confirmation.

        Parameters
        ----------
        resolved
            Resolved timeout in seconds, or ``None`` to wait indefinitely.
            ``0.0`` means fire-and-forget (no confirmation wait).
        """
        await self._backend.cancel_task(self._task_id)

        if resolved == 0.0:
            # Fire-and-forget: pre-seed a cancelled envelope so that any
            # subsequent ``await result`` immediately gets TaskCancelled.
            env = ResultEnvelope.cancelled(self._task_id)
            await self._backend.send_result(self._task_id, env.to_json())
            self._envelope = env  # also cache locally so .cancelled() is sync-true
            return True

        deadline = None if resolved is None else time.monotonic() + resolved
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                # Timed out waiting for worker confirmation — seed a cancelled
                # envelope so future reads don't hang forever.
                env = ResultEnvelope.cancelled(self._task_id)
                await self._backend.send_result(self._task_id, env.to_json())
                self._envelope = env
                return False
            # Long-poll for the worker's confirmation envelope instead of
            # busy-polling — wakes immediately on push-capable backends.
            wait_for = 5.0 if remaining is None else min(5.0, remaining)
            try:
                raw = await self._backend.get_result(self._task_id, timeout=wait_for)
            except TimeoutError:
                raw = None
            if raw is not None:
                self._envelope = ResultEnvelope.from_json(raw)
                return True

    # -- awaiting the result ---------------------------------------------------

    def __await__(self) -> Generator[Any, None, Any]:
        """``await result`` — shorthand for ``await result.result()``."""
        return self.result().__await__()

    async def result(
        self,
        timeout: TimeoutIn = False,
        stall_timeout: float | None = 600.0,
    ) -> Any:
        """Wait for and return the task's return value.

        Parameters
        ----------
        timeout
            How long to wait.  Defaults to ``False`` (wait indefinitely).
            See :data:`~offwork.TimeoutIn` for the full convention.
        stall_timeout
            Raise :class:`~offwork.TaskStalled` if the worker stops
            sending heartbeats for longer than this many seconds after
            the first heartbeat is observed.  Pass ``None`` to disable
            stall detection entirely.

        Raises
        ------
        RemoteError
            The remote function raised an exception.
        TaskCancelled
            The task was cancelled before or during execution.
        ThrottleError
            The task was rejected by the rate-limiter.
        TaskStalled
            The worker stopped sending heartbeats (only when
            *stall_timeout* is set).
        TimeoutError
            *timeout* elapsed before a result arrived.
        """
        if self._envelope is not None:
            return self._unwrap()

        resolved = resolve_timeout(timeout)
        logger.debug("Waiting for result of task %s", self._task_id[:8])

        if stall_timeout is None:
            raw = await self._backend.get_result(self._task_id, timeout=resolved)
            self._envelope = ResultEnvelope.from_json(raw)
            logger.debug(
                "Received result for task %s: status=%s",
                self._task_id[:8],
                self._envelope.status,
            )
            return self._unwrap()

        await self._wait_with_stall_detection(resolved, stall_timeout)
        return self._unwrap()

    async def wait(self, timeout: TimeoutIn = False) -> Self:
        """Wait until the result is available, then return ``self``.

        Unlike :meth:`result`, this does not raise on task errors —
        inspect :meth:`done`, :meth:`cancelled`, or :meth:`exception`
        afterwards.

        Parameters
        ----------
        timeout
            How long to wait.  Defaults to ``False`` (wait indefinitely).
            ``True`` or ``0`` returns immediately without blocking.
            See :data:`~offwork.TimeoutIn` for the full convention.

        Returns
        -------
        Self
            This handle (for chaining: ``if (await fut.wait()).done(): ...``).
        """
        if self._envelope is not None:
            return self
        resolved = resolve_timeout(timeout)
        try:
            if resolved == 0.0:
                raw = await self._backend.try_get_result(self._task_id)
            else:
                raw = await self._backend.get_result(self._task_id, timeout=resolved)
        except TimeoutError:
            return self
        if raw is not None:
            self._envelope = ResultEnvelope.from_json(raw)
        return self

    # -- non-blocking state queries --------------------------------------------

    def done(self) -> bool:
        """Return ``True`` if the result envelope has been received.

        This is a synchronous, non-blocking check of cached state —
        it does not contact the backend.  To update state from the
        backend first, use :meth:`check`.
        """
        return self._envelope is not None

    def cancelled(self) -> bool:
        """Return ``True`` if the task was cancelled.

        Synchronous.  Returns ``False`` while the task is still pending.
        """
        return self._envelope is not None and self._envelope.status == "cancelled"

    def exception(self) -> RemoteError | ThrottleError | TaskCancelled | None:
        """Return the exception the task raised, or ``None`` on success.

        Synchronous.

        Raises
        ------
        asyncio.InvalidStateError
            The task has not completed yet (envelope not received).
            Call :meth:`wait` or :meth:`check` first.
        """
        if self._envelope is None:
            raise asyncio.InvalidStateError(
                f"Task {self._task_id} is still pending"
            )
        if self._envelope.status == "cancelled":
            return TaskCancelled(f"Task {self._task_id} was cancelled")
        if self._envelope.status == "throttled":
            return ThrottleError(f"Task {self._task_id} was throttled (rate-limited)")
        if self._envelope.status == "error":
            return RemoteError(
                f"{self._envelope.error_type}: {self._envelope.error_message}",
                self._envelope.error_traceback,
            )
        return None

    async def check(self, timeout: TimeoutIn = 0.0) -> Self:
        """Poll the backend and update internal state, then return ``self``.

        Unlike :meth:`wait`, this defaults to a non-blocking poll
        (``timeout=0.0``). Useful in while-loops:

        .. code-block:: python

            while not (await fut.check()).done():
                await asyncio.sleep(1)

        Parameters
        ----------
        timeout
            How long to wait for a result.  Defaults to ``0.0``
            (non-blocking single poll).  Use ``False`` or ``-1`` to
            block until a result arrives.
            See :data:`~offwork.TimeoutIn` for the full convention.
        """
        if self._envelope is not None:
            return self
        resolved = resolve_timeout(timeout)
        raw: str | None
        try:
            if resolved == 0.0:
                raw = await self._backend.try_get_result(self._task_id)
            else:
                raw = await self._backend.get_result(self._task_id, timeout=resolved)
        except TimeoutError:
            return self
        if raw is not None:
            self._envelope = ResultEnvelope.from_json(raw)
        return self

    # -- cancellation ----------------------------------------------------------

    def cancel(self, timeout: TimeoutIn = 30.0) -> _CancelAwaitable:
        """Request cooperative cancellation of the task.

        Returns a :class:`_CancelAwaitable` that can be used with or
        without ``await``:

        .. code-block:: python

            fut.cancel()                  # fire-and-forget (background task)
            await fut.cancel()            # wait up to 30 s for confirmation
            await fut.cancel(timeout=60)  # wait up to 60 s
            await fut.cancel(timeout=False)  # wait indefinitely

        The worker observes the cancellation flag via its heartbeat loop
        and aborts execution cooperatively.

        Parameters
        ----------
        timeout
            How long to wait for worker confirmation.  Defaults to
            ``30.0`` seconds.  ``True`` or ``0`` return immediately after
            signalling (fire-and-forget).  ``False`` or ``-1`` wait
            indefinitely.
            See :data:`~offwork.TimeoutIn` for the full convention.

        Returns
        -------
        _CancelAwaitable
            ``True`` if cancellation was confirmed; ``False`` if
            *timeout* elapsed before the worker responded.
        """
        resolved = resolve_timeout(timeout)
        return _CancelAwaitable(self, resolved)

    # -- progress --------------------------------------------------------------

    def progress(self) -> _ProgressAwaitable:
        """Access task progress reported by :func:`offwork.progress`.

        Returns a :class:`_ProgressAwaitable` that supports two usage modes:

        .. code-block:: python

            # Single snapshot
            p = await fut.progress()
            if p:
                print(f"{p.percent:.0f}%")

            # Stream every update until the task finishes
            async for p in fut.progress():
                print(f"{p.percent:.0f}% — {p.message}")
        """
        return _ProgressAwaitable(self)

    def __repr__(self) -> str:
        s = "pending" if self._envelope is None else self._envelope.status
        return f"Result(task_id={self._task_id!r}, status={s!r})"

# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


class Stream(Result):
    """Async-iterable handle for a streaming (async-generator) task.

    Returned by :meth:`~offwork.typing.TracedFunction.stream`.  Iterate
    with ``async for`` to receive each value the remote async generator
    yields, in order, until it completes:

    .. code-block:: python

        async for chunk in my_gen.stream(url):
            process(chunk)

    A streaming task has no return value, so ``await``-ing the handle is
    not supported — use ``async for``.  All :class:`Result` controls
    (:meth:`cancel`, :meth:`progress`, state queries) still apply.

    Yields are **not** persisted: a consumer that starts iterating after
    values were already produced only sees values yielded from that point
    on.  If the task raises, the exception surfaces from the ``async for``.
    """

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncGenerator[Any, None]:
        backend = self._backend
        task_id = self._task_id
        next_seq = 0
        while True:
            yields, check_raw = await asyncio.gather(
                backend.get_yields(task_id, after_seq=next_seq - 1, timeout=1.0),
                backend.try_get_result(task_id),
            )
            for seq, value_json in yields:
                if seq < next_seq:
                    continue
                if seq != next_seq:
                    raise RuntimeError(
                        f"Stream gap for task {task_id}: expected seq "
                        f"{next_seq}, got {seq}"
                    )
                next_seq += 1
                yield _resolve(json.loads(value_json), {})

            if check_raw is not None and self._envelope is None:
                self._envelope = ResultEnvelope.from_json(check_raw)

            if self._envelope is not None:
                final = self._envelope.stream_yields
                if final is None or next_seq >= final:
                    self._drain_terminal()
                    return
                # Terminal envelope arrived before we drained every value;
                # keep polling until we have consumed all `final` yields.

    def _drain_terminal(self) -> None:
        """Raise on a non-ok terminal envelope (error / cancel / throttle)."""
        assert self._envelope is not None
        if self._envelope.status != "ok":
            self._unwrap()  # raises the appropriate error


class _StreamSubmission:
    """Lazy handle returned by ``traced_func.stream(...)``.

    Submits the streaming task on first use, supporting two patterns:

    .. code-block:: python

        # Iterate directly — submission happens on the first `async for`
        async for chunk in my_gen.stream(url):
            ...

        # Or await to get the underlying Stream handle (for cancel/progress)
        stream = await my_gen.stream(url)
        async for chunk in stream:
            ...
    """

    def __init__(
        self,
        func: Any,
        wrapper: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        backend: Any,
    ) -> None:
        self._func = func
        self._wrapper = wrapper
        self._args = args
        self._kwargs = kwargs
        self._backend = backend
        self._stream: Stream | None = None

    async def _ensure(self) -> Stream:
        if self._stream is None:
            from offwork.worker.remote import submit_remote_stream  # circular

            self._stream = await submit_remote_stream(
                self._func, self._wrapper, *self._args,
                _backend=self._backend, **self._kwargs,
            )
        return self._stream

    def __await__(self) -> Generator[Any, None, Stream]:
        return self._ensure().__await__()

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncGenerator[Any, None]:
        stream = await self._ensure()
        async for value in stream:
            yield value