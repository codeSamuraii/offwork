"""Fixtures for cross-process integration tests.

The worker subprocess and backend URL are shared across the whole test session
(session scope).  Each test function gets its own client connection
(function scope) so that global state does not leak between tests.

Configuration (set by the CI workflow via environment variables):
    PYFUSE_TEST_BACKEND  – "local", "redis", or "rabbitmq"
    PYFUSE_TEST_SIGNING  – "true" or "false"
    PYFUSE_TEST_SANDBOX  – "true" or "false"
    PYFUSE_SIGNING_TOKEN – 64-char hex token (present when signing=true)
"""

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Generator
from urllib.parse import urlparse

import pytest

import pyfuse.worker.remote as _remote


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return a currently-unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_tcp(host: str, port: int, timeout: float = 20.0) -> bool:
    """Poll until a TCP service accepts connections, or *timeout* seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# Session-scoped fixtures (computed once for the whole test run)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def backend_url() -> str:
    """Return the backend URL for this test session.

    For the local backend a random free port is chosen so that parallel
    CI jobs running on the same host cannot conflict.
    """
    backend = os.environ.get("PYFUSE_TEST_BACKEND", "local").lower()
    if backend == "redis":
        return "redis://localhost:6379"
    if backend in ("rabbitmq", "amqp"):
        # heartbeat=600 keeps the AMQP connection alive across the whole test
        # session.  Without this the default 60-second heartbeat timeout
        # causes the connection to be closed after 60 s of idle time, which
        # makes the listen() generator exit and stops the worker from
        # processing further tasks.
        return "amqp://localhost?heartbeat=600"
    # local: pick a free port
    port = _free_port()
    return f"local://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def signing_enabled() -> bool:
    """True when PYFUSE_TEST_SIGNING=true."""
    return os.environ.get("PYFUSE_TEST_SIGNING", "false").lower() == "true"


@pytest.fixture(scope="session")
def sandbox_enabled() -> bool:
    """True when PYFUSE_TEST_SANDBOX=true."""
    return os.environ.get("PYFUSE_TEST_SANDBOX", "false").lower() == "true"


@pytest.fixture(scope="session")
def worker_process(
    backend_url: str,
    signing_enabled: bool,
    sandbox_enabled: bool,
) -> Generator[subprocess.Popen[bytes], None, None]:
    """Spawn a pyfuse worker subprocess for the entire test session.

    The subprocess inherits the test runner's environment, so
    ``PYFUSE_SIGNING_TOKEN`` (when present) flows automatically to the
    worker without any extra plumbing.

    Readiness is detected by watching the worker's stderr for the
    "Listening for tasks" log line.  A background daemon thread drains
    the stderr pipe so it never fills up (which would freeze the worker),
    and forwards every line to the test-runner's stderr for CI visibility.

    For the local backend an additional TCP probe confirms the broker is
    accepting connections before the readiness wait begins.

    Teardown: SIGTERM → wait 10 s → SIGKILL.
    """
    # -u forces unbuffered output so log lines appear immediately in the pipe.
    cmd = [
        sys.executable, "-u", "-m", "pyfuse", "worker",
        "--backend", backend_url,
        "--no-auto-install",
        "--log-level", "DEBUG",
    ]
    if signing_enabled:
        cmd.append("--require-signing")
    if sandbox_enabled:
        cmd.append("--sandbox")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Background thread: drain stderr and signal when the worker is ready.
    # Draining is essential — if we never read from the pipe and the worker
    # writes more than 64 KB of debug output, it will block on the write()
    # syscall, freezing its asyncio event loop.
    ready = threading.Event()

    def _watch_stderr() -> None:
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            try:
                line = raw_line.decode("utf-8", errors="replace")
            except Exception:
                line = repr(raw_line) + "\n"
            sys.stderr.write(f"[worker] {line}")
            sys.stderr.flush()
            if "Listening for tasks" in line:
                ready.set()
        # Process exited or pipe closed — unblock any waiter.
        ready.set()

    watcher = threading.Thread(target=_watch_stderr, daemon=True)
    watcher.start()

    # For the local backend, also wait for the broker TCP port to be open.
    # This provides a fast early-failure signal (e.g. port already in use)
    # without waiting the full readiness timeout.
    if backend_url.startswith("local://"):
        parsed = urlparse(backend_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9748
        if not _wait_for_tcp(host, port, timeout=20.0):
            proc.terminate()
            proc.wait(timeout=5)
            watcher.join(timeout=5)
            raise RuntimeError(
                f"Worker broker did not start on {host}:{port}."
            )

    # Wait until the worker reports it is listening for tasks.
    # Sandbox container boot can take up to ~60 seconds on a cold CI runner,
    # so we allow up to 120 seconds here.
    ready_timeout = 120.0
    if not ready.wait(timeout=ready_timeout):
        proc.terminate()
        proc.wait(timeout=5)
        watcher.join(timeout=5)
        raise RuntimeError(
            f"Worker did not become ready within {ready_timeout:.0f} seconds."
        )

    if proc.poll() is not None:
        watcher.join(timeout=5)
        raise RuntimeError(
            f"Worker process exited prematurely (rc={proc.returncode})."
        )

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    watcher.join(timeout=5)


# ---------------------------------------------------------------------------
# Function-scoped fixtures (one per test)
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(
    backend_url: str,
    worker_process: subprocess.Popen[bytes],
) -> AsyncIterator[None]:
    """Connect to the backend as a client for the duration of one test.

    The *worker_process* dependency ensures the worker is running before
    any connection attempt is made.  The global backend state is reset
    during teardown so tests are fully isolated from each other.
    """
    import pyfuse

    pyfuse.connect(backend_url)
    yield
    try:
        await pyfuse.disconnect()
    except Exception:
        pass
    finally:
        _remote._active_backend = None
        _remote._atexit_registered = False


@pytest.fixture
async def client_no_signing(
    backend_url: str,
    worker_process: subprocess.Popen[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    """Connect as a client WITHOUT a signing token.

    Even when ``PYFUSE_SIGNING_TOKEN`` is set in the environment (signing=true
    scenario) this fixture removes it so the client submits unsigned tasks.
    Used to verify that a worker with signing enabled rejects them.
    """
    import pyfuse

    monkeypatch.delenv("PYFUSE_SIGNING_TOKEN", raising=False)
    pyfuse.connect(backend_url)
    yield
    try:
        await pyfuse.disconnect()
    except Exception:
        pass
    finally:
        _remote._active_backend = None
        _remote._atexit_registered = False
