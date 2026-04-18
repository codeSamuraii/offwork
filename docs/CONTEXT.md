# CONTEXT.md -- AI Assistant Guide

## What is pyfuse?

pyfuse is a Python library for distributed function execution via automatic source code serialization. A `@trace` decorator captures a function's source, imports, and full dependency tree via AST analysis. Workers reconstruct and execute functions from scratch with zero prior knowledge of the code, installing missing packages automatically.

**Version**: 0.4.0 | **License**: AGPL-3.0 | **Python**: 3.13+ | **Zero runtime dependencies**

## Project structure

```
pyfuse/
├── __init__.py              # Public API surface (trace, connect, serve, serialize, etc.)
├── __main__.py              # CLI: worker, run, info, serialize, reconstruct
├── _venv.py                 # Temporary virtual environment management (async)
├── py.typed                 # PEP 561 typed package marker
├── core/
│   ├── task.py              # Task dataclass: serializable envelope (graph + args + options)
│   ├── models.py            # FunctionNode and ImportInfo dataclasses, content hashing
│   ├── version.py           # _VERSION = "0.4.0"
│   ├── errors.py            # Error, WorkerError, RemoteError, DependencyError, TaskStalled, TaskCancelled
│   └── progress.py          # ProgressInfo dataclass, progress() function, context variable
├── graph/
│   ├── decorator.py         # @trace: marks functions, adds .run()/.start()/.map()
│   ├── graph.py             # Graph class: registration, auto-discovery, serialization
│   ├── store.py             # Content-addressable store: serialize/reconstruct/merge
│   ├── analyzer.py          # AST-based source capture, import extraction, dependency detection
│   └── tracing.py           # Runtime call-stack tracing via contextvars (TracingMixin)
├── integrations/
│   ├── __init__.py          # Framework integrations (lazy-loaded)
│   └── asgi.py              # PyfuseLifespan, PyfuseMiddleware for FastAPI/Starlette
└── worker/
    ├── worker.py            # Worker: reconstruct, cache, execute with retry/timeout (async)
    ├── remote.py            # connect(), disconnect(), serve(), submit_remote() (async)
    ├── result.py            # Result (awaitable future), ResultEnvelope
    ├── deps.py              # Third-party dependency extraction and pip installation (async)
    ├── backends/
    │   ├── base.py          # Backend ABC: async transport interface
    │   ├── redis.py         # RedisBackend: redis.asyncio with RPUSH/BLPOP pattern
    │   └── local.py         # LocalBackend: async-native TCP for same-machine IPC
    └── sandbox/
        ├── __init__.py      # re-exports DockerSandbox
        ├── docker.py        # DockerSandbox: Docker container isolation
        ├── guest_agent.py   # Stdlib-only agent deployed inside container
        ├── _protocol.py     # Length-prefixed JSON wire protocol
        └── Dockerfile       # Docker image for the guest agent
```

## Architecture overview

The system has three layers:

### 1. Graph layer (`graph/`)

Handles function analysis and serialization. The `@trace` decorator registers functions in a `Graph`, which uses `analyzer.py` to extract source code, imports, and dependency edges via AST walking. `TracingMixin` adds runtime call-stack recording for dependencies that static analysis can't resolve (e.g., untyped `obj.method()` calls).

The `Store` is a content-addressable JSON format where each function is identified by a SHA-256 hash of its content (source, imports, name, module, owner_class, closure vars -- but NOT dependencies). This enables efficient caching: changing a dependency's edges doesn't invalidate a node's hash.

### 2. Core layer (`core/`)

Data models and error types. `FunctionNode` represents a function in the graph, including class metadata (`class_keywords`, `class_attrs`, `class_decorators`). `ImportInfo` represents a single import binding. `Task` is a frozen dataclass that bundles a serialized graph with function name, arguments, and execution options (timeout, retries).

### 3. Worker layer (`worker/`)

Handles remote execution. Built entirely on `asyncio`:

- **`worker.py`**: `Worker` class reconstructs functions from serialized stores, caches compiled namespaces by subgraph content hash, and executes with retry/timeout policies. Async user functions are awaited directly; sync user functions run in `loop.run_in_executor()` with explicit context propagation via `contextvars.copy_context()`. Timeouts use `asyncio.wait_for()`.
- **`remote.py`**: Orchestrates the connection lifecycle, worker event loop (`asyncio.Semaphore` for bounded concurrency), heartbeat tasks (`asyncio.create_task`), progress injection, cancellation checking, and graceful shutdown via signal handling.
- **`result.py`**: `Result` is an awaitable future returned by `.start()`. Simple async polling loop for stall detection. Supports `cancel()` and `progress()` methods.
- **`deps.py`**: Package installation via `asyncio.create_subprocess_exec`.
- **`backends/`**: All backend methods are `async def`. `listen()` and `subscribe_results()` are async generators.
- **`sandbox/`**: Optional Docker-based execution isolation. `DockerSandbox` boots a container, connects to a stdlib-only `guest_agent.py` over TCP (length-prefixed JSON), and delegates code execution. When `--sandbox` is off, the worker runs code directly in the host process.

