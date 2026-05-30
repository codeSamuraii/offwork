"""Tests for task scheduling features."""

import json
import time

import pytest

from offwork.core.task import Task
from offwork.worker.schedule import ScheduleHandle
from offwork.worker.result import ResultEnvelope
from offwork.worker.backends.local import _Broker


class TestTaskSchedulingFields:
    def test_scheduled_at_roundtrip(self) -> None:
        ts = time.time() + 60
        task = Task(
            graph_json="{}", function_name="f", scheduled_at=ts,
        )
        restored = Task.from_json(task.to_json())
        assert restored.scheduled_at == pytest.approx(ts)

    def test_recur_interval_roundtrip(self) -> None:
        task = Task(
            graph_json="{}",
            function_name="f",
            recur_interval=30.0,
            schedule_id="sched123",
        )
        restored = Task.from_json(task.to_json())
        assert restored.recur_interval == 30.0
        assert restored.schedule_id == "sched123"

    def test_scheduling_fields_absent_by_default(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        assert task.scheduled_at is None
        assert task.recur_interval is None
        assert task.schedule_id is None

    def test_scheduling_fields_not_in_json_when_none(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        data = json.loads(task.to_json())
        assert "scheduled_at" not in data
        assert "recur_interval" not in data
        assert "schedule_id" not in data

    def test_recur_bounds_roundtrip(self) -> None:
        task = Task(
            graph_json="{}",
            function_name="f",
            recur_interval=10.0,
            recur_deadline=time.time() + 3600,
            recur_remaining=5,
            schedule_id="s",
        )
        restored = Task.from_json(task.to_json())
        assert restored.recur_deadline == pytest.approx(task.recur_deadline)
        assert restored.recur_remaining == 5

    def test_all_scheduling_fields_roundtrip(self) -> None:
        ts = time.time() + 120
        task = Task(
            graph_json='{"x": 1}',
            function_name="mod.func",
            args=(1, 2),
            kwargs={"k": "v"},
            scheduled_at=ts,
            recur_interval=60.0,
            schedule_id="abc123",
            throttle=10.0,
        )
        restored = Task.from_json(task.to_json())
        assert restored.scheduled_at == pytest.approx(ts)
        assert restored.recur_interval == 60.0
        assert restored.schedule_id == "abc123"
        assert restored.throttle == 10.0
        assert restored.args == (1, 2)
        assert restored.kwargs == {"k": "v"}


class TestScheduleHandle:
    @pytest.mark.asyncio
    async def test_cancel_calls_backend(self) -> None:
        class MockBackend:
            def __init__(self) -> None:
                self.cancelled: list[str] = []

            async def cancel_schedule(self, schedule_id: str) -> None:
                self.cancelled.append(schedule_id)

        backend = MockBackend()
        handle = ScheduleHandle("sched_abc", backend)  # type: ignore[arg-type]
        assert handle.schedule_id == "sched_abc"

        await handle.cancel()
        assert backend.cancelled == ["sched_abc"]

    def test_repr(self) -> None:
        handle = ScheduleHandle("x", None)  # type: ignore[arg-type]
        assert "x" in repr(handle)


class TestRunEveryValidation:
    @pytest.mark.asyncio
    async def test_default_run_for_is_one_hour(self) -> None:
        import offwork
        from offwork.worker.backends.base import Backend

        class FakeBackend(Backend):  # type: ignore[misc]
            def __init__(self) -> None:
                self.submitted: list[str] = []

            async def submit(self, task_json: str) -> None:
                self.submitted.append(task_json)

            async def listen(self, *a, **k): yield  # pragma: no cover

            async def send_result(self, *a, **k) -> None: ...
            async def get_result(self, *a, **k) -> str: return ""
            async def try_get_result(self, *a, **k): return None
            async def close(self) -> None: ...

        @offwork.task
        def tick(n: int) -> int: return n

        backend = FakeBackend()
        await tick.run_every(1.0, 42, backend=backend)
        data = json.loads(backend.submitted[0])
        # Default cap is 1 hour deadline, no max_runs.
        assert data["recur_deadline"] - data["scheduled_at"] == pytest.approx(3600.0)
        assert "recur_remaining" not in data

    @pytest.mark.asyncio
    async def test_max_runs_disables_default_run_for(self) -> None:
        import offwork
        from offwork.worker.backends.base import Backend

        class FakeBackend(Backend):  # type: ignore[misc]
            def __init__(self) -> None: self.submitted: list[str] = []
            async def submit(self, task_json: str) -> None: self.submitted.append(task_json)
            async def listen(self, *a, **k): yield  # pragma: no cover
            async def send_result(self, *a, **k) -> None: ...
            async def get_result(self, *a, **k) -> str: return ""
            async def try_get_result(self, *a, **k): return None
            async def close(self) -> None: ...

        @offwork.task
        def tick2(n: int) -> int: return n

        backend = FakeBackend()
        await tick2.run_every(1.0, 7, max_runs=3, backend=backend)
        data = json.loads(backend.submitted[0])
        assert data["recur_remaining"] == 3
        assert "recur_deadline" not in data

    @pytest.mark.asyncio
    async def test_invalid_run_for_rejected(self) -> None:
        import offwork

        @offwork.task
        def tick3(n: int) -> int: return n

        with pytest.raises(ValueError):
            await tick3.run_every(1.0, 1, run_for=0)
        with pytest.raises(ValueError):
            await tick3.run_every(1.0, 1, max_runs=0)


class TestBrokerScheduleCancellation:
    def test_schedule_cancel_and_check(self) -> None:
        broker = _Broker()
        resp = broker._dispatch({"op": "schedule_check", "schedule_id": "s1"})
        assert resp["data"] is False

        broker._dispatch({"op": "schedule_cancel", "schedule_id": "s1"})
        resp = broker._dispatch({"op": "schedule_check", "schedule_id": "s1"})
        assert resp["data"] is True

    def test_schedule_unknown_remains_false(self) -> None:
        broker = _Broker()
        broker._dispatch({"op": "schedule_cancel", "schedule_id": "s1"})
        resp = broker._dispatch({"op": "schedule_check", "schedule_id": "s2"})
        assert resp["data"] is False
