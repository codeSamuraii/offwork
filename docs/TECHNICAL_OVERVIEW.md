# Technical Overview

## How it works

pyfuse enables remote execution of Python functions without deploying code to workers. The client serializes a function's source code, its entire dependency tree, and its arguments into a self-contained JSON payload. The worker reconstructs the function from source, installs missing packages, and executes it.

```
  Client                              Worker
  ──────                              ──────
  @trace                              python -m pyfuse worker
  func.run(args)                      ← listen for tasks
    │                                   │
    ├─ capture source + deps            ├─ deserialize graph
    ├─ serialize to JSON                ├─ install missing packages
    ├─ submit via backend ──────────→   ├─ reconstruct source
    │                                   ├─ compile + exec
    ← wait for result ◄────────────── ├─ send result
```

## Architecture

```
pyfuse/
    __init__.py      Public API: trace, connect, serve, serialize, reconstruct, ...
    __main__.py      CLI: python -m pyfuse worker ...
    _decorator.py    @trace: marks functions for remote execution
    _analyzer.py     AST-based source and dependency analysis
    _graph.py        Dependency graph: registration, subgraph extraction, runtime tracing
    _store.py        Content-addressable store: serialization, reconstruction
    _models.py       FunctionNode, ImportInfo dataclasses (incl. content hashing)
    _task.py         Task: serializable envelope bundling graph + arguments
    _worker.py       Worker: reconstruct, install deps, execute with caching
    _backend.py      Backend ABC + RedisBackend: pluggable transport
    _shm_backend.py  SharedMemoryBackend: same-machine IPC via shared memory
    _remote.py       connect/disconnect/serve/submit_remote: remote execution orchestration
    _result.py       Result (future) + ResultEnvelope: result handling
    _deps.py         Third-party dependency extraction and pip installation
    _errors.py       Error, WorkerError, RemoteError, DependencyError
```

## Remote execution flow

### Client side: `func.run(*args)`

1. **Serialize** -- `Graph.serialize(func)` captures the function's subgraph (source, imports, dependencies) as JSON.
2. **Pack** -- A `Task` envelope bundles the serialized graph, function name, arguments, and execution options (timeout, retries).
3. **Submit** -- `backend.submit(task_json)` sends the task to the transport layer.
4. **Return future** -- A `Result` handle is returned immediately to the caller.

### Worker side: `serve()` / `python -m pyfuse worker`

1. **Listen** -- `backend.listen()` blocks until a task arrives.
2. **Deserialize** -- Parse the JSON graph into a `Store`.
3. **Cache check** -- Compute a subgraph key (SHA-256 of all reachable content hashes). If cached, skip to step 6.
4. **Install dependencies** -- Extract third-party imports, install missing packages via pip.
5. **Reconstruct** -- Produce a self-contained Python script from the store. `compile()` and `exec()` it into a fresh namespace.
6. **Execute** -- Call the function with the provided arguments. Apply retry/timeout policies.
7. **Send result** -- Wrap the return value (or exception traceback) in a `ResultEnvelope` and send it back.

### Client side: `future.result()`

1. **Block** -- `backend.get_result(task_id)` waits for the worker's response.
2. **Unwrap** -- If status is `"ok"`, return the value. If `"error"`, raise `RemoteError` with the remote traceback.

## Transport backends

The `Backend` ABC defines the transport interface:

| Method | Description |
|--------|-------------|
| `submit(task_json)` | Enqueue a serialized task |
| `listen()` | Blocking iterator yielding task JSON strings |
| `send_result(task_id, result_json)` | Store a result envelope |
| `get_result(task_id, timeout)` | Block until result is available |
| `try_get_result(task_id)` | Non-blocking result fetch |
| `close()` | Release resources |

### RedisBackend

Uses `RPUSH`/`BLPOP` patterns. Keys:
- `pyfuse:tasks` -- task queue
- `pyfuse:result:{task_id}` -- per-task result (TTL: 300s)

The `redis` package is imported lazily and is an optional dependency.

