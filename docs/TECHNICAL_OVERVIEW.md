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

## How `@trace` Works

When `@trace` is applied to a function at decoration time:

1. **Source extraction** -- `inspect.getsource()` retrieves the function's source. `textwrap.dedent()` normalizes indentation. Lines matching `@trace` are stripped.

2. **Import analysis** -- `inspect.getfile()` locates the source file. The file is parsed with `ast.parse()`. Top-level `Import` and `ImportFrom` nodes are extracted as `ImportInfo` objects.

3. **Name analysis** -- The function source is parsed with `ast.parse()`. All `ast.Name` nodes are collected into a set of used names.

4. **Import filtering** -- The module's imports are intersected with the function's used names. Only imports whose `bound_name` appears in the function body are kept.

5. **Dependency detection** -- The function's AST is walked for `ast.Call` nodes:
   - Bare calls like `helper()` are matched against registered function names.
   - `self.method()` / `cls.method()` calls are matched against methods in the same `owner_class`.

6. **Auto-refresh** -- After registration, all previously registered nodes are re-analyzed. This ensures dependencies are resolved regardless of decoration order.

The decorator returns the original function unchanged -- no wrapper, no runtime overhead.

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

## Nested Function Handling

Nested functions (those with `<locals>` in their `__qualname__`) are supported:
- Source extraction and dedenting work normally.
- They are emitted as top-level functions during reconstruction.
- If the function captures variables from an enclosing scope (`co_freevars` is non-empty), a warning is emitted at registration time since the closure context cannot be reconstructed.

## Dependency Graph Properties

- **Directed acyclic graph** -- edges point from caller to callee. Circular dependencies cause `graphlib.CycleError` during reconstruction.
- **Subgraph serialization** -- `serialize(func)` exports only the reachable subgraph from that function, keeping the JSON compact.
- **Order-independent registration** -- auto-refresh after each `register()` call ensures dependencies are correct regardless of `@trace` order.
- **Multi-module support** -- functions from different modules can be traced into the same graph. Qualified names prevent collisions. Same-module matches are preferred during dependency resolution.

## Limitations

- Only `self.X()` / `cls.X()` method calls are detected as class dependencies. Calls like `obj.method()` on arbitrary variables cannot be resolved statically.
- Nested functions that capture closure variables will reconstruct as top-level functions without the captured context.
- Functions must be defined in `.py` source files. Builtins, `exec`'d functions, and REPL definitions raise `PyFuseError`.
- `from . import *` (relative star imports) are not supported.
