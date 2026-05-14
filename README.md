# away

**Run any Python function on a remote worker — zero setup, zero deployment.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-strict%20mypy-blue)](https://mypy-lang.org/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)]()

Add `@trace` to a function. away captures its source, dependencies, and imports automatically.
Workers reconstruct and execute everything from scratch — no shared filesystem, no deployment pipeline.
Missing packages are installed on the fly.

## Quick start

```bash
pip install away
```

```python
import asyncio, math, away
from away import trace

away.connect("local://localhost:9748")

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
away worker --backend local://localhost:9748 --tmp   # start a worker
python my_script.py                                    # → 5.0
```

For multi-machine, swap `local://` for `redis://` or an `https://` managed broker URL. That's it.

## Sandbox

Run tasks inside Docker containers for isolation — transparent to clients:

```bash
away sandbox setup                                      # build image (once)
away worker --backend redis://localhost:6379 --sandbox  # run with isolation
```

See [Sandbox](docs/SANDBOX.md) for configuration and management.

## Signing

Pre-shared token or PIN-based pairing + HMAC-SHA256 — workers reject untrusted or tampered tasks:

```bash
# Token-based (recommended for CI/CD)
away token generate                                       # generate once
export AWAY_SIGNING_TOKEN=<token>                         # set on client & worker
away worker --backend redis://localhost:6379 --require-signing

# PIN-based pairing (interactive)
away worker --backend redis://localhost:6379 --pair       # displays a 6-digit PIN
away pair --backend redis://localhost:6379                # on client: enter the PIN
```

After setup, tasks are signed automatically. No client-side code changes. See [Signing & Pairing](docs/SIGNING.md) for details.

## Features

| | |
|-|-|
| **Auto dependency capture** | Functions, classes, constants, closures — recursive AST analysis |
| **Package auto-install** | Workers `pip install` missing packages before execution |
| **Async-native** | `.run()`, `.start()`, `.map()`, `asyncio.gather` |
| **Retry & timeout** | `@trace(timeout=30, retries=3)` with exponential backoff |
| **Scheduling** | `.run_in(delay)`, `.run_at(datetime)`, `.run_every(freq)` with cancellation |
| **Throttling** | `@trace(throttle=timedelta(hours=24)/50)` — rate-limit executions |
| **Progress & cancellation** | `away.progress(3, 10)` inside tasks; `await future.cancel()` on client |
| **Heartbeat & stall detection** | Workers heartbeat; clients raise `TaskStalled` on silence |
| **Content-hash caching** | Same code = cache hit, regardless of client |
| **Pluggable backends** | `local://` (same-machine TCP), `redis://`, `amqp://` (RabbitMQ), `http://`/`https://` (hosted broker API) |
| **Docker sandbox** | Container isolation, transparent to clients |
| **Signed execution** | Pre-shared token or PIN pairing + HMAC-SHA256 task authentication |
| **Graceful shutdown** | Ctrl+C drains in-flight tasks; second Ctrl+C force-quits |

## Documentation

| | |
|-|-|
| **[Quick Start](docs/QUICK_START.md)** | Tutorial and API walkthrough |
| **[Technical Overview](docs/TECHNICAL_OVERVIEW.md)** | Architecture, serialization format, internals |
| **[Signing & Pairing](docs/SIGNING.md)** | Cryptographic task signing protocol |
| **[Sandbox](docs/SANDBOX.md)** | Docker container isolation |
| **[Cloud POC](docs/CLOUD_POC.md)** | Local FastAPI + MongoDB + Kubernetes + React prototype for managed hosting |

## Examples

```bash
away worker --backend local://localhost:9748 --tmp
away run examples/remote_execution.py
```

[`remote_execution.py`](examples/remote_execution.py) · [`async_execution.py`](examples/async_execution.py) · [`package_installation.py`](examples/package_installation.py) · [`progress_reporting.py`](examples/progress_reporting.py) · [`cancellation.py`](examples/cancellation.py) · [`scheduling.py`](examples/scheduling.py) · [`throttling_and_retry.py`](examples/throttling_and_retry.py) · [`large_module.py`](examples/large_module.py)

## License

[AGPL-3.0](LICENSE)
