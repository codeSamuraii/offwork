from __future__ import annotations

import abc
from collections.abc import Iterator


class Backend(abc.ABC):
    """Abstract transport backend for remote task execution.

    Subclass this to implement custom transports (Redis, RabbitMQ,
    shared memory, etc.).
    """

    @abc.abstractmethod
    def submit(self, task_json: str) -> None:
        """Enqueue a serialized task for a worker to pick up."""

    @abc.abstractmethod
    def listen(self) -> Iterator[str]:
        """Blocking iterator that yields serialized task JSON strings."""

    @abc.abstractmethod
    def send_result(self, task_id: str, result_json: str) -> None:
        """Store a result envelope for the given task."""

    @abc.abstractmethod
    def get_result(self, task_id: str, timeout: float | None = None) -> str:
        """Block until result for *task_id* is available. Returns raw JSON."""

    @abc.abstractmethod
    def try_get_result(self, task_id: str) -> str | None:
        """Non-blocking result fetch. Returns ``None`` if not ready."""

    # -- Heartbeat -------------------------------------------------------------

    def send_heartbeat(self, task_id: str) -> None:
        """Signal that a worker is actively processing *task_id*.

        Called periodically by the worker while a task is running.
        The default implementation is a no-op; backends that support
        heartbeat-based stall detection should override this.
        """

    def get_heartbeat(self, task_id: str) -> float | None:
        """Return the timestamp of the last heartbeat for *task_id*.

        Returns ``None`` if no heartbeat has been recorded.  The
        timestamp is a ``time.time()`` value written by the worker.
        """
        return None

    def get_heartbeats(self, task_ids: list[str]) -> dict[str, float | None]:
        """Batch-fetch heartbeats for multiple tasks.

        Default implementation loops over :meth:`get_heartbeat`.
        Backends can override for efficiency (e.g. Redis ``MGET``).
        """
        return {tid: self.get_heartbeat(tid) for tid in task_ids}

    # -- Result notifications --------------------------------------------------

    def notify_result(self, task_id: str) -> None:
        """Publish a push notification that a result is ready.

        Called by the worker after :meth:`send_result`.  The default
        is a no-op; backends that support push notifications should
        override this together with :meth:`subscribe_results`.
        """

    def subscribe_results(self) -> Iterator[str]:
        """Blocking iterator yielding *task_id* strings as results arrive.

        Used by the client-side :class:`ResultWaiter` to receive push
        notifications.  The default raises ``NotImplementedError``;
        the waiter falls back to consolidated polling in that case.
        """
        raise NotImplementedError(
            "This backend does not support result subscriptions."
        )

    # -- Lifecycle -------------------------------------------------------------

    @abc.abstractmethod
    def close(self) -> None:
        """Release resources."""
