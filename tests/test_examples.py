"""Run every script in ``examples/`` end-to-end against a real worker.

Spawns one ``offwork worker --backend local://localhost:9749 --tmp`` worker
process, then for each example runs ``offwork run examples/<file>.py`` and
asserts the script exits cleanly.

Runs as a normal pytest test (``pytest tests/test_examples.py``) and also
directly (``python tests/test_examples.py``).

This is slow: each ``--tmp`` invocation builds a fresh venv. Use
``-k <name>`` to run a single example, or ``OFFWORK_EXAMPLES_PORT`` to pick
a different broker port.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
BACKEND = f"local://localhost:9748"
WORKER_BOOT_TIMEOUT = 60.0    # building the worker's --tmp venv can take a while
EXAMPLE_TIMEOUT = 60.0        # each example also builds its own --tmp venv


def _example_scripts() -> list[Path]:
    return sorted(p for p in EXAMPLES_DIR.glob("*.py") if not p.name.startswith("_"))


def _wait_for_port(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(0.5)
    raise TimeoutError(f"worker did not open {host}:{port} within {timeout}s")


@contextmanager
def _spawn_worker() -> Iterator[subprocess.Popen[bytes]]:
    cmd = [sys.executable, "-m", "offwork", "worker", "--backend", BACKEND, "--tmp"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(EXAMPLES_DIR.parent),
    )
    try:
        _wait_for_port("localhost", 9748, WORKER_BOOT_TIMEOUT)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _run_example(script: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "offwork", "run", str(script)]
    return subprocess.run(
        cmd,
        cwd=str(EXAMPLES_DIR.parent),
        capture_output=True,
        text=True,
        timeout=EXAMPLE_TIMEOUT,
    )


@pytest.fixture(scope="module")
def worker() -> Iterator[None]:
    with _spawn_worker():
        yield


@pytest.mark.parametrize("script", _example_scripts(), ids=lambda p: p.name)
def test_example_runs(worker: None, script: Path) -> None:
    result = _run_example(script)
    if result.returncode != 0:
        pytest.fail(
            f"{script.name} exited with {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )


def main() -> int:
    scripts = _example_scripts()
    print(f"Running {len(scripts)} example(s) against {BACKEND}")
    failures: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    with _spawn_worker():
        for script in scripts:
            print(f"--- {script.name} ", end="", flush=True)
            t0 = time.monotonic()
            result = _run_example(script)
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
