# pyfuse

Distributed Python task execution via automatic function serialization.

pyfuse lets you run any Python function on a remote worker with zero setup on the worker side. Decorate a function with `@trace`, and pyfuse captures its source code, dependencies, and imports automatically. The worker reconstructs and executes the function from scratch, installing missing packages as needed.

## Installation

```bash
pip install pyfuse
```

For Redis-based remote execution:

```bash
pip install pyfuse[redis]
```

## Quick example

```python
import math
from pyfuse import trace

def add(a: int, b: int) -> int:
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))
```

`@trace` captures the function's source and its entire dependency tree. `add()` is included automatically.

### Start a worker

```bash
pyfuse worker --backend redis://localhost:6379
```

### Submit work

```python
import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

# Run on a remote worker
future = hypotenuse.run(3.0, 4.0)
result = future.result()  # 5.0
```

Cleanup is automatic -- no need to call `disconnect()`.

## Features

- **Zero worker setup** -- workers reconstruct functions from source, no deployment needed
- **Automatic dependency detection** -- function calls, class methods, and imports are captured via AST analysis and runtime tracing
- **Auto-install** -- workers install missing third-party packages via pip
- **Retry and timeout** -- `@trace(timeout=30, retries=3)` for resilient execution
- **Pluggable backends** -- Redis for production, shared memory for same-machine IPC
- **Content-addressable caching** -- workers skip reconstruction for identical code
- **Typed** -- full `py.typed` support with strict mypy compliance

## Documentation

- [Quick Start](docs/QUICK_START.md)
- [Technical Overview](docs/TECHNICAL_OVERVIEW.md)

## License

AGPL-3.0
