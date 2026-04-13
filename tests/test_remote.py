"""Tests for the remote execution API: Backend, ResultEnvelope, Result, connect/disconnect/serve."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pyfuse.worker.backends.base import Backend
from pyfuse.core.errors import RemoteError
from pyfuse.worker.result import Result, ResultEnvelope
from pyfuse import trace
import pyfuse.worker.remote as _remote


# ---------------------------------------------------------------------------
# InMemoryBackend for testing (no Redis required)
# ---------------------------------------------------------------------------


class InMemoryBackend(Backend):
    """In-memory async backend for testing."""

    def __init__(self) -> None:
        self._tasks: collections.deque[str] = collections.deque()
        self._results: dict[str, collections.deque[str]] = {}
        self._stop = False

    async def submit(self, task_json: str) -> None:
        self._tasks.append(task_json)

    async def listen(self) -> AsyncIterator[str]:  # type: ignore[override]
        while not self._stop:
            if self._tasks:
                yield self._tasks.popleft()
            else:
                break

    async def send_result(self, task_id: str, result_json: str) -> None:
        self._results.setdefault(task_id, collections.deque()).append(result_json)

    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        q = self._results.get(task_id)
        if q:
            return q.popleft()
        raise TimeoutError(f"No result for {task_id}")

    async def try_get_result(self, task_id: str) -> str | None:
        q = self._results.get(task_id)
        if q:
            return q.popleft()
        return None

    async def close(self) -> None:
        self._stop = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_backend() -> AsyncIterator[None]:
    """Ensure no global backend leaks between tests."""
    yield
    _remote._active_backend = None
    _remote._atexit_registered = False


@pytest.fixture
def backend() -> InMemoryBackend:
    return InMemoryBackend()


# ---------------------------------------------------------------------------
# ResultEnvelope
# ---------------------------------------------------------------------------


class TestResultEnvelope:
    def test_success_roundtrip(self) -> None:
        env = ResultEnvelope.success("t1", 42)
        assert env.status == "ok"
        assert env.result == 42
        assert env.error_type is None

        restored = ResultEnvelope.from_json(env.to_json())
        assert restored.task_id == "t1"
        assert restored.status == "ok"
        assert restored.result == 42

    def test_failure_roundtrip(self) -> None:
        try:
            raise ValueError("bad input")
        except ValueError as exc:
            env = ResultEnvelope.failure("t2", exc)

        assert env.status == "error"
        assert env.error_type == "ValueError"
        assert env.error_message == "bad input"
        assert "Traceback" in (env.error_traceback or "")

        restored = ResultEnvelope.from_json(env.to_json())
        assert restored.status == "error"
        assert restored.error_type == "ValueError"
        assert restored.error_message == "bad input"

    def test_success_json_structure(self) -> None:
        env = ResultEnvelope.success("t3", [1, 2, 3])
        data = json.loads(env.to_json())
        assert data == {
            "task_id": "t3",
            "status": "ok",
            "result": [1, 2, 3],
        }

    def test_failure_json_excludes_result(self) -> None:
        env = ResultEnvelope.failure("t4", RuntimeError("oops"))
        data = json.loads(env.to_json())
        assert "result" not in data
        assert data["error_type"] == "RuntimeError"

    def test_from_json_accepts_bytes(self) -> None:
        env = ResultEnvelope.success("t5", "hello")
        raw = env.to_json().encode()
        restored = ResultEnvelope.from_json(raw)
        assert restored.result == "hello"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class TestResult:
    @pytest.mark.asyncio
    async def test_result_success(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t1", 99)
        await backend.send_result("t1", env.to_json())

        future = Result("t1", backend)
        assert future.task_id == "t1"
        assert await future.result(stall_timeout=None) == 99

    @pytest.mark.asyncio
    async def test_result_raises_on_error(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.failure("t2", ValueError("bad"))
        await backend.send_result("t2", env.to_json())

        future = Result("t2", backend)
        with pytest.raises(RemoteError, match="ValueError: bad"):
            await future.result(stall_timeout=None)

    @pytest.mark.asyncio
    async def test_result_caches(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t3", "cached")
        await backend.send_result("t3", env.to_json())

        future = Result("t3", backend)
        assert await future.result(stall_timeout=None) == "cached"
        # Second call should use cached envelope (queue is empty now)
        assert await future.result(stall_timeout=None) == "cached"

    @pytest.mark.asyncio
    async def test_done_false_when_no_result(self, backend: InMemoryBackend) -> None:
        future = Result("t4", backend)
        assert await future.done() is False

    @pytest.mark.asyncio
    async def test_done_true_when_result_available(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t5", True)
        await backend.send_result("t5", env.to_json())

        future = Result("t5", backend)
        assert await future.done() is True
        assert await future.result(stall_timeout=None) is True

    @pytest.mark.asyncio
    async def test_timeout_raises(self, backend: InMemoryBackend) -> None:
        future = Result("missing", backend)
        with pytest.raises(TimeoutError):
            await future.result(timeout=0, stall_timeout=None)


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    def test_connect_unknown_scheme_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown backend scheme"):
            _remote.connect("ftp://localhost")

    @pytest.mark.asyncio
    async def test_disconnect_clears_backend(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend
        await _remote.disconnect()
        assert _remote._active_backend is None

    @pytest.mark.asyncio
    async def test_disconnect_when_none_is_noop(self) -> None:
        _remote._active_backend = None
        await _remote.disconnect()  # should not raise

    def test_get_backend_raises_without_connect(self) -> None:
        with pytest.raises(RuntimeError, match="No backend connected"):
            _remote.get_backend()

    def test_get_backend_returns_active(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend
        assert _remote.get_backend() is backend

    def test_atexit_registered_on_connect(
        self, backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import atexit

        registered: list[Any] = []
        monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))
        monkeypatch.setattr(
            _remote, "_create_backend", lambda *a, **kw: backend
        )
        _remote.connect("redis://localhost")
        assert _remote._sync_disconnect in registered

    def test_atexit_registered_only_once(
        self, backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import atexit

        call_count = 0

        def mock_register(fn: Any) -> None:
            nonlocal call_count
            call_count += 1

        monkeypatch.setattr(atexit, "register", mock_register)
        monkeypatch.setattr(
            _remote, "_create_backend", lambda *a, **kw: backend
        )
        _remote.connect("redis://localhost")
        _remote.connect("redis://localhost")
        assert call_count == 1


# ---------------------------------------------------------------------------
# .run() on traced functions
# ---------------------------------------------------------------------------


class TestStartMethod:
    def test_traced_function_has_start(self) -> None:
        @trace
        def my_func(x: int) -> int:
            return x + 1

        assert hasattr(my_func, "start")
        assert asyncio.iscoroutinefunction(my_func.start)

    @pytest.mark.asyncio
    async def test_start_without_backend_raises(self) -> None:
        @trace
        def my_func(x: int) -> int:
            return x + 1

        with pytest.raises(RuntimeError, match="No backend connected"):
            await my_func.start(5)

    @pytest.mark.asyncio
    async def test_start_submits_to_backend(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        def double(x: int) -> int:
            return x * 2

        future = await double.start(7)
        assert isinstance(future, Result)
        assert len(backend._tasks) == 1

        # Verify the submitted task is valid JSON with correct structure
        task_data = json.loads(backend._tasks[0])
        assert "double" in task_data["function"]
        assert task_data["args"] == [7]

    @pytest.mark.asyncio
    async def test_start_returns_fuse_result(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        def add(a: int, b: int) -> int:
            return a + b

        future = await add.start(3, 4)
        assert isinstance(future, Result)
        assert future.task_id  # non-empty


class TestRunMethod:
    def test_traced_function_has_run(self) -> None:
        @trace
        def my_func(x: int) -> int:
            return x + 1

        assert hasattr(my_func, "run")
        assert asyncio.iscoroutinefunction(my_func.run)


# ---------------------------------------------------------------------------
# _handle_task output
# ---------------------------------------------------------------------------


class TestHandleTaskOutput:
    @pytest.mark.asyncio
    async def test_output_build_on_first_call(
        self,
        backend: InMemoryBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from pyfuse import pack
        from pyfuse.worker.worker import Worker

        @trace
        def add_one(x: int) -> int:
            return x + 1

        task = pack(add_one, 5)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        with caplog.at_level(logging.DEBUG, logger="pyfuse"):
            async for task_json in backend.listen():
                await _remote._handle_task(worker, backend, task_json)

        out = caplog.text
        assert "build" in out
        assert "ms" in out
        assert "\u2713" in out

    @pytest.mark.asyncio
    async def test_output_cached_on_second_call(
        self,
        backend: InMemoryBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from pyfuse import pack
        from pyfuse.worker.worker import Worker

        @trace
        def double(x: int) -> int:
            return x * 2

        task1 = pack(double, 3)
        task2 = pack(double, 7)
        await backend.submit(task1.to_json())
        await backend.submit(task2.to_json())

        worker = Worker(auto_install=False)
        with caplog.at_level(logging.DEBUG, logger="pyfuse"):
            async for task_json in backend.listen():
                await _remote._handle_task(worker, backend, task_json)

        records = [r.message for r in caplog.records if "\u2713" in r.message]
        assert len(records) == 2
        assert "build" in records[0]
        assert "cached" in records[1]

    @pytest.mark.asyncio
    async def test_output_error(
        self,
        backend: InMemoryBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from pyfuse import pack
        from pyfuse.worker.worker import Worker

        @trace
        def boom() -> None:
            raise RuntimeError("intentional")

        task = pack(boom)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        with caplog.at_level(logging.DEBUG, logger="pyfuse"):
            async for task_json in backend.listen():
                await _remote._handle_task(worker, backend, task_json)

        out = caplog.text
        assert "\u2717" in out
        assert "RuntimeError" in out
        assert "intentional" in out


# ---------------------------------------------------------------------------
# serve (worker loop)
# ---------------------------------------------------------------------------


class TestServe:
    @pytest.mark.asyncio
    async def test_serve_executes_tasks(self, backend: InMemoryBackend) -> None:
        """End-to-end: submit a task, run worker, check result."""
        from pyfuse import pack

        @trace
        def triple(x: int) -> int:
            return x * 3

        task = pack(triple, 5)
        await backend.submit(task.to_json())

        from pyfuse.worker.worker import Worker
        from pyfuse.core.task import Task

        worker = Worker(auto_install=False)
        async for task_json in backend.listen():
            t = Task.from_json(task_json)
            try:
                result = await worker.run(t)
                env = ResultEnvelope.success(t.task_id, result)
            except Exception as exc:
                env = ResultEnvelope.failure(t.task_id, exc)
            await backend.send_result(t.task_id, env.to_json())

        # Check the result
        raw = await backend.get_result(task.task_id)
        env = ResultEnvelope.from_json(raw)
        assert env.status == "ok"
        assert env.result == 15

    @pytest.mark.asyncio
    async def test_serve_handles_errors(self, backend: InMemoryBackend) -> None:
        """Tasks that raise should produce error envelopes, not crash."""
        from pyfuse import pack

        @trace
        def failing() -> None:
            raise RuntimeError("intentional")

        task = pack(failing)
        await backend.submit(task.to_json())

        from pyfuse.worker.worker import Worker
        from pyfuse.core.task import Task

        worker = Worker(auto_install=False)

        async for task_json in backend.listen():
            t = Task.from_json(task_json)
            try:
                result = await worker.run(t)
                env = ResultEnvelope.success(t.task_id, result)
            except Exception as exc:
                env = ResultEnvelope.failure(t.task_id, exc)
            await backend.send_result(t.task_id, env.to_json())

        raw = await backend.get_result(task.task_id)
        env = ResultEnvelope.from_json(raw)
        assert env.status == "error"
        assert env.error_type == "RuntimeError"
        assert "intentional" in (env.error_message or "")


# ---------------------------------------------------------------------------
# InMemoryBackend contract
# ---------------------------------------------------------------------------


class TestInMemoryBackend:
    @pytest.mark.asyncio
    async def test_submit_and_listen(self, backend: InMemoryBackend) -> None:
        await backend.submit('{"test": 1}')
        await backend.submit('{"test": 2}')
        results = [t async for t in backend.listen()]
        assert results == ['{"test": 1}', '{"test": 2}']

    @pytest.mark.asyncio
    async def test_send_and_get_result(self, backend: InMemoryBackend) -> None:
        await backend.send_result("t1", '{"ok": true}')
        assert await backend.get_result("t1") == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_try_get_result_none(self, backend: InMemoryBackend) -> None:
        assert await backend.try_get_result("missing") is None

    @pytest.mark.asyncio
    async def test_try_get_result_success(self, backend: InMemoryBackend) -> None:
        await backend.send_result("t1", '{"ok": true}')
        assert await backend.try_get_result("t1") == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_close(self, backend: InMemoryBackend) -> None:
        await backend.close()
        assert [t async for t in backend.listen()] == []
