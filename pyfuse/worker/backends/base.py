from __future__ import annotations

import abc
from collections.abc import AsyncIterator


class Backend(abc.ABC):
    """Abstract transport backend for remote task execution.

    Subclass this to implement custom transports (Redis, RabbitMQ,
    shared memory, etc.).
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
