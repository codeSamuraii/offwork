# Technical Overview

## Architecture

pyfuse is structured as internal modules behind a minimal public API:

```
pyfuse/
    __init__.py      trace, serialize, reconstruct, pack, execute, connect, serve, ...
    __main__.py      CLI entrypoint (python -m pyfuse worker ...)
    _decorator.py    @trace implementation
    _analyzer.py     AST-based source and dependency analysis
    _graph.py        Graph: registration, serialization, reconstruction
    _store.py        Store: content-addressable store with graph operations
    _models.py       ImportInfo, FunctionNode dataclasses (incl. content hashing)
    _task.py         Task: serializable envelope for remote execution
    _worker.py       Worker: reconstruct, install deps, execute
    _backend.py      Backend ABC + RedisBackend: pluggable transport layer
    _remote.py       connect/disconnect/serve/submit_remote: remote execution orchestration
    _result.py       ResultEnvelope + Result: result serialization and futures
    _deps.py         Dependency extraction and pip installation
    _errors.py       Error, WorkerError, RemoteError, DependencyError
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
| `qualified_name` | `"module.ClassName.method"` -- unique identifier (in-memory) |
| `name` | `"method"` -- simple function name (`__name__`) |
| `module` | `"module"` -- the module where the function is defined |
| `source` | Function source code, `@trace` stripped, zero-indented |
| `imports` | `list[ImportInfo]` -- only the imports this function uses |
| `dependencies` | `list[str]` -- qualified names of other traced functions (in-memory; serialized as content hashes in `deps`) |
| `owner_class` | `"ClassName"` for methods, `None` for standalone functions |
| `closure_vars` | `dict[str, str]` -- captured closure variable `repr()` values (empty for non-closures) |
| `closure_func_refs` | `dict[str, str]` -- closure-captured traced function references: variable name to qualified name (in-memory; serialized as content hashes) |

Additionally, `FunctionNode.content_hash()` computes a deterministic SHA-256 hash (16 hex chars) from the node's intrinsic content (`name`, `module`, `source`, `imports`, `owner_class`, `closure_vars`, `closure_func_refs`). Dependencies are excluded so that adding or removing edges does not change an existing node's hash.

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

7. **Auto-discovery of untraced dependencies** -- After the node is created, bare function calls in the source are checked against the registry. Calls to functions that are not registered, not builtins, and not stdlib/third-party are auto-registered by looking them up in `sys.modules` and extracting their source. This is recursive: auto-discovered functions are themselves checked for untraced dependencies. For cross-module imports (e.g., `from utils import helper`), the import is removed from the calling node and replaced by a dependency edge, so the reconstructed code is self-contained. See [Auto-Discovery of Untraced Dependencies](#auto-discovery-of-untraced-dependencies) below.

8. **Auto-refresh** -- After registration, all previously registered nodes are re-analyzed. This ensures dependencies are resolved regardless of decoration order.

9. **Wrapper creation** -- The decorator returns a wrapper that records runtime caller-callee edges. For generator functions, a specialized proxy generator is used instead of a simple wrapper. See [Runtime Tracing](#runtime-tracing) below.

## Star Import Resolution

When a module contains `from X import *`:

1. The module `X` is imported via `importlib.import_module()` (it's already in `sys.modules`).
2. If `X` defines `__all__`, those names are used. Otherwise, `dir(X)` minus private names.
3. Individual `ImportInfo` entries are created: `from X import name1`, `from X import name2`, etc.
4. The normal filtering step prunes to only names the function actually uses.

If the module cannot be imported, a warning is emitted and the star import is skipped.

## Auto-Discovery of Untraced Dependencies

When a `@trace`d function calls another function that is not decorated with `@trace`, pyfuse automatically discovers and registers the untraced function so that the serialized graph is self-contained.

### Mechanism

After registering a traced function, pyfuse extracts all bare function calls from its AST (`ast.Call` nodes with `ast.Name` targets). For each call that is:

1. **Not already in the registry** (not a traced or previously auto-discovered function).
2. **Not a builtin** (`len`, `print`, `range`, etc. are excluded).
3. **Not a class** (only `inspect.isfunction()` objects are registered).
4. **User-defined** (source file is not under the Python stdlib or `site-packages` directories).

...the function is looked up via `getattr(sys.modules[module], name)` and auto-registered with the same source extraction and import analysis pipeline used for traced functions.

### Transitive discovery

Auto-discovered functions are themselves checked for untraced dependencies. This is recursive: if `traced_func()` calls `helper_a()` which calls `helper_b()`, all three end up in the graph. Infinite recursion is prevented by skipping functions already in the registry.

### Cross-module imports

If a function is imported from another user module (e.g., `from utils import helper`), it is also auto-discovered. The import statement is removed from the calling function's imports and replaced by a dependency edge, so the reconstructed code emits the function inline rather than importing it.

Aliased imports (e.g., `from utils import helper as h`) are skipped because the function name and bound name differ, which would cause a name mismatch in reconstructed code.

### What is NOT auto-discovered

- **Standard library functions** (`json.dumps`, `csv.reader`, etc.) -- kept as import statements.
- **Third-party package functions** (anything installed in `site-packages`) -- kept as import statements.
- **Method calls** (`obj.method()`, `self.method()`) -- only bare function calls are candidates.
- **Classes used as constructors** (`MyClass()`) -- filtered by `inspect.isfunction()`.

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

The serialized format is a content-addressable JSON store. Each function node is identified by a SHA-256 content hash (truncated to 16 hex characters), computed from the node's own content (source, name, module, imports, metadata) but **not** from its dependency hashes.

```json
{
  "version": "0.2.0",
  "objects": {
    "b8a4483a7a203635": {
      "hash": "b8a4483a7a203635",
      "name": "parse_csv",
      "module": "mymodule",
      "source": "def parse_csv(data: str) -> dict:\n    ...",
      "imports": [
        {"statement": "import csv", "bound_name": "csv"}
      ],
      "deps": [],
      "owner_class": null
    },
    "c9243932be475d63": {
      "hash": "c9243932be475d63",
      "name": "csv_to_json",
      "module": "mymodule",
      "source": "def csv_to_json(data: str) -> str:\n    ...",
      "imports": [],
      "deps": ["b8a4483a7a203635"],
      "owner_class": null
    }
  },
  "refs": {
    "mymodule.parse_csv": "b8a4483a7a203635",
    "mymodule.csv_to_json": "c9243932be475d63"
  }
}
```

Key properties:
- **`objects`** -- flat dictionary keyed by content hash. Each value is a self-contained node. O(1) lookup by hash.
- **`refs`** -- maps qualified names to content hashes. This is the human-readable index for resolving function names.
- **`deps`** -- dependency edges are content hashes, not qualified names. This makes links rename-stable: renaming a function doesn't break references as long as the content stays the same.
- **Imports are granular** -- one entry per binding, not per statement.
- **Version field** -- enables future format evolution.
- **Optional fields** -- `closure_vars` and `closure_func_refs` are omitted when empty.

### Content hashing

The hash is computed from a canonical JSON serialization of the node's intrinsic content:

| Included in hash | Excluded from hash |
|---|---|
| `name`, `module`, `source` | `qualified_name` (derived, fragile on rename) |
| `imports` (sorted), `owner_class` | `deps` (structural, not content) |
| `closure_vars`, `closure_func_refs` (sorted) | |

Because dependencies are excluded, adding or removing an edge never changes an existing node's hash. This enables efficient incremental updates and deduplication.

### Deduplication

Two functions with identical content produce the same hash and occupy a single entry in `objects`, even if they have different qualified names (both appear in `refs` pointing to the same hash).

Across multiple serialized stores, a consumer (e.g., a remote worker) can cache objects by hash and compute `missing = incoming.keys() - cached.keys()` to receive only new objects.

### Merging stores

Two serialized stores can be merged by taking the union of their `objects` and `refs` dictionaries. Hash collisions are by definition identical content, so merging is always safe.

## Reconstruction Algorithm

Given a serialized store and a target function name:

1. **Resolve** the function name to a content hash via the `refs` index (matches by qualified name or simple name).

2. **Collect** the target and all transitive dependencies via BFS through the `deps` hash edges in `objects`.

3. **Build** temporary `FunctionNode` objects from the collected objects, translating dependency hashes back to qualified names via the inverted `refs` index.

4. **Topological sort** using `graphlib.TopologicalSorter`. Dependencies come before dependents.

5. **Deduplicate imports** across all nodes using insertion-order dict (`dict.fromkeys`).

6. **Assemble output**:
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

1. Checks a `contextvars.ContextVar`-based call stack maintained by the `Graph` instance.
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

### Async functions

For async coroutine functions (detected via `inspect.iscoroutinefunction`), the wrapper is an `async def` that:

1. Calls `_ensure_isolated_stack()` to get a per-task copy of the call stack (see [Thread and task safety](#thread-and-task-safety)).
2. Records the runtime dependency edge if a caller is on the stack.
3. Pushes the function's qualified name, `await`s the original coroutine, and pops in a `finally` block.

The wrapper preserves `inspect.iscoroutinefunction()` since it is itself `async def`.

### Async generator functions

For async generator functions (detected via `inspect.isasyncgenfunction`), the same proxy pattern as sync generators is used. The wrapper:

1. Records the caller -> async generator dependency edge at creation time.
2. Calls the original function to obtain the underlying async generator.
3. Returns a proxy async generator (`_proxy_async_generator`) that intercepts each async iteration step.

The proxy async generator maintains call stack context during each step:
- **`__anext__()` / `asend()`**: Calls `_ensure_isolated_stack()`, pushes the qualified name, forwards to the underlying async generator, pops in a `finally` block.
- **`athrow()`**: Same push/pop pattern, forwards the exception.
- **`aclose()`**: Forwards `GeneratorExit` to the underlying async generator.
- **`StopAsyncIteration`**: Caught and re-raised to signal iteration end (async generators cannot return values per PEP 525).

### Why this captures `obj.method()`

Since `@trace` is applied at class definition time, the wrapper replaces the method in the class dict. Any call to `instance.method()` dispatches through the wrapper, regardless of how the instance is referenced -- whether via `self`, a local variable, a function parameter, or any other expression.

When type annotations are present, `obj.method()` calls are detected statically (see [Type-Annotation-Based Method Detection](#type-annotation-based-method-detection)). Runtime tracing serves as a safety net for unannotated code and as the only mechanism for calls through complex indirection that type annotations cannot express.

### Merging with static analysis

Runtime-discovered dependencies are accumulated in `Graph._runtime_deps`. When `serialize()` is called, these are merged (unioned) with the statically detected dependencies before building the subgraph and computing content hashes. This ensures the serialized store is always the most complete picture.

### Thread and task safety

The call stack uses `contextvars.ContextVar`, which provides:
- **Per-thread isolation** in synchronous code -- each thread gets its own context automatically.
- **Per-task isolation** in async code -- each `asyncio.Task` gets a copy of the parent context.

Since `ContextVar` copies the *reference* to the mutable list (not the list itself), async wrappers call `_ensure_isolated_stack()` to set a fresh list copy. This prevents `append`/`pop` in one task from mutating another task's stack. Sync wrappers use `_get_call_stack()` directly since each thread has a fully independent context.

The shared `_runtime_deps` dict is guarded by a `threading.Lock`.

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
- **Content-addressable storage** -- each node is identified by a content hash, enabling deduplication and efficient incremental updates. Two nodes with identical content share the same hash regardless of qualified name.
- **Subgraph serialization** -- `serialize(func)` exports only the reachable subgraph from that function, keeping the JSON compact.
- **Incremental updates** -- adding a new function creates one new object entry; existing nodes are unaffected because dependency hashes are excluded from the content hash.
- **Store merging** -- two serialized stores can be merged by unioning `objects` and `refs`. Identical content hashes guarantee safe deduplication.
- **Order-independent registration** -- auto-refresh after each `register()` call ensures dependencies are correct regardless of `@trace` order.
- **Multi-module support** -- functions from different modules can be traced into the same graph. Qualified names prevent collisions. Same-module matches are preferred during dependency resolution.

## Task Envelope

The `Task` dataclass (`_task.py`) is a frozen, serializable envelope that bundles everything needed for remote function execution:

| Field | Type | Description |
|-------|------|-------------|
| `graph_json` | `str` | Serialized dependency graph (JSON) |
| `function_name` | `str` | Qualified name of the target function |
| `args` | `tuple[Any, ...]` | Positional arguments (default `()`) |
| `kwargs` | `dict[str, Any]` | Keyword arguments (default `{}`) |
| `task_id` | `str` | Auto-generated 12-char hex ID |

### Design properties

- **Frozen** -- tasks are immutable values, safe to pass across threads or serialize multiple times.
- **Zero internal imports** -- `_task.py` only imports stdlib (`json`, `uuid`, `dataclasses`, `typing`). Workers consuming tasks do not need the tracing machinery.
- **Transport-agnostic** -- `to_json()` / `from_json()` produce plain JSON strings. The user chooses the transport (Redis, HTTP, files, message queues).

### `pack()` convenience function

`pack(func, *args, **kwargs)` creates a `Task` in one call:

1. Unwraps the function via `inspect.unwrap()` to resolve the qualified name.
2. Calls `serialize(func)` to capture only the target function's subgraph (not the full graph).
3. Returns a `Task` with the scoped graph, qualified name, and arguments.

### Serialization format

```json
{
  "id": "a1b2c3d4e5f6",
  "graph": "{\"version\": \"0.3.0\", \"objects\": {...}, ...}",
  "function": "mymodule.csv_to_json",
  "args": [1, 2, 3],
  "kwargs": {"key": "value"}
}
```

The `graph` field contains the serialized store as a JSON string (not nested object), keeping the envelope flat and simple to parse. `from_json()` accepts both `str` and `bytes` for convenience with transport libraries (e.g., Redis returns bytes).

### Task Options

Tasks carry optional execution policy fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timeout` | `float | None` | `None` | Per-attempt timeout in seconds |
| `retries` | `int` | `0` | Number of retry attempts on failure |
| `retry_delay` | `float` | `1.0` | Base delay between retries (exponential backoff: `delay * 2^attempt`) |

