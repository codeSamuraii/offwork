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
| `closure_func_refs` | `dict[str, str]` -- closure-captured traced function references: variable name to qualified name (empty for non-closures) |

## How `@trace` Works

When `@trace` is applied to a function at decoration time:

1. **Source extraction** -- `inspect.getsource()` retrieves the function's source. `textwrap.dedent()` normalizes indentation. Lines matching `@trace` are stripped.

2. **Import analysis** -- `inspect.getfile()` locates the source file. The file is parsed with `ast.parse()`. Top-level `Import` and `ImportFrom` nodes are extracted as `ImportInfo` objects.

3. **Name analysis** -- The function source is parsed with `ast.parse()`. All `ast.Name` nodes are collected into a set of used names.

4. **Import filtering** -- The module's imports are intersected with the function's used names. Only imports whose `bound_name` appears in the function body are kept.

5. **Static dependency detection** -- The function's AST is walked for `ast.Call` nodes. Three kinds of calls are detected:
   - **Bare calls** like `helper()` are matched against registered function names.
   - **`self.method()` / `cls.method()` calls** are matched against methods in the same `owner_class`.
   - **`obj.method()` calls** where `obj` is any other variable are resolved via type annotations on function parameters. If the parameter has an annotation (e.g., `proc: Processor`), the annotation AST is walked to extract type names, and those names are matched against classes with traced methods in the registry. If no annotation is present and exactly one class in the registry has a method with that name, the dependency is inferred by unambiguous match. See [Type-Annotation-Based Method Detection](#type-annotation-based-method-detection) below.

6. **Closure capture** -- If the function has free variables (`co_freevars`), their values are captured via `inspect.getclosurevars()`:
   - Each value is passed through `repr()`, then validated with `ast.parse(repr_value, mode='eval')`.
   - If the repr is valid Python, it is stored in `closure_vars`.
   - If the repr is invalid but the value is a traced function (has `__pyfuse_traced__`), the function's qualified name is recorded in `closure_func_refs` and added to `dependencies`.
   - Otherwise, a warning is emitted and the variable is skipped.

7. **Auto-refresh** -- After registration, all previously registered nodes are re-analyzed. This ensures dependencies are resolved regardless of decoration order.

8. **Wrapper creation** -- The decorator returns a wrapper that records runtime caller-callee edges. For generator functions, a specialized proxy generator is used instead of a simple wrapper. See [Runtime Tracing](#runtime-tracing) below.

## Star Import Resolution

When a module contains `from X import *`:

1. The module `X` is imported via `importlib.import_module()` (it's already in `sys.modules`).
2. If `X` defines `__all__`, those names are used. Otherwise, `dir(X)` minus private names.
3. Individual `ImportInfo` entries are created: `from X import name1`, `from X import name2`, etc.
4. The normal filtering step prunes to only names the function actually uses.

If the module cannot be imported, a warning is emitted and the star import is skipped.

## Type-Annotation-Based Method Detection

Static analysis cannot determine the type of arbitrary variables, but type annotations on function parameters provide the needed information. When `detect_traced_dependencies` encounters `obj.method()` where `obj` is not `self`/`cls`:

1. **Annotation extraction** -- The function's AST is parsed to build a mapping from parameter names to type annotation names. The helper `_extract_annotation_type_names` walks the annotation AST and collects all `ast.Name.id` and `ast.Attribute.attr` values. This handles simple names (`Processor`), union types (`Processor | None`), generic types (`list[Processor]`, `dict[str, Processor]`), and module-qualified names (`mod.Processor`).

2. **Root variable resolution** -- For chained access patterns like `mapping['key'].method()`, the helper `_resolve_root_var` walks up through `ast.Subscript` and `ast.Attribute` nodes to find the root `ast.Name`. This allows type information on `mapping` to resolve the method call.

3. **Class lookup** -- A lookup table maps simple class names to their traced methods. For each `(variable, method)` pair:
   - If the variable has a type annotation, the annotation's type names are intersected with the class lookup to find a match.
   - If the variable has no annotation but exactly one class in the registry has a method with that name, the dependency is inferred (unambiguous match).
   - If the variable has an annotation that doesn't match any registered class, the call is left unresolved (no fallback to unambiguous match, avoiding false positives).

This supplements runtime tracing -- both mechanisms can detect the same edge, and dependencies are deduplicated.

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
- **Optional fields** -- `closure_vars` and `closure_func_refs` are omitted when empty for backward compatibility.

## Reconstruction Algorithm

Given a serialized graph and a target function name:

1. **Resolve** the function name to a qualified name (matches by qualified name or simple name).

2. **Collect** the target and all transitive dependencies via BFS through the `dependencies` edges.

3. **Topological sort** using `graphlib.TopologicalSorter`. Dependencies come before dependents.

4. **Deduplicate imports** across all nodes using insertion-order dict (`dict.fromkeys`).

5. **Assemble output**:
   - Sorted import statements at the top.
   - For each node in topological order:
     - If `closure_vars` is non-empty, values are hoisted as keyword-only parameters with default values (e.g., `def inner(x, *, scale=5)`).
     - If `closure_func_refs` is non-empty, captured function references are hoisted as keyword-only parameters defaulting to the referenced function's name (e.g., `def inner(x, *, fn=helper)`). Since the referenced function is a dependency, it is emitted earlier in the output and is in scope.
     - Standalone functions are emitted in topological order, separated by blank lines.
     - Methods are grouped into `class ClassName:` blocks. Each class is emitted at the position of its first method in the topological order. Method sources are indented by 4 spaces.

## Class Method Handling

Methods are identified by their `__qualname__`:
- `"ClassName.method"` -> `owner_class = "ClassName"`
- `"func"` -> `owner_class = None`
- `"outer.<locals>.inner"` -> `owner_class = None` (nested function)

During reconstruction, methods with the same `owner_class` are grouped and wrapped in a `class` block. Intra-class dependencies (`self.method()`, `cls.method()`) are detected by matching `ast.Attribute` calls where the receiver is `self` or `cls`.

## Runtime Tracing

Static analysis cannot resolve calls on arbitrary variables (`obj.method()`) when the type is not annotated, because the type of `obj` is unknown at analysis time. pyfuse supplements static analysis with always-on runtime tracing to capture these dependencies.

### Mechanism

`@trace` returns a `functools.wraps` wrapper instead of the original function. For regular (non-generator) functions, the wrapper:

1. Checks a thread-local call stack maintained by the `FuseGraph` instance.
2. If the stack is non-empty and the top entry differs from the current function, records a runtime dependency edge (caller -> callee).
3. Pushes the current function's qualified name onto the stack.
4. Calls the original function.
5. Pops the stack in a `finally` block.

Self-calls (recursion) are filtered out to prevent cycles.

### Generator functions

For generator functions (detected via `inspect.isgeneratorfunction`), a simple push/pop wrapper is insufficient: calling the function only creates the generator object, and the actual body executes lazily during iteration. A standard wrapper would pop the call stack before any of the body runs, missing all dependencies inside the generator.

pyfuse solves this with a **proxy generator**. The wrapper:

1. Records the caller -> generator dependency edge (at creation time, when the generator function is called).
2. Calls the original function to obtain the underlying generator.
3. Returns a proxy generator (`_proxy_generator`) that intercepts each iteration step.

The proxy generator maintains call stack context during each step:
- **`next()` / `send()`**: Pushes the generator's qualified name, forwards to the underlying generator, pops in a `finally` block.
- **`throw()`**: Pushes, forwards the exception to the underlying generator, pops.
- **`close()`**: Forwards `GeneratorExit` to the underlying generator.
- **`StopIteration`**: Return values (`StopIteration.value`) are preserved through the proxy.

This ensures that any traced function called from within the generator body -- during any iteration step -- is properly attributed as a dependency.

### Why this captures `obj.method()`

Since `@trace` is applied at class definition time, the wrapper replaces the method in the class dict. Any call to `instance.method()` dispatches through the wrapper, regardless of how the instance is referenced -- whether via `self`, a local variable, a function parameter, or any other expression.

When type annotations are present, `obj.method()` calls are detected statically (see [Type-Annotation-Based Method Detection](#type-annotation-based-method-detection)). Runtime tracing serves as a safety net for unannotated code and as the only mechanism for calls through complex indirection that type annotations cannot express.

### Merging with static analysis

Runtime-discovered dependencies are accumulated in `FuseGraph._runtime_deps`. When `serialize()` is called, these are merged (unioned) with the statically detected dependencies before building the subgraph. This ensures the serialized graph is always the most complete picture.

### Thread safety

Each thread gets its own call stack via `threading.local()`. The shared `_runtime_deps` dict is guarded by a `threading.Lock`.

## Nested Function Handling

Nested functions (those with `<locals>` in their `__qualname__`) are supported:
- Source extraction and dedenting work normally.
- They are emitted as top-level functions during reconstruction.

### Closure variable hoisting

If the function captures variables from an enclosing scope (`co_freevars` is non-empty), their values are captured at registration time via `inspect.getclosurevars()`. Each captured value is processed as follows:

1. `repr(value)` is called to produce a string representation.
2. The repr is validated by parsing it with `ast.parse(repr_value, mode='eval')`.
3. If valid, the variable is stored in `closure_vars` and hoisted during reconstruction as a keyword-only parameter with the repr as its default value.
4. If the repr is invalid Python but the value is a traced function (detected via the `__pyfuse_traced__` attribute), the variable name and the function's qualified name are stored in `closure_func_refs`. During reconstruction, these are hoisted as keyword-only parameters defaulting to the referenced function's name (which is guaranteed to be in scope because it is also recorded as a dependency).
5. If the repr is invalid and the value is not a traced function, a warning is emitted and the variable is skipped.

Example of both hoisting mechanisms in reconstructed code:

```python
def helper(x):
    return x + 1

def inner(x, *, scale=5, fn=helper):
    return fn(x) * scale
```

Here `scale` was a simple closure variable (hoisted via `closure_vars`) and `fn` was a reference to the traced function `helper` (hoisted via `closure_func_refs`).

## Dependency Graph Properties

- **Directed acyclic graph** -- edges point from caller to callee. Circular dependencies cause `graphlib.CycleError` during reconstruction.
- **Subgraph serialization** -- `serialize(func)` exports only the reachable subgraph from that function, keeping the JSON compact.
- **Order-independent registration** -- auto-refresh after each `register()` call ensures dependencies are correct regardless of `@trace` order.
- **Multi-module support** -- functions from different modules can be traced into the same graph. Qualified names prevent collisions. Same-module matches are preferred during dependency resolution.

## Limitations

### Static analysis boundaries

- **Type-annotation-based method detection** resolves `obj.method()` calls when the parameter has a type annotation matching a class in the registry, or when the method name is unambiguous. Without annotations and with multiple candidate classes, the call cannot be resolved statically and requires runtime tracing.
- **Local variable types** are not analyzed. Only parameter annotations are used for type-based resolution.
- **Dynamically computed imports** (`__import__()`, `importlib.import_module()` inside function bodies) are not detected.

### Runtime tracing boundaries

- **Unannotated `obj.method()` calls** require at least one runtime invocation to be detected. Adding type annotations to function parameters eliminates this requirement.
- **Circular dependencies** between functions cause `graphlib.CycleError` during reconstruction. Mutually recursive functions are not supported.
- **Async functions and async generators** are not currently wrapped for runtime tracing. Dependencies inside async function bodies are handled by static analysis only.

### Closure capture

- Closure variables are captured via `repr()` and validated with `ast.parse()`. Values whose `repr()` does not produce valid Python (e.g., file handles, sockets, custom objects without a reconstructable `__repr__`) trigger a warning and are omitted.
- If a closure captures a traced function reference, it is automatically detected and recorded as a dependency via `closure_func_refs`. The captured variable is hoisted as a keyword-only parameter defaulting to the function name.
- If a closure captures a non-traced callable or object with invalid repr, it will be omitted with a warning.

### Source availability

- Functions must be defined in `.py` source files on disk. Attempting to trace builtins, `exec`'d functions, or REPL-defined functions raises `PyFuseError` with a descriptive message.

### Import edge cases

- `from . import *` (relative star imports) are not supported. Absolute star imports (`from os.path import *`) are fully resolved.
- Dynamically computed imports (`__import__()`, `importlib.import_module()` inside function bodies) are not detected.
