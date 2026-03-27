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

The decorator is transparent -- it returns the original function unchanged with zero runtime overhead.

### 2. Serialize the dependency graph

```python
from pyfuse import serialize

# Serialize everything
graph_json = serialize()

# Or serialize only a function and its transitive dependencies
graph_json = serialize(csv_to_json)
```

The result is a JSON string containing function source code, import dependencies, and inter-function relationships.

### 3. Reconstruct source code

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

Functions are emitted in dependency order -- dependencies first, target function last. Imports are deduplicated across all functions.

### 4. Save and load graphs

The serialized format is plain JSON text. Save it to a file and reconstruct later:

```python
from pathlib import Path

Path("graph.json").write_text(graph_json)

# Later...
loaded = Path("graph.json").read_text()
source = reconstruct(loaded, "parse_csv")
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
```