### SharedMemoryBackend

Uses `multiprocessing.shared_memory` for zero-copy payload transfer and `multiprocessing.managers.BaseManager` for cross-process coordination. No external services required -- suitable for same-machine worker pools.

URL format: `shm://host:port?authkey=secret` (defaults: `127.0.0.1:9847`, authkey `pyfuse`).

The backend auto-detects its role: it tries to connect as a client first; if no server is running, it starts one. Shared memory blocks are tracked and cleaned up on exit via `atexit`.

### Custom backends

Subclass `Backend` to implement any transport (RabbitMQ, HTTP, message queues, etc.):

```python
pyfuse.connect("redis://...")  # built-in
func.run(*args, backend=my_custom_backend)  # per-call override
```

## How `@trace` works

When `@trace` is applied to a function:

### 1. Source capture

`inspect.getsource()` retrieves the function's source. `textwrap.dedent()` normalizes indentation. `@trace` decorator lines are stripped so reconstructed code doesn't depend on pyfuse.

### 2. Import analysis

The source file is parsed with `ast.parse()`. Top-level `Import` and `ImportFrom` nodes are extracted as individual `ImportInfo` objects (one per binding). Only imports whose bound name appears in the function body are kept.

### 3. Dependency detection

The function's AST is walked for `ast.Call` nodes. Four kinds of calls are detected:

| Pattern | Detection method |
|---------|-----------------|
| `helper()` | Matched against registered function names |
| `self.method()` / `cls.method()` | Matched against methods in the same class |
| `obj.method()` with type annotation | Annotation resolved to a class in the registry |
| `obj.method()` without annotation | Unambiguous match (single candidate) or runtime tracing |

### 4. Auto-discovery

When a traced function calls an untraced user-defined function, pyfuse automatically discovers and registers it. This is recursive: if `traced_func()` calls `helper_a()` which calls `helper_b()`, all three end up in the graph.

Cross-module imports (e.g., `from utils import helper`) are converted from import statements to inline dependency edges, so the reconstructed code is self-contained.

**Not auto-discovered:** standard library functions, third-party packages (kept as imports), method calls, class constructors.

### 5. Closure capture

If the function captures variables from an enclosing scope:
- Values are serialized via `repr()` and validated with `ast.parse()`.
- Valid reprs become keyword-only parameters with defaults in reconstructed code.
- Traced function references are recorded as dependency edges.
- Invalid reprs trigger a warning and are skipped.

### 6. Runtime tracing

`@trace` wraps the function to record caller-callee edges at runtime via a `contextvars.ContextVar`-based call stack. This catches dependencies that static analysis cannot resolve (e.g., `obj.method()` calls on untyped variables).

For generators and async generators, a proxy pattern intercepts each iteration step to maintain call stack context throughout lazy evaluation.

### 7. Wrapper setup

The returned wrapper gains:
- `.run(*args)` -- submit to remote worker, returns `Result` future
- `.delay(*args)` -- alias for `.run()`
- `.map(args_list)` -- batch submission
- `__pyfuse_traced__ = True` -- marker attribute

## Task envelope

`Task` is a frozen dataclass bundling everything for remote execution:

| Field | Type | Description |
|-------|------|-------------|
| `graph_json` | `str` | Serialized dependency graph |
| `function_name` | `str` | Qualified name of the target function |
| `args` | `tuple` | Positional arguments |
| `kwargs` | `dict` | Keyword arguments |
| `task_id` | `str` | Auto-generated 12-char hex ID |
| `timeout` | `float \| None` | Per-attempt timeout in seconds |
| `retries` | `int` | Number of retry attempts (default: 0) |
| `retry_delay` | `float` | Base delay between retries (default: 1.0, exponential backoff) |

### Object serialization in arguments

When arguments contain class instances, a custom JSON encoder serializes them via `class_name + __dict__`. On the worker side, after reconstructing the function's namespace, `resolve_args()` rebuilds the objects using the class from the reconstructed namespace.