These fields are set via `@trace(timeout=30, retries=3)` on the client side and enforced by `Worker.run_with_policy()` on the worker side. They are omitted from the JSON when at default values for backward compatibility.

## Worker

The `Worker` class (`_worker.py`) reconstructs and executes functions from serialized graphs. It is designed for the consumer side of a distributed system -- the worker has no prior knowledge of the functions it will execute.

### Execution pipeline

1. **Deserialize** -- Parse the JSON graph into a `Store`.
2. **Cache check** -- Compute a subgraph key (SHA-256 of all content hashes in the transitive closure). If the key is cached, skip to step 6.
3. **Install dependencies** -- If `auto_install=True` (default), extract third-party module names from the function's imports and install missing packages via pip. The `DEFAULT_IMPORT_TO_PACKAGE` mapping handles common aliases (e.g., `cv2` -> `opencv-python`, `PIL` -> `Pillow`).
4. **Reconstruct** -- Call `Store.reconstruct()` to produce a self-contained Python script.
5. **Compile and exec** -- `compile()` the source, `exec()` it into a fresh namespace, extract the target callable.
6. **Call** -- Invoke the function with the provided arguments.

### Caching

Functions are cached by subgraph content hash -- a SHA-256 of all content hashes reachable from the target function, sorted and joined. This means:

