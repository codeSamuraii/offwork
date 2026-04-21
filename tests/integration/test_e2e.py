"""Cross-process integration tests for pyfuse.

Each test spawns (via the ``worker_process`` fixture) a real pyfuse worker
as a separate subprocess and connects to the same backend as a client from
within the test runner process.  This validates the full stack:

    client process  →  backend (local/redis/rabbitmq)  →  worker process
                                                          (optional sandbox)

The combination of backend, signing, and sandbox modes is controlled by
environment variables set by the CI workflow:

    PYFUSE_TEST_BACKEND  – "local", "redis", or "rabbitmq"
    PYFUSE_TEST_SIGNING  – "true" or "false"
    PYFUSE_TEST_SANDBOX  – "true" or "false"
    PYFUSE_SIGNING_TOKEN – 64-char hex key (present when signing=true)
"""

import os

import pytest

from pyfuse import trace
from pyfuse.core.errors import RemoteError, SignatureError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RESULT_TIMEOUT = 60.0   # seconds to wait for a single task result
_SANDBOX_BOOT   = 35.0   # extra seconds for the Docker container to boot


async def _get(future, *, timeout: float = _RESULT_TIMEOUT) -> object:
    """Await a Result with an explicit timeout and stall detection disabled.

    Using stall_timeout=None avoids flakiness in slow CI environments where
    heartbeat intervals may exceed the default 10-second stall threshold.
    """
    return await future.result(timeout=timeout, stall_timeout=None)


# ---------------------------------------------------------------------------
# Basic remote execution
# ---------------------------------------------------------------------------


class TestBasicExecution:
    """Verify fundamental task-submission → result retrieval on every backend."""

    @pytest.mark.asyncio
    async def test_arithmetic(self, client: None) -> None:
        """Integer arithmetic is computed correctly by the remote worker."""

        @trace
        def add(a: int, b: int) -> int:
            return a + b

        future = await add.start(3, 4)
        result = await _get(future)
        assert result == 7

    @pytest.mark.asyncio
    async def test_string_processing(self, client: None) -> None:
        """String operations round-trip through the serialization layer."""

        @trace
        def greet(name: str) -> str:
            return f"hello {name}"

        future = await greet.start("world")
        result = await _get(future)
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_list_result(self, client: None) -> None:
        """Composite return values are serialized and deserialized correctly."""

        @trace
        def squares(n: int) -> list[int]:
            return [i * i for i in range(n)]

        future = await squares.start(5)
        result = await _get(future)
        assert result == [0, 1, 4, 9, 16]

    @pytest.mark.asyncio
    async def test_kwargs(self, client: None) -> None:
        """Keyword arguments are forwarded to the worker unchanged."""

        @trace
        def power(base: int, *, exp: int = 2) -> int:
            return base ** exp

        future = await power.start(3, exp=4)
        result = await _get(future)
        assert result == 81

    @pytest.mark.asyncio
    async def test_sequential_tasks(self, client: None) -> None:
        """Multiple tasks submitted in sequence all return correct results."""

        @trace
        def double(x: int) -> int:
            return x * 2

        results = []
        for i in range(3):
            future = await double.start(i)
            results.append(await _get(future))

        assert results == [0, 2, 4]


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    """Exceptions raised in the worker are surfaced as RemoteError on the client."""

    @pytest.mark.asyncio
    async def test_value_error_propagates(self, client: None) -> None:
        """ValueError raised in the worker becomes a RemoteError."""

        @trace
        def explode(msg: str) -> None:
            raise ValueError(msg)

        future = await explode.start("intentional")
        with pytest.raises(RemoteError, match="ValueError.*intentional"):
            await _get(future)

    @pytest.mark.asyncio
    async def test_runtime_error_propagates(self, client: None) -> None:
        """RuntimeError is preserved through the result envelope."""

        @trace
        def fail() -> None:
            raise RuntimeError("something went wrong")

        future = await fail.start()
        with pytest.raises(RemoteError, match="RuntimeError"):
            await _get(future)

    @pytest.mark.asyncio
    async def test_error_does_not_crash_worker(self, client: None) -> None:
        """A failing task must not crash the worker; it must stay alive for the next task."""

        @trace
        def bad() -> None:
            raise RuntimeError("intentional failure")

        @trace
        def ok() -> str:
            return "still alive"

        # First task fails (RuntimeError is caught by the worker and returned
        # as an error envelope; the worker process stays alive).
        future_bad = await bad.start()
        with pytest.raises(RemoteError):
            await _get(future_bad)

        # Worker should still be responsive for subsequent tasks.
        future_ok = await ok.start()
        result = await _get(future_ok)
        assert result == "still alive"


