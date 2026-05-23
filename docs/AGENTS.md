# offwork — Context for Coding Assistants

This file is a compact, technical orientation for AI coding assistants. For user-facing docs see [README.md](../README.md), [docs/FEATURES.md](FEATURES.md), [docs/TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md), [docs/SIGNING.md](SIGNING.md), [docs/SANDBOX.md](SANDBOX.md).

## What offwork is

A Python package that serializes a function — its source, its dependency graph, its imports, its closure, its arguments — into a self-contained JSON envelope, ships it to a worker process, and executes it there. Workers need no prior knowledge of the user's codebase: they reconstruct source from the payload, `pip install` missing packages on the fly, `compile` + `exec` the result, and return the value.

Add `@offwork.task` to one entry-point function. Call `await func.run(...)`. That is the entire surface area.

## Design goals

- **Zero deployment** — no shared filesystem, no image rebuilds, no code sync. The client ships everything the worker needs in one envelope.
- **Zero setup for users** — one decorator (`@offwork.task`), one connect call. Workers auto-install missing third-party packages.
- **Zero hard dependencies** — offwork itself has no required runtime deps. `redis`, `aio-pika`, `docker` are optional extras loaded lazily.
- **Async-native** — all I/O (`Backend`, `Worker`, `Result`, `_venv`) is `asyncio`. Sync user functions run in `loop.run_in_executor`.
- **Pluggable transport** — `Backend` ABC abstracts task queue + result store + heartbeat + cancellation + progress + scheduling + throttling.
- **Content-addressable caching** — functions are keyed by SHA-256 of their content. Same code → cache hit, regardless of source.
- **Strict typing** — strict mypy, `py.typed` marker shipped.

## Feature surface

| Feature | Entry point | Implementation |
|---|---|---|
| Decorator | `@offwork.task` | [offwork/graph/decorator.py](../offwork/graph/decorator.py) |
| Auto-capture (source, imports, closures, classes, module vars) | `Graph.serialize` | [offwork/graph/analyzer.py](../offwork/graph/analyzer.py), [offwork/graph/graph.py](../offwork/graph/graph.py) |
| Reconstruction → self-contained source | `Graph.reconstruct` | [offwork/graph/store.py](../offwork/graph/store.py) |
| Runtime call-stack tracing | `contextvars` | [offwork/graph/tracing.py](../offwork/graph/tracing.py) |
| Remote submit / await | `func.run`, `func.start`, `func.map` | [offwork/worker/remote.py](../offwork/worker/remote.py), [offwork/graph/decorator.py](../offwork/graph/decorator.py) |
| Worker loop (signing, scheduling, throttle, heartbeat) | `serve` | [offwork/worker/remote.py](../offwork/worker/remote.py) |
| Subgraph caching, reconstruct, retry, timeout | `Worker.run_with_policy` | [offwork/worker/worker.py](../offwork/worker/worker.py) |
| Auto-install of third-party packages | `install_package_as`, `ensure_dependencies` | [offwork/worker/deps.py](../offwork/worker/deps.py) |
| Worker-only imports (skip local install) | `worker_only_import` | [offwork/worker/deps.py](../offwork/worker/deps.py) |
| Result handle, status, progress, cancel | `Result` | [offwork/worker/result.py](../offwork/worker/result.py) |
| Scheduling (`run_in`, `run_at`, `run_every`) | `ScheduleHandle` | [offwork/worker/schedule.py](../offwork/worker/schedule.py) |
| Throttling (`throttle=...`) | `Backend.check_throttle` | backends + [offwork/worker/worker.py](../offwork/worker/worker.py) |
| Progress reporting | `offwork.progress(...)` | [offwork/core/progress.py](../offwork/core/progress.py) |
| Heartbeat & stall detection | `Backend.send_heartbeat` | `_heartbeat_loop` in [offwork/worker/remote.py](../offwork/worker/remote.py), [offwork/worker/result.py](../offwork/worker/result.py) |
| Local TCP backend | `local://` | [offwork/worker/backends/local.py](../offwork/worker/backends/local.py) |
| Redis backend | `redis://` | [offwork/worker/backends/redis.py](../offwork/worker/backends/redis.py) |
| RabbitMQ backend | `amqp://` | [offwork/worker/backends/rabbitmq.py](../offwork/worker/backends/rabbitmq.py) |
| Docker sandbox isolation | `--sandbox`, `DockerSandbox` | [offwork/worker/sandbox/](../offwork/worker/sandbox/) |
| HMAC-SHA256 task signing | `--require-signing`, token, pairing | [offwork/core/signing.py](../offwork/core/signing.py), [offwork/core/token.py](../offwork/core/token.py), [offwork/core/pairing.py](../offwork/core/pairing.py) |
| Temp venv (for `--tmp` and `offwork run`) | `temp_venv` | [offwork/_venv.py](../offwork/_venv.py) |
| CLI | `python -m offwork ...` | [offwork/__main__.py](../offwork/__main__.py) |

