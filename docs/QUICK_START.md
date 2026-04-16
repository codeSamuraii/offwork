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
pyfuse worker --backend redis://localhost:6379
```

The worker waits for tasks, reconstructs the function source on the fly, and executes it -- no prior knowledge of your code required.

### 3. Submit work

```python
import asyncio
import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

async def main():
    # Call locally (unchanged behavior)
    result = hypotenuse(3.0, 4.0)     # 5.0

    # Run on a remote worker
    result = await hypotenuse.run(3.0, 4.0)  # 5.0

asyncio.run(main())
```

`.run()` serializes the function and its dependencies, sends everything to the worker, waits for the result, and returns it directly.

## Retry and timeout

```python
@trace(timeout=30, retries=3)
def flaky_task(url: str) -> str:
    ...

result = await flaky_task.run("https://example.com")
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
result = await g.greet.run(g, "pyfuse")
print(result)  # "*** Hello, pyfuse! ***"
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
result = await to_yaml.run({"key": "value"})
```

Common mappings like `cv2` -> `opencv-python` and `PIL` -> `Pillow` are built in.

## Task cancellation

Cancel a pending or in-progress task with `await future.cancel()`:

```python
from pyfuse import TaskCancelled

future = await slow_task.start(data)
await asyncio.sleep(1)
await future.cancel()

try:
    result = await future
except TaskCancelled:
    print("Task was cancelled")
```

If the worker hasn't started execution yet, it skips the task entirely. If execution is already in progress, the client receives `TaskCancelled` when awaiting the result.

## Progress reporting

Long-running tasks can report progress back to the client:

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

Query progress from the client:

```python
future = await process_batch.start(items)

while not await future.done():
    p = await future.progress()
    if p is not None:
        print(f"{p.current}/{p.total} ({p.percent:.0f}%) - {p.message}")
    await asyncio.sleep(0.5)

result = await future
```

`progress()` is a silent no-op when called outside a worker, so the function works unchanged locally.

## Async API

pyfuse is async-native. All remote execution methods are coroutines.

### .run() -- submit and await

`.run()` submits the task to a worker and awaits the result:

```python
async def main():
    result = await hypotenuse.run(3.0, 4.0)
    print(result)  # 5.0
```

### .start() -- submit and get a handle

`.start()` submits the task and returns a `Result` handle for deferred awaiting:

```python
async def main():
    # Start a task, get a Result handle
    future = await hypotenuse.start(3.0, 4.0)

    # Do other work while the task runs...
    await asyncio.sleep(1)

    # Await the result when needed
    result = await future
    print(result)  # 5.0

    # Or use .result() with options
    result = await future.result(timeout=10.0)
```

### .map() -- batch submit and await

`.map()` submits a batch of tasks and awaits all results:

```python
async def main():
    results = await hypotenuse.map([(3.0, 4.0), (5.0, 12.0), (8.0, 15.0)])
    print(results)  # [5.0, 13.0, 17.0]
```

### asyncio.gather

Use `.run()` or `.start()` with `asyncio.gather` for concurrent execution of different tasks:

```python
async def main():
    # .run() returns a coroutine, so gather works directly
    r1, r2, r3 = await asyncio.gather(
        hypotenuse.run(3.0, 4.0),
        hypotenuse.run(5.0, 12.0),
        hypotenuse.run(8.0, 15.0),
    )

    # Or start tasks and gather the Result handles
    futures = [await hypotenuse.start(a, b) for a, b in [(3, 4), (5, 12)]]
    results = await asyncio.gather(*futures)
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

result = await fetch_and_process.run("https://example.com")
```

## Task signing and trusted workers

By default, any client can submit tasks to a worker. Enable Ed25519 signing to restrict access to trusted clients.

### Setup

```bash
# Install signing dependencies
pip install pyfuse[pairing]

# Generate a key pair on the client
pyfuse keypair generate -o ~/.pyfuse/my_key.pem

# Pair with the worker — the worker waits for the client to connect
# (run these two commands at the same time in separate terminals)
pyfuse pair accept --backend redis://localhost:6379 --trusted-keys /etc/pyfuse/keys  # worker
pyfuse pair request --backend redis://localhost:6379 --code 847291                   # client (uses the key generated above)

# Start the worker with signature verification
pyfuse worker --backend redis://localhost:6379 --trusted-keys /etc/pyfuse/keys
```

### Sign tasks from the client

```python
from pyfuse.core.signing import KeyPair

keypair = KeyPair.from_file("~/.pyfuse/my_key.pem")

result = await my_function.run(..., _keypair=keypair)
```

Workers reject tasks that are unsigned or signed by an untrusted key with `TrustError`.

See the [Security guide](SECURITY.md) for full details, including the pairing protocol, programmatic API, and error handling.

## Worker options

```bash
# 4 concurrent tasks
pyfuse worker --backend redis://localhost:6379 -c 4

# Disable automatic pip installs
pyfuse worker --backend redis://localhost:6379 --no-auto-install

# Run in an isolated temporary venv (auto-cleaned on exit)
pyfuse worker --backend redis://localhost:6379 --tmp

