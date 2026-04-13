"""Tests for the RabbitMQ backend (requires running RabbitMQ server).

Start RabbitMQ before running::

    docker run -d --name rabbitmq -p 5672:5672 rabbitmq:3
    pytest tests/test_rabbitmq_backend.py

Override the URL with ``PYFUSE_TEST_RABBITMQ_URL``.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest

try:
    import aio_pika

    _HAS_AIO_PIKA = True
except ImportError:
    aio_pika = None  # type: ignore[assignment]
    _HAS_AIO_PIKA = False

pytestmark = pytest.mark.skipif(
    not _HAS_AIO_PIKA, reason="aio-pika not installed",
)

RABBITMQ_URL = os.environ.get("PYFUSE_TEST_RABBITMQ_URL", "amqp://localhost")


async def _rabbitmq_available() -> bool:
    try:
        conn = await aio_pika.connect(RABBITMQ_URL)  # type: ignore[union-attr]
        await conn.close()
        return True
    except Exception:
        return False


@pytest.fixture
async def backend() -> AsyncIterator["RabbitMQBackend"]:
    if not await _rabbitmq_available():
        pytest.skip("RabbitMQ not available")
    from pyfuse.worker.backends.rabbitmq import RabbitMQBackend

    suffix = uuid.uuid4().hex[:8]
    b = RabbitMQBackend(
        RABBITMQ_URL,
        task_queue=f"pyfuse.test.tasks.{suffix}",
    )
    yield b
    await b.close()


if _HAS_AIO_PIKA:
    from pyfuse.worker.backends.rabbitmq import RabbitMQBackend


# ---------------------------------------------------------------------------
# Backend contract
# ---------------------------------------------------------------------------


class TestRabbitMQBackend:
    @pytest.mark.asyncio
    async def test_submit_and_listen(self, backend: RabbitMQBackend) -> None:
        await backend.submit('{"test": 1}')
        await backend.submit('{"test": 2}')

        results: list[str] = []
        async for task_json in backend.listen():
            results.append(task_json)
            if len(results) == 2:
                break

        assert results == ['{"test": 1}', '{"test": 2}']

    @pytest.mark.asyncio
    async def test_send_and_get_result(self, backend: RabbitMQBackend) -> None:
        await backend.send_result("t1", '{"ok": true}')
        result = await backend.get_result("t1")
        assert result == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_try_get_result_none(self, backend: RabbitMQBackend) -> None:
        assert await backend.try_get_result("missing") is None

    @pytest.mark.asyncio
    async def test_try_get_result_success(self, backend: RabbitMQBackend) -> None:
        await backend.send_result("t1", '{"ok": true}')
        result = await backend.try_get_result("t1")
        assert result == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_get_result_timeout(self, backend: RabbitMQBackend) -> None:
        with pytest.raises(TimeoutError):
            await backend.get_result("missing", timeout=0.5)

    @pytest.mark.asyncio
    async def test_heartbeat(self, backend: RabbitMQBackend) -> None:
        assert await backend.get_heartbeat("t1") is None
        await backend.send_heartbeat("t1")
        hb = await backend.get_heartbeat("t1")
        assert hb is not None and hb > 0

    @pytest.mark.asyncio
    async def test_cancellation(self, backend: RabbitMQBackend) -> None:
        assert not await backend.is_cancelled("t1")
        await backend.cancel_task("t1")
        assert await backend.is_cancelled("t1")
        # Peek should not consume -- repeated checks still return True
        assert await backend.is_cancelled("t1")

    @pytest.mark.asyncio
    async def test_progress(self, backend: RabbitMQBackend) -> None:
        assert await backend.get_progress("t1") is None
        await backend.send_progress("t1", '{"current": 50}')
        raw = await backend.get_progress("t1")
        assert raw == '{"current": 50}'

    @pytest.mark.asyncio
    async def test_progress_overwrite(self, backend: RabbitMQBackend) -> None:
        await backend.send_progress("t1", '{"current": 25}')
        await backend.send_progress("t1", '{"current": 75}')
        raw = await backend.get_progress("t1")
        assert raw == '{"current": 75}'

    @pytest.mark.asyncio
    async def test_close(self, backend: RabbitMQBackend) -> None:
        await backend.close()
        assert backend._connection is None
        assert backend._channel is None


# ---------------------------------------------------------------------------
# Result notifications
# ---------------------------------------------------------------------------


class TestNotifications:
    @pytest.mark.asyncio
    async def test_notify_and_subscribe(self, backend: RabbitMQBackend) -> None:
        received: list[str] = []

        async def _subscribe() -> None:
            async for task_id in backend.subscribe_results():
                received.append(task_id)
                if len(received) == 2:
                    break

        sub_task = asyncio.create_task(_subscribe())
        await asyncio.sleep(0.3)  # let subscriber bind

        await backend.notify_result("task_a")
        await backend.notify_result("task_b")

        await asyncio.wait_for(sub_task, timeout=5.0)
        assert received == ["task_a", "task_b"]


# ---------------------------------------------------------------------------
# Integration with connect()
# ---------------------------------------------------------------------------


class TestConnectDispatch:
    @pytest.mark.asyncio
    async def test_connect_amqp(self) -> None:
        if not await _rabbitmq_available():
            pytest.skip("RabbitMQ not available")
        import pyfuse.worker.remote as _remote

        try:
            backend = _remote.connect(RABBITMQ_URL)
            assert isinstance(backend, RabbitMQBackend)
            assert _remote._active_backend is backend
        finally:
            await _remote.disconnect()


# ---------------------------------------------------------------------------
# End-to-end with worker
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_submit_execute_result(self) -> None:
        if not await _rabbitmq_available():
            pytest.skip("RabbitMQ not available")
        from pyfuse import pack, trace
        from pyfuse.core.task import Task
        from pyfuse.worker.result import ResultEnvelope
        from pyfuse.worker.worker import Worker

        @trace
        def add(a: int, b: int) -> int:
            return a + b

        suffix = uuid.uuid4().hex[:8]
        client = RabbitMQBackend(
            RABBITMQ_URL,
            task_queue=f"pyfuse.test.e2e.{suffix}",
        )
        worker_backend = RabbitMQBackend(
            RABBITMQ_URL,
            task_queue=f"pyfuse.test.e2e.{suffix}",
        )

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
            assert env.status == "ok"
            assert env.result == 7
        finally:
            await client.close()
            await worker_backend.close()
