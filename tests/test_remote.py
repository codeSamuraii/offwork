"""Tests for the remote execution API: Backend, ResultEnvelope, Result, connect/disconnect/serve."""

import asyncio
import atexit
import collections
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from offwork import pack
import offwork
from offwork.core.errors import RemoteError
from offwork.core.task import Task, _TaskEncoder
from offwork.worker.backends.base import Backend
from offwork.worker.result import Result, ResultEnvelope, Stream
from offwork.worker.worker import Worker
import offwork.worker.remote as _remote


def _encode_yield(value: Any) -> str:
    """Encode a yielded value the way the worker does before send_yield."""
    return json.dumps(value, cls=_TaskEncoder)


# ---------------------------------------------------------------------------
# InMemoryBackend for testing (no Redis required)
# ---------------------------------------------------------------------------


class InMemoryBackend(Backend):
    """In-memory async backend for testing."""

    def __init__(self) -> None:
        self._tasks: collections.deque[str] = collections.deque()
        self._results: dict[str, collections.deque[str]] = {}
        self._yields: dict[str, list[str]] = {}
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

    async def send_yield(self, task_id: str, seq: int, value_json: str) -> None:
        buf = self._yields.setdefault(task_id, [])
        while len(buf) <= seq:
            buf.append("")
        buf[seq] = value_json

    async def get_yields(
        self, task_id: str, after_seq: int = -1, timeout: float | None = None,
    ) -> list[tuple[int, str]]:
        buf = self._yields.get(task_id, [])
        out = [
            (i, buf[i])
            for i in range(after_seq + 1, len(buf))
            if buf[i] != ""
        ]
        if not out and timeout:
            await asyncio.sleep(min(timeout, 0.01))
        return out

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

    def test_stream_complete_roundtrip(self) -> None:
        env = ResultEnvelope.stream_complete("t6", 3)
        assert env.status == "ok"
        assert env.stream_yields == 3
        assert env.result is None

        restored = ResultEnvelope.from_json(env.to_json())
        assert restored.status == "ok"
        assert restored.stream_yields == 3
        assert restored.result is None

    def test_stream_complete_json_excludes_result(self) -> None:
        env = ResultEnvelope.stream_complete("t7", 0)
        data = json.loads(env.to_json())
        assert data == {
            "task_id": "t7",
            "status": "ok",
            "stream_yields": 0,
        }
        assert "result" not in data


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
        assert future.done() is False

    @pytest.mark.asyncio
    async def test_done_true_when_result_available(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t5", True)
        await backend.send_result("t5", env.to_json())

        future = Result("t5", backend)
        assert future.done() is False  # not yet fetched from backend
        await future.check()           # poll backend, populate cache
        assert future.done() is True
        assert await future.result(stall_timeout=None) is True

    @pytest.mark.asyncio
    async def test_timeout_raises(self, backend: InMemoryBackend) -> None:
        future = Result("missing", backend)
        with pytest.raises(TimeoutError):
            await future.result(timeout=0.01, stall_timeout=None)


# ---------------------------------------------------------------------------
# Stream (async-generator client handle)
# ---------------------------------------------------------------------------


async def _seed_stream(
    backend: InMemoryBackend, task_id: str, values: list[Any]
) -> None:
    """Populate the backend as a finished streaming task would."""
    for seq, value in enumerate(values):
        await backend.send_yield(task_id, seq, _encode_yield(value))
    env = ResultEnvelope.stream_complete(task_id, len(values))
    await backend.send_result(task_id, env.to_json())


class TestStream:
    @pytest.mark.asyncio
    async def test_yields_values_in_order(self, backend: InMemoryBackend) -> None:
        await _seed_stream(backend, "s1", [0, 10, 20])

        stream = Stream("s1", backend)
        out = [value async for value in stream]
        assert out == [0, 10, 20]

    @pytest.mark.asyncio
    async def test_empty_stream(self, backend: InMemoryBackend) -> None:
        await _seed_stream(backend, "s2", [])

        stream = Stream("s2", backend)
        out = [value async for value in stream]
        assert out == []

    @pytest.mark.asyncio
    async def test_preserves_sentinel_encoded_values(
        self, backend: InMemoryBackend
    ) -> None:
        await _seed_stream(backend, "s3", [b"bytes", {"k": 1}])

        stream = Stream("s3", backend)
        out = [value async for value in stream]
        assert out == [b"bytes", {"k": 1}]

    @pytest.mark.asyncio
    async def test_error_terminal_raises(self, backend: InMemoryBackend) -> None:
        await backend.send_yield("s4", 0, _encode_yield(1))
        env = ResultEnvelope.failure("s4", ValueError("boom"))
        await backend.send_result("s4", env.to_json())

        stream = Stream("s4", backend)
        out: list[Any] = []
        with pytest.raises(RemoteError, match="ValueError: boom"):
            async for value in stream:
                out.append(value)
        assert out == [1]

    @pytest.mark.asyncio
    async def test_gap_in_sequence_raises(self, backend: InMemoryBackend) -> None:
        # seq 0 then seq 2 (1 missing), with a terminal envelope of 3 yields
        await backend.send_yield("s5", 0, _encode_yield("a"))
        await backend.send_yield("s5", 2, _encode_yield("c"))
        env = ResultEnvelope.stream_complete("s5", 3)
        await backend.send_result("s5", env.to_json())

        stream = Stream("s5", backend)
        with pytest.raises(RuntimeError, match="Stream gap"):
            async for _ in stream:
                pass


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


class TestSubmitMethod:
    def test_traced_function_has_submit(self) -> None:
        @offwork.task
        def my_func(x: int) -> int:
            return x + 1

        assert hasattr(my_func, "submit")
        assert asyncio.iscoroutinefunction(my_func.submit)

    @pytest.mark.asyncio
    async def test_start_without_backend_raises(self) -> None:
        @offwork.task
        def my_func(x: int) -> int:
            return x + 1

        with pytest.raises(RuntimeError, match="No backend connected"):
            await my_func.submit(5)

    @pytest.mark.asyncio
    async def test_start_submits_to_backend(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @offwork.task
        def double(x: int) -> int:
            return x * 2

        future = await double.submit(7)
        assert isinstance(future, Result)
        assert len(backend._tasks) == 1

        # Verify the submitted task is valid JSON with correct structure
        task_data = json.loads(backend._tasks[0])
        assert "double" in task_data["function"]
        assert task_data["args"] == [7]

    @pytest.mark.asyncio
    async def test_start_returns_fuse_result(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @offwork.task
        def add(a: int, b: int) -> int:
            return a + b

        future = await add.submit(3, 4)
        assert isinstance(future, Result)
        assert future.task_id  # non-empty


class TestRunMethod:
    def test_traced_function_has_run(self) -> None:
        @offwork.task
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
        @offwork.task
        def add_one(x: int) -> int:
            return x + 1

        task = pack(add_one, 5)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        with caplog.at_level(logging.DEBUG, logger="offwork"):
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
        @offwork.task
        def double(x: int) -> int:
            return x * 2

        task1 = pack(double, 3)
        task2 = pack(double, 7)
        await backend.submit(task1.to_json())
        await backend.submit(task2.to_json())

        worker = Worker(auto_install=False)
        with caplog.at_level(logging.DEBUG, logger="offwork"):
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
        @offwork.task
        def boom() -> None:
            raise RuntimeError("intentional")

        task = pack(boom)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        with caplog.at_level(logging.DEBUG, logger="offwork"):
            async for task_json in backend.listen():
                await _remote._handle_task(worker, backend, task_json)

        out = caplog.text
        assert "\u2717" in out
        assert "RuntimeError" in out
        assert "intentional" in out


# ---------------------------------------------------------------------------
# Streaming (async-generator) tasks through _handle_task
# ---------------------------------------------------------------------------


class TestStreamingTask:
    @pytest.mark.asyncio
    async def test_async_generator_streams_yields(
        self, backend: InMemoryBackend
    ) -> None:
        @offwork.task
        async def counter(n: int) -> Any:
            for i in range(n):
                yield i * 10

        task = pack(counter, 3)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        async for task_json in backend.listen():
            await _remote._handle_task(worker, backend, task_json)

        # Yields landed in the channel, terminal envelope records the count.
        stream = Stream(task.task_id, backend)
        out = [value async for value in stream]
        assert out == [0, 10, 20]

    @pytest.mark.asyncio
    async def test_async_generator_terminal_envelope(
        self, backend: InMemoryBackend
    ) -> None:
        @offwork.task
        async def gen() -> Any:
            yield "a"
            yield "b"

        task = pack(gen)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        async for task_json in backend.listen():
            await _remote._handle_task(worker, backend, task_json)

        raw = await backend.get_result(task.task_id)
        env = ResultEnvelope.from_json(raw)
        assert env.status == "ok"
        assert env.stream_yields == 2

    @pytest.mark.asyncio
    async def test_async_generator_error_mid_stream(
        self, backend: InMemoryBackend
    ) -> None:
        @offwork.task
        async def flaky() -> Any:
            yield 1
            raise RuntimeError("mid-stream")

        task = pack(flaky)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        async for task_json in backend.listen():
            await _remote._handle_task(worker, backend, task_json)

        stream = Stream(task.task_id, backend)
        out: list[Any] = []
        with pytest.raises(RemoteError, match="RuntimeError: mid-stream"):
            async for value in stream:
                out.append(value)
        assert out == [1]

    @pytest.mark.asyncio
    async def test_sync_generator_rejected(
        self, backend: InMemoryBackend
    ) -> None:
        @offwork.task
        def sync_gen() -> Any:
            yield 1

        task = pack(sync_gen)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        async for task_json in backend.listen():
            await _remote._handle_task(worker, backend, task_json)

        raw = await backend.get_result(task.task_id)
        env = ResultEnvelope.from_json(raw)
        assert env.status == "error"

    @pytest.mark.asyncio
    async def test_is_streaming_detects_async_generator(
        self, backend: InMemoryBackend
    ) -> None:
        @offwork.task
        async def agen() -> Any:
            yield 1

        @offwork.task
        def plain() -> int:
            return 1

        worker = Worker(auto_install=False)
        assert await worker.is_streaming(Task.from_json(pack(agen).to_json())) is True
        assert await worker.is_streaming(Task.from_json(pack(plain).to_json())) is False


# ---------------------------------------------------------------------------
# serve (worker loop)
# ---------------------------------------------------------------------------


class TestServe:
    @pytest.mark.asyncio
    async def test_serve_executes_tasks(self, backend: InMemoryBackend) -> None:
        """End-to-end: submit a task, run worker, check result."""

        @offwork.task
        def triple(x: int) -> int:
            return x * 3

        task = pack(triple, 5)
        await backend.submit(task.to_json())

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

        @offwork.task
        def failing() -> None:
            raise RuntimeError("intentional")

        task = pack(failing)
        await backend.submit(task.to_json())

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
