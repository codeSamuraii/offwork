# Quick Start

## Install

```bash
pip install pyfuse
pip install pyfuse[redis]   # for Redis backend (multi-machine)
```

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

## Third-party packages

Workers auto-install missing packages. When the import name differs from the pip package:

```python
from pyfuse import install_package_as

with install_package_as("PyYAML"):
    import yaml
```

Common mappings (`cv2` → `opencv-python`, `PIL` → `Pillow`, etc.) are built in.

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

PIN-based pairing + HMAC-SHA256 — workers reject untrusted or tampered tasks:

```bash
pyfuse worker --backend redis://localhost:6379 --pair   # displays a 6-digit PIN
pyfuse pair --backend redis://localhost:6379             # on client: enter the PIN
```

After pairing, tasks are signed automatically. No client-side code changes. See [Signing & Pairing](SIGNING.md) for details.

## Backends

| Backend | URL | Use case |
|---------|-----|----------|
| Local | `local://host:port` | Same-machine IPC (async TCP, no deps) |
| Redis | `redis://host:port` | Multi-machine production |

```python
pyfuse.connect("local://localhost:9748")
pyfuse.connect("redis://localhost:6379")
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

## Framework integration (FastAPI / Starlette)

Use the built-in ASGI integration for seamless lifecycle management:

```python
from fastapi import FastAPI
from pyfuse import trace
from pyfuse.integrations.asgi import pyfuse_lifespan

app = FastAPI(lifespan=pyfuse_lifespan("redis://localhost:6379"))

@trace
def heavy_task(x: float) -> float: ...

@app.post("/compute")
async def compute(x: float):
    return {"result": await heavy_task.run(x)}
```

`pyfuse_lifespan` connects the backend on startup and disconnects on shutdown.
For apps that don't support the lifespan protocol, use `PyfuseMiddleware` instead:

```python
from pyfuse.integrations.asgi import PyfuseMiddleware

app = PyfuseMiddleware(app, url="redis://localhost:6379")
```

See [`examples/fastapi_app.py`](../examples/fastapi_app.py) for a complete example.

## Next steps

- **[Technical Overview](TECHNICAL_OVERVIEW.md)** — Architecture, serialization format, internals
- **[Sandbox](SANDBOX.md)** — Docker container isolation setup and management
- **[Signing & Pairing](SIGNING.md)** — Cryptographic task authentication protocol
