from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import traceback as tb_mod
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, Self

from pyfuse.core.errors import RemoteError, TaskStalled
from pyfuse.worker.backends.base import Backend

logger = logging.getLogger(__name__)

_MISSING = object()
_FALLBACK_POLL_INTERVAL = 0.5  # consolidated fallback polling


@dataclass(frozen=True)
class ResultEnvelope:
    """Serializable envelope for a task result (success or error).

    Use the :meth:`success` and :meth:`failure` class methods to create
    instances.
    """

    task_id: str
    status: str  # "ok" or "error"
    result: Any = None
    error_type: str | None = None
    error_message: str | None = None
    error_traceback: str | None = None

    @classmethod
    def success(cls, task_id: str, result: Any) -> Self:
        return cls(task_id=task_id, status="ok", result=result)

    @classmethod
    def failure(cls, task_id: str, exc: BaseException) -> Self:
        return cls(
            task_id=task_id,
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
            error_traceback="".join(tb_mod.format_exception(exc)),
        )

    def to_json(self) -> str:
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "status": self.status,
        }
        if self.status == "ok":
            d["result"] = self.result
        else:
            d["error_type"] = self.error_type
            d["error_message"] = self.error_message
            d["error_traceback"] = self.error_traceback
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Self:
        data = json.loads(raw)
        return cls(
            task_id=data["task_id"],
            status=data["status"],
            result=data.get("result"),
            error_type=data.get("error_type"),
            error_message=data.get("error_message"),
            error_traceback=data.get("error_traceback"),
        )


# ---------------------------------------------------------------------------
# ResultWaiter: per-backend singleton that fans out notifications
# ---------------------------------------------------------------------------


class _WaiterSlot:
    """Pending-result slot for one task_id."""

    __slots__ = (
        "event", "future", "raw",
        "last_hb_value", "last_hb_change",
    )

    def __init__(self) -> None:
        self.event = threading.Event()
        self.future: asyncio.Future[str] | None = None
        self.raw: str | None = None
        self.last_hb_value: float | None = None
        self.last_hb_change: float | None = None


