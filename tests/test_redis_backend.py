import asyncio

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from offwork.worker.backends.redis import RedisBackend


class _FakeRedis:
    def __init__(self) -> None:
        self.listen_calls = 0
        self.result_calls = 0

    async def blpop(self, key: str, timeout: int | None = None) -> tuple[str, str] | None:
        if key == "offwork:tasks":
            self.listen_calls += 1
            if self.listen_calls == 1:
                raise RedisTimeoutError("transient read timeout")
            return (key, "task-json")
        self.result_calls += 1
        raise RedisTimeoutError("read timeout")

    async def aclose(self) -> None:
        return


@pytest.mark.asyncio
async def test_listen_retries_after_redis_timeout() -> None:
    backend = RedisBackend()
    backend._redis = _FakeRedis()

    stream = backend.listen()
    item = await anext(stream)

    assert item == "task-json"


@pytest.mark.asyncio
async def test_get_result_translates_redis_timeout() -> None:
    backend = RedisBackend()
    backend._redis = _FakeRedis()

    with pytest.raises(TimeoutError, match="Timed out waiting for result"):
        await backend.get_result("task-1", timeout=0.01)


@pytest.mark.asyncio
async def test_get_result_returns_payload() -> None:
    backend = RedisBackend()

    class _ResultRedis:
        async def blpop(self, key: str, timeout: int | None = None) -> tuple[str, bytes]:
            return (key, b"ok")

        async def aclose(self) -> None:
            return

    backend._redis = _ResultRedis()

    result = await backend.get_result("task-2", timeout=1)
    assert result == "ok"
