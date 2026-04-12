# CONTEXT.md -- AI Assistant Guide

## What is pyfuse?

pyfuse is a Python library for distributed function execution via automatic source code serialization. A `@trace` decorator captures a function's source, imports, and full dependency tree via AST analysis. Workers reconstruct and execute functions from scratch with zero prior knowledge of the code, installing missing packages automatically.

**Version**: 0.4.0 | **License**: AGPL-3.0 | **Python**: 3.13+ | **Zero runtime dependencies**

## Project structure

```
pyfuse/
├── __init__.py              # Public API surface (trace, connect, serve, serialize, etc.)
├── __main__.py              # CLI: worker, run, info, serialize, reconstruct
├── _venv.py                 # Temporary virtual environment management (run/worker --tmp)
├── py.typed                 # PEP 561 typed package marker
├── core/
│   ├── task.py              # Task dataclass: serializable envelope (graph + args + options)
│   ├── models.py            # FunctionNode and ImportInfo dataclasses, content hashing (incl. class_keywords, class_attrs, class_decorators)
│   ├── version.py           # _VERSION = "0.4.0"
│   └── errors.py            # Error, WorkerError, RemoteError, DependencyError, TaskStalled
├── graph/
│   ├── decorator.py         # @trace: marks functions, adds .run()/.map()/.arun()/.amap()
│   ├── graph.py             # Graph class: registration, auto-discovery, serialization
│   ├── store.py             # Content-addressable store: serialize/reconstruct/merge
│   ├── analyzer.py          # AST-based source capture, import extraction, dependency detection, class attrs/decorators
│   └── tracing.py           # Runtime call-stack tracing via contextvars (TracingMixin)
└── worker/
    ├── worker.py            # Worker: reconstruct, cache, execute with retry/timeout
    ├── remote.py            # connect(), disconnect(), serve(), submit_remote()
    ├── result.py            # Result (future), ResultEnvelope, ResultWaiter (notification fan-out)
    ├── deps.py              # Third-party dependency extraction and pip installation
    └── backends/
        ├── base.py          # Backend ABC: submit, listen, send_result, get_result, heartbeat, notifications
        ├── redis.py         # RedisBackend: RPUSH/BLPOP pattern
        └── shm.py           # SharedMemoryBackend: multiprocessing shared memory IPC
```

## Architecture overview

The system has three layers:

### 1. Graph layer (`graph/`)

Handles function analysis and serialization. The `@trace` decorator registers functions in a `Graph`, which uses `analyzer.py` to extract source code, imports, and dependency edges via AST walking. `TracingMixin` adds runtime call-stack recording for dependencies that static analysis can't resolve (e.g., untyped `obj.method()` calls).

The `Store` is a content-addressable JSON format where each function is identified by a SHA-256 hash of its content (source, imports, name, module, owner_class, closure vars -- but NOT dependencies). This enables efficient caching: changing a dependency's edges doesn't invalidate a node's hash.

### 2. Core layer (`core/`)

Data models and error types. `FunctionNode` represents a function in the graph, including class metadata (`class_keywords`, `class_attrs`, `class_decorators`). `ImportInfo` represents a single import binding. `Task` is a frozen dataclass that bundles a serialized graph with function name, arguments, and execution options (timeout, retries).

### 3. Worker layer (`worker/`)

Handles remote execution. The `Worker` class reconstructs functions from serialized stores, caches compiled namespaces by subgraph content hash, and executes with retry/timeout policies (including `async def` functions via `asyncio.run()`). `remote.py` orchestrates the connection lifecycle, worker event loop, and heartbeat threads. `Result` is an awaitable future returned by `.run()`. The `ResultWaiter` singleton per backend uses push notifications to fan out results to many waiters without polling.

## Data flow

```
@trace(func)
  -> analyzer.get_function_source(func)         # extract + dedent source
  -> analyzer.get_module_imports(func)           # parse file for imports
  -> graph.register(func)                        # add FunctionNode to graph
  -> graph._auto_register(func)                  # recursively discover deps

func.run(*args)
  -> graph.serialize(func)                       # build Store, export JSON
  -> Task(graph_json, function_name, args, ...)  # package into Task
  -> backend.submit(task.to_json())              # send via transport
  -> return Result(task_id, backend)             # future handle

Worker.run(task)
  -> store = Store.from_json(task.graph_json)    # deserialize
  -> deps.ensure_dependencies(store, func_name)  # pip install missing
  -> store.reconstruct(func_name)                # emit Python source
  -> compile() + exec() into namespace           # build callable
  -> namespace[func_name](*args, **kwargs)       # execute
  -> backend.send_result(task_id, envelope)      # return result
```

## Key patterns and conventions

