"""Result future and result envelope for remote task execution."""

import json
import time
import asyncio
import logging
import traceback as tb_mod
from typing import Any, Self
from dataclasses import dataclass
from collections.abc import Generator

from away.core.task import _TaskEncoder, _resolve
from away.core.errors import RemoteError, TaskStalled, TaskCancelled, ThrottleError
from away.core.progress import ProgressInfo
from away.worker.backends.base import Backend

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

    @classmethod
    def success(cls, task_id: str, result: Any) -> Self:
        """Create an envelope for a successful result."""
        return cls(task_id=task_id, status="ok", result=result)

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
        result = _resolve(data.get("result"), {}) if data.get("status") == "ok" else None
        return cls(
            task_id=data["task_id"],
            status=data["status"],
            result=result,
            error_type=data.get("error_type"),
            error_message=data.get("error_message"),
            error_traceback=data.get("error_traceback"),
        )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class Result:
    """Awaitable future-like handle for a remotely submitted task.

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
        """Unwrap a cached envelope, raising on error or cancellation."""
        assert self._envelope is not None
        if self._envelope.status == "cancelled":
            raise TaskCancelled(
                f"Task {self._task_id} was cancelled"
            ) from None
        if self._envelope.status == "throttled":
            raise ThrottleError(
                f"Task {self._task_id} was throttled (rate-limited)"
            ) from None
        if self._envelope.status == "error":
            msg = (
                f"{self._envelope.error_type}: "
                f"{self._envelope.error_message}"
            )
            raise RemoteError(msg, self._envelope.error_traceback) from None
        return self._envelope.result

    async def result(
        self,
        timeout: float | None = None,
        stall_timeout: float | None = 10.0,
    ) -> Any:
        """Await the result.

        Parameters
        ----------
        timeout
            Maximum seconds to wait for the result.
        stall_timeout
            Raises :class:`TaskStalled` if the worker stops sending
            heartbeats for longer than this many seconds after at least
            one heartbeat has been observed.  Set to ``None`` to
            disable stall detection.

        Raises :class:`RemoteError` if the remote execution failed.
        """
        if self._envelope is not None:
            return self._unwrap()

        logger.debug("Waiting for result of task %s", self._task_id[:8])
        if stall_timeout is None:
            raw = await self._backend.get_result(self._task_id, timeout=timeout)
            self._envelope = ResultEnvelope.from_json(raw)
            logger.debug(
                "Received result for task %s: status=%s",
                self._task_id[:8], self._envelope.status,
            )
            return self._unwrap()

        await self._wait_with_stall_detection(timeout, stall_timeout)
        return self._unwrap()

    async def _wait_with_stall_detection(
        self,
        timeout: float | None,
        stall_timeout: float,
    ) -> None:
        """Poll for result with heartbeat-based stall detection."""
        deadline = None if timeout is None else time.monotonic() + timeout
        last_hb_value: float | None = None
        last_hb_change: float | None = None

        while True:
            raw = await self._backend.try_get_result(self._task_id)
            if raw is not None:
                self._envelope = ResultEnvelope.from_json(raw)
                logger.debug(
                    "Received result for task %s: status=%s",
                    self._task_id[:8], self._envelope.status,
                )
                return

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out waiting for result of task {self._task_id}"
                    )

            logger.debug("Polling heartbeat for task %s", self._task_id[:8])
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

            await asyncio.sleep(1.0)

    def __await__(self) -> Generator[Any, None, Any]:
        """Allow ``await result`` as shorthand for ``await result.result()``."""
        return self.result().__await__()

    # -- cancellation ----------------------------------------------------------

    async def cancel(self) -> None:
        """Cancel the task.

        Marks the task as cancelled in the backend.  If the worker
        hasn't started execution yet, it will skip the task.  If
        execution is already in progress, it will continue but the
        client will receive a :class:`TaskCancelled` error.

        Awaiting the result after cancellation raises
        :class:`TaskCancelled`.
        """
        await self._backend.cancel_task(self._task_id)
        await self._backend.send_result(
            self._task_id,
            ResultEnvelope.cancelled(self._task_id).to_json(),
        )

    # -- progress --------------------------------------------------------------

    async def progress(self) -> ProgressInfo | None:
        """Return the latest progress reported by the task, or ``None``.

        Progress is available when the task function calls
        :func:`away.progress`.
        """
        raw = await self._backend.get_progress(self._task_id)
        if raw is None:
            return None
        return ProgressInfo.from_json(raw)

    # -- non-blocking queries --------------------------------------------------

    async def done(self) -> bool:
        """Check whether the result is available."""
        if self._envelope is not None:
            return True
        raw = await self._backend.try_get_result(self._task_id)
        if raw is not None:
            self._envelope = ResultEnvelope.from_json(raw)
            return True
        return False

    async def status(self) -> str:
        """Return ``"pending"``, ``"success"``, ``"error"``, or ``"cancelled"``."""
        if self._envelope is None:
            raw = await self._backend.try_get_result(self._task_id)
            if raw is None:
                return "pending"
            self._envelope = ResultEnvelope.from_json(raw)
        if self._envelope.status == "ok":
            return "success"
        if self._envelope.status == "cancelled":
            return "cancelled"
        if self._envelope.status == "throttled":
            return "throttled"
        return "error"

    def __repr__(self) -> str:
        s = "pending" if self._envelope is None else self._envelope.status
        return f"Result(task_id={self._task_id!r}, status={s!r})"