# ---------------------------------------------------------------------------
# Signing enforcement
# ---------------------------------------------------------------------------


class TestSigning:
    """Verify HMAC signing behaviour: acceptance and rejection."""

    @pytest.mark.asyncio
    async def test_signed_task_accepted(
        self, client: None, signing_enabled: bool
    ) -> None:
        """A properly signed task is accepted and executed by the worker."""
        if not signing_enabled:
            pytest.skip("Signing is not enabled for this scenario")

        @trace
        def compute(x: int) -> int:
            return x * 2

        future = await compute.start(21)
        result = await _get(future)
        assert result == 42

    @pytest.mark.asyncio
    async def test_unsigned_task_rejected(
        self, client_no_signing: None, signing_enabled: bool
    ) -> None:
        """When signing is required, unsigned tasks are rejected with an error result."""
        if not signing_enabled:
            pytest.skip("Signing is not enabled for this scenario")

        @trace
        def noop() -> str:
            return "should not reach here"

        # The client connected without a signing token, so the task is unsigned.
        # The worker (started with --require-signing) must reject it.
        future = await noop.start()
        with pytest.raises((RemoteError, SignatureError)):
            await _get(future, timeout=15.0)

    @pytest.mark.asyncio
    async def test_signing_does_not_break_normal_execution(
        self, client: None, signing_enabled: bool
    ) -> None:
        """Signing is transparent to the caller when token is present on both sides."""
        if not signing_enabled:
            pytest.skip("Signing is not enabled for this scenario")

        @trace
        def multiply(a: int, b: int) -> int:
            return a * b

        future = await multiply.start(6, 7)
        result = await _get(future)
        assert result == 42


# ---------------------------------------------------------------------------
# Sandbox isolation
# ---------------------------------------------------------------------------


class TestSandbox:
    """Verify that tasks run correctly inside the Docker sandbox."""

    @pytest.mark.asyncio
    async def test_sandboxed_arithmetic(
        self, client: None, sandbox_enabled: bool
    ) -> None:
        """Tasks execute and return correct results when the sandbox is active."""
        if not sandbox_enabled:
            pytest.skip("Sandbox is not enabled for this scenario")

        @trace
        def add(a: int, b: int) -> int:
            return a + b

        future = await add.start(10, 32)
        # Allow extra time for the container to boot on first use.
        result = await _get(future, timeout=_RESULT_TIMEOUT + _SANDBOX_BOOT)
        assert result == 42

    @pytest.mark.asyncio
    async def test_sandboxed_error_propagation(
        self, client: None, sandbox_enabled: bool
    ) -> None:
        """Exceptions raised inside the sandbox are forwarded as RemoteError."""
        if not sandbox_enabled:
            pytest.skip("Sandbox is not enabled for this scenario")

        @trace
        def boom() -> None:
            raise ValueError("sandboxed failure")

        future = await boom.start()
        with pytest.raises(RemoteError, match="ValueError.*sandboxed failure"):
            await _get(future, timeout=_RESULT_TIMEOUT + _SANDBOX_BOOT)

    @pytest.mark.asyncio
    async def test_sandbox_with_signing(
        self, client: None, sandbox_enabled: bool, signing_enabled: bool
    ) -> None:
        """Signed tasks execute correctly inside the sandbox."""
        if not sandbox_enabled or not signing_enabled:
            pytest.skip("Both sandbox and signing must be enabled for this test")

        @trace
        def triple(x: int) -> int:
            return x * 3

        future = await triple.start(14)
        result = await _get(future, timeout=_RESULT_TIMEOUT + _SANDBOX_BOOT)
        assert result == 42