- **Same code = cache hit** regardless of how the graph was serialized or where it came from.
- **Any change to the function or its dependencies** produces a different key, invalidating the cache.
- Cache can be inspected via `cache_info()` and cleared via `clear_cache()`.

### Concurrency

`serve()` accepts a `concurrency` parameter (default `1`). When greater than one, tasks are dispatched to a `ThreadPoolExecutor` with that many workers. The CLI equivalent is `--concurrency` / `-c`:

```bash
python -m pyfuse worker --backend redis://localhost:6379 -c 4
```

### API surface

| Method | Description |
|--------|-------------|
| `run(task)` | Execute a `Task` (recommended) |
| `run_async(task)` | Execute a `Task`, awaiting the result |
| `execute(json_str, name, *args, **kwargs)` | Execute from raw JSON + function name |
| `execute_async(json_str, name, *args, **kwargs)` | Async variant of `execute()` |
| `cache_info()` | Return `{"size": N, "keys": [...]}` |
| `clear_cache()` | Drop all cached functions |

The module-level `execute()` function provides one-shot execution (no caching across calls). It accepts either a `Task` or `(json_str, function_name, *args, **kwargs)`.

## Remote Execution

The remote execution layer provides a seamless way to submit traced functions to a remote worker and retrieve results, without manual serialization or transport wiring.

