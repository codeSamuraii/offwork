# away — Context for Coding Assistants

This file is a compact, technical orientation for AI coding assistants. For user-facing docs see [README.md](../README.md), [docs/QUICK_START.md](QUICK_START.md), [docs/TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md), [docs/SIGNING.md](SIGNING.md), [docs/SANDBOX.md](SANDBOX.md).

## What away is

A Python package that serializes a function — its source, its dependency graph, its imports, its closure, its arguments — into a self-contained JSON envelope, ships it to a worker process, and executes it there. Workers need no prior knowledge of the user's codebase: they reconstruct source from the payload, `pip install` missing packages on the fly, `compile` + `exec` the result, and return the value.

Add `@trace` to one entry-point function. Call `await func.run(...)`. That is the entire surface area.

## Design goals

- **Zero deployment** — no shared filesystem, no image rebuilds, no code sync. The client ships everything the worker needs in one envelope.
- **Zero setup for users** — one decorator (`@trace`), one connect call. Workers auto-install missing third-party packages.
- **Zero hard dependencies** — away itself has no required runtime deps. `redis`, `aio-pika`, `docker` are optional extras loaded lazily.
- **Async-native** — all I/O (`Backend`, `Worker`, `Result`, `_venv`) is `asyncio`. Sync user functions run in `loop.run_in_executor`.
- **Pluggable transport** — `Backend` ABC abstracts task queue + result store + heartbeat + cancellation + progress + scheduling + throttling.
- **Content-addressable caching** — functions are keyed by SHA-256 of their content. Same code → cache hit, regardless of source.
- **Strict typing** — strict mypy, `py.typed` marker shipped.

## Feature surface

| Feature | Entry point | Implementation |
|---|---|---|
| Decorator | `@trace` | [away/graph/decorator.py](../away/graph/decorator.py) |
| Auto-capture (source, imports, closures, classes, module vars) | `Graph.serialize` | [away/graph/analyzer.py](../away/graph/analyzer.py), [away/graph/graph.py](../away/graph/graph.py) |
| Reconstruction → self-contained source | `Graph.reconstruct` | [away/graph/store.py](../away/graph/store.py) |
| Runtime call-stack tracing | `contextvars` | [away/graph/tracing.py](../away/graph/tracing.py) |
| Remote submit / await | `func.run`, `func.start`, `func.map` | [away/worker/remote.py](../away/worker/remote.py), [away/graph/decorator.py](../away/graph/decorator.py) |
| Worker loop (signing, scheduling, throttle, heartbeat) | `serve` | [away/worker/remote.py](../away/worker/remote.py) |
| Subgraph caching, reconstruct, retry, timeout | `Worker.run_with_policy` | [away/worker/worker.py](../away/worker/worker.py) |
| Auto-install of third-party packages | `install_package_as`, `ensure_dependencies` | [away/worker/deps.py](../away/worker/deps.py) |
| Worker-only imports (skip local install) | `worker_only_import` | [away/worker/deps.py](../away/worker/deps.py) |
| Result handle, status, progress, cancel | `Result` | [away/worker/result.py](../away/worker/result.py) |
| Scheduling (`run_in`, `run_at`, `run_every`) | `ScheduleHandle` | [away/worker/schedule.py](../away/worker/schedule.py) |
| Throttling (`throttle=...`) | `Backend.check_throttle` | backends + [away/worker/worker.py](../away/worker/worker.py) |
| Progress reporting | `away.progress(...)` | [away/core/progress.py](../away/core/progress.py) |
| Heartbeat & stall detection | `Backend.send_heartbeat` | `_heartbeat_loop` in [away/worker/remote.py](../away/worker/remote.py), [away/worker/result.py](../away/worker/result.py) |
| Local TCP backend | `local://` | [away/worker/backends/local.py](../away/worker/backends/local.py) |
| Redis backend | `redis://` | [away/worker/backends/redis.py](../away/worker/backends/redis.py) |
| RabbitMQ backend | `amqp://` | [away/worker/backends/rabbitmq.py](../away/worker/backends/rabbitmq.py) |
| Docker sandbox isolation | `--sandbox`, `DockerSandbox` | [away/worker/sandbox/](../away/worker/sandbox/) |
| HMAC-SHA256 task signing | `--require-signing`, token, pairing | [away/core/signing.py](../away/core/signing.py), [away/core/token.py](../away/core/token.py), [away/core/pairing.py](../away/core/pairing.py) |
| Temp venv (for `--tmp` and `away run`) | `temp_venv` | [away/_venv.py](../away/_venv.py) |
| CLI | `python -m away ...` | [away/__main__.py](../away/__main__.py) |

## Repository layout

