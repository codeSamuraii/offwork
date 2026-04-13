# Technical Overview

## How it works

pyfuse enables remote execution of Python functions without deploying code to workers. The client serializes a function's source code, its entire dependency tree, and its arguments into a self-contained JSON payload. The worker reconstructs the function from source, installs missing packages, and executes it.

```
  Client                              Worker
  ──────                              ──────
  @trace                              pyfuse worker
  await func.run(args)                ← listen for tasks
    │                                   │
    ├─ capture source + deps            ├─ deserialize graph
    ├─ serialize to JSON                ├─ install missing packages
    ├─ submit via backend ──────────→   ├─ reconstruct source
    │                                   ├─ compile + exec
    ← await result    ◄────────────── ├─ send result
```

## Architecture

```
pyfuse/
    __init__.py              Public API: trace, connect, serve, serialize, reconstruct, ...
    __main__.py              CLI: pyfuse worker/run/info/serialize/reconstruct
    _venv.py                 Temporary virtual environment management (async)
    core/
        models.py            FunctionNode, ImportInfo dataclasses (incl. content hashing, class metadata)
        task.py              Task: serializable envelope bundling graph + arguments
        version.py           Version constant
        errors.py            Error, WorkerError, RemoteError, DependencyError, TaskStalled
    graph/
        decorator.py         @trace: marks functions for remote execution
        analyzer.py          AST-based source and dependency analysis, class attrs/decorators
        graph.py             Dependency graph: registration, auto-discovery, runtime tracing
        store.py             Content-addressable store: serialization, reconstruction
        tracing.py           Runtime call-stack tracing via contextvars
    worker/
        worker.py            Worker: reconstruct, install deps, execute with caching (async)
        remote.py            connect/disconnect/serve/submit_remote: orchestration (async)
        result.py            Result (awaitable future), ResultEnvelope
        deps.py              Third-party dependency extraction and pip installation (async)
        backends/
            base.py          Backend ABC: async pluggable transport interface
            redis.py         RedisBackend: redis.asyncio with RPUSH/BLPOP pattern
            local.py         LocalBackend: async-native TCP for same-machine IPC
```

## Remote execution flow

### Client side: `await func.run(*args)` / `await func.start(*args)`

1. **Serialize** -- `Graph.serialize(func)` captures the function's subgraph (source, imports, dependencies) as JSON.
2. **Pack** -- A `Task` envelope bundles the serialized graph, function name, arguments, and execution options (timeout, retries).
3. **Submit** -- `await backend.submit(task_json)` sends the task to the transport layer.
4. **Return** -- `.run()` awaits the result and returns the value directly. `.start()` returns a `Result` handle immediately.

### Worker side: `await serve()` / `pyfuse worker`

1. **Listen** -- `async for task_json in backend.listen()` yields tasks as they arrive.
2. **Cancellation check** -- If `await backend.is_cancelled(task_id)` returns ``True``, skip execution and log the cancellation.
3. **Deserialize** -- Parse the JSON graph into a `Store`.
4. **Cache check** -- Compute a subgraph key (SHA-256 of all reachable content hashes). If cached, skip to step 7.
5. **Install dependencies** -- Extract third-party imports, install missing packages via `asyncio.create_subprocess_exec`.
6. **Reconstruct** -- Produce a self-contained Python script from the store. `compile()` and `exec()` it into a fresh namespace.
7. **Execute** -- Call the function with the provided arguments. Async functions are awaited directly; sync functions run in an executor with explicit context propagation (for progress reporting). Apply retry/timeout policies via `asyncio.wait_for`.
8. **Send result** -- Wrap the return value (or exception traceback) in a `ResultEnvelope` and send it back. If cancelled during execution, skip result delivery.

### Client side: `await future` / `await future.result()`

The `Result` object supports two waiting strategies:

**Without stall detection** (`stall_timeout=None`) -- `await backend.get_result(task_id, timeout)` blocks until the worker's response arrives (e.g. Redis `BLPOP`).

**With stall detection** (default, `stall_timeout=10.0`) -- An async polling loop calls `try_get_result()` and checks heartbeats every second. If the heartbeat hasn't changed for longer than `stall_timeout`, `TaskStalled` is raised.

**Unwrap** -- If status is `"ok"`, return the value. If `"error"`, raise `RemoteError` with the remote traceback.

## Transport backends

The `Backend` ABC defines an async transport interface:

