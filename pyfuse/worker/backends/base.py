"""Abstract base class for transport backends."""

import abc
from collections.abc import AsyncIterator


class Backend(abc.ABC):
    """Abstract transport backend for remote task execution.

    Subclass this to implement custom transports (Redis, RabbitMQ,
    TCP, etc.).
    """

    @abc.abstractmethod
    async def submit(self, task_json: str) -> None:
        """Enqueue a serialized task for a worker to pick up."""

    @abc.abstractmethod
    def listen(self) -> AsyncIterator[str]:
        """Async iterator that yields serialized task JSON strings."""

    @abc.abstractmethod
    async def send_result(self, task_id: str, result_json: str) -> None:
        """Store a result envelope for the given task."""

    @abc.abstractmethod
    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        """Await until result for *task_id* is available. Returns raw JSON."""

    @abc.abstractmethod
    async def try_get_result(self, task_id: str) -> str | None:
        """Non-blocking result fetch. Returns ``None`` if not ready."""

    # -- Heartbeat -------------------------------------------------------------

    async def send_heartbeat(self, task_id: str) -> None:
        """Signal that a worker is actively processing *task_id*.

        Called periodically by the worker while a task is running.
        The default implementation is a no-op; backends that support
        heartbeat-based stall detection should override this.
        """

    async def get_heartbeat(self, task_id: str) -> float | None:
        """Return the timestamp of the last heartbeat for *task_id*.

        Returns ``None`` if no heartbeat has been recorded.  The
        timestamp is a ``time.time()`` value written by the worker.
        """
        return None

    async def get_heartbeats(self, task_ids: list[str]) -> dict[str, float | None]:
        """Batch-fetch heartbeats for multiple tasks.

        Default implementation loops over :meth:`get_heartbeat`.
        Backends can override for efficiency (e.g. Redis ``MGET``).
        """
        return {tid: await self.get_heartbeat(tid) for tid in task_ids}

    # -- Cancellation ----------------------------------------------------------

    async def cancel_task(self, task_id: str) -> None:
        """Mark a task as cancelled.

        The worker checks this flag before starting execution.
        The default implementation is a no-op.
        """

    async def is_cancelled(self, task_id: str) -> bool:
        """Return whether a task has been cancelled.

        The default implementation always returns ``False``.
        """
        return False

    # -- Progress --------------------------------------------------------------

    async def send_progress(self, task_id: str, progress_json: str) -> None:
        """Store the latest progress data for a task.

        Called by the worker when user code calls :func:`pyfuse.progress`.
        The default implementation is a no-op.
        """

    async def get_progress(self, task_id: str) -> str | None:
        """Return the latest progress JSON for a task, or ``None``.

        The default returns ``None``.
        """
        return None

    # -- Schedule cancellation ------------------------------------------------

    async def cancel_schedule(self, schedule_id: str) -> None:
        """Mark a recurring schedule as cancelled.

        The worker checks this before re-enqueuing the next occurrence.
        The default implementation is a no-op.
        """

    async def is_schedule_cancelled(self, schedule_id: str) -> bool:
        """Return whether a recurring schedule has been cancelled."""
        return False

    # -- Throttle --------------------------------------------------------------

    async def check_throttle(self, function_name: str) -> bool:
        """Return ``True`` if the function is allowed to execute.

        Returns ``False`` when the cooldown period from a previous
        execution has not elapsed.  The default always returns ``True``.
        """
        return True

    async def record_throttle(
        self, function_name: str, throttle_seconds: float,
    ) -> None:
        """Record that a function was just executed, starting a cooldown.

        Subsequent :meth:`check_throttle` calls within *throttle_seconds*
        should return ``False``.  The default is a no-op.
        """

    # -- Result notifications --------------------------------------------------

    async def notify_result(self, task_id: str) -> None:
        """Publish a push notification that a result is ready.

        Called by the worker after :meth:`send_result`.  The default
        is a no-op; backends that support push notifications should
        override this together with :meth:`subscribe_results`.
        """

    def subscribe_results(self) -> AsyncIterator[str]:
        """Async iterator yielding *task_id* strings as results arrive.

        Used by the client-side :class:`Result` to receive push
        notifications.  The default raises ``NotImplementedError``;
        the result falls back to polling in that case.
        """
        raise NotImplementedError(
            "This backend does not support result subscriptions."
        )

    # -- Lifecycle -------------------------------------------------------------

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources."""
