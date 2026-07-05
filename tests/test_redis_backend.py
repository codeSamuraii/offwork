import asyncio

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

import offwork
import uuid
from offwork import pack
from offwork.core.task import Task
from offwork.worker.result import ResultEnvelope
from offwork.worker.worker import Worker
from offwork.worker.backends.redis import RedisBackend
import offwork.worker.remote as _remote

try:
    import redis.asyncio as _redis
    _HAS_REDIS = True
except ImportError:
    _HAS_REDIS = False

import os

REDIS_URL = os.environ.get("OFFWORK_TEST_REDIS_URL", "redis://localhost:6379")


async def _redis_available() -> bool:
    if not _HAS_REDIS:
        return False
    try:
        client = _redis.Redis.from_url(REDIS_URL)
        await client.ping()
        await client.aclose()
        return True
    except Exception:
        return False


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


class TestConnectDispatch:
    @pytest.mark.asyncio
    async def test_connect_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not await _redis_available():
            pytest.skip("Redis not available")

        monkeypatch.delenv("BROKER_URL", raising=False)
        try:
            ctx = _remote.connect(REDIS_URL)
            assert isinstance(ctx.backend, RedisBackend)
            assert _remote._active_backend is ctx.backend
        finally:
            await _remote.disconnect()


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_submit_execute_result(self) -> None:
        if not await _redis_available():
            pytest.skip("Redis not available")

        @offwork.task
        def add(a: int, b: int) -> int:
            return a + b

        suffix = uuid.uuid4().hex[:8]
        queue_key = f"offwork.test.e2e.{suffix}"
        client = RedisBackend(REDIS_URL, queue_key=queue_key)
        worker_backend = RedisBackend(REDIS_URL, queue_key=queue_key)

        try:
            task = pack(add, 3, 4)
            await client.submit(task.to_json())

            worker = Worker(auto_install=False)
            async for task_json in worker_backend.listen():
                t = Task.from_json(task_json)
                try:
                    result = await worker.run(t)
                    env = ResultEnvelope.success(t.task_id, result)
                except Exception as exc:
                    env = ResultEnvelope.failure(t.task_id, exc)
                await worker_backend.send_result(t.task_id, env.to_json())
                break

            raw = await client.get_result(task.task_id)
            env = ResultEnvelope.from_json(raw)
            assert env.status == "ok", f"unexpected envelope: {env!r}"
            assert env.result == 7
        finally:
            await client.close()
            await worker_backend.close()
