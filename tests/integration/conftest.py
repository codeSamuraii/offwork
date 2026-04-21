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
        return "amqp://localhost"
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

    Teardown: SIGTERM → wait 10 s → SIGKILL.
    """
    cmd = [
        sys.executable, "-m", "pyfuse", "worker",
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait until the broker/worker is reachable before yielding.
    if backend_url.startswith("local://"):
        parsed = urlparse(backend_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9748
        if not _wait_for_tcp(host, port, timeout=20.0):
            proc.terminate()
            _out, err = proc.communicate(timeout=5)
            raise RuntimeError(
                f"Worker broker did not start on {host}:{port}.\n"
                f"stderr:\n{err.decode()}"
            )
    else:
        # Redis / RabbitMQ workers connect asynchronously; give them time
        # to connect and begin listening before the first task is submitted.
        time.sleep(4)

    if proc.poll() is not None:
        _out, err = proc.communicate()
        raise RuntimeError(
            f"Worker process exited prematurely (rc={proc.returncode}).\n"
            f"stderr:\n{err.decode()}"
        )

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


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
