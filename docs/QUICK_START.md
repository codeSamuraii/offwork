# Quick Start

pyfuse lets you run any Python function on a remote worker -- with zero setup on the worker side. Decorate a function with `@trace`, and pyfuse captures its source code, dependencies, and imports automatically. The worker reconstructs and executes the function from scratch, installing missing packages as needed.

## Installation

```bash
poetry install
```

For Redis-based remote execution:

```bash
pip install redis
```

## Run a function remotely

### 1. Mark functions with `@trace`

```python
import math
from pyfuse import trace


def add(a: int, b: int) -> int:
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))
```

`@trace` captures the function's source and its entire dependency tree. `add()` is included automatically -- it doesn't need `@trace`.

### 2. Start a worker

```bash
python -m pyfuse worker --backend redis://localhost:6379
```

The worker waits for tasks, reconstructs the function source on the fly, and executes it -- no prior knowledge of your code required.

### 3. Submit work

```python
import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

# Call locally (unchanged behavior)
result = hypotenuse(3.0, 4.0)     # 5.0

# Run on a remote worker
future = hypotenuse.run(3.0, 4.0)
result = future.result()           # 5.0 (blocks until worker returns)
```

`.run()` serializes the function and its dependencies, sends everything to the worker, and returns a `Result` future.

## Retry and timeout

```python
@trace(timeout=30, retries=3)
def flaky_task(url: str) -> str:
    ...

future = flaky_task.run("https://example.com")
result = future.result()
```

Each attempt is capped at 30 seconds. On failure, retries use exponential backoff (1s, 2s, 4s).

## Class methods

`@trace` works on methods. Dependencies via `self.method()` are detected automatically:

```python
class Greeter:
    @trace
    def greet(self, name: str) -> str:
        return self.format_greeting(f"Hello, {name}!")

    def format_greeting(self, msg: str) -> str:
        return f"*** {msg} ***"

g = Greeter()
future = g.greet.run(g, "pyfuse")
print(future.result())  # "*** Hello, pyfuse! ***"
```

The worker reconstructs the entire class with all required methods.

## Third-party dependencies

Workers auto-install missing packages via pip. When the import name doesn't match the package name, use `install_package_as`:

```python
from pyfuse import install_package_as

with install_package_as("PyYAML"):
    import yaml

@trace
def to_yaml(data: object) -> str:
    return yaml.dump(data, default_flow_style=False)

# The worker installs PyYAML before executing this
future = to_yaml.run({"key": "value"})
```

Common mappings like `cv2` -> `opencv-python` and `PIL` -> `Pillow` are built in.

## Async support

pyfuse supports `async def` functions and provides async-native APIs for result handling.

### Await a result

Every `Result` is awaitable. Use `await` directly or call `.aresult()`:

```python
import asyncio

async def main():
    # Await a result directly
    result = await hypotenuse.run(3.0, 4.0)
    print(result)  # 5.0

    # Or use .aresult() with options
    future = hypotenuse.run(3.0, 4.0)
    result = await future.aresult(timeout=10.0)
```

### .arun() and .amap()

`.arun()` submits and awaits in one call. `.amap()` submits a batch and awaits all results concurrently:

```python
async def main():
    # Submit and await a single task
    result = await hypotenuse.arun(3.0, 4.0)

    # Submit batch, await all concurrently
    results = await hypotenuse.amap([(3.0, 4.0), (5.0, 12.0), (8.0, 15.0)])
    print(results)  # [5.0, 13.0, 17.0]
```

### asyncio.gather

Results work with `asyncio.gather` for concurrent execution of different tasks:

```python
async def main():
    r1, r2, r3 = await asyncio.gather(
        hypotenuse.run(3.0, 4.0),
        hypotenuse.run(5.0, 12.0),
        hypotenuse.run(8.0, 15.0),
    )
```

### Async functions

`async def` functions are executed transparently on workers:

```python
@trace
async def fetch_and_process(url: str) -> str:
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.text()

result = await fetch_and_process.arun("https://example.com")
```

## Batch submission

Submit multiple tasks at once with `.map()`:

```python
futures = hypotenuse.map([(3.0, 4.0), (5.0, 12.0), (8.0, 15.0)])
results = [f.result() for f in futures]  # [5.0, 13.0, 17.0]
```

## Worker options

```bash
# 4 concurrent threads
python -m pyfuse worker --backend redis://localhost:6379 -c 4

# Disable automatic pip installs
python -m pyfuse worker --backend redis://localhost:6379 --no-auto-install

# Run in an isolated temporary venv (auto-cleaned on exit)
python -m pyfuse worker --backend redis://localhost:6379 --tmp
```

Or start a worker programmatically:

```python
import pyfuse
pyfuse.serve("redis://localhost:6379", concurrency=4)
```

## Running scripts in a temporary venv

The `run` command creates an isolated venv, auto-detects third-party dependencies from the script (including `install_package_as` blocks), installs them, and runs the script:

```bash
python -m pyfuse run examples/remote_execution.py
```

## Backends

pyfuse supports pluggable transport backends:

| Backend | URL scheme | Use case |
|---------|-----------|----------|
| Redis | `redis://` / `rediss://` | Production, multi-machine |
| Shared memory | `shm://` | Same-machine IPC, zero-copy |

```python
# Redis (requires pip install redis)
pyfuse.connect("redis://localhost:6379")

# Shared memory (no external services needed)
pyfuse.connect("shm://localhost:9847")
```

The backend can also be set via the `PYFUSE_BACKEND` environment variable:

```bash
export PYFUSE_BACKEND=redis://localhost:6379
```

