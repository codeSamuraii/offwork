"""Run every script in ``examples/`` end-to-end against a real worker.

When the developer's shell already exports ``BROKER_URL`` (typically
``wss://…`` for cloud e2e), example clients connect there — see
``offwork.worker.remote._resolve_url``.  Otherwise this module spawns a
local worker on an ephemeral port and injects that URL into the example
subprocess env so the hard-coded ``local://localhost:9748`` connect calls
in the scripts still reach the test worker.

Runs as a normal pytest test (``pytest tests/test_examples.py``) and also
directly (``python tests/test_examples.py``).

This is slow: each ``--tmp`` invocation builds a fresh venv. Use
``-k <name>`` to run a single example. Set ``OFFWORK_EXAMPLES_PORT`` to
pin the local worker port (default: pick a free port).
"""
from __future__ import annotations

import os
import select
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
_WORKER_HOST = "127.0.0.1"
WORKER_BOOT_TIMEOUT = 60.0    # building the worker's --tmp venv can take a while
EXAMPLE_TIMEOUT = 60.0        # each example also builds its own --tmp venv

# Forwarded into worker / ``offwork run`` children.  A hosted broker URL in
# ``HTTPS_PROXY`` makes worker-side ``requests`` tunnel through the broker
# (ProxyError / 403) — a common leak when ``export HTTPS_PROXY=$BROKER_URL``.
_SUBPROCESS_UNSET = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
})


def _example_scripts() -> list[Path]:
    return sorted(p for p in EXAMPLES_DIR.glob("*.py") if not p.name.startswith("_"))


def _cloud_broker_configured() -> bool:
    return bool(os.environ.get("BROKER_URL"))


def _pick_local_backend() -> str:
    port_s = os.environ.get("OFFWORK_EXAMPLES_PORT")
    if port_s:
        port = int(port_s)
        _assert_port_free(_WORKER_HOST, port)
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((_WORKER_HOST, 0))
            port = s.getsockname()[1]
    return f"local://{_WORKER_HOST}:{port}"


def _assert_port_free(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"port {host}:{port} is not available for the examples worker "
                f"(is another offwork worker already listening?). {exc}"
            ) from exc


def _subprocess_env(*, local_backend: str | None) -> dict[str, str]:
    env = os.environ.copy()
    for key in _SUBPROCESS_UNSET:
        env.pop(key, None)
    if local_backend is not None:
        # BROKER_URL wins over the scripts' connect("local://localhost:9748").
        env["BROKER_URL"] = local_backend
    return env


def _drain_available(pipe: IO[bytes] | None) -> str:
    if pipe is None:
        return ""
    chunks: list[bytes] = []
    fd = pipe.fileno()
    while True:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            break
        chunk = pipe.read(4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace")


def _wait_for_worker(
    proc: subprocess.Popen[bytes],
    host: str,
    port: int,
    timeout: float,
) -> None:
    """Wait until *host*:*port* accepts connections or the worker exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            output = _drain_available(proc.stdout)
            raise RuntimeError(
                f"worker exited with code {code} before opening {host}:{port}\n"
                f"--- worker output ---\n{output or '(empty)'}"
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(0.25)
    output = _drain_available(proc.stdout)
    raise TimeoutError(
        f"worker did not open {host}:{port} within {timeout}s\n"
        f"--- worker output (tail) ---\n{output or '(empty)'}"
    )


@contextmanager
def _spawn_worker(backend: str) -> Iterator[subprocess.Popen[bytes]]:
    parsed_port = int(backend.rsplit(":", 1)[-1])
    proc = subprocess.Popen(
        [sys.executable, "-m", "offwork", "worker", "--backend", backend, "--tmp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(EXAMPLES_DIR.parent),
        env=_subprocess_env(local_backend=None),
    )
    try:
        _wait_for_worker(proc, _WORKER_HOST, parsed_port, WORKER_BOOT_TIMEOUT)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()


@pytest.fixture(scope="module")
def example_broker() -> Iterator[str | None]:
    """Local worker URL, or ``None`` when the shell already sets ``BROKER_URL``."""
    if _cloud_broker_configured():
        yield None
        return
    backend = _pick_local_backend()
    with _spawn_worker(backend):
        yield backend


def _run_example(script: Path, *, local_backend: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "offwork", "run", str(script)],
        cwd=str(EXAMPLES_DIR.parent),
        capture_output=True,
        text=True,
        timeout=EXAMPLE_TIMEOUT,
        env=_subprocess_env(local_backend=local_backend),
    )


@pytest.mark.parametrize("script", _example_scripts(), ids=lambda p: p.name)
def test_example_runs(example_broker: str | None, script: Path) -> None:
    result = _run_example(script, local_backend=example_broker)
    if result.returncode != 0:
        pytest.fail(
            f"{script.name} exited with {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )


def main() -> int:
    scripts = _example_scripts()
    broker = os.environ.get("BROKER_URL")
    failures: list[tuple[str, subprocess.CompletedProcess[str]]] = []

    if broker:
        print(f"Running {len(scripts)} example(s) against BROKER_URL={broker}")
        for script in scripts:
            print(f"--- {script.name} ", end="", flush=True)
            t0 = time.monotonic()
            result = _run_example(script, local_backend=None)
            elapsed = time.monotonic() - t0
            if result.returncode == 0:
                print(f"OK ({elapsed:.1f}s)")
            else:
                print(f"FAIL ({elapsed:.1f}s, exit {result.returncode})")
                failures.append((script.name, result))
    else:
        backend = _pick_local_backend()
        print(f"Running {len(scripts)} example(s) against {backend}")
        with _spawn_worker(backend):
            for script in scripts:
                print(f"--- {script.name} ", end="", flush=True)
                t0 = time.monotonic()
                result = _run_example(script, local_backend=backend)
                elapsed = time.monotonic() - t0
                if result.returncode == 0:
                    print(f"OK ({elapsed:.1f}s)")
                else:
                    print(f"FAIL ({elapsed:.1f}s, exit {result.returncode})")
                    failures.append((script.name, result))

    if failures:
        print(f"\n{len(failures)} failure(s):", file=sys.stderr)
        for name, res in failures:
            print(f"\n=== {name} ===", file=sys.stderr)
            print("--- stdout ---", file=sys.stderr)
            print(res.stdout, file=sys.stderr)
            print("--- stderr ---", file=sys.stderr)
            print(res.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
