from __future__ import annotations

import abc
from collections.abc import Iterator
from typing import Any


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


class RedisBackend(Backend):
    """Redis-backed transport using ``RPUSH``/``BLPOP`` for tasks and results.

    Parameters
    ----------
    url
        Redis connection URL (e.g. ``redis://localhost:6379``).
    queue_key
        Redis key for the task queue.
    result_ttl
        Seconds before result keys expire.
    """

    DEFAULT_QUEUE_KEY = "pyfuse:tasks"
    RESULT_PREFIX = "pyfuse:result:"
    DEFAULT_RESULT_TTL = 300

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        *,
        queue_key: str | None = None,
        result_ttl: int | None = None,
    ) -> None:
        try:
            import redis as _redis
        except ImportError:
            raise ImportError(
                "redis package is required for RedisBackend. "
                "Install it with: pip install redis"
            ) from None
        self._redis: Any = _redis.Redis.from_url(url)
        self._queue_key = queue_key or self.DEFAULT_QUEUE_KEY
        self._result_ttl = result_ttl or self.DEFAULT_RESULT_TTL

    def submit(self, task_json: str) -> None:
        self._redis.rpush(self._queue_key, task_json)

    def listen(self) -> Iterator[str]:
        while True:
            result = self._redis.blpop(self._queue_key)
            if result is None:
                continue
            _, raw = result
            yield raw.decode() if isinstance(raw, bytes) else raw

    def send_result(self, task_id: str, result_json: str) -> None:
        key = f"{self.RESULT_PREFIX}{task_id}"
        self._redis.rpush(key, result_json)
        self._redis.expire(key, self._result_ttl)

    def get_result(self, task_id: str, timeout: float | None = None) -> str:
        key = f"{self.RESULT_PREFIX}{task_id}"
        t = int(timeout) if timeout else 0
        result = self._redis.blpop(key, timeout=t)
        if result is None:
            raise TimeoutError(
                f"Timed out waiting for result of task {task_id}"
            )
        _, raw = result
        return raw.decode() if isinstance(raw, bytes) else raw

    def try_get_result(self, task_id: str) -> str | None:
        key = f"{self.RESULT_PREFIX}{task_id}"
        raw = self._redis.lpop(key)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    def close(self) -> None:
        self._redis.close()
