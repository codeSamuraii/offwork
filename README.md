# pyfuse

**Run any Python function on a remote worker — zero setup, zero deployment.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-strict%20mypy-blue)](https://mypy-lang.org/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)]()

Add `@trace` to a function. pyfuse captures its source code, dependencies, and imports automatically. Workers reconstruct and execute everything from scratch — no shared filesystem, no Docker image to build, no deployment pipeline. Missing packages are installed on the fly.

## Quick start

**Install:**

```bash
pip install pyfuse
```

**Write your code** — only the entry point needs `@trace`:

```python
# my_script.py
import asyncio, math, pyfuse
from pyfuse import trace

pyfuse.connect("local://localhost:9748")

def add(a, b):
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

async def main():
    result = await hypotenuse.run(3.0, 4.0)
    print(result)  # 5.0

asyncio.run(main())
```

**Start a worker and run:**

```bash
pyfuse worker --backend local://localhost:9748 --tmp   # Terminal 1
python my_script.py                                    # Terminal 2 → 5.0
```

`--tmp` creates an isolated venv that's cleaned up on exit. For multi-machine setups, swap `local://` for `redis://`:

```bash
pyfuse worker --backend redis://localhost:6379 --tmp
```

That's it. See the **[Quick Start guide](docs/QUICK_START.md)** for the full tutorial.

## How it works

`@trace` uses AST analysis to capture everything the function touches — helper functions, class hierarchies, module constants, closures, third-party imports. The client serializes it all into a self-contained JSON payload. The worker reconstructs and executes a runnable Python script from it — no shared filesystem or prior deployment needed.

See the [Technical Overview](docs/TECHNICAL_OVERVIEW.md) for the full execution flow and architecture.

## Features

| Feature | Description |
|---------|-------------|
| **Automatic dependency capture** | Helpers, classes, constants, closures — all detected recursively via AST |
| **Package auto-install** | Workers `pip install` missing packages before execution |
| **Async-native** | `.run()`, `.start()`, `.map()`, `asyncio.gather` — all built on `asyncio` |
| **Retry & timeout** | `@trace(timeout=30, retries=3)` with exponential backoff |
| **Progress reporting** | `pyfuse.progress(3, 10)` inside tasks, `await future.progress()` on client |
| **Task cancellation** | `await future.cancel()` — cooperative, raises `TaskCancelled` |
| **Heartbeat & stall detection** | Workers send heartbeats; clients raise `TaskStalled` on silence |
| **Content-hash caching** | Same code = cache hit, regardless of which client sent it |
| **Pluggable backends** | `redis://` for multi-machine, `local://` for same-machine IPC |
| **Docker sandbox** | Isolate execution in containers — transparent to clients |
| **Signed execution** | PIN-based pairing + HMAC-SHA256 — workers reject untrusted tasks |
| **Graceful shutdown** | Ctrl+C waits for in-flight tasks; second Ctrl+C force-quits |

## Security

### Sandboxed execution

Run tasks inside Docker containers for isolation. No client-side changes needed:

```bash
pyfuse sandbox setup                                      # build the sandbox image (once)
pyfuse worker --backend redis://localhost:6379 --sandbox  # run with isolation
```

### Signed execution

PIN-based pairing ensures workers only execute code from trusted clients:

```bash
# Worker — displays a 6-digit PIN, then starts serving once paired
pyfuse worker --backend redis://localhost:6379 --pair

# Client — enter the PIN shown by the worker
pyfuse pair --backend redis://localhost:6379
```

After pairing, tasks are signed automatically. See [Signing & Pairing](docs/SIGNING.md) for details.

## Examples

```bash
pyfuse worker --backend local://localhost:9748 --tmp     # start a worker
pyfuse run examples/remote_execution.py                  # run an example
```

| Example | What it shows |
|---------|--------------|
| [`remote_execution.py`](examples/remote_execution.py) | Basic remote execution with auto-discovered dependencies |
| [`async_execution.py`](examples/async_execution.py) | `.run()`, `.start()`, `.map()`, `asyncio.gather` |
| [`package_installation.py`](examples/package_installation.py) | Third-party package auto-install on workers |
| [`progress_reporting.py`](examples/progress_reporting.py) | Real-time progress tracking |
| [`cancellation.py`](examples/cancellation.py) | Cancelling pending or in-progress tasks |
| [`large_module.py`](examples/large_module.py) | Stress test — 47 functions across 7 files, one `@trace` |

## Documentation

| Doc | Content |
|-----|---------|
| **[Quick Start](docs/QUICK_START.md)** | Tutorial — installation, usage, API walkthrough |
| **[Technical Overview](docs/TECHNICAL_OVERVIEW.md)** | Architecture, internals, serialization format |
| **[Signing & Pairing](docs/SIGNING.md)** | Cryptographic task signing protocol |
| **[Sandbox](docs/SANDBOX.md)** | Docker container isolation |

## License

[AGPL-3.0](LICENSE)