## Result handling

```python
future = hypotenuse.run(3.0, 4.0)

# Check status without blocking
future.done()    # True / False
future.status    # "pending", "success", or "error"

# Block until result
result = future.result(timeout=10)  # raises TimeoutError if too slow

# Async await
result = await future                        # shorthand
result = await future.aresult(timeout=10)    # with options

# Remote errors are re-raised on the client
from pyfuse import RemoteError
try:
    future.result()
except RemoteError as e:
    print(e)  # includes the remote traceback
```

## Heartbeat and stall detection

Workers send periodic heartbeats while executing tasks. Clients detect stalled tasks when heartbeats stop arriving.

```python
from pyfuse import TaskStalled

# Async -- stall detection enabled by default (10s threshold)
try:
    result = await future.aresult(stall_timeout=10.0)
except TaskStalled as e:
    print(e)  # "Task abc123 stalled: no heartbeat for 12.3s"

# Sync -- opt-in via stall_timeout parameter
try:
    result = future.result(stall_timeout=10.0)
except TaskStalled as e:
    print(e)

# Disable stall detection
result = await future.aresult(stall_timeout=None)
```

Stall detection only triggers after at least one heartbeat has been observed — a task that hasn't started yet won't be flagged as stalled.

## Advanced: serialization and reconstruction

Under the hood, pyfuse serializes functions into a content-addressable JSON store. You can use this directly for inspection, caching, or custom transports.

### Serialize

```python
from pyfuse import serialize

# Serialize a function and its dependencies
graph_json = serialize(hypotenuse)

# Or serialize all traced functions
graph_json = serialize()
```

The output is a JSON string containing each function's source, imports, and dependency edges, identified by content hashes:

```json
{
  "version": "0.3.0",
  "objects": {
    "a1b2...": {"name": "add", "source": "...", "imports": [], ...},
    "c3d4...": {"name": "hypotenuse", "source": "...", "imports": [...], ...}
  },
  "deps": {"c3d4...": ["a1b2..."]},
  "refs": {"mymodule.add": "a1b2...", "mymodule.hypotenuse": "c3d4..."}
}
```

### Reconstruct

```python
from pyfuse import reconstruct

source = reconstruct(graph_json, "hypotenuse")
print(source)
```

Output:

```python
import math


def add(a: int, b: int) -> int:
    return a + b


def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a ** 2, b ** 2))
```

Functions are emitted in dependency order with deduplicated imports -- a self-contained script ready to execute.

### Pack and execute manually

```python
from pyfuse import pack, execute, Task

# Pack a function with arguments into a Task
task = pack(hypotenuse, 3.0, 4.0)
print(task.task_id)         # "a1b2c3d4e5f6"
print(task.function_name)   # "mymodule.hypotenuse"

# Serialize for any transport
task_json = task.to_json()

# On the receiving side
task = Task.from_json(task_json)
result = execute(task)      # 5.0
```

### Save and load

```python
from pathlib import Path

Path("graph.json").write_text(graph_json)

loaded = Path("graph.json").read_text()
source = reconstruct(loaded, "hypotenuse")
```

### Merge stores

Two serialized stores can be safely merged -- identical content hashes guarantee deduplication:

```python
import json

store_a = json.loads(serialize(func_a))
store_b = json.loads(serialize(func_b))

merged = json.dumps({
    "version": "0.3.0",
    "objects": {**store_a["objects"], **store_b["objects"]},
    "deps": {**store_a["deps"], **store_b["deps"]},
    "refs": {**store_a["refs"], **store_b["refs"]},
})
```

## What gets captured

| Detected | How |
|----------|-----|
| Direct function calls (`helper()`) | AST analysis + auto-discovery |
| Class constructors (`MyClass()`) | Auto-registers all methods of the class |
| `self.method()` / `cls.method()` | AST analysis |
| `@staticmethod` / `@classmethod` | Descriptor unwrapping + auto-discovery |
| `super()` calls and inheritance | Base class discovery + dependency edges |
| `obj.method()` with type annotation | Type annotation resolution |
| `obj.method()` without annotation | Runtime tracing on first call |
| Module-level constants (`MAX = 5`) | Captured and emitted in reconstructed source |
| Standard library imports (`json`, `csv`) | Kept as import statements |
| Third-party imports (`numpy`, `yaml`) | Kept as imports, auto-installed on worker |
| Closure variables | Captured via `repr()` |
| `__slots__` objects as arguments | Serialized via MRO slot inspection |
| Generators and async functions | Supported with proxy wrappers |

## What is NOT captured

- Functions without source code (builtins, `exec`'d, REPL-defined)
- Dynamic imports (`__import__()`, `importlib.import_module()` in function bodies)
- Relative star imports (`from . import *`)
- Metaclasses and `__init_subclass__` hooks
- Circular dependencies (raises `CycleError`)

## Error handling

```python
from pyfuse import trace, Error, RemoteError

# Tracing errors
try:
    trace(len)  # built-in, no source
except Error as e:
    print(e)  # "Cannot trace function 'len': source code unavailable..."

# Remote execution errors
try:
    future.result()
except RemoteError as e:
    print(e)  # includes remote traceback
```

## Running the examples

```bash
# Remote execution
python -m pyfuse worker --backend shm://localhost:9847 --tmp    # Terminal 1
python -m pyfuse run examples/remote_execution.py               # Terminal 2
```

The `--tmp` flag creates an isolated temporary venv for the worker, which is auto-cleaned on exit.
The `pyfuse run` command also creates a temporary venv, auto-detects dependencies from the script, installs them, and runs the script -- no setup required.