| Method | Description |
|--------|-------------|
| `async submit(task_json)` | Enqueue a serialized task |
| `async listen()` | Async iterator yielding task JSON strings |
| `async send_result(task_id, result_json)` | Store a result envelope |
| `async get_result(task_id, timeout)` | Block until result is available |
| `async try_get_result(task_id)` | Non-blocking result fetch |
| `async send_heartbeat(task_id)` | Signal active processing (no-op default) |
| `async get_heartbeat(task_id)` | Get last heartbeat timestamp |
| `async get_heartbeats(task_ids)` | Batch heartbeat fetch (default loops over `get_heartbeat`) |
| `async cancel_task(task_id)` | Mark a task as cancelled (no-op default) |
| `async is_cancelled(task_id)` | Check cancellation flag (default returns `False`) |
| `async send_progress(task_id, json)` | Store latest progress data (no-op default) |
| `async get_progress(task_id)` | Get latest progress JSON (default returns `None`) |
| `async notify_result(task_id)` | Push notification that result is ready (no-op default) |
| `async subscribe_results()` | Async iterator yielding task_ids on result arrival |
| `async close()` | Release resources |

All methods are `async def`. Methods below the line are non-abstract with safe defaults.

### RedisBackend

Uses `redis.asyncio.Redis` with `RPUSH`/`BLPOP` patterns. Keys:
- `pyfuse:tasks` -- task queue
- `pyfuse:result:{task_id}` -- per-task result (TTL: 300s)
- `pyfuse:heartbeat:{task_id}` -- worker heartbeat timestamp (TTL: 30s)
- `pyfuse:cancel:{task_id}` -- cancellation flag (TTL: 3600s)
- `pyfuse:progress:{task_id}` -- latest progress JSON (TTL: 300s)
- `pyfuse:notify` -- Pub/Sub channel for result notifications

Result notifications use Redis Pub/Sub (`PUBLISH`/`SUBSCRIBE`). Batch heartbeat fetching uses `MGET` for efficiency.

The `redis` package is imported lazily and is an optional dependency.

### LocalBackend

An async-native TCP backend for same-machine IPC.  A lightweight broker server built on `asyncio.start_server` handles task dispatch, result routing, and heartbeats -- no threads, no `multiprocessing`, no external services.

URL format: `local://host:port` (default: `127.0.0.1:9748`).

The broker auto-starts as a subprocess on first connection (or can be started explicitly with `server=True`). All I/O is native `asyncio` -- the backend opens TCP connections to the broker and communicates via a length-prefixed JSON protocol. Streaming operations (`listen()`, `subscribe_results()`) use dedicated connections; RPC operations share a single connection protected by an `asyncio.Lock`.

### Custom backends

Subclass `Backend` to implement any transport (RabbitMQ, HTTP, message queues, etc.):