# Run tasks inside a Docker sandbox
pyfuse worker --backend redis://localhost:6379 --sandbox docker

# Run tasks inside a tart micro-VM (macOS Apple Silicon)
pyfuse worker --backend redis://localhost:6379 --sandbox vm
```

Or start a worker programmatically:

```python
import asyncio
import pyfuse

asyncio.run(pyfuse.serve("redis://localhost:6379", concurrency=4))
```

## Sandboxed execution

By default, workers run tasks in the host process. For security or isolation, you can run tasks inside Docker containers or tart micro-VMs. Sandboxing is fully transparent to clients.

### Quick setup

```bash
# Docker (any platform)
pyfuse sandbox setup --docker
pyfuse worker --backend redis://localhost:6379 --sandbox docker

# tart VM (macOS Apple Silicon only)
pyfuse sandbox setup
pyfuse worker --backend redis://localhost:6379 --sandbox vm
```

See the [Sandbox guide](SANDBOX.md) for full setup and management instructions.

## Running scripts in a temporary venv

The `run` command creates an isolated venv, auto-detects third-party dependencies from the script (including `install_package_as` blocks), installs them, and runs the script:

```bash
pyfuse run examples/remote_execution.py
```

## Backends

pyfuse supports pluggable transport backends:

| Backend | URL scheme | Use case |
|---------|-----------|----------|
| Redis | `redis://` / `rediss://` | Production, multi-machine |
| Local | `local://` | Same-machine IPC (async-native TCP) |

```python
# Redis (requires pip install redis)
pyfuse.connect("redis://localhost:6379")

# Local (no external services needed)
pyfuse.connect("local://localhost:9748")
```

The backend can also be set via the `PYFUSE_BACKEND` environment variable:

```bash
export PYFUSE_BACKEND=redis://localhost:6379
```

## Result handling

```python
# .run() returns the result directly
result = await hypotenuse.run(3.0, 4.0)  # 5.0

# .start() returns a Result handle
future = await hypotenuse.start(3.0, 4.0)

# Check status without blocking
await future.done()      # True / False
await future.status()    # "pending", "success", "error", or "cancelled"

# Check progress (from pyfuse.progress() calls)
p = await future.progress()    # ProgressInfo or None
if p: print(f"{p.current}/{p.total}")

# Cancel a task
await future.cancel()    # raises TaskCancelled when awaited

# Await the result
result = await future                          # shorthand
result = await future.result(timeout=10)       # with timeout
result = await future.result(stall_timeout=5)  # with stall detection

# Remote errors are re-raised on the client
from pyfuse import RemoteError
try:
    result = await future.result()
except RemoteError as e:
    print(e)  # includes the remote traceback
```

## Heartbeat and stall detection

Workers send periodic heartbeats while executing tasks. Clients detect stalled tasks when heartbeats stop arriving.

```python
from pyfuse import TaskStalled

# Stall detection enabled by default (10s threshold)
try:
    result = await future.result(stall_timeout=10.0)
except TaskStalled as e:
    print(e)  # "Task abc123 stalled: no heartbeat for 12.3s"

# Disable stall detection
result = await future.result(stall_timeout=None)
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
  "version": "0.4.0",
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
result = await execute(task)      # 5.0
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
    "version": "0.4.0",
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
| Class-level attributes (`count = 0`) | Extracted from class source AST |
| Class decorators (`@dataclass`) | Extracted from class source AST |
| Metaclass keywords (`metaclass=ABCMeta`) | Extracted from class definition keywords |
| Standard library imports (`json`, `csv`) | Kept as import statements |
| Third-party imports (`numpy`, `yaml`) | Kept as imports, auto-installed on worker |
| Closure variables | Captured via `repr()`, constructor expressions, or pickle |
| Non-traced functions in closures | Auto-discovered and registered as dependencies |
| Lambda functions in closures | Source extracted via AST |
| `__slots__` objects as arguments | Serialized via MRO slot inspection |
| Generators and async functions | Supported with proxy wrappers |

## What is NOT captured

- Functions without source code (builtins, `exec`'d, REPL-defined)
- Dynamic imports (`__import__()`, `importlib.import_module()` in function bodies)
- Relative star imports (`from . import *`)
- Circular dependencies (raises `CycleError`)

## Error handling

```python
from pyfuse import trace, Error, RemoteError, TaskCancelled

# Tracing errors
try:
    trace(len)  # built-in, no source
except Error as e:
    print(e)  # "Cannot trace function 'len': source code unavailable..."

# Remote execution errors
try:
    result = await future.result()
except RemoteError as e:
    print(e)  # includes remote traceback
except TaskCancelled:
    print("Task was cancelled")
```

## Running the examples

```bash
# Remote execution
pyfuse worker --backend local://localhost:9748 --tmp   # Terminal 1
pyfuse run examples/remote_execution.py               # Terminal 2
```

The `--tmp` flag creates an isolated temporary venv for the worker, which is auto-cleaned on exit.
The `pyfuse run` command also creates a temporary venv, auto-detects dependencies from the script, installs them, and runs the script -- no setup required.