### API semantics

- **`func(args)`** -- runs locally (unchanged behavior, sync or async depending on definition).
- **`func.run(args)`** -- packs the function and its dependency graph, submits to the configured backend, and returns a `Result` future.

### Backend abstraction

The `Backend` ABC (`_backend.py`) defines the transport interface:

| Method | Description |
|--------|-------------|
| `submit(task_json)` | Enqueue a serialized task |
| `listen()` | Blocking iterator yielding task JSON strings |
| `send_result(task_id, result_json)` | Store a result envelope |
| `get_result(task_id, timeout)` | Block until result is available |
| `try_get_result(task_id)` | Non-blocking result fetch |
| `close()` | Release resources |

`RedisBackend` is the built-in implementation, using `RPUSH`/`BLPOP` patterns with keys `pyfuse:tasks` (task queue) and `pyfuse:result:{task_id}` (per-task result). The `redis` package is imported lazily and is an optional dependency.

Custom backends can be implemented by subclassing `Backend` for any transport (RabbitMQ, shared memory, HTTP, etc.).

### Result envelope

`ResultEnvelope` (`_result.py`) is the wire format for worker responses:

```json
{"task_id": "abc123", "status": "ok", "result": 42}
```

On error, the traceback is captured and forwarded:

```json
{"task_id": "abc123", "status": "error", "error_type": "ValueError", "error_message": "...", "error_traceback": "Traceback ..."}
```