### Wire format

```json
{
  "id": "a1b2c3d4e5f6",
  "graph": "{\"version\": \"0.3.0\", \"objects\": {...}, ...}",
  "function": "mymodule.hypotenuse",
  "args": [3.0, 4.0],
  "kwargs": {},
  "timeout": 30,
  "retries": 2
}
```

The `graph` field is a JSON string (not nested), keeping the envelope flat. `timeout`, `retries`, and `retry_delay` are omitted when at default values.

## Worker caching

The `Worker` class caches compiled functions by subgraph content hash -- a SHA-256 of all content hashes reachable from the target function, sorted and joined. This means:

- **Same code = cache hit**, regardless of serialization source.
- **Any change** to the function or its dependencies invalidates the cache.
- Repeated calls with identical graphs skip reconstruction entirely.

```python
worker = Worker()
worker.cache_info()   # {"size": 3, "keys": [...]}
worker.clear_cache()
```

## Execution policies

`Worker.run_with_policy(task)` enforces retry and timeout options:

- **Timeout**: Each attempt runs in a `ThreadPoolExecutor` with `future.result(timeout=...)`. Raises `TimeoutError` on expiry.
- **Retries**: On failure, waits `retry_delay * 2^attempt` seconds before retrying. After all attempts exhausted, the last exception is raised.
- **Concurrency**: `serve(concurrency=N)` dispatches tasks to a thread pool. The CLI equivalent is `-c N`.

## Result handling

### ResultEnvelope (wire format)

Success:
```json
{"task_id": "abc123", "status": "ok", "result": 42}
```

Error (includes remote traceback):
```json
{
  "task_id": "abc123",
  "status": "error",
  "error_type": "ValueError",
  "error_message": "...",
  "error_traceback": "Traceback ..."
}
```

### Result (client-side future)

| Method / Property | Description |
|------------------|-------------|
| `.result(timeout=None)` | Block until result; raises `RemoteError` on failure |
| `.wait(timeout=None)` | Alias for `.result()` |
| `.done()` | Non-blocking check |
| `.status` | `"pending"`, `"success"`, or `"error"` |
| `.task_id` | The task identifier |

## Serialization format

Functions are stored in a content-addressable JSON format. Each function is identified by a SHA-256 content hash (16 hex chars) computed from its intrinsic content.

```json
{
  "version": "0.3.0",
  "objects": {
    "a1b2...": {
      "name": "add",
      "module": "mymodule",
      "source": "def add(a: int, b: int) -> int:\n    return a + b",
      "imports": [],
      "owner_class": null
    },
    "c3d4...": {
      "name": "hypotenuse",
      "module": "mymodule",
      "source": "def hypotenuse(a: float, b: float) -> float:\n    ...",
      "imports": [{"statement": "import math", "bound_name": "math"}],
      "owner_class": null
    }
  },
  "deps": {
    "c3d4...": ["a1b2..."]
  },
  "refs": {
    "mymodule.add": "a1b2...",
    "mymodule.hypotenuse": "c3d4..."
  }
}
```

### Content hashing

| Included in hash | Excluded from hash |
|---|---|
| `name`, `module`, `source` | `qualified_name` (derived, fragile on rename) |
| `imports` (sorted), `owner_class` | `deps` (structural, not content) |
| `closure_vars`, `closure_func_refs` (sorted) | |

Because dependencies are excluded from the hash, adding or removing an edge never changes a node's hash. This enables workers to cache objects by hash and request only missing ones: `missing = incoming.keys() - cached.keys()`.

### Reconstruction algorithm

Given a store and a target function name:

1. **Resolve** -- Look up the content hash via the `refs` index.
2. **Walk** -- BFS through `deps` to collect all transitive dependencies.
3. **Sort** -- Topological sort: dependencies before dependents.
4. **Deduplicate imports** -- Merge imports across all functions.
5. **Assemble** -- Emit imports at the top, then functions in order. Methods are grouped into `class` blocks. Closure variables become keyword-only parameters with defaults.

