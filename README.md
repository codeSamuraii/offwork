# pyfuse

**Run any Python function on a remote worker -- with zero setup on the worker side.**

[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-strict%20mypy-blue)](https://mypy-lang.org/)

pyfuse captures a function's source code, its entire dependency tree, and its imports automatically via a single `@trace` decorator. Workers reconstruct and execute the function from scratch -- no deployment, no shared filesystem, no Docker. Missing packages are installed on the fly.

```python
import math

import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")

def add(a: int, b: int) -> int:
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

future = hypotenuse.run(3.0, 4.0)
print(future.result())  # 5.0
```

Only the entry point needs `@trace`. Everything it calls -- `add()`, imports, class methods -- is captured automatically.

## Installation

```bash
pip install pyfuse
```

For Redis-based remote execution:

```bash
pip install pyfuse[redis]
```

## How it works

```
  Client                              Worker
  ------                              ------
  @trace                              pyfuse worker --backend redis://...
  func.run(args)                      <- listen for tasks
    |                                   |
    +- capture source + deps            +- deserialize graph
    +- serialize to JSON                +- install missing packages
    +- submit via backend ---------->   +- reconstruct source
    |                                   +- compile + exec
    <- wait for result <--------------  +- send result back
```

1. `@trace` analyzes the function's AST to capture source, imports, and called functions (recursively).
2. `.run()` serializes everything into a JSON payload and sends it to a worker via the configured backend.
3. The worker reconstructs a self-contained Python script, installs any missing packages, executes the function, and returns the result.
4. Content-hash-based caching means identical code is never rebuilt twice.

## Quick start

### 1. Start a worker

```bash
pyfuse worker --backend redis://localhost:6379
```

### 2. Run the example above

The worker reconstructs `hypotenuse` and `add` from source, executes, and returns `5.0`. No code needs to be deployed to the worker.

## Features

### Automatic dependency detection

pyfuse walks the AST to find every function call, import, and method reference your function depends on. Untraced helpers are auto-discovered recursively:

```python
@trace
def pipeline(data):
    cleaned = clean(data)        # auto-discovered
    return transform(cleaned)    # auto-discovered
```

### Class methods

`@trace` works on methods. `self.method()` dependencies are detected automatically:

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

### Retry and timeout

```python
@trace(timeout=30, retries=3)
def flaky_task(url: str) -> str:
    ...
```

Each attempt is capped at 30 seconds. On failure, retries use exponential backoff (1s, 2s, 4s).

### Third-party packages

Workers auto-install missing packages. When the import name doesn't match the pip package, use `install_package_as`:

```python
from pyfuse import install_package_as

with install_package_as("PyYAML"):
    import yaml

@trace
def to_yaml(data: object) -> str:
    return yaml.dump(data, default_flow_style=False)
```

Common mappings (`cv2` -> `opencv-python`, `PIL` -> `Pillow`, etc.) are built in.

### Batch submission

```python
futures = hypotenuse.map([(3.0, 4.0), (5.0, 12.0), (8.0, 15.0)])
results = [f.result() for f in futures]  # [5.0, 13.0, 17.0]
```

### Result handling

```python
future = hypotenuse.run(3.0, 4.0)

future.done()    # True / False
future.status    # "pending", "success", or "error"
future.result(timeout=10)  # blocks, raises TimeoutError if too slow

# Remote errors are re-raised with the remote traceback
from pyfuse import RemoteError
try:
    future.result()
except RemoteError as e:
    print(e)
```

### Serialization and reconstruction

Use the serialization layer directly for inspection, caching, or custom transports:

```python
from pyfuse import serialize, reconstruct

# Serialize a function's dependency graph to JSON
graph_json = serialize(hypotenuse)

# Reconstruct a self-contained Python script
source = reconstruct(graph_json, "hypotenuse")
print(source)
# import math
#
# def add(a: int, b: int) -> int:
#     return a + b
#
# def hypotenuse(a: float, b: float) -> float:
#     return math.sqrt(add(a ** 2, b ** 2))
```

### Pack and execute manually

```python
from pyfuse import pack, execute, Task

task = pack(hypotenuse, 3.0, 4.0)
task_json = task.to_json()  # send over any transport

# On the receiving side
task = Task.from_json(task_json)
result = execute(task)  # 5.0
```

## Backends

| Backend | URL scheme | Use case |
|---------|-----------|----------|
| Redis | `redis://` / `rediss://` | Production, multi-machine |
| Shared memory | `shm://` | Same-machine IPC, zero-copy |

```python
pyfuse.connect("redis://localhost:6379")
pyfuse.connect("shm://localhost:9847")
```

Or via environment variable:

```bash
export PYFUSE_BACKEND=redis://localhost:6379
```

Custom backends can be created by subclassing `pyfuse.Backend`.

## Worker options

```bash
# 4 concurrent threads
pyfuse worker --backend redis://localhost:6379 -c 4

# Disable automatic pip installs
pyfuse worker --backend redis://localhost:6379 --no-auto-install
```

Or programmatically:

```python
import pyfuse
pyfuse.serve("redis://localhost:6379", concurrency=4)
```

## CLI

```bash
pyfuse worker --backend URL [-c N] [--no-auto-install]   # Start a worker
pyfuse info                                               # Show version and config
pyfuse serialize module:function                          # Serialize a function to JSON
pyfuse reconstruct graph.json function_name               # Reconstruct source from graph
```

## What gets captured

| Detected | How |
|----------|-----|
| Direct function calls (`helper()`) | AST analysis + auto-discovery |
| `self.method()` / `cls.method()` | AST analysis |
| `obj.method()` with type annotation | Annotation resolution |
| `obj.method()` without annotation | Runtime tracing on first call |
| Standard library imports | Kept as import statements |
| Third-party imports | Kept as imports, auto-installed on worker |
| Closure variables | Captured via `repr()` |
| Generators and async functions | Supported with proxy wrappers |

## Limitations

- **Python 3.13+** required
- Functions must be defined in `.py` files (no builtins, no REPL, no `exec`'d code)
- Circular dependencies raise `CycleError`
- `obj.method()` without type annotations needs at least one runtime call for tracing
- Dynamic imports (`__import__()`, `importlib.import_module()`) are not detected

## Examples

- **[`examples/serialization.py`](examples/serialization.py)** -- Serialize and reconstruct functions (no external services)
- **[`examples/remote_execution.py`](examples/remote_execution.py)** -- Remote execution with functions, class methods, and retries
- **[`examples/package_installation.py`](examples/package_installation.py)** -- Auto-installing third-party packages on workers
- **[`examples/large_module.py`](examples/large_module.py)** -- Stress test: 47 functions across 7 files, one `@trace`

## Documentation

- **[Quick Start](docs/QUICK_START.md)** -- Get up and running
- **[Technical Overview](docs/TECHNICAL_OVERVIEW.md)** -- Architecture and internals

## License

[AGPL-3.0](LICENSE)