### Result

`Result` is a future-like handle returned by `func.run()`:

- **`.task_id`** -- the unique task identifier.
- **`.result(timeout=None)`** -- blocks until the result arrives; raises `RemoteError` if the remote execution failed.
- **`.done()`** -- non-blocking check.

### How `.run()` is attached

`@trace` wrappers gain a `.run()` method via `_make_run_method()` in `_graph.py`. The method uses a lazy import to avoid circular dependencies: `from pyfuse._remote import submit_remote` is only resolved when `.run()` is actually called.

### Global backend configuration

`connect(url)` sets the module-level `_active_backend` in `_remote.py`. All subsequent `.run()` calls use this backend. `disconnect()` closes and clears it. The backend is looked up at call time, so functions traced before `connect()` still gain remote execution.

### Worker CLI

```bash
python -m pyfuse worker --backend redis://localhost:6379 [--no-auto-install]
```

The `serve(url)` function in `_remote.py` creates a `Worker`, loops over `backend.listen()`, executes each task, and pushes `ResultEnvelope` responses. Exceptions are caught per-task and sent as error envelopes rather than crashing the worker.

## Limitations

### Static analysis boundaries

- **Type-annotation-based method detection** resolves `obj.method()` calls when the parameter has a type annotation matching a class in the registry, or when the method name is unambiguous. Without annotations and with multiple candidate classes, the call cannot be resolved statically and requires runtime tracing.
- **Local variable types** are not analyzed. Only parameter annotations are used for type-based resolution.
- **Dynamically computed imports** (`__import__()`, `importlib.import_module()` inside function bodies) are not detected.

### Runtime tracing boundaries

- **Unannotated `obj.method()` calls** require at least one runtime invocation to be detected. Adding type annotations to function parameters eliminates this requirement.
- **Circular dependencies** between functions cause `graphlib.CycleError` during reconstruction. Mutually recursive functions are not supported.

### Closure capture

- Closure variables are captured via `repr()` and validated with `ast.parse()`. Values whose `repr()` does not produce valid Python (e.g., file handles, sockets, custom objects without a reconstructable `__repr__`) trigger a warning and are omitted.
- If a closure captures a traced function reference, it is automatically detected and recorded as a dependency via `closure_func_refs`. The captured variable is hoisted as a keyword-only parameter defaulting to the function name.
- If a closure captures a non-traced callable or object with invalid repr, it will be omitted with a warning.

### Source availability

- Functions must be defined in `.py` source files on disk. Attempting to trace builtins, `exec`'d functions, or REPL-defined functions raises `Error` with a descriptive message.

### Import edge cases

- `from . import *` (relative star imports) are not supported. Absolute star imports (`from os.path import *`) are fully resolved.
- Dynamically computed imports (`__import__()`, `importlib.import_module()` inside function bodies) are not detected.

## Backward Compatibility

All renamed classes have aliases at their original names: `FuseGraph`, `FuseStore`, `FuseWorker`, `FuseResult`, and `PyFuseError` remain importable and are identical to the new names.