## Data model

### ImportInfo

A single import binding:

| Field | Example |
|-------|---------|
| `statement` | `"import csv"`, `"from os.path import join"` |
| `bound_name` | `"csv"`, `"join"` |
| `package` | `"opencv-python"` (from `install_package_as`, or `None`) |

Multi-name imports are split into individual objects for per-function tracking.

### FunctionNode

One function in the dependency graph:

| Field | Description |
|-------|-------------|
| `qualified_name` | `"module.ClassName.method"` -- unique in-memory identifier |
| `name` | `"method"` -- simple function name |
| `module` | `"module"` -- where the function is defined |
| `source` | Source code with `@trace` stripped, zero-indented |
| `imports` | `list[ImportInfo]` -- only imports this function uses |
| `dependencies` | `list[str]` -- qualified names of dependencies |
| `owner_class` | `"ClassName"` for methods, `None` for standalone functions |
| `closure_vars` | `dict[str, str]` -- captured closure variable `repr()` values |
| `closure_func_refs` | `dict[str, str]` -- references to traced functions captured in closures |

## Dependency auto-installation

When `auto_install=True` (default), the worker extracts third-party module names from the function's imports and installs missing packages via pip.

### Package name resolution

Import names are mapped to pip packages in this priority order:
1. Explicit `import_to_package` argument to `Worker` or `serve()`
2. Hints from `install_package_as()` blocks (embedded in the serialized graph)
3. `DEFAULT_IMPORT_TO_PACKAGE` built-in mapping (`cv2` -> `opencv-python`, `PIL` -> `Pillow`, etc.)

### `install_package_as` context manager

A no-op at runtime. The `@trace` AST analyzer detects the `with` block and records the package name on every `ImportInfo` inside it:

```python
with install_package_as("opencv-python"):
    import cv2
```

The worker sees the `package` field on the import and knows to `pip install opencv-python` instead of `pip install cv2`.

## Thread and task safety

- The runtime call stack uses `contextvars.ContextVar`, providing per-thread isolation in sync code and per-task isolation in async code.
- Async wrappers call `_ensure_isolated_stack()` to copy the stack list, preventing mutations from leaking across `asyncio.Task`s.
- The shared `_runtime_deps` dict is guarded by a `threading.Lock`.

## Star import resolution

When a module contains `from X import *`:
1. Import `X` and read `__all__` (or `dir(X)` minus private names).
2. Create individual `ImportInfo` entries per exported name.
3. Filter to only names the function actually uses.

## Limitations

### Source requirements
- Functions must be defined in `.py` files. Builtins, `exec`'d functions, and REPL definitions raise `Error`.

### Dependency detection
- `obj.method()` calls without type annotations require at least one runtime invocation, or an unambiguous match (single candidate class in the registry).
- Local variable types are not analyzed -- only parameter annotations.
- Dynamic imports (`__import__()`, `importlib.import_module()` in function bodies) are not detected.
- Circular dependencies raise `CycleError` during reconstruction.

### Closure capture
- Values whose `repr()` is not valid Python (file handles, sockets, etc.) are skipped with a warning.
- Non-traced callables captured in closures are skipped.

### Imports
- Relative star imports (`from . import *`) are not supported.
- Aliased cross-module imports (`from utils import helper as h`) are skipped to avoid name mismatches.

## CLI reference

```bash
# Start a worker
python -m pyfuse worker --backend redis://localhost:6379
python -m pyfuse worker --backend redis://localhost:6379 -c 4
python -m pyfuse worker --backend redis://localhost:6379 --no-auto-install

# Show configuration
python -m pyfuse info

# Serialize a function to JSON
python -m pyfuse serialize mymodule:csv_to_json

# Reconstruct source from a graph file
python -m pyfuse reconstruct graph.json csv_to_json
```

## Backward compatibility

All renamed classes have aliases at their original names: `FuseGraph`, `FuseStore`, `FuseWorker`, `FuseResult`, and `PyFuseError` remain importable.
