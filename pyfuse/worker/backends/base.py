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

    @abc.abstractmethod
    def close(self) -> None:
        """Release resources."""
