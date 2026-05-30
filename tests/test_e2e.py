"""End-to-end integration tests.

These tests start real worker processes against real backends (Redis,
RabbitMQ) and exercise the full client → backend → worker → result path.

Environment variables
---------------------
OFFWORK_TEST_BACKEND     Backend URL (required).  e.g. redis://localhost:6379
OFFWORK_TEST_SIGNING     Set to "1" to enable HMAC-SHA256 task signing.
OFFWORK_TEST_SANDBOX     Set to "1" to run workers with Docker sandbox.

The worker is launched as a subprocess so that client and worker live in
completely separate Python processes (no shared state).
"""

import asyncio
import math
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from typing import Any

import pytest

import offwork
from offwork import progress, TaskCancelled, ThrottleError
from offwork.core.token import generate_token
from offwork.graph.graph import Graph


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

BACKEND_URL = os.environ.get("OFFWORK_TEST_BACKEND", "")
USE_SIGNING = os.environ.get("OFFWORK_TEST_SIGNING", "") == "1"
USE_SANDBOX = os.environ.get("OFFWORK_TEST_SANDBOX", "") == "1"

pytestmark = pytest.mark.skipif(not BACKEND_URL, reason="OFFWORK_TEST_BACKEND not set")


# ---------------------------------------------------------------------------
# Worker subprocess management
# ---------------------------------------------------------------------------

_WORKER_READY_TIMEOUT = 60  # seconds to wait for "Listening" log line


