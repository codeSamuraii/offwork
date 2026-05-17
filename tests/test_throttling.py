"""Tests for throttling features."""

import json
import time
from datetime import timedelta

import pytest

from pyfuse.core.task import Task
from pyfuse.core.errors import ThrottleError
from pyfuse.graph.decorator import trace
from pyfuse.worker.result import ResultEnvelope, Result
from pyfuse.worker.backends.local import _Broker


class TestTaskThrottleField:
    def test_throttle_roundtrip(self) -> None:
        task = Task(graph_json="{}", function_name="f", throttle=30.0)
        restored = Task.from_json(task.to_json())
        assert restored.throttle == 30.0

    def test_throttle_absent_by_default(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        assert task.throttle is None

    def test_throttle_not_in_json_when_none(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        data = json.loads(task.to_json())
        assert "throttle" not in data


class TestResultEnvelopeThrottled:
    def test_throttled_status(self) -> None:
        env = ResultEnvelope.throttled("t1")
        assert env.status == "throttled"
        assert env.task_id == "t1"

    def test_throttled_json_roundtrip(self) -> None:
        env = ResultEnvelope.throttled("t1")
        raw = env.to_json()
        restored = ResultEnvelope.from_json(raw)
        assert restored.status == "throttled"
        assert restored.task_id == "t1"


class TestResultUnwrapThrottled:
    @pytest.mark.asyncio
    async def test_throttle_error_raised(self) -> None:
        class MockBackend:
            async def get_result(self, task_id: str, timeout: float | None = None) -> str:
                return ResultEnvelope.throttled(task_id).to_json()

            async def try_get_result(self, task_id: str) -> str | None:
                return ResultEnvelope.throttled(task_id).to_json()

            async def get_heartbeat(self, task_id: str) -> float | None:
                return None

        result = Result("t1", MockBackend())  # type: ignore[arg-type]
        with pytest.raises(ThrottleError):
            await result.result(stall_timeout=None)

    @pytest.mark.asyncio
    async def test_throttled_status_string(self) -> None:
        class MockBackend:
            async def try_get_result(self, task_id: str) -> str | None:
                return ResultEnvelope.throttled(task_id).to_json()

        result = Result("t1", MockBackend())  # type: ignore[arg-type]
        assert await result.status() == "throttled"


class TestBrokerThrottle:
    def test_check_throttle_allowed_by_default(self) -> None:
        broker = _Broker()
        resp = broker._dispatch({"op": "throttle_check", "function_name": "fn"})
        assert resp["data"] is True

    def test_record_then_check_blocked(self) -> None:
        broker = _Broker()
        broker._dispatch({
            "op": "throttle_record",
            "function_name": "fn",
            "seconds": 60.0,
        })
        resp = broker._dispatch({"op": "throttle_check", "function_name": "fn"})
        assert resp["data"] is False

    def test_expired_throttle_allows(self) -> None:
        broker = _Broker()
        # Set a throttle that already expired
        broker._throttles["fn"] = time.time() - 1.0
        resp = broker._dispatch({"op": "throttle_check", "function_name": "fn"})
        assert resp["data"] is True

    def test_different_functions_independent(self) -> None:
        broker = _Broker()
        broker._dispatch({
            "op": "throttle_record",
            "function_name": "fn_a",
            "seconds": 60.0,
        })
        resp = broker._dispatch({"op": "throttle_check", "function_name": "fn_b"})
        assert resp["data"] is True


class TestTraceThrottleOption:
    def test_throttle_timedelta_converted(self) -> None:
        @trace(throttle=timedelta(hours=1))
        def my_func() -> int:
            return 1

        opts = my_func.__pyfuse_options__  # type: ignore[attr-defined]
        assert opts["throttle"] == 3600.0

    def test_throttle_float_stored(self) -> None:
        @trace(throttle=120.0)
        def my_func() -> int:
            return 1

        opts = my_func.__pyfuse_options__  # type: ignore[attr-defined]
        assert opts["throttle"] == 120.0

    def test_throttle_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="throttle must be positive"):
            @trace(throttle=-1.0)
            def my_func() -> int:
                return 1
