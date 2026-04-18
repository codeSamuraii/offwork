# pyfuse

**Run any Python function on a remote worker — zero setup, zero deployment.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-strict%20mypy-blue)](https://mypy-lang.org/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)]()

Add `@trace` to a function. pyfuse captures its source, dependencies, and imports automatically.
Workers reconstruct and execute everything from scratch — no shared filesystem, no deployment pipeline.
Missing packages are installed on the fly.

## Quick start

```bash
pip install pyfuse
```

```python
import asyncio, math, pyfuse
from pyfuse import trace

pyfuse.connect("local://localhost:9748")

def add(a, b):
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

async def main():
    print(await hypotenuse.run(3.0, 4.0))  # 5.0

asyncio.run(main())
```

Only the entry point needs `@trace` — everything it calls is captured automatically.

```bash
pyfuse worker --backend local://localhost:9748 --tmp   # start a worker
python my_script.py                                    # → 5.0
```

For multi-machine, swap `local://` for `redis://`. That's it.

## Sandbox

Run tasks inside Docker containers for isolation — transparent to clients:

```bash
pyfuse sandbox setup                                      # build image (once)
pyfuse worker --backend redis://localhost:6379 --sandbox  # run with isolation
```

See [Sandbox](docs/SANDBOX.md) for configuration and management.

## Signing

PIN-based pairing + HMAC-SHA256 — workers reject untrusted or tampered tasks:

```bash
pyfuse worker --backend redis://localhost:6379 --pair    # displays a 6-digit PIN
pyfuse pair --backend redis://localhost:6379             # on client: enter the PIN
```

After pairing, tasks are signed automatically. No client-side code changes. See [Signing & Pairing](docs/SIGNING.md) for details.

## Features

| | |
|-|-|
| **Auto dependency capture** | Functions, classes, constants, closures — recursive AST analysis |
| **Package auto-install** | Workers `pip install` missing packages before execution |
| **Async-native** | `.run()`, `.start()`, `.map()`, `asyncio.gather` |
| **Retry & timeout** | `@trace(timeout=30, retries=3)` with exponential backoff |
| **Progress & cancellation** | `pyfuse.progress(3, 10)` inside tasks; `await future.cancel()` on client |
| **Heartbeat & stall detection** | Workers heartbeat; clients raise `TaskStalled` on silence |
| **Content-hash caching** | Same code = cache hit, regardless of client |
| **Pluggable backends** | `redis://` (multi-machine) or `local://` (same-machine TCP) |
| **Docker sandbox** | Container isolation, transparent to clients |
| **Signed execution** | PIN-based pairing + HMAC-SHA256 task authentication |
| **Graceful shutdown** | Ctrl+C drains in-flight tasks; second Ctrl+C force-quits |

## Documentation

| | |
|-|-|
| **[Quick Start](docs/QUICK_START.md)** | Tutorial and API walkthrough |
| **[Technical Overview](docs/TECHNICAL_OVERVIEW.md)** | Architecture, serialization format, internals |
| **[Signing & Pairing](docs/SIGNING.md)** | Cryptographic task signing protocol |
| **[Sandbox](docs/SANDBOX.md)** | Docker container isolation |

## Examples

```bash
pyfuse worker --backend local://localhost:9748 --tmp
pyfuse run examples/remote_execution.py
```

[`remote_execution.py`](examples/remote_execution.py) · [`async_execution.py`](examples/async_execution.py) · [`package_installation.py`](examples/package_installation.py) · [`progress_reporting.py`](examples/progress_reporting.py) · [`cancellation.py`](examples/cancellation.py) · [`large_module.py`](examples/large_module.py)

## License

[AGPL-3.0](LICENSE)