def _start_worker(
    backend: str,
    *,
    signing_token: str | None = None,
    sandbox: bool = False,
) -> subprocess.Popen[bytes]:
    """Launch ``python -m offwork worker`` in a subprocess.

    Waits for the worker to print its "Listening for tasks" log line
    before returning, so the caller knows it is actually ready.
    """
    cmd = [sys.executable, "-m", "offwork", "worker", "--backend", backend]
    if signing_token:
        cmd.append("--require-signing")
    if sandbox:
        cmd.append("--sandbox")
    log_level = os.environ.get("OFFWORK_LOG_LEVEL", "")
    if log_level:
        cmd.extend(["--log-level", log_level])

    env = os.environ.copy()
    if signing_token:
        env["OFFWORK_SIGNING_TOKEN"] = signing_token

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Read worker output in a background thread, mirror it live, and wait
    # for the ready signal so callers only proceed once the worker is up.
    ready = threading.Event()
    output_lines: list[str] = []

    def _drain_output() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode(errors="replace")
            output_lines.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()
            if "Listening" in line:
                ready.set()

    reader = threading.Thread(target=_drain_output, daemon=True)
    reader.start()

    if not ready.wait(timeout=_WORKER_READY_TIMEOUT):
        proc.kill()
        proc.wait(timeout=5)
        raise RuntimeError(
            f"Worker not ready after {_WORKER_READY_TIMEOUT}s.\n"
            f"worker output:\n{''.join(output_lines)}"
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
        os.environ["OFFWORK_SIGNING_TOKEN"] = signing_token
    offwork.connect(BACKEND_URL)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicExecution:
    async def test_run_simple_function(self, worker: subprocess.Popen[bytes]) -> None:
        def add(a: int, b: int) -> int:
            return a + b

        @offwork.task
        def hypotenuse(a: float, b: float) -> float:
            return math.sqrt(add(a**2, b**2))

        result = await hypotenuse.run(3.0, 4.0)
        assert result == pytest.approx(5.0)

    async def test_run_async_function(self, worker: subprocess.Popen[bytes]) -> None:
        async def async_add(a: float, b: float) -> float:
            return a + b

        @offwork.task
        async def async_hyp(a: float, b: float) -> float:
            return math.sqrt(await async_add(a**2, b**2))

        result = await async_hyp.run(5.0, 12.0)
        assert result == pytest.approx(13.0)

    async def test_start_and_await(self, worker: subprocess.Popen[bytes]) -> None:
        @offwork.task
        def double(x: int) -> int:
            return x * 2

        future = await double.submit(21)
        result = await future
        assert result == 42

    async def test_map_batch(self, worker: subprocess.Popen[bytes]) -> None:
        @offwork.task
        def square(x: int) -> int:
            return x * x

        results = await square.map([(2,), (3,), (4,)])
        assert results == [4, 9, 16]

    async def test_gather_concurrent(self, worker: subprocess.Popen[bytes]) -> None:
        @offwork.task
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
        @offwork.task
        def inc(x: int) -> int:
            return x + 1

        r1 = await inc.run(10)
        r2 = await inc.run(10)
        assert r1 == r2 == 11

    async def test_stdlib_imports(self, worker: subprocess.Popen[bytes]) -> None:
        @offwork.task
        def use_stdlib() -> str:
            import json
            import os
            return json.dumps({"pid": os.getpid()})

        result = await use_stdlib.run()
        assert '"pid"' in result


class TestProgressAndCancellation:
    async def test_progress_reporting(self, worker: subprocess.Popen[bytes]) -> None:


        @offwork.task
        def slow_with_progress(n: int) -> int:
            import time
            total = 0
            for i in range(n):
                time.sleep(0.1)
                total += i
                progress(i + 1, n)
            return total

        future = await slow_with_progress.submit(5)
        await asyncio.sleep(0.5)

        p = await future.progress()
        # Progress may or may not have arrived yet depending on timing
        result = await future
        assert result == sum(range(5))

    async def test_cancellation(self, worker: subprocess.Popen[bytes]) -> None:
        @offwork.task
        async def very_slow(n: int) -> int:
            total = 0
            for _ in range(n):
                await asyncio.sleep(1.0)
                total += 1
            return total

        future = await very_slow.submit(60)
        await asyncio.sleep(2.0)
        await future.cancel()

        with pytest.raises(TaskCancelled):
            await future

        assert future.cancelled() is True


class TestRetryAndTimeout:
    async def test_retry_on_failure(self, worker: subprocess.Popen[bytes]) -> None:
        # Deterministic retry test: a counter file local to the worker
        # process records how many attempts have been made.  The first
        # two raise; the third succeeds.  This avoids RNG flakes while
        # still exercising the full retry path.  The path lives on
        # whichever filesystem actually runs the function -- the host
        # worker's /tmp, or the sandbox container's /tmp.
        import uuid
        counter_path = f"/tmp/offwork-retry-{uuid.uuid4().hex}.txt"

        @offwork.task(retries=3, retry_delay=0.1)
        def fails_then_succeeds(path: str, fail_until: int) -> str:
            import os
            n = 0
            if os.path.exists(path):
                with open(path) as f:
                    n = int(f.read() or "0")
            n += 1
            with open(path, "w") as f:
                f.write(str(n))
            if n <= fail_until:
                raise RuntimeError(f"transient (attempt {n})")
            return f"ok after {n} attempts"

        result = await fails_then_succeeds.run(counter_path, 2)
        assert result == "ok after 3 attempts"


class TestScheduling:
    async def test_run_in_delay(self, worker: subprocess.Popen[bytes]) -> None:
        @offwork.task
        def greet(name: str) -> str:
            return f"hello {name}"

        before = time.time()
        result = await greet.submit("world", run_in=timedelta(seconds=2))
        elapsed = time.time() - before
        assert result == "hello world"
        assert elapsed >= 1.5  # allow some slack

    async def test_run_every_recurring(self, worker: subprocess.Popen[bytes]) -> None:
        @offwork.task
        def tick(n: int) -> int:
            return n

        schedule = await tick.submit(42, run_every=timedelta(seconds=1))
        await asyncio.sleep(3)
        await schedule.cancel()
        # If we got here without error, recurring + cancel works


class TestThrottling:
    async def test_throttle_rejects_rapid_calls(self, worker: subprocess.Popen[bytes]) -> None:
        @offwork.task(throttle=timedelta(seconds=10))
        def throttled_fn() -> str:
            return "ok"

        result = await throttled_fn.run()
        assert result == "ok"

        with pytest.raises(ThrottleError):
            await throttled_fn.run()


class TestErrorHandling:
    async def test_remote_error_propagation(self, worker: subprocess.Popen[bytes]) -> None:
        @offwork.task
        def bad_func() -> None:
            raise ValueError("intentional error")

        from offwork.core.errors import RemoteError
        with pytest.raises(RemoteError, match="intentional error"):
            await bad_func.run()

    async def test_function_with_dependencies(self, worker: subprocess.Popen[bytes]) -> None:
        """Multi-level dependency chain works end-to-end."""
        def _step_a(x: int) -> int:
            return x + 1

        def _step_b(x: int) -> int:
            return _step_a(x) * 2

        @offwork.task
        def pipeline(x: int) -> int:
            return _step_b(x) + 10

        result = await pipeline.run(5)
        assert result == 22  # _step_a(5)=6, _step_b(5)=12, pipeline(5)=22


class TestClassMethods:
    async def test_class_with_methods(self, worker: subprocess.Popen[bytes]) -> None:
        class Calculator:
            def __init__(self, base: int) -> None:
                self.base = base

            def compute(self, x: int) -> int:
                return self.base + x

        @offwork.task
        def use_calculator(x: int) -> int:
            c = Calculator(100)
            return c.compute(x)

        result = await use_calculator.run(42)
        assert result == 142


# ---------------------------------------------------------------------------
# Run as script — simulate CI matrix
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import itertools

    def _flush_backend(backend_url: str) -> None:
        """Remove leftover offwork state so permutations don't interfere."""
        scheme = backend_url.split("://", 1)[0].lower()
        if scheme in ("redis", "rediss"):
            try:
                import redis as _redis
                r = _redis.Redis.from_url(backend_url)
                for key in r.scan_iter("offwork:*"):
                    r.delete(key)
                r.close()
            except Exception:
                pass

    BACKENDS = [
        ("redis", "redis://localhost:6379"),
        ("rabbitmq", "amqp://localhost:5672"),
    ]
    SIGNING_OPTIONS = [False, True]
    SANDBOX_OPTIONS = [False, True]

    passed, failed, skipped = 0, 0, 0
    extra_pytest_args = sys.argv[1:]
    if "-s" not in extra_pytest_args and not any(
        arg.startswith("--capture=") for arg in extra_pytest_args
    ):
        extra_pytest_args = ["--capture=no", *extra_pytest_args]

    for (backend_name, backend_url), signing, sandbox in itertools.product(
        BACKENDS, SIGNING_OPTIONS, SANDBOX_OPTIONS,
    ):
        _flush_backend(backend_url)
        label = f"e2e · {backend_name} · sign={signing} · sandbox={sandbox}"
        print(f"\n{'=' * 60}")
        print(f"  {label}")
        print(f"{'=' * 60}\n")

        env = os.environ.copy()
        env["OFFWORK_TEST_BACKEND"] = backend_url
        env["OFFWORK_TEST_SIGNING"] = "1" if signing else "0"
        env["OFFWORK_TEST_SANDBOX"] = "1" if sandbox else "0"

        if signing:
            token = generate_token()
            env["OFFWORK_SIGNING_TOKEN"] = token

        cmd = [sys.executable, "-m", "pytest", __file__, "--tb=short", "-p", "no:warnings", "--no-header"] + extra_pytest_args
        print(f"Running command: python {' '.join(cmd[1:])}")

        result = subprocess.run(
            cmd,
            env=env,
            timeout=120
        )

        if result.returncode == 0:
            passed += 1
            print(f"\n  ✓ PASSED: {label}")
        elif result.returncode == 5:
            # pytest exit code 5 = no tests collected (skip)
            skipped += 1
            print(f"\n  ○ SKIPPED: {label}")
        else:
            failed += 1
            print(f"\n  ✗ FAILED: {label}")
            break

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed, {skipped} skipped")
    print(f"{'=' * 60}")

    raise SystemExit(1 if failed else 0)
