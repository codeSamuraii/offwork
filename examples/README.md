# Examples

Each example is a self-contained script. Run any of them with:

```bash
offwork worker --backend local://localhost:9748 --tmp  # Terminal 1 — start a worker
offwork run examples/<script>.py                       # Terminal 2 — run the example
```

`offwork run` creates a temporary venv, auto-detects and installs dependencies, and runs the script. No global installs required.

---

## [remote_execution.py](remote_execution.py)

The baseline: one decorator on an entry-point function, plain helpers around it. Demonstrates that offwork captures the full call graph without any extra annotations.

```python
def add(a, b): ...          # plain helper — no @offwork.task
def multiply(a, b): ...     # another helper

@offwork.task
def dot_product(u, v):
    return sum(multiply(a, b) for a, b in zip(u, v))  # both helpers are captured
```

## [async_execution.py](async_execution.py)

All four async execution patterns side by side:

```python
result  = await func.run(3, 4)                   # submit + await
future  = await func.submit(3, 4)                # submit → handle
result  = await future                           # await later
results = await func.map([(3, 4), (5, 12)])      # batch
r1, r2  = await asyncio.gather(func.run(3, 4), func.run(5, 12))
```

## [package_installation.py](package_installation.py)

Workers auto-install missing packages before execution. Also shows `worker_only_import` — packages that exist only on the worker, resolved to lightweight stubs on the client:

```python
from offwork import worker_only_import, install_package_as

with worker_only_import():          # client never installs this
    import requests

with install_package_as("PyYAML"): # pip name differs from import name
    import yaml
```

## [progress_reporting.py](progress_reporting.py)

Real-time progress from a long-running task. `offwork.progress()` is a no-op when called outside a worker:

```python
@offwork.task
def process(n: int) -> int:
    for i in range(n):
        offwork.progress(i + 1, n, message=f"step {i+1}/{n}")
        ...

future = await process.submit(100)
async for p in future.progress():    # streams each update until done
    print(f"{p.percent:.0f}%")
```

## [streaming.py](streaming.py)

An `async def ... yield` task streams each value back as it is produced. Consume it with `async for` via `.stream(...)`:

```python
@offwork.task
async def tail_lines(count: int):
    for i in range(count):
        yield f"line {i + 1}"

async for line in tail_lines.stream(5):   # values arrive in order
    print(line)
```

## [cancellation.py](cancellation.py)

Cooperative task cancellation. The client cancels a pending or in-flight task; awaiting it raises `TaskCancelled`:

```python
future = await slow_task.submit()
await asyncio.sleep(1)
await future.cancel()           # wait up to 30 s for worker confirmation

try:
    await future
except offwork.TaskCancelled:
    print("cancelled")
```

## [scheduling.py](scheduling.py)

Three scheduling modes — delayed, point-in-time, and recurring:

```python
await func.run(*args, run_in=timedelta(seconds=5))             # after a delay
await func.run(*args, run_at=datetime(2026, 6, 1, 9, 0))       # at a specific time

schedule = await func.submit(*args, run_every=timedelta(minutes=10))
await asyncio.sleep(35)
await schedule.cancel()    # stop after ~3 executions
```

## [throttling_and_retry.py](throttling_and_retry.py)

Rate-limiting and fault tolerance in the decorator:

```python
@offwork.task(throttle=timedelta(minutes=30))   # at most once per 30 min
def rate_limited(): ...

@offwork.task(retries=3, timeout=10)            # up to 3 retries, 10s each
def flaky(): ...
```

Calling a throttled task during the cooldown raises `ThrottleError` immediately.

## [large_module.py](large_module.py)

Stress test: a module with 47 functions across 7 files, 3 classes, and deep dependency chains. Only the entry point is decorated — offwork discovers everything else automatically. Useful for verifying auto-discovery at scale.

```bash
offwork worker --backend redis://localhost:6379  # requires Redis
python examples/large_module.py
```

## [csv_etl.py](csv_etl.py)

Fan-out ETL pattern: split a large CSV into chunks, process each chunk on a worker, merge results. Each task is pure (bytes in, dict out). Demonstrates `.map()` for batch submission:

```python
chunks = [(0, 500), (500, 1000), (1000, 1500)]
results = await summarize_chunk.map([(data, start, end) for start, end in chunks])
merged = merge(results)
```

## [pdf_report.py](pdf_report.py)

FastAPI endpoint that offloads PDF rendering to a worker. The web process stays lightweight; heavy CPU work (`reportlab`) runs on the worker pool. Shows the typical "offload CPU work from a web handler" pattern:

```python
@offwork.task
def render_report(data: dict) -> bytes:   # returns PDF bytes
    ...

# In the FastAPI handler:
pdf_bytes = await render_report.run(report_data)
return Response(pdf_bytes, media_type="application/pdf")
```

## [email_attachments.py](email_attachments.py)

Stateful poller (IMAP connection, seen-UID tracking) stays local; per-attachment analysis is offloaded. Illustrates the pattern of keeping stateful I/O local while farming out pure compute:

```python
@offwork.task
def process_attachment(filename: str, data: bytes) -> dict:
    classification = _classify(filename)
    text = _extract_text(data)
    return _score_risk(classification, text)
```

## [scheduled_backup.py](scheduled_backup.py)

Recurring backup job via `submit(run_every=...)`. The task is small; it delegates to three plain helpers (`_archive`, `_compress`, `_upload`) that offwork discovers automatically:

```python
schedule = await snapshot_directory.submit(source_path, run_every=timedelta(hours=6))
# runs every 6 hours; call await schedule.cancel() to stop
```

## [persistent_storage.py](persistent_storage.py)

Per-user persistent volume on the hosted cloud broker. Opt in with
`@offwork.task(storage=True)` and read/write via `offwork.storage_path()`.
State survives across task runs and worker scale-to-zero cycles.

Requires `wss://` (or `ws://`) — `local://`, Redis, and RabbitMQ raise
`StorageNotSupportedError` if `storage=True`.

```bash
export OFFWORK_BACKEND=wss://offwork.live/api/v1/broker/ws
export OFFWORK_API_KEY=<your key>
python examples/persistent_storage.py
```

```python
@offwork.task(storage=True)
def touch_counter(label: str) -> dict:
    path = offwork.storage_path("demo_counter.json")
    ...
```