```
away/
    __init__.py          Public API surface (re-exports). __all__ is the contract.
    __main__.py          CLI: worker, run, pair, token, sandbox, info, serialize, reconstruct.
    _venv.py             Async temp venv (used by --tmp and `away run`).
    typing.py            Public type aliases.
    core/
        models.py        FunctionNode, ImportInfo dataclasses + content hashing.
        task.py          Task envelope (graph_json + name + args + options).
        errors.py        Error hierarchy. All exceptions inherit Error.
        progress.py      ProgressInfo + progress() contextvar callback.
        version.py       _VERSION (resolved from package metadata).
        signing.py       HMAC-SHA256 sign/verify, derive_key.
        token.py         Token generate/save/load (~/.away/token).
        pairing.py       6-digit-PIN ECDH-style key exchange.
    graph/
        decorator.py     @trace. Wraps function with .run/.start/.map and traced markers.
        analyzer.py      AST analysis: imports, calls, closures, classes, module vars,
                         install_package_as / worker_only_import detection,
                         star-import resolution.
        graph.py         Graph: registry, auto-discovery, serialize/reconstruct entry.
        store.py         Content-addressable Store: pack, unpack, topo-sort, emit source.
        tracing.py       Runtime caller→callee tracking via contextvars.ContextVar.
    worker/
        worker.py        Worker: subgraph cache, ensure_dependencies, reconstruct,
                         exec, retry/timeout (run_with_policy).
        remote.py        connect/disconnect/serve, submit_remote, _heartbeat_loop,
                         throttle/cancel/scheduling checks, recurring re-enqueue.
        result.py        Result (awaitable), ResultEnvelope (wire format).
        deps.py          Third-party detection, DEFAULT_IMPORT_TO_PACKAGE,
                         install_package_as / worker_only_import context managers,
                         meta-path stub finder, async pip subprocess.
        schedule.py      ScheduleHandle for recurring tasks.
        backends/
            base.py      Backend ABC. Source of truth for transport contract.
            local.py     Async TCP broker (auto-spawned subprocess).
            redis.py     redis.asyncio (RPUSH/BLPOP, Pub/Sub, MGET).
            rabbitmq.py  aio-pika (durable queue, fanout exchange, TTL queues).
        sandbox/
            docker.py    DockerSandbox: build image, start container, TCP exec.
            guest_agent.py   Stdlib-only agent running inside the container.
            _protocol.py     4-byte big-endian length-prefixed JSON.
            Dockerfile       Container image definition.
tests/                   Pytest. conftest.py wires fixtures (Redis, sandbox, etc.).
examples/                Runnable examples (use with `away run --tmp`).
docs/                    Public docs.
```

## End-to-end flow

Client side, on `await func.run(*args)`:

1. `Graph.default().serialize(func)` (in [graph/graph.py](../away/graph/graph.py)) walks the function's subgraph and emits JSON via `Store`.
2. `Task(graph_json=..., function_name=..., args=..., kwargs=..., timeout=..., retries=...)` ([core/task.py](../away/core/task.py)).
3. `Backend.submit(task_json)` enqueues. Optionally signed via `sign_json` ([core/signing.py](../away/core/signing.py)).
4. `Result(task_id, backend)` is returned (or awaited directly for `.run`).

Worker side: `serve` ([worker/remote.py](../away/worker/remote.py)) drives the loop and delegates to `Worker.run_with_policy` ([worker/worker.py](../away/worker/worker.py)):

1. `async for task_json in backend.listen()` (in `serve`).
2. Optional `verify_and_load_json` if `--require-signing`.
3. Wait for `scheduled_at`; check `is_cancelled`; check `check_throttle` (in `_run_task`, [remote.py](../away/worker/remote.py)).
4. `_heartbeat_loop` runs concurrently for the duration of execution.
5. `Worker.run_with_policy` looks up subgraph cache (SHA-256 of all reachable content hashes).
6. On miss: `ensure_dependencies` → `Graph.reconstruct(json, name)` → `compile` + `exec` into fresh namespace.
7. `resolve_args` rebuilds class instances against the reconstructed namespace.
8. Sync funcs go through `loop.run_in_executor` with `contextvars.copy_context()` to propagate the `progress` callback. Async funcs are awaited.
9. `asyncio.wait_for(timeout)` per attempt, exponential `retry_delay * 2^attempt` between attempts.
10. `ResultEnvelope` ([worker/result.py](../away/worker/result.py)) sent via `backend.send_result`. If cancelled mid-execution, the result is discarded.
11. Re-enqueue if `recur_interval` set and schedule not cancelled. Record throttle on success.

## Public API contract

The `__all__` in [away/__init__.py](../away/__init__.py) is the public surface. Anything else is internal and subject to change. Notable exports:

- Decorator: `trace`.
- Lifecycle: `connect(url)`, `disconnect()`, `serve(url, concurrency=, sandbox=, ...)`.
- Power-user: `Task`, `Worker`, `Backend`, `serialize`, `reconstruct`, `pack`, `execute`, `get_graph`, `Graph`.
- Result: `Result`, `ResultEnvelope`, `ProgressInfo`, `progress`.
- Scheduling: `ScheduleHandle`.
- Errors: `Error` (base), `WorkerError`, `RemoteError`, `DependencyError`, `TaskStalled`, `TaskCancelled`, `ThrottleError`, `SignatureError`, `PairingError`, `WorkerOnlyError`.
- Auth: `generate_token`, `save_token`, `load_token`, `clear_token`, `resolve_signing_key`, `sign_json`, `verify_and_load_json`, `compute_signature`, `verify_signature`, `derive_key`, plus pairing helpers.
- Sandbox: `DockerSandbox`.

`func.run`, `func.start`, `func.map`, `func.run_in`, `func.run_at`, `func.run_every` are attributes attached by `@trace` ([graph/decorator.py](../away/graph/decorator.py)).

## Conventions and invariants

- **Async by default.** Every `Backend` method is `async def`. Adding a sync helper is a smell — use `loop.run_in_executor` only for unavoidable blocking calls (pip subprocess, sync user code).
- **No required runtime dependencies.** `redis`, `aio_pika`, `docker` are imported lazily inside the modules that need them. Do not move these imports to the top of any always-imported file.
- **Content hash excludes structural data.** `FunctionNode`'s hash includes `source`, `imports`, `closure_*`, `module_vars`, `class_*` but NOT `dependencies`. This is load-bearing for cache reuse — see [core/models.py](../away/core/models.py).
- **`@trace` is stripped from reconstructed source.** Reconstructed code must not import away. Anything that survives reconstruction must be in stdlib or installable via pip.
- **Closure capture is multi-tier.** Order matters: `repr()` → traced refs → lambdas → user funcs → stdlib constructor expressions → pickle → warning. See [graph/analyzer.py](../away/graph/analyzer.py).
- **Auto-discovery is recursive.** Calling an untraced user function from a traced one registers it transitively. Cross-module imports become inline edges.
- **Backend defaults are no-ops.** `Backend` ABC supplies safe defaults for cancellation, progress, throttling, scheduling, notifications. Subclasses override only what they support.
- **Subgraph cache key.** `Worker` keys cache by SHA-256 of sorted reachable content hashes — not by `task_id`, not by `function_name`.
- **Result envelope statuses.** `"ok" | "error" | "cancelled" | "throttled"`. Anything else is a bug.
- **Tests use real backends where reasonable** (e.g. real Redis when available). See `tests/conftest.py`.

## Where things live (cheat-sheet for common edits)

- New decorator option (e.g. `@trace(priority=...)`) → [graph/decorator.py](../away/graph/decorator.py), [core/task.py](../away/core/task.py), `Worker.run_with_policy` in [worker/worker.py](../away/worker/worker.py).
- New backend → subclass `Backend` in [worker/backends/base.py](../away/worker/backends/base.py), wire URL scheme in [worker/remote.py](../away/worker/remote.py).
- New auto-discovery rule → [graph/analyzer.py](../away/graph/analyzer.py); update reconstruction in [graph/store.py](../away/graph/store.py); add fields to `FunctionNode` in [core/models.py](../away/core/models.py) (remember the content-hash inclusion rule).
- New CLI subcommand → [away/__main__.py](../away/__main__.py).
- New error type → [core/errors.py](../away/core/errors.py); export from [away/__init__.py](../away/__init__.py) `__all__`.
- New package-name mapping (`cv2` → `opencv-python`) → `DEFAULT_IMPORT_TO_PACKAGE` in [worker/deps.py](../away/worker/deps.py).

## Run / develop

```bash
# Worker (isolated venv, auto-cleaned on exit)
away worker --backend redis://localhost:6379 --tmp

# Run an example script in a temp venv with auto-detected deps
python -m away run --tmp examples/remote_execution.py

# Tests
pytest

# Strict typing
mypy away
```

Worker logs are concise and structured. The first execution of a new graph shows `build` + any `pip <pkg>` annotations; repeats show `build` (cached venv) or `cached` (subgraph cache hit).


----

# Question

Can you make sure all the example scripts in `examples/` can be run standalone ? For example, the FastAPI example need the user to make a request. I want these examples to represent real situations, but I'd like the user to be able to run them and see the results immediately.

This means: generating image data and report data in the script directly, and making the request for the user.