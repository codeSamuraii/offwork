# Quick Start

This guide walks you through pyfuse's features step by step.

## Installation

```bash
pip install pyfuse
pip install pyfuse[redis]   # for Redis backend (multi-machine)
```

## 1. Run a function remotely

### Mark the entry point with `@trace`

```python
import math
from pyfuse import trace

def add(a: int, b: int) -> int:
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))
```

Only the entry point needs `@trace`. Everything it calls — `add()`, `math.sqrt()`, etc. — is captured automatically via AST analysis.

### Start a worker

```bash
pyfuse worker --backend local://localhost:9748 --tmp
```

`--tmp` runs in an isolated temporary venv (cleaned up on exit).

### Submit work

```python
import asyncio
import math
import pyfuse
from pyfuse import trace

pyfuse.connect("local://localhost:9748")

def add(a: int, b: int) -> int:
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

async def main():
    result = await hypotenuse.run(3.0, 4.0)
    print(result)  # 5.0

asyncio.run(main())
```

`.run()` serializes the function and its dependencies, sends everything to the worker, and returns the result.

## 2. Async API

All remote execution methods are coroutines:

```python
# Submit and await result
result = await func.run(3.0, 4.0)

# Submit, get a handle, await later
future = await func.start(3.0, 4.0)
result = await future

# Batch submit
results = await func.map([(3, 4), (5, 12), (8, 15)])

# Concurrent execution
r1, r2 = await asyncio.gather(func.run(3, 4), func.run(5, 12))
```

`async def` functions work transparently — workers await them directly.

## 3. Retry and timeout

```python
@trace(timeout=30, retries=3)
def flaky_task(url: str) -> str:
    ...
```

Each attempt is capped at 30 seconds. Retries use exponential backoff (1s, 2s, 4s).

## 4. Class methods

`@trace` works on methods. `self.method()` dependencies are detected automatically:

```python
class Greeter:
    @trace
    def greet(self, name: str) -> str:
        return self.format_greeting(f"Hello, {name}!")

    def format_greeting(self, msg: str) -> str:
        return f"*** {msg} ***"

g = Greeter()
result = await g.greet.run(g, "pyfuse")  # "*** Hello, pyfuse! ***"
```

The worker reconstructs the entire class with all required methods, including `super()` chains, `@dataclass` decorators, and metaclass keywords.

## 5. Third-party packages

Workers auto-install missing packages. When the import name doesn't match the pip package:

```python
from pyfuse import install_package_as

with install_package_as("PyYAML"):
    import yaml

@trace
def to_yaml(data: object) -> str:
    return yaml.dump(data, default_flow_style=False)
```

Common mappings (`cv2` → `opencv-python`, `PIL` → `Pillow`, etc.) are built in.

## 6. Progress reporting

```python
from pyfuse import trace, progress

@trace
def process_batch(items: list[str]) -> list[str]:
    results = []
    for i, item in enumerate(items):
        results.append(transform(item))
        progress(i + 1, len(items), message=f"Processing {item}")
    return results
```

Query from the client:

```python
future = await process_batch.start(items)

while not await future.done():
    p = await future.progress()
    if p:
        print(f"{p.current}/{p.total} ({p.percent:.0f}%) - {p.message}")
    await asyncio.sleep(0.5)

result = await future
```

`progress()` is a silent no-op when called outside a worker.

## 7. Task cancellation

```python
from pyfuse import TaskCancelled

future = await slow_task.start(data)
await future.cancel()

try:
    result = await future
except TaskCancelled:
    print("Task was cancelled")
```

## 8. Result handling

```python
future = await func.start(3.0, 4.0)

await future.done()       # True / False
await future.status()     # "pending" | "success" | "error" | "cancelled"
await future.cancel()     # cancel the task
result = await future     # await the result

# With options
result = await future.result(timeout=10, stall_timeout=5.0)

# Errors are re-raised
from pyfuse import RemoteError, TaskStalled
try:
    result = await future.result()
except RemoteError as e:
    print(e)  # includes remote traceback
except TaskStalled as e:
    print(e)  # worker stopped responding
```

## 9. Backends

| Backend | URL scheme | Use case |
|---------|-----------|----------|
| Local | `local://` | Same-machine IPC (async TCP, no external deps) |
| Redis | `redis://` / `rediss://` | Multi-machine production |

```python
pyfuse.connect("local://localhost:9748")
pyfuse.connect("redis://localhost:6379")
```

Or via environment variable:

```bash
export PYFUSE_BACKEND=redis://localhost:6379
```

## 10. Worker options

```bash
pyfuse worker --backend redis://localhost:6379 -c 4           # 4 concurrent tasks
pyfuse worker --backend redis://localhost:6379 --no-auto-install  # no pip installs
pyfuse worker --backend redis://localhost:6379 --tmp          # isolated temp venv
pyfuse worker --backend redis://localhost:6379 --sandbox      # Docker sandbox
pyfuse worker --backend redis://localhost:6379 --pair         # signed execution
```

Programmatic:

```python
await pyfuse.serve("redis://localhost:6379", concurrency=4)
```

## 11. Running scripts

The `pyfuse run` command creates a temporary venv, auto-detects dependencies, installs them, and runs the script:

```bash
pyfuse worker --backend local://localhost:9748 --tmp   # Terminal 1 (if the script submits remote work)
pyfuse run examples/remote_execution.py                # Terminal 2
```

Scripts that call `.run()` or `.start()` need a worker running. Scripts using only local execution don't.

## Error handling

```python
from pyfuse import Error, RemoteError, TaskCancelled

try:
    trace(len)  # built-in — no source
except Error as e:
    print(e)

try:
    result = await future.result()
except RemoteError as e:
    print(e)  # includes remote traceback
except TaskCancelled:
    print("Cancelled")
```

## Next steps

- **[Sandbox](SANDBOX.md)** — Docker container isolation
- **[Signing & Pairing](SIGNING.md)** — Cryptographic task authentication
- **[Technical Overview](TECHNICAL_OVERVIEW.md)** — Architecture, serialization format, internals
