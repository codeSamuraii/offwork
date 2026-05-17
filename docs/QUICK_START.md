# Quick Start

## Install

```bash
pip install pyfuse
pip install pyfuse[redis]      # Redis backend (multi-machine)
pip install pyfuse[rabbitmq]   # RabbitMQ backend (multi-machine, AMQP)
```

pyfuse itself has zero runtime dependencies. Backend extras are only needed when you actually use the corresponding URL scheme.

## Remote execution

Add `@trace` to the entry point. Everything it calls is captured automatically.

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

```bash
pyfuse worker --backend local://localhost:9748 --tmp   # Terminal 1
python my_script.py                                    # Terminal 2 → 5.0
```

`--tmp` runs the worker in an isolated venv, cleaned up on exit. For multi-machine, swap `local://` for `redis://`.

## Async API

```python
result = await func.run(3.0, 4.0)                          # submit + await
future = await func.start(3.0, 4.0)                        # submit, get handle
result = await future                                       # await later
results = await func.map([(3, 4), (5, 12)])                 # batch
r1, r2 = await asyncio.gather(func.run(3, 4), func.run(5, 12))  # concurrent
```

`async def` functions are awaited directly on the worker.

## Retry and timeout

```python
@trace(timeout=30, retries=3)
def flaky_task(url: str) -> str: ...
```

Retries use exponential backoff (1s, 2s, 4s).

## Scheduling

Execute tasks on a delay, at a specific time, or on a recurring schedule:

```python
from datetime import datetime, timedelta

# Run after a delay
result = await func.run_in(timedelta(minutes=5), *args)

# Run at a specific time
result = await func.run_at(datetime(2026, 4, 21, 9, 0), *args)

# Recurring execution (every hour)
schedule = await func.run_every(timedelta(hours=1), *args)
await schedule.cancel()  # stop the schedule
```

`start_at` and `start_in` return a `Result` handle (like `.start()`).

## Throttling

Rate-limit how often a function can be executed:

```python
from datetime import timedelta

@trace(throttle=timedelta(hours=24) / 50)  # ~29 min cooldown
def expensive_api_call(query: str) -> str: ...
```

If a task arrives during the cooldown window, the worker returns a `ThrottleError` immediately (no retry). The cooldown is only recorded after a successful execution.

## Third-party packages

Workers auto-install missing packages. When the import name differs from the pip package:

```python
from pyfuse import install_package_as

with install_package_as("PyYAML"):
    import yaml
```

Common mappings (`cv2` → `opencv-python`, `PIL` → `Pillow`, etc.) are built in.

### Worker-only imports

Skip installing packages locally — the worker installs them on demand:

```python
from pyfuse import worker_only_import

with worker_only_import():
    import requests

with worker_only_import("opencv-python-headless"):
    import cv2
```

The local `requests` and `cv2` resolve to lightweight stubs. They're fine to reference inside a `@trace` function (the worker re-imports them for real), but raise `WorkerOnlyError` if used directly on the client.

Only the names imported literally inside the `with` block are stubbed — real installed packages and their transitive imports are unaffected.

## Progress, cancellation, and results

```python
from pyfuse import progress, TaskCancelled, RemoteError, TaskStalled

# Inside a task — report progress (no-op when called locally)
@trace
def train(epochs: int) -> float:
    for i in range(epochs):
        ...
        progress(i + 1, epochs, message=f"epoch {i+1}")
    return accuracy

# On the client
future = await train.start(100)

p = await future.progress()             # ProgressInfo or None
if p: print(f"{p.percent:.0f}%")

await future.cancel()                   # cooperative cancellation

try:
    result = await future.result(timeout=60, stall_timeout=10)
except TaskCancelled: ...               # task was cancelled
except TaskStalled: ...                 # worker stopped responding
except RemoteError as e: print(e)       # includes remote traceback
```

## Sandbox

Run tasks inside Docker containers — transparent to clients:

```bash
pyfuse sandbox setup                                      # build image (once)
pyfuse worker --backend redis://localhost:6379 --sandbox   # run with isolation
```

See [Sandbox](SANDBOX.md) for configuration and management.

## Signing

Pre-shared token or PIN-based pairing + HMAC-SHA256 — workers reject untrusted or tampered tasks:

```bash
# Token-based (recommended for CI/CD)
pyfuse token generate                                       # generate once
export PYFUSE_SIGNING_TOKEN=<token>                         # set on client & worker
pyfuse worker --backend redis://localhost:6379 --require-signing

# PIN-based pairing (interactive)
pyfuse worker --backend redis://localhost:6379 --pair       # displays a 6-digit PIN
pyfuse pair --backend redis://localhost:6379                # on client: enter the PIN
```

After setup, tasks are signed automatically. No client-side code changes. See [Signing & Pairing](SIGNING.md) for details.

## Backends

| Backend | URL | Install | Use case |
|---------|-----|---------|----------|
| Local | `local://host:port` | (built-in) | Same-machine IPC (async TCP, no deps) |
| Redis | `redis://host:port` | `pip install pyfuse[redis]` | Multi-machine production |
| RabbitMQ | `amqp://host:port` | `pip install pyfuse[rabbitmq]` | Multi-machine production with AMQP |

```python
pyfuse.connect("local://localhost:9748")
pyfuse.connect("redis://localhost:6379")
pyfuse.connect("amqp://guest:guest@localhost/")
```

Or: `export PYFUSE_BACKEND=redis://localhost:6379`

## Worker options

```bash
pyfuse worker --backend redis://localhost:6379 -c 4              # 4 concurrent tasks
pyfuse worker --backend redis://localhost:6379 --no-auto-install  # skip pip installs
pyfuse worker --backend redis://localhost:6379 --sandbox --pair   # Docker + signing
```

Programmatic:

```python
await pyfuse.serve("redis://localhost:6379", concurrency=4, sandbox=True)
```

## Running scripts

```bash
pyfuse worker --backend local://localhost:9748 --tmp   # Terminal 1
pyfuse run examples/remote_execution.py                # Terminal 2
```

`pyfuse run` creates a temporary venv, auto-detects dependencies, installs them, and runs the script.

## Next steps

- **[Technical Overview](TECHNICAL_OVERVIEW.md)** — Architecture, serialization format, internals
- **[Sandbox](SANDBOX.md)** — Docker container isolation setup and management
- **[Signing & Pairing](SIGNING.md)** — Cryptographic task authentication protocol
