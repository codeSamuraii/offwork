# pyfuse

**Run any Python function on a remote worker — with zero setup.**

[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-strict%20mypy-blue)](https://mypy-lang.org/)

`pyfuse` captures a function's source code, dependencies, and imports via a single `@trace` decorator.<br/>Workers reconstruct and execute the function from scratch – no deployment, no shared filesystem. Packages are installed automatically.

```python
import asyncio
import math

import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")

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

Only the entry point needs `@trace`. Everything it calls -- `add()`, imports, class methods -- is captured automatically.

## Getting started

Start an isolated worker (temporary venv with `--tmp`, cleaned up on exit):

```bash
pyfuse worker --tmp --backend local://localhost:9748   # Same machine (async TCP)
pyfuse worker --tmp --backend redis://localhost:6379   # Remote using Redis
```

Run a script:

```bash
python examples/remote_execution.py
# 5.0
```

The worker reconstructs the function from source, installs missing packages, executes it, and returns the result. Identical code is cached and never rebuilt twice.

## Installation

```bash
pip install pyfuse
pip install pyfuse[redis]  # for Redis backend
```

## Third-party packages

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

## Features

- **Automatic dependency detection** -- AST-based, recursive. Untraced helpers, class methods, module-level constants, class-level attributes, and class decorators are all captured.
- **Third-party package auto-install** -- Workers install missing packages via pip before execution.
- **Async-native** -- The entire I/O layer is built on `asyncio`. `.run()`, `.start()`, `.map()`, `await result`, and `asyncio.gather` all work out of the box.
- **Heartbeat & stall detection** -- Workers send periodic heartbeats. Clients raise `TaskStalled` when a worker stops responding.
- **Task cancellation** -- `await future.cancel()` cancels pending or in-progress tasks.
- **Progress reporting** -- Call `pyfuse.progress(75.0)` or `pyfuse.progress(3, 10)` inside tasks; query with `await future.progress()`.
- **Graceful shutdown** -- Workers finish in-progress tasks before stopping. Second Ctrl+C force-quits.
- **Class methods** -- `self.method()` and `cls.method()` dependencies are detected. Entire class hierarchies (including `super()`), class-level attributes, decorators (`@dataclass`, etc.), and metaclass keywords are reconstructed.
- **Retry and timeout** -- `@trace(timeout=30, retries=3)` with exponential backoff.
- **Batch submission** -- `await func.map([(a1, b1), (a2, b2)])` submits and awaits multiple tasks.
- **Pluggable backends** -- Redis (`redis://`) for multi-machine, local (`local://`) for same-machine IPC.
- **Content-hash caching** -- Workers cache compiled functions by content hash. Same code from different clients = cache hit.

## Examples

```bash
# Start a worker, then run an example:
pyfuse worker --backend redis://localhost:6379 --tmp
pyfuse run examples/remote_execution.py
```

- **[`examples/remote_execution.py`](examples/remote_execution.py)** -- Remote execution with auto-discovered dependencies
- **[`examples/async_execution.py`](examples/async_execution.py)** -- Async: `.run()`, `.start()`, `.map()`, `asyncio.gather`
- **[`examples/package_installation.py`](examples/package_installation.py)** -- Auto-installing third-party packages on workers
- **[`examples/progress_reporting.py`](examples/progress_reporting.py)** -- Real-time progress tracking from long-running tasks
- **[`examples/cancellation.py`](examples/cancellation.py)** -- Cancelling pending or in-progress tasks
- **[`examples/large_module.py`](examples/large_module.py)** -- Stress test: 47 functions across 7 files, one `@trace`

## Documentation

- **[Quick Start](docs/QUICK_START.md)** -- Usage guide with detailed examples
- **[Technical Overview](docs/TECHNICAL_OVERVIEW.md)** -- Architecture, serialization format, and internals

## License

[AGPL-3.0](LICENSE)
