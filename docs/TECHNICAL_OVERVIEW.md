# Technical Overview

## Architecture

pyfuse is structured as five internal modules behind a minimal public API:

```
pyfuse/
    __init__.py      trace, serialize, reconstruct, FuseGraph, PyFuseError
    _decorator.py    @trace implementation
    _analyzer.py     AST-based source and dependency analysis
    _graph.py        FuseGraph: registration, serialization, reconstruction
    _models.py       ImportInfo, FunctionNode dataclasses
    _errors.py       PyFuseError exception
```

## Data Model

### ImportInfo

Represents a single import binding:

| Field | Example |
|-------|---------|
| `statement` | `"import csv"`, `"from os.path import join"` |
| `bound_name` | `"csv"`, `"join"` -- the name usable in code |

Multi-name imports (`import csv, json` or `from os.path import join, exists`) are split into individual `ImportInfo` objects. This allows precise per-function import tracking.

### FunctionNode

Represents one function in the dependency graph:

| Field | Description |
|-------|-------------|
| `qualified_name` | `"module.ClassName.method"` -- unique identifier |
| `name` | `"method"` -- simple function name (`__name__`) |
| `module` | `"module"` -- the module where the function is defined |
| `source` | Function source code, `@trace` stripped, zero-indented |
| `imports` | `list[ImportInfo]` -- only the imports this function uses |
| `dependencies` | `list[str]` -- qualified names of other traced functions |
| `owner_class` | `"ClassName"` for methods, `None` for standalone functions |
| `closure_vars` | `dict[str, str]` -- captured closure variable `repr()` values (empty for non-closures) |

## How `@trace` Works

When `@trace` is applied to a function at decoration time:

1. **Source extraction** -- `inspect.getsource()` retrieves the function's source. `textwrap.dedent()` normalizes indentation. Lines matching `@trace` are stripped.

2. **Import analysis** -- `inspect.getfile()` locates the source file. The file is parsed with `ast.parse()`. Top-level `Import` and `ImportFrom` nodes are extracted as `ImportInfo` objects.

3. **Name analysis** -- The function source is parsed with `ast.parse()`. All `ast.Name` nodes are collected into a set of used names.

4. **Import filtering** -- The module's imports are intersected with the function's used names. Only imports whose `bound_name` appears in the function body are kept.

5. **Static dependency detection** -- The function's AST is walked for `ast.Call` nodes:
   - Bare calls like `helper()` are matched against registered function names.
   - `self.method()` / `cls.method()` calls are matched against methods in the same `owner_class`.

6. **Closure capture** -- If the function has free variables (`co_freevars`), their values are captured via `inspect.getclosurevars()` and stored as `repr()` strings in the node's `closure_vars` dict.

7. **Auto-refresh** -- After registration, all previously registered nodes are re-analyzed. This ensures dependencies are resolved regardless of decoration order.

