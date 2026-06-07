"""Redis-backed transport using ``RPUSH``/``BLPOP`` for tasks and results."""

import math
import time
import asyncio
from typing import Any
from urllib.parse import parse_qs, urlparse
from collections.abc import AsyncIterator

try:
    import redis.asyncio as _redis
    from redis.exceptions import TimeoutError as RedisTimeoutError
except ImportError:
    raise ImportError(
        "redis package is required for RedisBackend. "
        "Install it with: pip install redis"
    ) from None

from offwork.worker.backends.base import Backend


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

    DEFAULT_QUEUE_KEY = "offwork:tasks"
    RESULT_PREFIX = "offwork:result:"
    HEARTBEAT_PREFIX = "offwork:heartbeat:"
    CANCEL_PREFIX = "offwork:cancel:"
    PROGRESS_PREFIX = "offwork:progress:"
    SCHEDULE_PREFIX = "offwork:schedule:"
    THROTTLE_PREFIX = "offwork:throttle:"
    NOTIFY_CHANNEL = "offwork:notify"
    DEFAULT_RESULT_TTL = 300
    HEARTBEAT_TTL = 30
    CANCEL_TTL = 3600
    PROGRESS_TTL = 300
    SCHEDULE_TTL = 2592000  # 30 days

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        *,
        queue_key: str | None = None,
        result_ttl: int | None = None,
    ) -> None:
        query = parse_qs(urlparse(url).query)
        connect_kwargs: dict[str, Any] = {}
        if "socket_timeout" not in query:
            connect_kwargs["socket_timeout"] = None
        self._redis: Any = _redis.Redis.from_url(url, **connect_kwargs)
        self._queue_key = queue_key or self.DEFAULT_QUEUE_KEY
        self._result_ttl = result_ttl or self.DEFAULT_RESULT_TTL

    async def submit(self, task_json: str) -> None:
        await self._redis.rpush(self._queue_key, task_json)

    async def listen(self) -> AsyncIterator[str]:
        """Block on ``BLPOP`` and yield task JSON strings as they arrive."""
        while True:
            try:
                result = await self._redis.blpop(self._queue_key)
            except RedisTimeoutError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError() from None
                continue
            if result is None:
                continue
            _, raw = result
            yield raw.decode() if isinstance(raw, bytes) else raw

    async def send_result(self, task_id: str, result_json: str) -> None:
        key = f"{self.RESULT_PREFIX}{task_id}"
        # Pipeline RPUSH + EXPIRE into one round-trip.
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.rpush(key, result_json)
            pipe.expire(key, self._result_ttl)
            await pipe.execute()

    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        key = f"{self.RESULT_PREFIX}{task_id}"
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            if deadline is None:
                block_seconds = 0
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out waiting for result of task {task_id}"
                    )
                block_seconds = max(1, math.ceil(remaining))
            try:
                result = await self._redis.blpop(key, timeout=block_seconds)
            except RedisTimeoutError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError() from None
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for result of task {task_id}"
                    ) from None
                continue
            if result is None:
                raise TimeoutError(
                    f"Timed out waiting for result of task {task_id}"
                )
            _, raw = result
            return raw.decode() if isinstance(raw, bytes) else raw

    async def try_get_result(self, task_id: str) -> str | None:
        """Non-blocking ``LPOP``; returns ``None`` if not yet available."""
        key = f"{self.RESULT_PREFIX}{task_id}"
        raw = await self._redis.lpop(key)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def send_heartbeat(self, task_id: str) -> None:
        key = f"{self.HEARTBEAT_PREFIX}{task_id}"
        await self._redis.set(key, str(time.time()), ex=self.HEARTBEAT_TTL)

    async def get_heartbeat(self, task_id: str) -> float | None:
        key = f"{self.HEARTBEAT_PREFIX}{task_id}"
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return float(raw)

    async def get_heartbeats(self, task_ids: list[str]) -> dict[str, float | None]:
        """Batch fetch via ``MGET`` for efficiency."""
        if not task_ids:
            return {}
        keys = [f"{self.HEARTBEAT_PREFIX}{tid}" for tid in task_ids]
        values = await self._redis.mget(keys)
        return {
            tid: float(v) if v is not None else None
            for tid, v in zip(task_ids, values)
        }

    async def cancel_task(self, task_id: str) -> None:
        key = f"{self.CANCEL_PREFIX}{task_id}"
        await self._redis.set(key, "1", ex=self.CANCEL_TTL)

    async def is_cancelled(self, task_id: str) -> bool:
        key = f"{self.CANCEL_PREFIX}{task_id}"
        return int(await self._redis.exists(key)) > 0

    async def send_progress(self, task_id: str, progress_json: str) -> None:
        key = f"{self.PROGRESS_PREFIX}{task_id}"
        await self._redis.set(key, progress_json, ex=self.PROGRESS_TTL)

    async def get_progress(self, task_id: str) -> str | None:
        key = f"{self.PROGRESS_PREFIX}{task_id}"
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def cancel_schedule(self, schedule_id: str) -> None:
        key = f"{self.SCHEDULE_PREFIX}{schedule_id}"
        await self._redis.set(key, "1", ex=self.SCHEDULE_TTL)

    async def is_schedule_cancelled(self, schedule_id: str) -> bool:
        key = f"{self.SCHEDULE_PREFIX}{schedule_id}"
        return int(await self._redis.exists(key)) > 0

    async def check_throttle(self, function_name: str) -> bool:
        key = f"{self.THROTTLE_PREFIX}{function_name}"
        return int(await self._redis.exists(key)) == 0

    async def record_throttle(
        self, function_name: str, throttle_seconds: float,
    ) -> None:
        key = f"{self.THROTTLE_PREFIX}{function_name}"
        await self._redis.set(key, "1", ex=max(1, int(throttle_seconds)))

    async def notify_result(self, task_id: str) -> None:
        """Publish task_id on the Pub/Sub notification channel."""
        await self._redis.publish(self.NOTIFY_CHANNEL, task_id)

    async def subscribe_results(self) -> AsyncIterator[str]:
        """Subscribe to the Pub/Sub channel and yield task IDs on result arrival."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self.NOTIFY_CHANNEL)
        try:
            while True:
                # Block until a message arrives or the timeout fires;
                # avoid burning a 10 ms sleep on each empty wakeup.
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0,
                )
                if msg is None or msg["type"] != "message":
                    continue
                data = msg["data"]
                yield data.decode() if isinstance(data, bytes) else data
        finally:
            await pubsub.unsubscribe(self.NOTIFY_CHANNEL)
            await pubsub.aclose()

    async def close(self) -> None:
        await self._redis.aclose()