## Data flow

```
@trace(func)
  -> analyzer.get_function_source(func)         # extract + dedent source
  -> analyzer.get_module_imports(func)           # parse file for imports
  -> graph.register(func)                        # add FunctionNode to graph
  -> graph._auto_register(func)                  # recursively discover deps

await func.run(*args)          # submit + await result (returns value)
await func.start(*args)        # submit only (returns Result)
  -> graph.serialize(func)                       # build Store, export JSON
  -> Task(graph_json, function_name, args, ...)  # package into Task
  -> await backend.submit(task.to_json())        # send via transport
  -> return Result(task_id, backend)             # future handle (.start())
  -> return await result                         # awaited value (.run())

await Worker.run(task)
  -> store = Store.from_json(task.graph_json)    # deserialize
  -> await deps.ensure_dependencies(...)         # pip install missing
  -> store.reconstruct(func_name)                # emit Python source
  -> compile() + exec() into namespace           # build callable
  -> await namespace[func_name](*args)           # execute (or run_in_executor for sync)
  -> await backend.send_result(task_id, envelope)  # return result
```

## Key patterns and conventions

- **Strict mypy**: The project uses `mypy --strict`. All public APIs have type annotations.
- **Frozen dataclasses**: `Task`, `ImportInfo`, `ResultEnvelope` are frozen. `FunctionNode` is mutable (dependencies are added after creation).
- **Content hashing**: `FunctionNode.content_hash()` produces a 16-char hex SHA-256 digest. Dependencies are excluded from the hash so structural changes don't invalidate content caches.
- **Auto-discovery**: When a traced function calls an untraced user-defined function, pyfuse automatically finds and registers it. This is recursive. Class constructors (`MyClass()`), `@staticmethod`, `@classmethod`, and entire class hierarchies (via `super()`) are discovered too.
- **Cross-module inlining**: Imports like `from utils import helper` where `helper` is a user function get converted from import statements to inline dependency edges, making reconstructed code self-contained.
- **Module-level variables**: Constants and assignments (`MAX_RETRIES = 5`, `CONFIG = {...}`) referenced by traced functions are captured and emitted in reconstructed source.
- **Class-level attributes**: Class body statements (assignments, annotated assignments, docstrings) are extracted from AST and emitted in reconstructed class blocks. Class decorators (`@dataclass`, etc.) and metaclass keywords (`metaclass=ABCMeta`) are captured and emitted.
- **Closure handling**: Multi-tier capture: repr validation → traced functions → lambdas (source extraction) → non-traced user functions (auto-registration) → constructor expressions (defaultdict/Counter/deque) → pickle fallback → warning. Traced function references become dependency edges.
- **Decorator stripping**: `@trace` lines are removed from captured source so reconstructed code doesn't depend on pyfuse.
- **Backend auto-detection**: `connect()` picks Redis or local TCP backend based on URL scheme. Falls back to `PYFUSE_BACKEND` env var.
- **Worker caching**: Keyed by SHA-256 of all reachable content hashes (sorted + joined). Same code from different clients = cache hit.
- **Async-native I/O**: All backend methods, worker execution, result handling, pip installation, and subprocess management use `asyncio`. Sync user functions run in `loop.run_in_executor()` with explicit `contextvars.copy_context()` to propagate progress callbacks.
- **Heartbeat**: Workers send heartbeats via `asyncio.create_task`. Client-side stall detection tracks when heartbeat *values* last changed using local monotonic clock (no cross-machine timestamp comparison).
- **Task cancellation**: Cooperative via backend flags. Workers check before execution; clients store a "cancelled" result envelope. `TaskCancelled` is raised on await.
- **Progress reporting**: Uses `contextvars.ContextVar` for the progress callback. Sync functions get context propagated via explicit copy. Progress updates are fire-and-forget async tasks.
- **Graceful shutdown**: Signal-based (`SIGINT`/`SIGTERM`). First signal stops the listener and waits for in-flight tasks. Second signal cancels all tasks.

## Serialization format (v0.4.0)

```json
{
  "version": "0.4.0",
  "objects": {
    "<content_hash>": {
      "name": "func_name",
      "module": "module_name",
      "source": "def func_name(...): ...",
      "imports": [{"statement": "import x", "bound_name": "x", "package": null}],
      "owner_class": null,
      "closure_vars": {},
      "closure_func_refs": {},
      "module_vars": {},
      "class_bases": [],
      "class_keywords": {},
      "class_attrs": [],
      "class_decorators": []
    }
  },
  "deps": {"<hash>": ["<dep_hash>", ...]},
  "refs": {"module.qualified_name": "<hash>"}
}
```

