from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from pyfuse.worker.backends.base import Backend


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
    HEARTBEAT_PREFIX = "pyfuse:heartbeat:"
    NOTIFY_CHANNEL = "pyfuse:notify"
    DEFAULT_RESULT_TTL = 300
    HEARTBEAT_TTL = 30

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        *,
        queue_key: str | None = None,
        result_ttl: int | None = None,
    ) -> None:
        try:
            import redis.asyncio as _redis
        except ImportError:
            raise ImportError(
                "redis package is required for RedisBackend. "
                "Install it with: pip install redis"
            ) from None
        self._redis: Any = _redis.Redis.from_url(url)
        self._queue_key = queue_key or self.DEFAULT_QUEUE_KEY
        self._result_ttl = result_ttl or self.DEFAULT_RESULT_TTL

    async def submit(self, task_json: str) -> None:
        await self._redis.rpush(self._queue_key, task_json)

    async def listen(self) -> AsyncIterator[str]:
        while True:
            result = await self._redis.blpop(self._queue_key)
            if result is None:
                continue
            _, raw = result
            yield raw.decode() if isinstance(raw, bytes) else raw

    async def send_result(self, task_id: str, result_json: str) -> None:
        key = f"{self.RESULT_PREFIX}{task_id}"
        await self._redis.rpush(key, result_json)
        await self._redis.expire(key, self._result_ttl)

    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        key = f"{self.RESULT_PREFIX}{task_id}"
        t = int(timeout) if timeout else 0
        result = await self._redis.blpop(key, timeout=t)
        if result is None:
            raise TimeoutError(
                f"Timed out waiting for result of task {task_id}"
            )
        _, raw = result
        return raw.decode() if isinstance(raw, bytes) else raw

    async def try_get_result(self, task_id: str) -> str | None:
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
        if not task_ids:
            return {}
        keys = [f"{self.HEARTBEAT_PREFIX}{tid}" for tid in task_ids]
        values = await self._redis.mget(keys)
        return {
            tid: float(v) if v is not None else None
            for tid, v in zip(task_ids, values)
        }

    async def notify_result(self, task_id: str) -> None:
        await self._redis.publish(self.NOTIFY_CHANNEL, task_id)

    async def subscribe_results(self) -> AsyncIterator[str]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self.NOTIFY_CHANNEL)
        try:
            while True:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0,
                )
                if msg is not None and msg["type"] == "message":
                    data = msg["data"]
                    yield data.decode() if isinstance(data, bytes) else data
                elif msg is None:
                    await asyncio.sleep(0.01)
        finally:
            await pubsub.unsubscribe(self.NOTIFY_CHANNEL)
            await pubsub.aclose()

    async def close(self) -> None:
        await self._redis.aclose()
