# offwork

[![PyPI](https://img.shields.io/pypi/v/offwork)](https://pypi.org/project/offwork/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-strict%20mypy-blue)](https://mypy-lang.org/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)]()

**Run any Python function on a remote worker with just two lines of code.**

Put `.connect()` somewhere at the start of your script, add `@offwork.task` to your function, that's it.
You can now run it remotely — no shared codebase, no deployment pipeline.

`offwork` captures its entire dependency graph (helpers, imports, closures, constants) and ships it to the worker as a self-contained payload. The worker doesn't need to have any prior knowledge of your code.

## Quick start

```bash
pip install offwork
offwork worker --backend local://localhost:9748 --tmp  # start a worker in a temp venv
```

```python
import asyncio, math, offwork

offwork.connect("local://localhost:9748")

def add(a: float, b: float) -> float:
    return a + b

@offwork.task  # only the entry point needs this - add() is captured automatically
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

async def main():
    print(await hypotenuse.run(3.0, 4.0))  # 5.0

asyncio.run(main())
```

`.run()` serializes the function graph, submits it to the worker, and returns the result. The worker reconstructs source, installs any missing packages, and executes.

### Multi-machine

Swap `local://` for Redis or RabbitMQ to run on a remote worker:

```bash
pip install offwork[redis]
offwork worker --backend redis://other-machine:6379
```

See [Features](docs/FEATURES.md) for the full API.

## Features

| | |
|-|-|
| **Async-native** | `.run()`, `.start()`, `.map()`, `asyncio.gather` — all coroutines |
| **Scheduling** | `.run_in(delay)`, `.run_at(datetime)`, `.run_every(interval)` with cancellation |
| **Retry & timeout** | `@offwork.task(timeout=30, retries=3)` with exponential backoff |
| **Throttling** | `@offwork.task(throttle=timedelta(hours=24)/50)` — rate-limit executions |
| **Progress & cancellation** | `offwork.progress(3, 10)` inside tasks; `await future.cancel()` on client |
| **Heartbeat & stall detection** | Workers heartbeat every second; clients raise `TaskStalled` on silence |
| **Package auto-install** | Workers `pip install` missing packages before execution |
| **Docker sandbox** | Optional container isolation, fully transparent to clients |
| **Signed execution** | Pre-shared token or PIN pairing + HMAC-SHA256 task authentication |

### Security

#### Signing

To make sure your worker only executes trusted code, use `--require-signing`.<br>
Configure a shared token or pair interactively with a PIN. See [Signing & Pairing](docs/SIGNING.md) for details.

```bash
# Token (CI/CD)
offwork token generate
export OFFWORK_SIGNING_TOKEN=<token>                         # client & worker
offwork worker --backend redis://localhost:6379 --require-signing

# PIN pairing (interactive)
offwork worker --backend redis://localhost:6379 --pair       # shows 6-digit PIN
offwork pair --backend redis://localhost:6379                # client: enter PIN
```

After pairing, tasks are signed automatically with no client code changes. See [Signing & Pairing](docs/SIGNING.md).

#### Sandbox

To avoid side-effects on the worker machine, run tasks inside Docker containers:

```bash
offwork sandbox setup                                      # build image (once)
offwork worker --backend redis://localhost:6379 --sandbox  # run with isolation
```

See [Sandbox](docs/SANDBOX.md) for configuration.

## Documentation

| | |
|-|-|
| **[Features](docs/FEATURES.md)** | Full feature guide and API walkthrough |
| **[Technical Overview](docs/TECHNICAL_OVERVIEW.md)** | Architecture, serialization format, internals |
| **[Signing & Pairing](docs/SIGNING.md)** | Cryptographic task signing protocol |
| **[Sandbox](docs/SANDBOX.md)** | Docker container isolation |

## Examples

```bash
offwork worker --backend local://localhost:9748 --tmp  # start worker
offwork run examples/remote_execution.py              # run any example
```

See [examples/README.md](examples/README.md) for a guide to all examples.

## License

[AGPL-3.0](LICENSE)