## Repository layout

```
offwork/
    __init__.py          Public API surface (re-exports). __all__ is the contract.
    __main__.py          CLI: worker, run, pair, token, sandbox, info, serialize, reconstruct.
    _venv.py             Async temp venv (used by --tmp and `offwork run`).
    typing.py            Public type aliases.
    core/
        models.py        FunctionNode, ImportInfo dataclasses + content hashing.
        task.py          Task envelope (graph_json + name + args + options).
        errors.py        Error hierarchy. All exceptions inherit Error.
        progress.py      ProgressInfo + progress() contextvar callback.
        version.py       _VERSION (resolved from package metadata).
        signing.py       HMAC-SHA256 sign/verify, derive_key.
        token.py         Token generate/save/load (~/.offwork/token).
        pairing.py       6-digit-PIN ECDH-style key exchange.
    graph/
        decorator.py     @offwork.task. Wraps function with .run/.start/.map and traced markers.
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
examples/                Runnable examples (use with `offwork run --tmp`).
docs/                    Public docs.
```

## End-to-end flow

Client side, on `await func.run(*args)`:

1. `Graph.default().serialize(func)` (in [graph/graph.py](../offwork/graph/graph.py)) walks the function's subgraph and emits JSON via `Store`.
2. `Task(graph_json=..., function_name=..., args=..., kwargs=..., timeout=..., retries=...)` ([core/task.py](../offwork/core/task.py)).
3. `Backend.submit(task_json)` enqueues. Optionally signed via `sign_json` ([core/signing.py](../offwork/core/signing.py)).
4. `Result(task_id, backend)` is returned (or awaited directly for `.run`).

Worker side: `serve` ([worker/remote.py](../offwork/worker/remote.py)) drives the loop and delegates to `Worker.run_with_policy` ([worker/worker.py](../offwork/worker/worker.py)):

1. `async for task_json in backend.listen()` (in `serve`).
2. Optional `verify_and_load_json` if `--require-signing`.
3. Wait for `scheduled_at`; check `is_cancelled`; check `check_throttle` (in `_run_task`, [remote.py](../offwork/worker/remote.py)).
4. `_heartbeat_loop` runs concurrently for the duration of execution.
5. `Worker.run_with_policy` looks up subgraph cache (SHA-256 of all reachable content hashes).
6. On miss: `ensure_dependencies` → `Graph.reconstruct(json, name)` → `compile` + `exec` into fresh namespace.
7. `resolve_args` rebuilds class instances against the reconstructed namespace.
8. Sync funcs go through `loop.run_in_executor` with `contextvars.copy_context()` to propagate the `progress` callback. Async funcs are awaited.
9. `asyncio.wait_for(timeout)` per attempt, exponential `retry_delay * 2^attempt` between attempts.
10. `ResultEnvelope` ([worker/result.py](../offwork/worker/result.py)) sent via `backend.send_result`. If cancelled mid-execution, the result is discarded.
11. Re-enqueue if `recur_interval` set and schedule not cancelled. Record throttle on success.

## Public API contract

The `__all__` in [offwork/__init__.py](../offwork/__init__.py) is the public surface. Anything else is internal and subject to change. Notable exports:

