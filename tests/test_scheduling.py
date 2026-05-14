"""Tests for task scheduling features."""

import json
import time

import pytest

from away.core.task import Task
from away.worker.schedule import ScheduleHandle
from away.worker.result import ResultEnvelope
from away.worker.backends.local import _Broker


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