```python
pyfuse.connect("redis://...")  # built-in
await func.start(*args, backend=my_custom_backend)  # per-call override
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

Class constructors (`MyClass()`) are auto-discovered: pyfuse registers all user-defined methods of the class. `@staticmethod` and `@classmethod` descriptors are unwrapped and registered correctly. When a method uses `super()`, base classes and their methods are discovered recursively, and `class Foo(Base):` headers are emitted in reconstructed source.

Class-level attributes (assignments, annotated assignments, docstrings) are extracted from the class source AST and emitted in reconstructed class blocks. Class decorators (e.g., `@dataclass`) are captured and emitted above the class header. Metaclass keywords (e.g., `metaclass=ABCMeta`) and other class keywords are extracted from the class definition and included in the reconstructed header.

Module-level constants and variables referenced by traced functions (e.g., `MAX_RETRIES = 5`) are captured and emitted in reconstructed source.

**Not auto-discovered:** standard library functions, third-party packages (kept as imports).

### 5. Closure capture

If the function captures variables from an enclosing scope, pyfuse uses a multi-tier capture strategy:

1. **`repr()` validation** -- Values whose `repr()` is valid Python (passes `ast.parse()`) are stored directly. They become keyword-only parameters with defaults in reconstructed code.
2. **Traced functions** -- References to `@trace`-decorated functions are recorded as dependency edges.
3. **Lambda functions** -- Source is extracted via `inspect.getsource()` + AST walking, stored as a closure variable expression.
4. **Non-traced user functions** -- Automatically discovered and registered as dependencies (same as traced functions).
5. **Constructor expressions** -- Common stdlib types (`defaultdict`, `Counter`, `deque`) whose `repr()` isn't valid Python are captured via self-contained constructor expressions (e.g., `__import__('collections').defaultdict(int, {'a': 1})`).
6. **Pickle fallback** -- Picklable objects are serialized via `pickle.dumps()` + base64 encoding into a self-contained expression.
7. **Warning** -- Objects that can't be captured by any method trigger a warning with the variable name and type.

### 6. Runtime tracing

`@trace` wraps the function to record caller-callee edges at runtime via a `contextvars.ContextVar`-based call stack. This catches dependencies that static analysis cannot resolve (e.g., `obj.method()` calls on untyped variables).

For generators and async generators, a proxy pattern intercepts each iteration step to maintain call stack context throughout lazy evaluation.

### 7. Wrapper setup

The returned wrapper gains:
- `.run(*args)` -- submit to remote worker and await result (coroutine)
- `.start(*args)` -- submit to remote worker, returns `Result` handle (coroutine)
- `.map(args_list)` -- batch submit and await all results (coroutine)
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

When arguments contain class instances, a custom JSON encoder serializes them via `class_name + __dict__`. Objects using `__slots__` are supported by walking the MRO to collect all slot names. On the worker side, after reconstructing the function's namespace, `resolve_args()` rebuilds the objects using the class from the reconstructed namespace.

### Wire format

```json
{
  "id": "a1b2c3d4e5f6",
  "graph": "{\"version\": \"0.4.0\", \"objects\": {...}, ...}",
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

- **Timeout**: Each attempt is wrapped in `asyncio.wait_for(self.run(task), timeout=...)`. Raises `TimeoutError` on expiry.
- **Retries**: On failure, waits `retry_delay * 2^attempt` seconds (via `asyncio.sleep`) before retrying. After all attempts exhausted, the last exception is raised.
- **Concurrency**: `serve(concurrency=N)` uses `asyncio.Semaphore(N)` with `asyncio.TaskGroup` to limit concurrent task execution. The CLI equivalent is `-c N`.

## Async architecture

The entire I/O layer is built on `asyncio`:

- **Backend methods** are all `async def`. `listen()` and `subscribe_results()` are async generators.
- **Worker execution**: Async user functions are `await`ed directly. Sync user functions run in `loop.run_in_executor(None, ...)` to avoid blocking the event loop.
- **Heartbeats** use `asyncio.create_task()` with `asyncio.Event` for cancellation -- no daemon threads.
- **Worker loop**: `asyncio.TaskGroup` + `asyncio.Semaphore` for bounded concurrency.
- **Subprocess management**: `asyncio.create_subprocess_exec()` for pip installs and venv creation.
- **Timeouts**: `asyncio.wait_for()` instead of thread-based timeout hacks.
- **Result waiting**: Simple async polling loop for stall detection, or direct `await backend.get_result()`.

The graph/serialization layer (AST analysis, content hashing, source reconstruction) remains synchronous -- it's pure CPU work that doesn't benefit from async.

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
| `await result` | Shorthand for `await result.result()` |
| `await result.result(timeout, stall_timeout=10.0)` | Await with options; raises `RemoteError` on failure, `TaskStalled` on stall, `TaskCancelled` on cancel |
| `await result.cancel()` | Cancel the task; awaiting raises `TaskCancelled` |
| `await result.progress()` | Latest `ProgressInfo` (or `None`) |
| `await result.done()` | Non-blocking check |
| `await result.status()` | `"pending"`, `"success"`, `"error"`, or `"cancelled"` |
| `.task_id` | The task identifier |

## Serialization format

Functions are stored in a content-addressable JSON format. Each function is identified by a SHA-256 content hash (16 hex chars) computed from its intrinsic content.

```json
{
  "version": "0.4.0",
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
      "owner_class": null,
      "module_vars": {"PRECISION": "PRECISION = 1e-10"}
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

Optional fields (`closure_vars`, `closure_func_refs`, `module_vars`, `class_bases`, `class_keywords`, `class_attrs`, `class_decorators`) are omitted when empty.

### Content hashing

| Included in hash | Excluded from hash |
|---|---|
| `name`, `module`, `source` | `qualified_name` (derived, fragile on rename) |
| `imports` (sorted), `owner_class` | `deps` (structural, not content) |
| `closure_vars`, `closure_func_refs` (sorted) | |
| `module_vars` (sorted), `class_bases` | |
| `class_keywords` (sorted), `class_attrs`, `class_decorators` | |

Because dependencies are excluded from the hash, adding or removing an edge never changes a node's hash. This enables workers to cache objects by hash and request only missing ones: `missing = incoming.keys() - cached.keys()`.

### Reconstruction algorithm

Given a store and a target function name:

1. **Resolve** -- Look up the content hash via the `refs` index.
2. **Walk** -- BFS through `deps` to collect all transitive dependencies.
3. **Sort** -- Topological sort: dependencies before dependents.
4. **Deduplicate imports** -- Merge imports across all functions.
5. **Assemble** -- Emit imports, then module-level variable assignments, then functions in order. Methods are grouped into `class` blocks with decorators, base classes, metaclass keywords, class-level attributes, and methods. Closure variables become keyword-only parameters with defaults.

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
| `module_vars` | `dict[str, str]` -- module-level variable assignments (name -> source) |
| `class_bases` | `list[str]` -- base class names for methods in classes with inheritance |
| `class_keywords` | `dict[str, str]` -- class definition keywords (e.g., `{"metaclass": "ABCMeta"}`) |
| `class_attrs` | `list[str]` -- class-level attribute source lines (assignments, docstrings) |
| `class_decorators` | `list[str]` -- class decorator source strings (without `@` prefix) |

## Dependency auto-installation

When `auto_install=True` (default), the worker extracts third-party module names from the function's imports and installs missing packages via pip using `asyncio.create_subprocess_exec`.

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

## Heartbeat and stall detection

Workers send periodic heartbeats (every 1 second) while executing a task via an `asyncio.Task`. Clients use heartbeat data for stall detection.

**Worker side:** An `asyncio.Task` calls `await backend.send_heartbeat(task_id)` every second. The task is cancelled when execution completes (success or failure).

**Client side:** The `Result.result()` method's async polling loop calls `await backend.get_heartbeat(task_id)` once per second. It tracks when each task's heartbeat value last *changed* using the client's monotonic clock -- no cross-machine timestamp comparison. If the heartbeat hasn't changed for longer than `stall_timeout`, `TaskStalled` is raised.

Stall detection only triggers after at least one heartbeat has been observed, avoiding false positives for tasks that haven't started yet.

| Method | Stall detection |
|--------|----------------|
| `await result.result()` | On by default (`stall_timeout=10.0`). Disable with `stall_timeout=None` |

## Task cancellation

Tasks can be cancelled via `await result.cancel()`. Cancellation is cooperative:

1. **Client** calls `cancel()`, which sets a cancellation flag in the backend and stores a ``"cancelled"`` result envelope.
2. **Worker** checks `is_cancelled()` before starting execution. If cancelled, the task is skipped entirely (no result sent, since the cancel already stored one).
3. **During execution**: if `cancel()` is called while a task is running, the execution continues. When the worker finishes, it checks `is_cancelled()` again and discards the result if cancelled.
4. **Client** receives `TaskCancelled` when awaiting a cancelled task.

### Backend storage

| Backend | Cancellation storage |
|---------|---------------------|
| Redis | `SET pyfuse:cancel:{task_id} 1 EX 3600` |
| Local | In-memory `set()` in the broker |

## Progress reporting

Tasks can report progress via `pyfuse.progress(percent)` or `pyfuse.progress(current, total)`.

### How it works

1. **Context variable**: A `contextvars.ContextVar` holds the progress callback. The worker sets it before executing each task.
2. **Sync function support**: `Worker.run()` explicitly propagates context variables to executor threads via `contextvars.copy_context().run()`.
3. **Rate-limited sends**: The progress callback rate-limits backend sends to one per 50 ms. Intermediate updates are stored locally. A ``flush()`` coroutine sends the final state after execution completes.
4. **No-op locally**: When called outside a worker, `progress()` is a silent no-op (context variable is ``None``).

### Backend storage

| Backend | Progress storage |
|---------|-----------------|
| Redis | `SET pyfuse:progress:{task_id} <json> EX 300` |
| Local | In-memory `dict` in the broker |

## Graceful shutdown

Workers support graceful shutdown via signal handling:

1. **First SIGINT/SIGTERM**: Sets a shutdown event, stops accepting new tasks, and waits for in-progress tasks to complete.
2. **Second SIGINT/SIGTERM**: Cancels all in-progress tasks immediately and exits.

Signal handlers are installed via `loop.add_signal_handler()` (Unix). On Windows, falls back to `KeyboardInterrupt` handling. The worker logs shutdown progress:

```
12:30:00 INFO    Graceful shutdown: waiting for 2 task(s) to complete... (Ctrl+C to force quit)
12:30:02 INFO    Worker stopped.
```

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
- Objects that are neither repr-serializable, picklable, nor user-defined callables are skipped with a warning (e.g., file handles, sockets).

### Imports
- Relative star imports (`from . import *`) are not supported.
- Aliased cross-module imports (`from utils import helper as h`) are skipped to avoid name mismatches.

### Classes
- Metaclasses and `__init_subclass__` hooks are replayed when the parent class is in the dependency tree (i.e., referenced via `super()` or constructor call).
- Class-level attributes defined via complex descriptors or external decorators (beyond simple assignments) may not be captured.

## CLI reference

```bash
# Start a worker
pyfuse worker --backend redis://localhost:6379
pyfuse worker --backend redis://localhost:6379 -c 4
pyfuse worker --backend redis://localhost:6379 --no-auto-install
pyfuse worker --backend redis://localhost:6379 --tmp   # isolated temp venv

# Run a script in a temporary venv (auto-detects and installs dependencies)
pyfuse run examples/script.py

# Show configuration
pyfuse info

# Serialize a function to JSON
pyfuse serialize mymodule:csv_to_json

# Reconstruct source from a graph file
pyfuse reconstruct graph.json csv_to_json
```
