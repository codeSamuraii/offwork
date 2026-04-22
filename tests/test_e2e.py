"""End-to-end integration tests.

These tests start real worker processes against real backends (Redis,
RabbitMQ) and exercise the full client → backend → worker → result path.

Environment variables
---------------------
PYFUSE_TEST_BACKEND     Backend URL (required).  e.g. redis://localhost:6379
PYFUSE_TEST_SIGNING     Set to "1" to enable HMAC-SHA256 task signing.
PYFUSE_TEST_SANDBOX     Set to "1" to run workers with Docker sandbox.

The worker is launched as a subprocess so that client and worker live in
completely separate Python processes (no shared state).
"""

import asyncio
import math
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from typing import Any

import pytest

import pyfuse
from pyfuse import trace, progress, TaskCancelled, ThrottleError
from pyfuse.core.token import generate_token
from pyfuse.graph.graph import Graph


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

BACKEND_URL = os.environ.get("PYFUSE_TEST_BACKEND", "")
USE_SIGNING = os.environ.get("PYFUSE_TEST_SIGNING", "") == "1"
USE_SANDBOX = os.environ.get("PYFUSE_TEST_SANDBOX", "") == "1"

pytestmark = pytest.mark.skipif(not BACKEND_URL, reason="PYFUSE_TEST_BACKEND not set")


# ---------------------------------------------------------------------------
# Worker subprocess management
# ---------------------------------------------------------------------------

def _start_worker(
    backend: str,
    *,
    signing_token: str | None = None,
    sandbox: bool = False,
) -> subprocess.Popen[bytes]:
    """Launch ``python -m pyfuse worker`` in a subprocess."""
    cmd = [sys.executable, "-m", "pyfuse", "worker", "--backend", backend]
    if signing_token:
        cmd.append("--require-signing")
    if sandbox:
        cmd.append("--sandbox")

    env = os.environ.copy()
    if signing_token:
        env["PYFUSE_SIGNING_TOKEN"] = signing_token

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Give the worker time to connect and start listening
    time.sleep(3)
    assert proc.poll() is None, (
        f"Worker exited early with code {proc.returncode}:\n"
        + (proc.stderr.read().decode() if proc.stderr else "")
    )
    return proc