class ResultWaiter:
    """Per-backend singleton that listens for result notifications.

    Runs two daemon threads:
    - **listener**: receives push notifications (or polls as fallback)
      and resolves individual ``_WaiterSlot`` objects.
    - **heartbeat monitor**: batch-fetches heartbeats once per second
      and updates slot state for stall detection.
    """

    @classmethod
    def for_backend(cls, backend: Backend) -> ResultWaiter:
        """Return (or create) the waiter for *backend*."""
        waiter: ResultWaiter | None = getattr(backend, "_result_waiter", None)
        if waiter is None:
            waiter = cls(backend)
            backend._result_waiter = waiter  # type: ignore[attr-defined]
        return waiter

    @classmethod
    def stop_for(cls, backend: Backend) -> None:
        """Stop and remove the waiter for *backend*, if any."""
        waiter: ResultWaiter | None = getattr(backend, "_result_waiter", None)
        if waiter is not None:
            waiter.stop()
            backend._result_waiter = None  # type: ignore[attr-defined]

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self._slots: dict[str, _WaiterSlot] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._started = False

    # -- public ----------------------------------------------------------------

    def register(
        self,
        task_id: str,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> _WaiterSlot:
        """Register interest in *task_id* and return a slot to wait on."""
        self._ensure_started()
        with self._lock:
            slot = self._slots.get(task_id)
            if slot is None:
                slot = _WaiterSlot()
                self._slots[task_id] = slot
            if loop is not None and slot.future is None:
                slot.future = loop.create_future()
        return slot

    def unregister(self, task_id: str) -> None:
        with self._lock:
            self._slots.pop(task_id, None)

    def stop(self) -> None:
        self._stop_event.set()
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=3.0)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=3.0)

    # -- internals -------------------------------------------------------------

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            self._started = True
            self._listener_thread = threading.Thread(
                target=self._listen_loop, daemon=True,
                name="pyfuse-result-listener",
            )
            self._listener_thread.start()
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True,
                name="pyfuse-heartbeat-monitor",
            )
            self._heartbeat_thread.start()

    def _resolve(self, task_id: str) -> None:
        """Fetch a result for *task_id* and wake the waiter."""
        with self._lock:
            slot = self._slots.get(task_id)
        if slot is None or slot.event.is_set():
            return
        raw = self._backend.try_get_result(task_id)
        if raw is None:
            return  # spurious notification
        slot.raw = raw
        slot.event.set()
        if slot.future is not None and not slot.future.done():
            loop = slot.future.get_loop()
            loop.call_soon_threadsafe(slot.future.set_result, raw)

    # -- listener thread -------------------------------------------------------

    def _listen_loop(self) -> None:
        try:
            for task_id in self._backend.subscribe_results():
                if self._stop_event.is_set():
                    break
                self._resolve(task_id)
        except NotImplementedError:
            self._poll_fallback()
        except Exception:
            logger.debug("Listener error, falling back to polling", exc_info=True)
            self._poll_fallback()

    def _poll_fallback(self) -> None:
        """Consolidated polling for backends without push notifications."""
        while not self._stop_event.is_set():
            with self._lock:
                pending = [
                    tid for tid, slot in self._slots.items()
                    if not slot.event.is_set()
                ]
            for task_id in pending:
                self._resolve(task_id)
            self._stop_event.wait(_FALLBACK_POLL_INTERVAL)

    # -- heartbeat monitor thread ----------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                pending = [
                    tid for tid, slot in self._slots.items()
                    if not slot.event.is_set()
                ]
            if pending:
                try:
                    heartbeats = self._backend.get_heartbeats(pending)
                except Exception:
                    heartbeats = {}
                now = time.monotonic()
                with self._lock:
                    for tid in pending:
                        slot = self._slots.get(tid)
                        if slot is None:
                            continue
                        hb = heartbeats.get(tid)
                        if hb is not None and hb != slot.last_hb_value:
                            slot.last_hb_value = hb
                            slot.last_hb_change = now
            self._stop_event.wait(1.0)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class Result:
    """Future-like handle for a remotely submitted task.

    Returned by ``traced_func.run(...)``.
    """

    def __init__(self, task_id: str, backend: Backend) -> None:
        self._task_id = task_id
        self._backend = backend
        self._envelope: ResultEnvelope | None = None

    @property
    def task_id(self) -> str:
        return self._task_id

    def _unwrap(self) -> Any:
        """Unwrap a cached envelope, raising on error."""
        assert self._envelope is not None
        if self._envelope.status == "error":
            msg = f"{self._envelope.error_type}: {self._envelope.error_message}"
            if self._envelope.error_traceback:
                msg += f"\n\nRemote traceback:\n{self._envelope.error_traceback}"
            raise RemoteError(msg)
        return self._envelope.result

    # -- helpers ---------------------------------------------------------------

    def _try_fetch(self) -> bool:
        """One-shot fetch; returns True if envelope was populated."""
        if self._envelope is not None:
            return True
        raw = self._backend.try_get_result(self._task_id)
        if raw is not None:
            self._envelope = ResultEnvelope.from_json(raw)
            return True
        return False

    @staticmethod
    def _check_slot_stall(
        slot: _WaiterSlot,
        stall_timeout: float,
        now: float,
        task_id: str,
    ) -> None:
        """Raise :class:`TaskStalled` if the slot's heartbeat went stale."""
        if slot.last_hb_change is not None and (now - slot.last_hb_change) > stall_timeout:
            elapsed = now - slot.last_hb_change
            raise TaskStalled(
                f"Task {task_id} stalled: no heartbeat for "
                f"{elapsed:.1f}s (threshold: {stall_timeout}s)"
            )

    def _resolve_slot(self, slot: _WaiterSlot) -> None:
        """If slot was already resolved, populate envelope from it."""
        if slot.raw is not None and self._envelope is None:
            self._envelope = ResultEnvelope.from_json(slot.raw)

    # -- sync ------------------------------------------------------------------

    def result(
        self,
        timeout: float | None = None,
        stall_timeout: float | None = None,
    ) -> Any:
        """Block until the result arrives, then return it.

        Parameters
        ----------
        timeout
            Maximum seconds to wait for the result.
        stall_timeout
            When set, uses notification-based waiting with heartbeat
            monitoring.  Raises :class:`TaskStalled` if the worker
            stops heartbeating for longer than this many seconds.

        Raises :class:`RemoteError` if the remote execution failed.
        """
        if self._envelope is not None:
            return self._unwrap()

        # Fast path: no stall monitoring — use backend's native blocking wait.
        if stall_timeout is None:
            raw = self._backend.get_result(self._task_id, timeout=timeout)
            self._envelope = ResultEnvelope.from_json(raw)
            return self._unwrap()

        # Race-condition guard: result may already be stored.
        if self._try_fetch():
            return self._unwrap()

        waiter = ResultWaiter.for_backend(self._backend)
        slot = waiter.register(self._task_id)

        # Double-check after registration.
        if not slot.event.is_set():
            early = self._backend.try_get_result(self._task_id)
            if early is not None:
                slot.raw = early
                slot.event.set()

        try:
            deadline = None if timeout is None else time.monotonic() + timeout
            while not slot.event.is_set():
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Timed out waiting for result of task {self._task_id}"
                        )
                wait_time = min(1.0, remaining) if remaining is not None else 1.0
                slot.event.wait(timeout=wait_time)
                if not slot.event.is_set():
                    self._check_slot_stall(
                        slot, stall_timeout, time.monotonic(), self._task_id,
                    )
            self._resolve_slot(slot)
            return self._unwrap()
        finally:
            waiter.unregister(self._task_id)

    # -- async -----------------------------------------------------------------

    async def aresult(
        self,
        timeout: float | None = None,
        stall_timeout: float | None = 10.0,
    ) -> Any:
        """Await the result without blocking the event loop.

        Uses push notifications when the backend supports them;
        falls back to consolidated polling otherwise.

        Parameters
        ----------
        timeout
            Maximum seconds to wait.
        stall_timeout
            Raises :class:`TaskStalled` if the worker stops sending
            heartbeats for longer than this many seconds after at least
            one heartbeat has been observed.  Set to ``None`` to
            disable stall detection.
        """
        if self._envelope is not None:
            return self._unwrap()

        # Race-condition guard.
        if self._try_fetch():
            return self._unwrap()

        loop = asyncio.get_running_loop()
        waiter = ResultWaiter.for_backend(self._backend)
        slot = waiter.register(self._task_id, loop=loop)

        # Double-check after registration.
        if not slot.event.is_set():
            raw = self._backend.try_get_result(self._task_id)
            if raw is not None:
                slot.raw = raw
                slot.event.set()
                if slot.future is not None and not slot.future.done():
                    slot.future.set_result(raw)

        try:
            assert slot.future is not None
            if slot.future.done():
                self._resolve_slot(slot)
                return self._unwrap()

            if stall_timeout is None and timeout is not None:
                raw = await asyncio.wait_for(slot.future, timeout=timeout)
                self._envelope = ResultEnvelope.from_json(raw)
                return self._unwrap()

            if stall_timeout is None:
                raw = await slot.future
                self._envelope = ResultEnvelope.from_json(raw)
                return self._unwrap()

            # Wait in 1-second chunks so we can check stall status.
            deadline = None if timeout is None else loop.time() + timeout
            while True:
                remaining = None
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Timed out waiting for result of task {self._task_id}"
                        )
                wait_time = min(1.0, remaining) if remaining is not None else 1.0
                try:
                    raw = await asyncio.wait_for(
                        asyncio.shield(slot.future), timeout=wait_time,
                    )
                    self._envelope = ResultEnvelope.from_json(raw)
                    return self._unwrap()
                except asyncio.TimeoutError:
                    self._check_slot_stall(
                        slot, stall_timeout, time.monotonic(), self._task_id,
                    )
        finally:
            waiter.unregister(self._task_id)

    def __await__(self) -> Generator[Any, None, Any]:
        """Allow ``await result`` as shorthand for ``await result.aresult()``."""
        return self.aresult().__await__()

    # -- non-blocking queries (unchanged) --------------------------------------

    @property
    def status(self) -> str:
        """Return ``"pending"``, ``"success"``, or ``"error"``."""
        if self._envelope is None:
            raw = self._backend.try_get_result(self._task_id)
            if raw is None:
                return "pending"
            self._envelope = ResultEnvelope.from_json(raw)
        return "success" if self._envelope.status == "ok" else "error"

    def done(self) -> bool:
        """Non-blocking check whether the result is available."""
        if self._envelope is not None:
            return True
        raw = self._backend.try_get_result(self._task_id)
        if raw is not None:
            self._envelope = ResultEnvelope.from_json(raw)
            return True
        return False

    def __repr__(self) -> str:
        s = "pending" if self._envelope is None else self._envelope.status
        return f"Result(task_id={self._task_id!r}, status={s!r})"