- Decorator: `task`.
- Lifecycle: `connect(url)`, `disconnect()`, `serve(url, concurrency=, sandbox=, ...)`.
- Power-user: `Task`, `Worker`, `Backend`, `serialize`, `reconstruct`, `pack`, `execute`, `get_graph`, `Graph`.
- Result: `Result`, `ResultEnvelope`, `ProgressInfo`, `progress`.
- Scheduling: `ScheduleHandle`.
- Errors: `Error` (base), `WorkerError`, `RemoteError`, `DependencyError`, `TaskStalled`, `TaskCancelled`, `ThrottleError`, `SignatureError`, `PairingError`, `WorkerOnlyError`.
- Auth: `generate_token`, `save_token`, `load_token`, `clear_token`, `resolve_signing_key`, `sign_json`, `verify_and_load_json`, `compute_signature`, `verify_signature`, `derive_key`, plus pairing helpers.
- Sandbox: `DockerSandbox`.

`func.run`, `func.start`, `func.map`, `func.run_in`, `func.run_at`, `func.run_every` are attributes attached by `@offwork.task` ([graph/decorator.py](../offwork/graph/decorator.py)).

## Conventions and invariants

- **Async by default.** Every `Backend` method is `async def`. Adding a sync helper is a smell — use `loop.run_in_executor` only for unavoidable blocking calls (pip subprocess, sync user code).
- **No required runtime dependencies.** `redis`, `aio_pika`, `docker` are imported lazily inside the modules that need them. Do not move these imports to the top of any always-imported file.
- **Content hash excludes structural data.** `FunctionNode`'s hash includes `source`, `imports`, `closure_*`, `module_vars`, `class_*` but NOT `dependencies`. This is load-bearing for cache reuse — see [core/models.py](../offwork/core/models.py).
- **`@offwork.task` is stripped from reconstructed source.** Reconstructed code must not import offwork. Anything that survives reconstruction must be in stdlib or installable via pip.
- **Closure capture is multi-tier.** Order matters: `repr()` → traced refs → lambdas → user funcs → stdlib constructor expressions → pickle → warning. See [graph/analyzer.py](../offwork/graph/analyzer.py).
- **Auto-discovery is recursive.** Calling an untraced user function from a traced one registers it transitively. Cross-module imports become inline edges.
- **Backend defaults are no-ops.** `Backend` ABC supplies safe defaults for cancellation, progress, throttling, scheduling, notifications. Subclasses override only what they support.
- **Subgraph cache key.** `Worker` keys cache by SHA-256 of sorted reachable content hashes — not by `task_id`, not by `function_name`.
- **Result envelope statuses.** `"ok" | "error" | "cancelled" | "throttled"`. Anything else is a bug.
- **Tests use real backends where reasonable** (e.g. real Redis when available). See `tests/conftest.py`.

## Where things live (cheat-sheet for common edits)

- New decorator option (e.g. `@offwork.task(priority=...)`) → [graph/decorator.py](../offwork/graph/decorator.py), [core/task.py](../offwork/core/task.py), `Worker.run_with_policy` in [worker/worker.py](../offwork/worker/worker.py).
- New backend → subclass `Backend` in [worker/backends/base.py](../offwork/worker/backends/base.py), wire URL scheme in [worker/remote.py](../offwork/worker/remote.py).
- New auto-discovery rule → [graph/analyzer.py](../offwork/graph/analyzer.py); update reconstruction in [graph/store.py](../offwork/graph/store.py); add fields to `FunctionNode` in [core/models.py](../offwork/core/models.py) (remember the content-hash inclusion rule).
- New CLI subcommand → [offwork/__main__.py](../offwork/__main__.py).
- New error type → [core/errors.py](../offwork/core/errors.py); export from [offwork/__init__.py](../offwork/__init__.py) `__all__`.
- New package-name mapping (`cv2` → `opencv-python`) → `DEFAULT_IMPORT_TO_PACKAGE` in [worker/deps.py](../offwork/worker/deps.py).

## Run / develop

```bash
# Worker (isolated venv, auto-cleaned on exit)
offwork worker --backend redis://localhost:6379 --tmp

# Run an example script in a temp venv with auto-detected deps
python -m offwork run --tmp examples/remote_execution.py

# Tests
pytest

# Strict typing
mypy offwork
```

Worker logs are concise and structured. The first execution of a new graph shows `build` + any `pip <pkg>` annotations; repeats show `build` (cached venv) or `cached` (subgraph cache hit).