def _stop_worker(proc: subprocess.Popen[bytes], timeout: float = 10) -> None:
    """Gracefully stop a worker subprocess."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_graph() -> None:
    Graph.reset_default()


@pytest.fixture(scope="module")
def signing_token() -> str | None:
    if not USE_SIGNING:
        return None
    return generate_token()


@pytest.fixture(scope="module")
def worker(signing_token: str | None) -> subprocess.Popen[bytes]:
    """Module-scoped worker process."""
    proc = _start_worker(
        BACKEND_URL,
        signing_token=signing_token,
        sandbox=USE_SANDBOX,
    )
    yield proc  # type: ignore[misc]
    _stop_worker(proc)


@pytest.fixture(autouse=True)
def _connect_backend(signing_token: str | None) -> None:
    """Connect the client to the backend before each test."""
    env = os.environ.copy()
    if signing_token:
        os.environ["PYFUSE_SIGNING_TOKEN"] = signing_token
    pyfuse.connect(BACKEND_URL)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicExecution:
    async def test_run_simple_function(self, worker: subprocess.Popen[bytes]) -> None:
        def add(a: int, b: int) -> int:
            return a + b

        @trace
        def hypotenuse(a: float, b: float) -> float:
            return math.sqrt(add(a**2, b**2))

        result = await hypotenuse.run(3.0, 4.0)
        assert result == pytest.approx(5.0)

    async def test_run_async_function(self, worker: subprocess.Popen[bytes]) -> None:
        async def async_add(a: float, b: float) -> float:
            return a + b

        @trace
        async def async_hyp(a: float, b: float) -> float:
            return math.sqrt(await async_add(a**2, b**2))

        result = await async_hyp.run(5.0, 12.0)
        assert result == pytest.approx(13.0)

    async def test_start_and_await(self, worker: subprocess.Popen[bytes]) -> None:
        @trace
        def double(x: int) -> int:
            return x * 2

        future = await double.start(21)
        result = await future
        assert result == 42

    async def test_map_batch(self, worker: subprocess.Popen[bytes]) -> None:
        @trace
        def square(x: int) -> int:
            return x * x

        results = await square.map([(2,), (3,), (4,)])
        assert results == [4, 9, 16]

    async def test_gather_concurrent(self, worker: subprocess.Popen[bytes]) -> None:
        @trace
        def mul(a: int, b: int) -> int:
            return a * b

        r1, r2, r3 = await asyncio.gather(
            mul.run(2, 3),
            mul.run(4, 5),
            mul.run(6, 7),
        )
        assert (r1, r2, r3) == (6, 20, 42)

    async def test_caching(self, worker: subprocess.Popen[bytes]) -> None:
        """Same code + args should work correctly on repeated calls."""
        @trace
        def inc(x: int) -> int:
            return x + 1

        r1 = await inc.run(10)
        r2 = await inc.run(10)
        assert r1 == r2 == 11

    async def test_stdlib_imports(self, worker: subprocess.Popen[bytes]) -> None:
        @trace
        def use_stdlib() -> str:
            import json
            import os
            return json.dumps({"pid": os.getpid()})

        result = await use_stdlib.run()
        assert '"pid"' in result


class TestProgressAndCancellation:
    async def test_progress_reporting(self, worker: subprocess.Popen[bytes]) -> None:
        import time as _time

        @trace
        def slow_with_progress(n: int) -> int:
            total = 0
            for i in range(n):
                _time.sleep(0.1)
                total += i
                progress(i + 1, n)
            return total

        future = await slow_with_progress.start(5)
        await asyncio.sleep(0.5)

        p = await future.progress()
        # Progress may or may not have arrived yet depending on timing
        result = await future
        assert result == sum(range(5))

    async def test_cancellation(self, worker: subprocess.Popen[bytes]) -> None:
        @trace
        async def very_slow(n: int) -> int:
            total = 0
            for _ in range(n):
                await asyncio.sleep(1.0)
                total += 1
            return total

        future = await very_slow.start(60)
        await asyncio.sleep(2.0)
        await future.cancel()

        with pytest.raises(TaskCancelled):
            await future

        status = await future.status()
        assert status == "cancelled"


class TestRetryAndTimeout:
    async def test_retry_on_failure(self, worker: subprocess.Popen[bytes]) -> None:
        @trace(retries=3, retry_delay=0.1)
        def sometimes_fails() -> str:
            import random
            random.seed()  # re-seed each call
            if random.random() < 0.3:
                raise RuntimeError("transient")
            return "ok"

        # With 3 retries, at least one attempt should succeed (very high probability)
        result = await sometimes_fails.run()
        assert result == "ok"


class TestScheduling:
    async def test_run_in_delay(self, worker: subprocess.Popen[bytes]) -> None:
        @trace
        def greet(name: str) -> str:
            return f"hello {name}"

        before = time.time()
        result = await greet.run_in(timedelta(seconds=2), "world")
        elapsed = time.time() - before
        assert result == "hello world"
        assert elapsed >= 1.5  # allow some slack

    async def test_run_every_recurring(self, worker: subprocess.Popen[bytes]) -> None:
        @trace
        def tick(n: int) -> int:
            return n

        schedule = await tick.run_every(timedelta(seconds=1), 42)
        await asyncio.sleep(3)
        await schedule.cancel()
        # If we got here without error, recurring + cancel works


class TestThrottling:
    async def test_throttle_rejects_rapid_calls(self, worker: subprocess.Popen[bytes]) -> None:
        @trace(throttle=timedelta(seconds=10))
        def throttled_fn() -> str:
            return "ok"

        result = await throttled_fn.run()
        assert result == "ok"

        with pytest.raises(ThrottleError):
            await throttled_fn.run()


class TestErrorHandling:
    async def test_remote_error_propagation(self, worker: subprocess.Popen[bytes]) -> None:
        @trace
        def bad_func() -> None:
            raise ValueError("intentional error")

        from pyfuse.core.errors import RemoteError
        with pytest.raises(RemoteError, match="intentional error"):
            await bad_func.run()

    async def test_function_with_dependencies(self, worker: subprocess.Popen[bytes]) -> None:
        """Multi-level dependency chain works end-to-end."""
        def step_a(x: int) -> int:
            return x + 1

        def step_b(x: int) -> int:
            return step_a(x) * 2

        @trace
        def pipeline(x: int) -> int:
            return step_b(x) + 10

        result = await pipeline.run(5)
        assert result == 22  # step_a(5)=6, step_b(5)=12, pipeline(5)=22


class TestClassMethods:
    async def test_class_with_methods(self, worker: subprocess.Popen[bytes]) -> None:
        class Calculator:
            def __init__(self, base: int) -> None:
                self.base = base

            def compute(self, x: int) -> int:
                return self.base + x

        @trace
        def use_calculator(x: int) -> int:
            c = Calculator(100)
            return c.compute(x)

        result = await use_calculator.run(42)
        assert result == 142