8. **Wrapper creation** -- The decorator returns a thin `functools.wraps` wrapper that records runtime caller-callee edges (see [Runtime Tracing](#runtime-tracing) below).

## Star Import Resolution

When a module contains `from X import *`:

1. The module `X` is imported via `importlib.import_module()` (it's already in `sys.modules`).
2. If `X` defines `__all__`, those names are used. Otherwise, `dir(X)` minus private names.
3. Individual `ImportInfo` entries are created: `from X import name1`, `from X import name2`, etc.
4. The normal filtering step prunes to only names the function actually uses.

If the module cannot be imported, a warning is emitted and the star import is skipped.

## Serialization Format

The serialized format is JSON:

```json
{
  "version": "0.1.0",
  "nodes": {
    "mymodule.parse_csv": {
      "qualified_name": "mymodule.parse_csv",
      "name": "parse_csv",
      "module": "mymodule",
      "source": "def parse_csv(data: str) -> dict:\n    ...",
      "imports": [
        {"statement": "import csv", "bound_name": "csv"}
      ],
      "dependencies": [],
      "owner_class": null
    },
    "mymodule.csv_to_json": {
      "qualified_name": "mymodule.csv_to_json",
      "name": "csv_to_json",
      "module": "mymodule",
      "source": "def csv_to_json(data: str) -> str:\n    ...",
      "imports": [],
      "dependencies": ["mymodule.parse_csv"],
      "owner_class": null
    }
  }
}
```

Key properties:
- **Flat node dictionary** keyed by qualified name -- O(1) lookup.
- **Dependencies are qualified names** -- unambiguous references into the same `nodes` dict.
- **Imports are granular** -- one entry per binding, not per statement.
- **Version field** -- enables future format evolution.

## Reconstruction Algorithm

Given a serialized graph and a target function name:

1. **Resolve** the function name to a qualified name (matches by qualified name or simple name).

2. **Collect** the target and all transitive dependencies via BFS through the `dependencies` edges.

3. **Topological sort** using `graphlib.TopologicalSorter`. Dependencies come before dependents.

4. **Deduplicate imports** across all nodes using insertion-order dict (`dict.fromkeys`).

5. **Assemble output**:
   - Sorted import statements at the top.
   - Standalone functions in topological order, separated by blank lines.
   - Methods grouped into `class ClassName:` blocks. Each class is emitted at the position of its first method in the topological order. Method sources are indented by 4 spaces.

## Class Method Handling

Methods are identified by their `__qualname__`:
- `"ClassName.method"` -> `owner_class = "ClassName"`
- `"func"` -> `owner_class = None`
- `"outer.<locals>.inner"` -> `owner_class = None` (nested function)

During reconstruction, methods with the same `owner_class` are grouped and wrapped in a `class` block. Intra-class dependencies (`self.method()`, `cls.method()`) are detected by matching `ast.Attribute` calls where the receiver is `self` or `cls`.

## Runtime Tracing

Static analysis cannot resolve calls on arbitrary variables (`obj.method()`) because the type of `obj` is unknown at analysis time. pyfuse supplements static analysis with always-on runtime tracing to capture these dependencies.

### Mechanism

`@trace` returns a thin `functools.wraps` wrapper instead of the original function. The wrapper:

1. Checks a thread-local call stack maintained by the `FuseGraph` instance.
2. If the stack is non-empty and the top entry differs from the current function, records a runtime dependency edge (caller -> callee).
3. Pushes the current function's qualified name onto the stack.
4. Calls the original function.
5. Pops the stack in a `finally` block.

Self-calls (recursion) are filtered out to prevent cycles.

### Why this captures `obj.method()`

Since `@trace` is applied at class definition time, the wrapper replaces the method in the class dict. Any call to `instance.method()` dispatches through the wrapper, regardless of how the instance is referenced -- whether via `self`, a local variable, a function parameter, or any other expression.

### Merging with static analysis

Runtime-discovered dependencies are accumulated in `FuseGraph._runtime_deps`. When `serialize()` is called, these are merged (unioned) with the statically detected dependencies before building the subgraph. This ensures the serialized graph is always the most complete picture.

### Thread safety

Each thread gets its own call stack via `threading.local()`. The shared `_runtime_deps` dict is guarded by a `threading.Lock`.

## Nested Function Handling

Nested functions (those with `<locals>` in their `__qualname__`) are supported:
- Source extraction and dedenting work normally.
- They are emitted as top-level functions during reconstruction.
- If the function captures variables from an enclosing scope (`co_freevars` is non-empty), their values are captured at registration time via `inspect.getclosurevars()` and stored as `repr()` strings. During reconstruction, these are hoisted as keyword-only parameters with default values, producing runnable code.

## Dependency Graph Properties

- **Directed acyclic graph** -- edges point from caller to callee. Circular dependencies cause `graphlib.CycleError` during reconstruction.
- **Subgraph serialization** -- `serialize(func)` exports only the reachable subgraph from that function, keeping the JSON compact.
- **Order-independent registration** -- auto-refresh after each `register()` call ensures dependencies are correct regardless of `@trace` order.
- **Multi-module support** -- functions from different modules can be traced into the same graph. Qualified names prevent collisions. Same-module matches are preferred during dependency resolution.

## Limitations

### Runtime tracing boundaries

- **Intra-class dependencies** via `self.method()` and `cls.method()` are detected statically. Calls on arbitrary variables (`obj.method()`) require at least one runtime invocation to be detected.
- **Circular dependencies** between functions cause `graphlib.CycleError` during reconstruction. Mutually recursive functions are not supported.
- **Generators and async functions**: The wrapper records the dependency when the generator/coroutine is created. Dependencies during iteration/awaiting are handled by static analysis only.

### Closure capture

- Closure variables are captured via `repr()`. Values whose `repr()` does not produce valid Python (e.g., file handles, sockets, custom objects without `__repr__`) will trigger a warning and be omitted.
- If a closure captures a function reference, `repr()` produces a non-reconstructable string. If the captured function is traced, runtime tracing will detect the dependency independently.

### Source availability

- Functions must be defined in `.py` source files on disk. Attempting to trace builtins, `exec`'d functions, or REPL-defined functions raises `PyFuseError` with a descriptive message.

### Import edge cases

- `from . import *` (relative star imports) are not supported. Absolute star imports (`from os.path import *`) are fully resolved.
- Dynamically computed imports (`__import__()`, `importlib.import_module()` inside function bodies) are not detected.
