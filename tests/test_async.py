"""Tests for async features: Result.aresult, __await__, .arun(), .amap(),
async worker execution, and heartbeat-based stall detection."""
from __future__ import annotations

import asyncio
import collections
import json
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from pyfuse import trace
from pyfuse.core.errors import RemoteError, TaskStalled
from pyfuse.core.models import FunctionNode, ImportInfo
from pyfuse.core.task import Task
from pyfuse.graph.store import Store
from pyfuse.worker.backends.base import Backend
from pyfuse.worker.result import Result, ResultEnvelope, ResultWaiter
from pyfuse.worker.worker import Worker
import pyfuse.worker.remote as _remote


# ---------------------------------------------------------------------------
# InMemoryBackend with heartbeat support
# ---------------------------------------------------------------------------


class InMemoryBackend(Backend):
    def __init__(self) -> None:
        self._tasks: collections.deque[str] = collections.deque()
        self._results: dict[str, collections.deque[str]] = {}
        self._heartbeats: dict[str, float] = {}
        self._stop = threading.Event()
        self._notify_queue: queue.Queue[str] = queue.Queue()

    def submit(self, task_json: str) -> None:
        self._tasks.append(task_json)

    def listen(self) -> Iterator[str]:
        while not self._stop.is_set():
            if self._tasks:
                yield self._tasks.popleft()
            else:
                break

    def send_result(self, task_id: str, result_json: str) -> None:
        self._results.setdefault(task_id, collections.deque()).append(result_json)

    def get_result(self, task_id: str, timeout: float | None = None) -> str:
        q = self._results.get(task_id)
        if q:
            return q.popleft()
        raise TimeoutError(f"No result for {task_id}")

    def try_get_result(self, task_id: str) -> str | None:
        q = self._results.get(task_id)
        if q:
            return q.popleft()
        return None

    def send_heartbeat(self, task_id: str) -> None:
        self._heartbeats[task_id] = time.time()

    def get_heartbeat(self, task_id: str) -> float | None:
        return self._heartbeats.get(task_id)

    def get_heartbeats(self, task_ids: list[str]) -> dict[str, float | None]:
        return {tid: self._heartbeats.get(tid) for tid in task_ids}

    def notify_result(self, task_id: str) -> None:
        self._notify_queue.put(task_id)

    def subscribe_results(self) -> Iterator[str]:
        while not self._stop.is_set():
            try:
                task_id = self._notify_queue.get(timeout=0.5)
                yield task_id
            except queue.Empty:
                continue

    def close(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(
    name: str,
    source: str | None = None,
    imports: list[ImportInfo] | None = None,
) -> FunctionNode:
    return FunctionNode(
        qualified_name=f"m.{name}",
        name=name,
        module="m",
        source=source or f"def {name}():\n    pass\n",
        imports=imports or [],
        dependencies=[],
        owner_class=None,
        closure_vars={},
        closure_func_refs={},
    )


def _async_store() -> tuple[Store, str]:
    """Store with an async function."""
    node = _node("af", source="async def af(x):\n    return x + 10\n")
    store = Store()
    h = store.put(node)
    store.set_ref("m.af", h)
    return store, store.to_json()


def _sync_store() -> tuple[Store, str]:
    """Store with a sync function."""
    node = _node("f", source="def f(x):\n    return x * 2\n")
    store = Store()
    h = store.put(node)
    store.set_ref("m.f", h)
    return store, store.to_json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_backend() -> Iterator[None]:
    yield
    if _remote._active_backend is not None:
        ResultWaiter.stop_for(_remote._active_backend)
    _remote._active_backend = None
    _remote._atexit_registered = False


@pytest.fixture
def backend() -> Iterator[InMemoryBackend]:
    b = InMemoryBackend()
    yield b
    ResultWaiter.stop_for(b)
    b.close()


# ---------------------------------------------------------------------------
# Result.aresult
# ---------------------------------------------------------------------------


class TestAresult:
    def test_aresult_success(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t1", 42)
        backend.send_result("t1", env.to_json())

        future = Result("t1", backend)

        async def check() -> None:
            value = await future.aresult()
            assert value == 42

        asyncio.run(check())

    def test_aresult_raises_on_error(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.failure("t2", ValueError("bad"))
        backend.send_result("t2", env.to_json())

        future = Result("t2", backend)

        async def check() -> None:
            with pytest.raises(RemoteError, match="ValueError: bad"):
                await future.aresult()

        asyncio.run(check())

    def test_aresult_uses_cached_envelope(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t3", "cached")
        backend.send_result("t3", env.to_json())

        future = Result("t3", backend)

        async def check() -> None:
            assert await future.aresult() == "cached"
            # Second call uses cached envelope (queue is now empty)
            assert await future.aresult() == "cached"

        asyncio.run(check())

    def test_aresult_timeout(self, backend: InMemoryBackend) -> None:
        future = Result("missing", backend)

        async def check() -> None:
            with pytest.raises(TimeoutError):
                await future.aresult(timeout=0.05)

        asyncio.run(check())

    def test_aresult_delayed_result(self, backend: InMemoryBackend) -> None:
        """Result arrives after a short delay."""
        future = Result("t4", backend)

        async def deliver() -> None:
            await asyncio.sleep(0.05)
            env = ResultEnvelope.success("t4", 99)
            backend.send_result("t4", env.to_json())
            backend.notify_result("t4")

        async def check() -> None:
            asyncio.create_task(deliver())
            value = await future.aresult(timeout=1.0)
            assert value == 99

        asyncio.run(check())


# ---------------------------------------------------------------------------
# Result.__await__
# ---------------------------------------------------------------------------


class TestResultAwait:
    def test_await_result(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t1", 77)
        backend.send_result("t1", env.to_json())

        future = Result("t1", backend)

        async def check() -> None:
            value = await future
            assert value == 77

        asyncio.run(check())

    def test_await_raises_on_error(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.failure("t2", RuntimeError("boom"))
        backend.send_result("t2", env.to_json())

        future = Result("t2", backend)

        async def check() -> None:
            with pytest.raises(RemoteError, match="RuntimeError: boom"):
                await future

        asyncio.run(check())


# ---------------------------------------------------------------------------
# Worker: async function execution via run()
# ---------------------------------------------------------------------------


class TestWorkerAsyncRun:
    def test_run_async_function_via_task(self) -> None:
        """Worker.run() transparently handles async def functions."""
        _, json_str = _async_store()
        task = Task(graph_json=json_str, function_name="af", args=(5,))
        worker = Worker(auto_install=False)
        result = worker.run(task)
        assert result == 15

    def test_execute_async_function(self) -> None:
        """Worker.execute() transparently handles async def functions."""
        _, json_str = _async_store()
        worker = Worker(auto_install=False)
        result = worker.execute(json_str, "af", 5)
        assert result == 15

    def test_run_with_policy_async(self) -> None:
        """run_with_policy handles async functions via run()."""
        _, json_str = _async_store()
        task = Task(graph_json=json_str, function_name="af", args=(5,))
        worker = Worker(auto_install=False)
        result = worker.run_with_policy(task)
        assert result == 15

    def test_run_with_policy_timeout_async(self) -> None:
        """run_with_policy with timeout handles async functions."""
        _, json_str = _async_store()
        task = Task(
            graph_json=json_str, function_name="af", args=(5,), timeout=5.0
        )
        worker = Worker(auto_install=False)
        result = worker.run_with_policy(task)
        assert result == 15

    def test_sync_still_works(self) -> None:
        """Sync functions still work normally."""
        _, json_str = _sync_store()
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        worker = Worker(auto_install=False)
        assert worker.run(task) == 42


# ---------------------------------------------------------------------------
# _handle_task with async functions
# ---------------------------------------------------------------------------


class TestHandleTaskAsync:
    def test_handle_async_task(self, backend: InMemoryBackend) -> None:
        """_handle_task correctly processes async functions."""
        from pyfuse import pack

        @trace
        async def async_double(x: int) -> int:
            return x * 2

        task = pack(async_double, 7)
        backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        for task_json in backend.listen():
            _remote._handle_task(worker, backend, task_json)

        raw = backend.get_result(task.task_id)
        env = ResultEnvelope.from_json(raw)
        assert env.status == "ok"
        assert env.result == 14


# ---------------------------------------------------------------------------
# .arun() on traced functions
# ---------------------------------------------------------------------------


class TestArun:
    def test_arun_exists(self) -> None:
        @trace
        def my_func(x: int) -> int:
            return x + 1

        assert hasattr(my_func, "arun")
        assert asyncio.iscoroutinefunction(my_func.arun)

    def test_arun_submits_and_awaits(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        def double(x: int) -> int:
            return x * 2

        async def check() -> None:
            # Start arun as a task; it submits then polls for the result
            task = asyncio.create_task(double.arun(5))
            # Yield control so arun can run and submit
            await asyncio.sleep(0.01)

            # The task should now be submitted
            assert len(backend._tasks) == 1
            task_data = json.loads(backend._tasks[0])
            task_id = task_data["id"]
            env = ResultEnvelope.success(task_id, 10)
            backend.send_result(task_id, env.to_json())
            backend.notify_result(task_id)

            result = await task
            assert result == 10

        asyncio.run(check())

    def test_arun_on_async_func(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        async def async_triple(x: int) -> int:
            return x * 3

        assert hasattr(async_triple, "arun")
        assert asyncio.iscoroutinefunction(async_triple.arun)


# ---------------------------------------------------------------------------
# .amap() on traced functions
# ---------------------------------------------------------------------------


class TestAmap:
    def test_amap_exists(self) -> None:
        @trace
        def my_func(x: int) -> int:
            return x + 1

        assert hasattr(my_func, "amap")
        assert asyncio.iscoroutinefunction(my_func.amap)

    def test_amap_submits_batch(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        def add(a: int, b: int) -> int:
            return a + b

        async def check() -> None:
            task = asyncio.create_task(add.amap([(1, 2), (3, 4), (5, 6)]))
            # Yield so amap can submit all tasks
            await asyncio.sleep(0.01)

            # Three tasks should be submitted
            assert len(backend._tasks) == 3

            # Simulate worker responses
            for task_json in list(backend._tasks):
                task_data = json.loads(task_json)
                task_id = task_data["id"]
                args = task_data["args"]
                env = ResultEnvelope.success(task_id, sum(args))
                backend.send_result(task_id, env.to_json())
                backend.notify_result(task_id)

            results = await task
            assert results == [3, 7, 11]

        asyncio.run(check())


# ---------------------------------------------------------------------------
# asyncio.gather with Result.__await__
# ---------------------------------------------------------------------------


class TestGather:
    def test_gather_multiple_results(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        def square(x: int) -> int:
            return x ** 2

        async def check() -> None:
            f1 = square.run(3)
            f2 = square.run(4)

            # Simulate worker responses
            for i, task_json in enumerate(list(backend._tasks)):
                task_data = json.loads(task_json)
                task_id = task_data["id"]
                val = task_data["args"][0] ** 2
                env = ResultEnvelope.success(task_id, val)
                backend.send_result(task_id, env.to_json())
                backend.notify_result(task_id)

            r1, r2 = await asyncio.gather(f1, f2)
            assert r1 == 9
            assert r2 == 16

        asyncio.run(check())


# ---------------------------------------------------------------------------
# .map() still works (sync)
# ---------------------------------------------------------------------------


class TestMapStillWorks:
    def test_sync_map(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        def inc(x: int) -> int:
            return x + 1

        futures = inc.map([(1,), (2,), (3,)])
        assert len(futures) == 3
        assert all(isinstance(f, Result) for f in futures)


# ---------------------------------------------------------------------------
# Heartbeat: worker side
# ---------------------------------------------------------------------------


class TestWorkerHeartbeat:
    def test_handle_task_sends_heartbeats(self, backend: InMemoryBackend) -> None:
        """_handle_task sends heartbeats while executing."""
        from pyfuse import pack

        @trace
        def slow_add(a: int, b: int) -> int:
            import time as _t
            _t.sleep(0.3)
            return a + b

        task = pack(slow_add, 1, 2)
        backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        for task_json in backend.listen():
            _remote._handle_task(worker, backend, task_json)

        # Worker should have sent heartbeats
        assert task.task_id in backend._heartbeats

        # Result should still be correct
        raw = backend.get_result(task.task_id)
        env = ResultEnvelope.from_json(raw)
        assert env.status == "ok"
        assert env.result == 3

    def test_heartbeat_stops_after_completion(
        self, backend: InMemoryBackend
    ) -> None:
        """Heartbeat thread stops once the task completes."""
        from pyfuse import pack

        @trace
        def quick() -> int:
            return 42

        task = pack(quick)
        backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        for task_json in backend.listen():
            _remote._handle_task(worker, backend, task_json)

        # Record timestamp right after completion
        hb_after = backend._heartbeats.get(task.task_id)

        # Wait a bit — heartbeat should NOT update further
        time.sleep(0.2)
        hb_later = backend._heartbeats.get(task.task_id)
        assert hb_after == hb_later


# ---------------------------------------------------------------------------
# Heartbeat: stall detection (async)
# ---------------------------------------------------------------------------


class TestStallDetectionAsync:
    def test_no_stall_when_heartbeating(self, backend: InMemoryBackend) -> None:
        """aresult doesn't raise when heartbeats keep arriving."""
        future = Result("t1", backend)

        async def check() -> None:
            # Simulate a worker heartbeating and then delivering
            async def worker_sim() -> None:
                for _ in range(3):
                    backend.send_heartbeat("t1")
                    await asyncio.sleep(0.05)
                env = ResultEnvelope.success("t1", 42)
                backend.send_result("t1", env.to_json())
                backend.notify_result("t1")

            asyncio.create_task(worker_sim())
            value = await future.aresult(stall_timeout=1.0)
            assert value == 42

        asyncio.run(check())

    def test_stall_detected_when_heartbeat_stops(
        self, backend: InMemoryBackend
    ) -> None:
        """aresult raises TaskStalled when heartbeats stop."""
        future = Result("t2", backend)

        async def check() -> None:
            # Send one heartbeat then stop
            backend.send_heartbeat("t2")
            await asyncio.sleep(0.01)

            with pytest.raises(TaskStalled, match="stalled"):
                await future.aresult(stall_timeout=0.2)

        asyncio.run(check())

    def test_no_stall_without_any_heartbeat(
        self, backend: InMemoryBackend
    ) -> None:
        """No stall raised if no heartbeat was ever seen (worker hasn't started)."""
        future = Result("t3", backend)

        async def check() -> None:
            # Deliver result after short delay (no heartbeats at all)
            async def deliver() -> None:
                await asyncio.sleep(0.15)
                env = ResultEnvelope.success("t3", 99)
                backend.send_result("t3", env.to_json())
                backend.notify_result("t3")

            asyncio.create_task(deliver())
            # stall_timeout is very short but no heartbeat was ever seen,
            # so stall detection shouldn't trigger
            value = await future.aresult(timeout=1.0, stall_timeout=0.05)
            assert value == 99

        asyncio.run(check())

    def test_stall_timeout_none_disables(
        self, backend: InMemoryBackend
    ) -> None:
        """stall_timeout=None disables stall detection."""
        future = Result("t4", backend)

        async def check() -> None:
            # Send heartbeat, then stop — but stall detection is off
            backend.send_heartbeat("t4")

            # Deliver result after stall would have triggered
            async def deliver() -> None:
                await asyncio.sleep(0.3)
                env = ResultEnvelope.success("t4", 77)
                backend.send_result("t4", env.to_json())
                backend.notify_result("t4")

            asyncio.create_task(deliver())
            value = await future.aresult(timeout=1.0, stall_timeout=None)
            assert value == 77

        asyncio.run(check())

    def test_stall_includes_task_id(self, backend: InMemoryBackend) -> None:
        """TaskStalled message includes the task_id."""
        future = Result("my_task_99", backend)

        async def check() -> None:
            backend.send_heartbeat("my_task_99")
            await asyncio.sleep(0.01)

            with pytest.raises(TaskStalled, match="my_task_99"):
                await future.aresult(stall_timeout=0.15)

        asyncio.run(check())


# ---------------------------------------------------------------------------
# Heartbeat: stall detection (sync)
# ---------------------------------------------------------------------------


class TestStallDetectionSync:
    def test_sync_stall_detection(self, backend: InMemoryBackend) -> None:
        """result(stall_timeout=...) detects stall in sync mode."""
        future = Result("t1", backend)

        # Send one heartbeat then stop
        backend.send_heartbeat("t1")

        with pytest.raises(TaskStalled, match="stalled"):
            future.result(stall_timeout=0.2)

    def test_sync_no_stall_without_param(
        self, backend: InMemoryBackend
    ) -> None:
        """result() without stall_timeout uses blocking get_result."""
        env = ResultEnvelope.success("t2", 55)
        backend.send_result("t2", env.to_json())

        future = Result("t2", backend)
        assert future.result() == 55

    def test_sync_stall_timeout_with_result(
        self, backend: InMemoryBackend
    ) -> None:
        """result(stall_timeout=...) returns normally when result arrives."""
        env = ResultEnvelope.success("t3", 123)
        backend.send_result("t3", env.to_json())

        future = Result("t3", backend)
        assert future.result(stall_timeout=1.0) == 123


# ---------------------------------------------------------------------------
# Backend heartbeat defaults
# ---------------------------------------------------------------------------


class TestBackendHeartbeatDefaults:
    def test_base_send_heartbeat_is_noop(self) -> None:
        """Base Backend.send_heartbeat is a no-op (no error)."""

        class MinimalBackend(Backend):
            def submit(self, task_json: str) -> None: ...
            def listen(self) -> Iterator[str]:
                return iter([])
            def send_result(self, task_id: str, result_json: str) -> None: ...
            def get_result(self, task_id: str, timeout: float | None = None) -> str:
                raise TimeoutError
            def try_get_result(self, task_id: str) -> str | None:
                return None
            def close(self) -> None: ...

        b = MinimalBackend()
        b.send_heartbeat("any_task")  # should not raise
        assert b.get_heartbeat("any_task") is None


# ---------------------------------------------------------------------------
# Notification-based result delivery (ResultWaiter)
# ---------------------------------------------------------------------------


class TestResultWaiterNotification:
    """Tests for the notification-based ResultWaiter architecture."""

    def test_concurrent_aresults_via_single_listener(
        self, backend: InMemoryBackend
    ) -> None:
        """Multiple concurrent aresult() calls share one ResultWaiter."""
        results = [Result(f"t{i}", backend) for i in range(5)]

        async def check() -> None:
            tasks = [asyncio.create_task(r.aresult(timeout=2.0)) for r in results]
            # Let tasks register with the waiter
            await asyncio.sleep(0.05)

            # Deliver all results via notification
            for i in range(5):
                env = ResultEnvelope.success(f"t{i}", i * 10)
                backend.send_result(f"t{i}", env.to_json())
                backend.notify_result(f"t{i}")

            values = await asyncio.gather(*tasks)
            assert values == [0, 10, 20, 30, 40]

        asyncio.run(check())

    def test_result_arrives_before_aresult(
        self, backend: InMemoryBackend
    ) -> None:
        """aresult() returns immediately when result is already stored."""
        env = ResultEnvelope.success("early", 42)
        backend.send_result("early", env.to_json())

        future = Result("early", backend)

        async def check() -> None:
            value = await future.aresult(timeout=1.0)
            assert value == 42

        asyncio.run(check())

    def test_result_arrives_between_register_and_wait(
        self, backend: InMemoryBackend
    ) -> None:
        """Race condition: result arrives after try_get but before await."""
        future = Result("race", backend)

        async def check() -> None:
            # Start aresult as a task
            task = asyncio.create_task(future.aresult(timeout=2.0))
            # Deliver result immediately
            await asyncio.sleep(0.01)
            env = ResultEnvelope.success("race", "won")
            backend.send_result("race", env.to_json())
            backend.notify_result("race")

            value = await task
            assert value == "won"

        asyncio.run(check())

    def test_waiter_is_singleton_per_backend(
        self, backend: InMemoryBackend
    ) -> None:
        """ResultWaiter.for_backend returns the same instance."""
        w1 = ResultWaiter.for_backend(backend)
        w2 = ResultWaiter.for_backend(backend)
        assert w1 is w2

    def test_waiter_cleanup_on_disconnect(
        self, backend: InMemoryBackend
    ) -> None:
        """ResultWaiter.stop_for removes the waiter from the backend."""
        _ = ResultWaiter.for_backend(backend)
        assert getattr(backend, "_result_waiter", None) is not None
        ResultWaiter.stop_for(backend)
        assert getattr(backend, "_result_waiter", None) is None

    def test_sync_result_with_stall_timeout_uses_waiter(
        self, backend: InMemoryBackend
    ) -> None:
        """Sync result(stall_timeout=...) resolves via ResultWaiter."""
        future = Result("sync_w", backend)

        def deliver() -> None:
            time.sleep(0.1)
            env = ResultEnvelope.success("sync_w", 77)
            backend.send_result("sync_w", env.to_json())
            backend.notify_result("sync_w")

        t = threading.Thread(target=deliver)
        t.start()
        value = future.result(timeout=2.0, stall_timeout=5.0)
        t.join()
        assert value == 77


# ---------------------------------------------------------------------------
# Fallback polling for backends without subscribe_results
# ---------------------------------------------------------------------------


class TestFallbackPolling:
    def test_backend_without_subscribe_falls_back(self) -> None:
        """Backend that raises NotImplementedError falls back to polling."""

        class NoSubBackend(Backend):
            def __init__(self) -> None:
                self._results: dict[str, str] = {}

            def submit(self, task_json: str) -> None: ...
            def listen(self) -> Iterator[str]:
                return iter([])
            def send_result(self, task_id: str, result_json: str) -> None:
                self._results[task_id] = result_json
            def get_result(self, task_id: str, timeout: float | None = None) -> str:
                r = self._results.get(task_id)
                if r:
                    return r
                raise TimeoutError
            def try_get_result(self, task_id: str) -> str | None:
                return self._results.pop(task_id, None)
            def close(self) -> None: ...
            # subscribe_results not implemented — uses base's NotImplementedError

        b = NoSubBackend()
        env = ResultEnvelope.success("fb1", 99)
        b.send_result("fb1", env.to_json())

        future = Result("fb1", b)

        async def check() -> None:
            value = await future.aresult(timeout=2.0)
            assert value == 99

        try:
            asyncio.run(check())
        finally:
            ResultWaiter.stop_for(b)

    def test_fallback_delayed_result(self) -> None:
        """Fallback polling picks up delayed results."""

        class NoSubBackend(Backend):
            def __init__(self) -> None:
                self._results: dict[str, str] = {}

            def submit(self, task_json: str) -> None: ...
            def listen(self) -> Iterator[str]:
                return iter([])
            def send_result(self, task_id: str, result_json: str) -> None:
                self._results[task_id] = result_json
            def get_result(self, task_id: str, timeout: float | None = None) -> str:
                r = self._results.get(task_id)
                if r:
                    return r
                raise TimeoutError
            def try_get_result(self, task_id: str) -> str | None:
                return self._results.pop(task_id, None)
            def close(self) -> None: ...

        b = NoSubBackend()
        future = Result("fb2", b)

        async def check() -> None:
            async def deliver() -> None:
                await asyncio.sleep(0.2)
                env = ResultEnvelope.success("fb2", "delayed")
                b.send_result("fb2", env.to_json())

            asyncio.create_task(deliver())
            value = await future.aresult(timeout=2.0)
            assert value == "delayed"

        try:
            asyncio.run(check())
        finally:
            ResultWaiter.stop_for(b)


# ---------------------------------------------------------------------------
# Batch heartbeat stall detection via ResultWaiter
# ---------------------------------------------------------------------------


class TestBatchHeartbeatStall:
    def test_batch_heartbeat_stall(self, backend: InMemoryBackend) -> None:
        """ResultWaiter's heartbeat monitor detects stall via batch fetch."""
        future = Result("hb1", backend)

        # Send one heartbeat so stall detection can trigger
        backend.send_heartbeat("hb1")

        async def check() -> None:
            with pytest.raises(TaskStalled, match="stalled"):
                await future.aresult(stall_timeout=0.3)

        asyncio.run(check())

    def test_batch_heartbeat_no_false_positive(
        self, backend: InMemoryBackend
    ) -> None:
        """Continuous heartbeats prevent false stall detection."""
        future = Result("hb2", backend)

        async def check() -> None:
            async def heartbeat_and_deliver() -> None:
                for _ in range(5):
                    backend.send_heartbeat("hb2")
                    await asyncio.sleep(0.1)
                env = ResultEnvelope.success("hb2", "alive")
                backend.send_result("hb2", env.to_json())
                backend.notify_result("hb2")

            asyncio.create_task(heartbeat_and_deliver())
            value = await future.aresult(stall_timeout=2.0)
            assert value == "alive"

        asyncio.run(check())