## Public API summary

```python
# Decorator
@trace                                    # capture function
@trace(timeout=30, retries=3)             # with execution options

# Remote execution (all async)
pyfuse.connect("redis://localhost:6379")  # configure backend (sync)
await pyfuse.serve("redis://...", concurrency=4)  # start worker loop
await func.run(*args)                     # submit + await result (returns value)
future = await func.start(*args)          # submit (returns Result handle)
results = await func.map([(a1, b1), ...]) # batch submit + await all (returns values)

# Result handling
result = await future                     # shorthand for await future.result()
result = await future.result(timeout=10, stall_timeout=10.0)  # with options
await future.done()                       # non-blocking check
await future.status()                     # "pending", "success", "error", or "cancelled"

# Cancellation
await future.cancel()                     # cancel task; raises TaskCancelled when awaited

# Progress reporting
pyfuse.progress(75.0)                     # report percentage (no-op locally)
pyfuse.progress(3, 10, message="step 3")  # report current/total with message
p = await future.progress()               # get latest ProgressInfo (or None)
if p: print(f"{p.current}/{p.total} {p.percent:.0f}%")

# Serialization (sync -- pure CPU)
pyfuse.serialize(func)                    # -> JSON string
pyfuse.reconstruct(json_str, "name")      # -> Python source string
pyfuse.pack(func, *args)                  # -> Task
await pyfuse.execute(task)                # -> return value

# Inspection
pyfuse.get_graph()                        # -> Graph
pyfuse.get_graph().to_mermaid(func)       # -> Mermaid diagram

# ASGI / FastAPI / Starlette integration
from pyfuse.integrations.asgi import pyfuse_lifespan, PyfuseLifespan, PyfuseMiddleware
app = FastAPI(lifespan=pyfuse_lifespan("redis://..."))  # lifespan
app = PyfuseMiddleware(app, url="redis://...")           # middleware
```

## Testing

```bash
pytest                    # run all tests
pytest tests/test_api.py  # specific module
```

16 test modules covering: API surface, AST analysis, async features (Result.result, await, .run(), .start(), .map(), gather, heartbeat, stall detection), auto-discovery (including metaclass keywords, class attributes, class decorators, `__init_subclass__`), cancellation and progress reporting, dependency management, graph operations, integration scenarios, local backend (async-native TCP), remote execution, runtime tracing (including closure capture of non-traced functions, lambdas, constructor expressions, pickle fallback), store operations, stress tests (47 functions across 7 files), task serialization, temp venv management, and worker caching/execution.

All async tests use `pytest-asyncio` with `asyncio_mode = "auto"`.

## Development

```bash
poetry install                  # install with dev dependencies
poetry install --extras redis   # include redis support
mypy pyfuse/                    # type checking (strict mode)
pytest                          # test suite
```

## Important notes for modifications

- The `Store.reconstruct()` method in `graph/store.py` handles topological sorting, import deduplication, and class block assembly. This is the most complex reconstruction logic.
- `analyzer.py` is the core of static analysis (~365 lines). Changes here affect what gets captured.
- `tracing.py` uses `contextvars.ContextVar` for thread/async safety. The `_runtime_deps` dict is guarded by `threading.Lock`.
- The `Task` wire format keeps `graph` as a JSON string (not nested object) to keep the envelope flat.
- Backend implementations must satisfy the `Backend` ABC in `backends/base.py`. All methods are `async def`. New methods (`notify_result`, `subscribe_results`, `get_heartbeats`, `cancel_task`, `is_cancelled`, `send_progress`, `get_progress`) are non-abstract with safe defaults -- custom backends don't break.
- `install_package_as()` is a no-op at runtime; the AST analyzer in `decorator.py`/`analyzer.py` detects the `with` block pattern and tags `ImportInfo` objects with the package name.
- `_capture_closure()` in `graph.py` uses a multi-tier strategy: repr validation → traced functions → lambdas (source extraction) → non-traced user functions (auto-registration) → constructor expressions (defaultdict/Counter/deque) → pickle fallback → warning. Returns function objects for auto-registration.
- `_set_class_metadata()` in `graph.py` captures class-level attributes and decorators from the class source AST. Called from both `_auto_register_class` and `_discover_self_call_deps` to handle both constructor-discovered and directly-traced method classes.
- `_resolve_class_bases()` now also extracts class definition keywords (e.g., `metaclass=ABCMeta`) and adds necessary imports for keyword values.
- `guest_agent.py` is intentionally stdlib-only so it can be deployed into containers without installing pyfuse. It duplicates `_protocol.py` wire helpers for this reason.
- `DockerSandbox` is an async context manager (`__aenter__`/`__aexit__`). The `Worker` calls `start()`/`stop()` at lifecycle boundaries.
- `DockerSandbox` auto-builds the Docker image from the bundled `Dockerfile` on first use.