- **Strict mypy**: The project uses `mypy --strict`. All public APIs have type annotations.
- **Frozen dataclasses**: `Task`, `ImportInfo`, `ResultEnvelope` are frozen. `FunctionNode` is mutable (dependencies are added after creation).
- **Content hashing**: `FunctionNode.content_hash()` produces a 16-char hex SHA-256 digest. Dependencies are excluded from the hash so structural changes don't invalidate content caches.
- **Auto-discovery**: When a traced function calls an untraced user-defined function, pyfuse automatically finds and registers it. This is recursive. Class constructors (`MyClass()`), `@staticmethod`, `@classmethod`, and entire class hierarchies (via `super()`) are discovered too.
- **Cross-module inlining**: Imports like `from utils import helper` where `helper` is a user function get converted from import statements to inline dependency edges, making reconstructed code self-contained.
- **Module-level variables**: Constants and assignments (`MAX_RETRIES = 5`, `CONFIG = {...}`) referenced by traced functions are captured and emitted in reconstructed source.
- **Class-level attributes**: Class body statements (assignments, annotated assignments, docstrings) are extracted from AST and emitted in reconstructed class blocks. Class decorators (`@dataclass`, etc.) and metaclass keywords (`metaclass=ABCMeta`) are captured and emitted.
- **Closure handling**: Multi-tier capture: `repr()` validation, then lambda source extraction, then auto-discovery for non-traced user functions, then constructor expressions for common stdlib types (`defaultdict`, `Counter`, `deque`), then pickle fallback for picklable objects. Traced function references become dependency edges.
- **Decorator stripping**: `@trace` lines are removed from captured source so reconstructed code doesn't depend on pyfuse.
- **Backend auto-detection**: `connect()` picks Redis or shared memory based on URL scheme. Falls back to `PYFUSE_BACKEND` env var.
- **Worker caching**: Keyed by SHA-256 of all reachable content hashes (sorted + joined). Same code from different clients = cache hit.
- **Async transparency**: Workers detect `async def` functions and run them via `asyncio.run()`. Results are awaitable via `asyncio.Future` fan-out.
- **Notification fan-out**: `ResultWaiter` singleton per backend runs one listener thread and one heartbeat thread, serving all pending `Result` objects. No per-task polling.
- **Heartbeat monitoring**: Workers send heartbeats every 1s. Client-side stall detection tracks when heartbeat *values* last changed using local monotonic clock (no cross-machine timestamp comparison).

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

# Remote execution
pyfuse.connect("redis://localhost:6379")  # configure backend
pyfuse.serve("redis://...", concurrency=4)  # start worker loop
func.run(*args)                           # -> Result (awaitable future)
func.map([(a1, b1), (a2, b2)])            # -> list[Result]
await func.arun(*args)                    # async submit + await
await func.amap([(a1, b1), ...])          # async batch submit + await all

# Result handling
result = future.result(timeout=10)        # sync blocking
result = await future                     # async await
result = await future.aresult(timeout=10, stall_timeout=10.0)  # async with options

# Serialization
pyfuse.serialize(func)                    # -> JSON string
pyfuse.reconstruct(json_str, "name")      # -> Python source string
pyfuse.pack(func, *args)                  # -> Task
pyfuse.execute(task)                      # -> return value

# Inspection
pyfuse.get_graph()                        # -> Graph
pyfuse.get_graph().to_mermaid(func)       # -> Mermaid diagram
```

## Testing

```bash
pytest                    # run all tests
pytest tests/test_api.py  # specific module
```

15 test modules covering: API surface, AST analysis, async features (aresult, await, arun, amap, gather, heartbeat, stall detection, notification-based result delivery), auto-discovery (including metaclass keywords, class attributes, class decorators, `__init_subclass__`), dependency management, graph operations, integration scenarios, remote execution, runtime tracing (including closure capture of non-traced functions, lambdas, constructor expressions, pickle fallback), shared memory backend, store operations, stress tests (47 functions across 7 files), task serialization, temp venv management, and worker caching/execution.

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
- Backend implementations must satisfy the `Backend` ABC in `backends/base.py`. New methods (`notify_result`, `subscribe_results`, `get_heartbeats`) are non-abstract with safe defaults -- custom backends don't break.
- `ResultWaiter` in `result.py` is a per-backend singleton with two daemon threads (listener + heartbeat). It uses `loop.call_soon_threadsafe()` for async fan-out and `threading.Event` for sync fan-out.
- `install_package_as()` is a no-op at runtime; the AST analyzer in `decorator.py`/`analyzer.py` detects the `with` block pattern and tags `ImportInfo` objects with the package name.
- `_capture_closure()` in `graph.py` uses a multi-tier strategy: repr validation → traced functions → lambdas (source extraction) → non-traced user functions (auto-registration) → constructor expressions (defaultdict/Counter/deque) → pickle fallback → warning. Returns function objects for auto-registration.
- `_set_class_metadata()` in `graph.py` captures class-level attributes and decorators from the class source AST. Called from both `_auto_register_class` and `_discover_self_call_deps` to handle both constructor-discovered and directly-traced method classes.
- `_resolve_class_bases()` now also extracts class definition keywords (e.g., `metaclass=ABCMeta`) and adds necessary imports for keyword values.
