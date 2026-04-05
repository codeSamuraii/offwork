# Quick Start

## Installation

```bash
poetry install
```

## Basic Usage

### 1. Trace functions with `@trace`

Decorate any function to register it in the dependency graph:

```python
import csv
import json

from pyfuse import trace

@trace
def parse_csv(data: str) -> list[list[str]]:
    return list(csv.reader(data.splitlines()))

@trace
def to_json(data: object) -> str:
    return json.dumps(data, indent=2)

@trace
def csv_to_json(data: str) -> str:
    return to_json(parse_csv(data))
```

Functions called by a `@trace`d function are automatically discovered and included in the dependency graph, even without `@trace`. Standard library and third-party functions are kept as import statements.

### 2. Serialize the dependency graph

```python
from pyfuse import serialize

# Serialize everything
graph_json = serialize()

# Or serialize only a function and its transitive dependencies
graph_json = serialize(csv_to_json)
```

### 3. Pack a function for remote execution

`pack()` captures a function's subgraph and bundles it with arguments into a `Task` -- a serializable envelope ready to send to a worker:

```python
from pyfuse import pack

task = pack(csv_to_json, "a,b\n1,2")
print(task.task_id)        # auto-generated unique ID
print(task.function_name)  # qualified name
print(task.args)           # (\"a,b\\n1,2\",)

# Serialize for transport (Redis, HTTP, file, etc.)
task_json = task.to_json()
```

On the receiving side:

```python
from pyfuse import Task

task = Task.from_json(task_json)
```

The result is a JSON string containing a content-addressable object store. Each function is stored once, identified by a content hash, with dependencies linked by hash:

```json
{
  "version": "0.2.0",
  "objects": {
    "a1b2...": {"hash": "a1b2...", "name": "parse_csv", "source": "...", "deps": [], ...},
    "c3d4...": {"hash": "c3d4...", "name": "csv_to_json", "source": "...", "deps": ["a1b2..."], ...}
  },
  "refs": {
    "mymodule.parse_csv": "a1b2...",
    "mymodule.csv_to_json": "c3d4..."
  }
}
```

Shared dependencies are stored once and referenced by multiple callers. Workers can cache objects by hash and only request missing ones.

### 4. Reconstruct source code

```python
from pyfuse import reconstruct

source = reconstruct(graph_json, "csv_to_json")
print(source)
```

Output:

```python
import csv
import json


def parse_csv(data: str) -> list[list[str]]:
    return list(csv.reader(data.splitlines()))


def to_json(data: object) -> str:
    return json.dumps(data, indent=2)


def csv_to_json(data: str) -> str:
    return to_json(parse_csv(data))
```

Functions are emitted in dependency order -- dependencies first, target function last. Imports are deduplicated across all functions. Dependencies are resolved by content hash, so the output is stable regardless of how functions are named or organized.

### 5. Execute on a worker

`FuseWorker` reconstructs and executes functions from serialized graphs. It caches compiled functions by subgraph content hash, so repeated calls with identical graphs skip reconstruction entirely. Missing third-party dependencies are auto-installed via pip.

```python
from pyfuse import FuseWorker, pack

task = pack(csv_to_json, "a,b\n1,2")

# One-shot execution
from pyfuse import execute
result = execute(task)

# Or with a persistent worker (caches compiled functions)
worker = FuseWorker()
result = worker.run(task)

# The worker also accepts raw JSON + function name
result = worker.execute(graph_json, "csv_to_json", "a,b\n1,2")

# Async support
result = await worker.run_async(task)
```

### 6. Save and load graphs

The serialized format is plain JSON text. Save it to a file and reconstruct later:

```python
from pathlib import Path

Path("graph.json").write_text(graph_json)

# Later...
loaded = Path("graph.json").read_text()
source = reconstruct(loaded, "parse_csv")
```

### 7. Merge stores

Two serialized stores can be merged by combining their `objects` and `refs` dictionaries. Identical content hashes guarantee safe deduplication:

```python
import json

store_a = json.loads(serialize(func_a))
store_b = json.loads(serialize(func_b))

merged = json.dumps({
    "version": "0.2.0",
    "objects": {**store_a["objects"], **store_b["objects"]},
    "refs": {**store_a["refs"], **store_b["refs"]},
})

# Shared dependencies appear only once in the merged store
source = reconstruct(merged, "func_a")
```

## Class Methods

`@trace` works on class methods. Dependencies via `self.method()` are detected automatically:

```python
class Pipeline:
    @trace
    def step_a(self, x):
        return x.strip()

    @trace
    def step_b(self, x):
        return self.step_a(x).lower()

source = reconstruct(serialize(), "step_b")
```

Output:

```python
class Pipeline:
    def step_a(self, x):
        return x.strip()

    def step_b(self, x):
        return self.step_a(x).lower()
```

## Generator Functions

`@trace` works on generator functions. Dependencies inside the generator body are captured during iteration:

```python
@trace
def generate_items(items):
    for item in items:
        yield transform(item)  # dependency on transform() detected
```

## Async Functions and Async Generators

`@trace` works on `async def` functions and async generators. Dependencies are captured both statically and at runtime (via `await` / `async for`):

```python
@trace
async def fetch_data(url: str) -> dict:
    return await async_helper(url)  # dependency on async_helper() detected

@trace
async def stream_items(items):
    for item in items:
        yield await transform(item)  # dependency on transform() detected
```

## Object Method Calls

When a function parameter has a type annotation, `obj.method()` calls are resolved statically -- no execution needed:

```python
class Processor:
    @trace
    def step(self, x):
        return x.upper()

@trace
def run(proc: Processor, data: str) -> str:
    return proc.step(data)  # dependency on Processor.step detected via annotation

source = reconstruct(serialize(), "run")
```

Without annotations, the dependency is detected at runtime when the function is called. Adding type annotations is recommended for complete static analysis.

## Star Imports

Star imports (`from os.path import *`) are resolved automatically. Only the names actually used by the function are included as explicit imports in the reconstructed code.

## Definition Order

Functions can be `@trace`d in any order. pyfuse automatically resolves dependencies regardless of which function is decorated first.

## Error Handling

Attempting to trace a function without available source code (builtins, `exec`'d functions, REPL definitions) raises `PyFuseError`:

```python
from pyfuse import trace, PyFuseError

try:
    trace(len)  # built-in, no source
except PyFuseError as e:
    print(e)  # "Cannot trace function 'len': source code unavailable..."
```

## Running the Examples

```bash
poetry run python examples/basic_usage.py
poetry run python examples/class_methods.py
poetry run python examples/subgraph_serialization.py
poetry run python examples/save_and_load.py
poetry run python examples/mermaid_visualization.py

# Redis worker (requires Redis on localhost:6379 and `pip install redis`)
poetry run python examples/redis_worker.py worker   # Terminal 1
poetry run python examples/redis_worker.py push     # Terminal 2
```
