# Async generators (streaming)

A streaming task is a remote `async def ... yield` function. The client
iterates it with `async for`, receiving each value in order as the worker
produces it, until the generator completes or raises:

```python
@offwork.task
async def tail_lines(path: str):
    async for line in follow(path):
        yield line.rstrip()

async for line in tail_lines.stream("/var/log/app.log"):
    print(line)
```

This document describes how that works end-to-end: the client handle, the
worker execution loop, each transport backend, and the hosted cloud broker.
The design rule throughout is **no polling** — every wait is parked on an
event-driven doorbell, so thousands of concurrent streams each cost one idle
waiter rather than a timer.

---

## 1. Client side (`offwork`)

### Submission and the `Stream` handle

`traced_func.stream(*args)` returns a lazy `_StreamSubmission`. It supports
two shapes:

```python
# Iterate directly — the task is submitted on the first `async for`.
async for v in gen.stream(x):
    ...

# Or await it to get the Stream handle (for cancel / progress), then iterate.
stream = await gen.stream(x)
async for v in stream:
    ...
```

`Stream` subclasses `Result`, so `cancel()`, `progress()`, and state queries
all apply. A streaming task has **no return value**, so the handle is not
awaitable for a result — you consume it with `async for`.

### The consume loop — `Stream._stream`

[offwork/worker/result.py](../offwork/worker/result.py) drives iteration.
It tracks `next_seq` (the next contiguous sequence number it expects) and
loops:

1. **Long-poll for the next batch.** Call
   `backend.get_yields(task_id, after_seq=next_seq - 1, timeout=_STREAM_POLL_SECONDS)`.
   Backends that support blocking park on a doorbell for up to
   `_STREAM_POLL_SECONDS` (25.0 s) and return early the instant a value or
   the terminal envelope lands. This is the only wait in the loop — there is
   no `sleep`.
2. **Yield values in order.** For each `(seq, value_json)` returned, verify
   contiguity (a gap raises `RuntimeError`), decode with
   `_resolve(json.loads(value_json), {})`, and `yield` it. While values keep
   arriving the loop stays on this cheap yield-only path and never touches
   the result store.
3. **Check for completion only on an empty poll.** When `get_yields` comes
   back empty (timeout or a terminal wake), read the terminal envelope via
   `try_get_result`.
4. **Tail-drain before stopping.** The terminal envelope can race *ahead* of
   the final yields — especially for error streams, whose envelope carries
   no `stream_yields` count. So once an envelope is present, do one more
   `get_yields` to drain any buffered tail. Only when the tail is empty and
   `next_seq >= envelope.stream_yields` (or the count is `None`) does the
   loop call `_drain_terminal()` and return.

### Terminal envelope

[ResultEnvelope.stream_complete](../offwork/worker/result.py) is the success
envelope for a stream: `status="ok"`, no `result`, and
`stream_yields=<count>` so the client knows exactly how many values to expect
before stopping. If the generator raises, the worker sends a normal failure
envelope; `_drain_terminal` calls `_unwrap()`, which re-raises the original
exception as a `RemoteError` out of the `async for`.

### Constants

| Constant | Value | Where | Meaning |
|----------|-------|-------|---------|
| `_STREAM_POLL_SECONDS` | `25.0` | [result.py](../offwork/worker/result.py) | upper bound the client asks each backend to block per `get_yields` |
| `_DEFAULT_LONG_POLL_SECONDS` | `30.0` | [ws.py](../offwork/worker/backends/ws.py) | WS long-poll cap; request timeout is `wait_seconds + 10.0` |

---

## 2. Worker side

### Dispatch — `remote.py`

When the worker accepts a task it first calls
`worker.is_streaming(task)` ([worker.py](../offwork/worker/worker.py)), which
inspects the rebuilt function:

- `inspect.isasyncgenfunction` → streaming path.
- `inspect.isgeneratorfunction` → **error**: sync generators are unsupported
  (planned for v2).

For a streaming task, [remote.py](../offwork/worker/remote.py) wires an
`on_yield` callback that encodes each value and pushes it to the backend:

```python
async def _on_yield(seq, value):
    value_json = json.dumps(value, cls=_TaskEncoder)
    await backend.send_yield(task.task_id, seq, value_json)

exec_task = asyncio.create_task(worker.run_stream(task, _on_yield))
```

The execution runs as its own `asyncio.Task` so the heartbeat loop can cancel
it on demand (cancellation surfaces as a `cancelled` envelope).

### Driving the generator — `Worker.run_stream`

[worker.py](../offwork/worker/worker.py) instantiates the async generator and
iterates it, invoking `on_yield(seq, value)` for each value with a zero-based
monotonic `seq`, and always `aclose()`-ing it in a `finally`:

```python
agen = cached.func(*args, **kwargs)
seq = 0
try:
    async for value in agen:
        await on_yield(seq, value)
        seq += 1
finally:
    await agen.aclose()
return seq  # total count → stream_yields
```

The returned count becomes `ResultEnvelope.stream_complete(task_id, count)`.
Retries are not applied to streaming tasks (a half-consumed generator can't
be safely restarted), and sandboxed execution routes through
`sandbox.execute_stream` / the guest agent, which detects
`isasyncgenfunction` and reports `stream_yields` the same way.

---

## 3. Backends

All backends implement the same two methods from the `Backend` ABC
([base.py](../offwork/worker/backends/base.py)):

- `send_yield(task_id, seq, value_json)` — append one value (producer side).
- `get_yields(task_id, after_seq=-1, timeout=None) -> list[(seq, value_json)]`
  — return the contiguous run with `seq > after_seq`, optionally blocking up
  to `timeout` for the next one.

Yields are **ordered** and **not persisted for late joiners**: a consumer that
starts after values were produced only sees values from that point on. Each
backend pairs a durable per-task value store with a separate, capped
**doorbell** that producers ring so blocked consumers wake immediately.

### `local` — in-process dev backend

[local.py](../offwork/worker/backends/local.py) holds yields in memory. It has
no cross-process doorbell, so `get_yields` sleeps `min(timeout, 0.05)` on an
empty read — a 20 Hz tick that is acceptable only for single-process local
development. (This is the one place a short sleep exists, by design.)

### `redis`

[redis.py](../offwork/worker/backends/redis.py)

- **Store:** a Redis list `offwork:yields:<task_id>`; the list index equals
  `seq`. `send_yield` does `RPUSH` + `EXPIRE` in a pipeline.
- **Doorbell:** a capped list `offwork:yields:<task_id>:bell`. The same
  pipeline does `RPUSH bell 1` + `LTRIM bell -1 -1` + `EXPIRE`, so at most one
  signal is ever queued.
- **Wait:** `get_yields` does `LRANGE start -1`; if empty and `timeout` is set,
  it `BLPOP`s the bell (`timeout=max(1, int(timeout))`) and re-reads. Naturally
  race-free — the bell item persists until a consumer pops it, so a signal that
  arrives between the `LRANGE` and the `BLPOP` is not lost.

`send_result` rings the same bell, so a consumer blocked on `BLPOP` wakes on
completion and re-checks the terminal envelope.

### `rabbitmq`

[rabbitmq.py](../offwork/worker/backends/rabbitmq.py)

- **Store:** each yield is its own peek-able KV slot keyed by sequence,
  `<yield_prefix><task_id>.<seq>`. `send_yield` writes the slot via `_kv_put`.
  `_read_yield_run` peeks consecutive slots from `after_seq + 1` until it hits
  an empty slot, returning the contiguous run.
- **Doorbell:** a per-task queue `<yieldbell_prefix><task_id>` declared with
  `x-max-length: 1` (one signal max), `x-message-ttl`, and `x-expires` (so
  abandoned streams' queues self-delete). `_ring_yield_bell` publishes an empty
  message to it.
- **Wait:** `get_yields` reads the run; if empty and `timeout` is set, it opens
  a fresh channel, `consume`s the bell queue, and
  `asyncio.wait_for(future, timeout)` on the first delivery, then re-reads the
  run.

Both `send_yield` and `send_result` ring the bell, so completion also wakes a
parked consumer.

### `ws` — hosted client transport

[ws.py](../offwork/worker/backends/ws.py) is the client-side backend that talks
to the cloud broker over one persistent multiplexed WebSocket. It does not
store yields itself — it forwards the ops:

- `send_yield` → a `send_yield` request.
- `get_yields` → a `get_yields` request carrying
  `{task_id, after_seq, wait_seconds}`, where
  `wait_seconds = min(timeout, _DEFAULT_LONG_POLL_SECONDS)`. The request
  timeout is `wait_seconds + 10.0` (a margin over the server's long-poll), and
  `None` when not waiting. The broker holds the request open and the comment
  is explicit: *one in-flight request per stream regardless of rate — no busy
  polling.*

---

## 4. Cloud app (`cloud_poc`)

The hosted broker is FastAPI + MongoDB + RabbitMQ. The `ws` backend's
`send_yield` / `get_yields` ops are dispatched in
[routes/broker_ws.py](../../cloud_poc/backend/app/routes/broker_ws.py) to
[broker_service.py](../../cloud_poc/backend/app/broker_service.py), and the
event-driven wakeups go through
[notify.py](../../cloud_poc/backend/app/notify.py).

### Storing yields — `put_yield`

Each yield is `$push`ed onto the task document's `yields` array as
`[seq, value_json]` (array index = `seq`), with `updated_at` bumped. It then
calls `notify.signal_yield(task_id)`. Crucially, `put_yield` touches **only**
`yields` + `updated_at` — it never writes the append-only usage ledger
(`usage_ledger` / `metered_*`), preserving billing integrity.

### Long-poll read — `get_yields`

This is the server-side counterpart of the client's long-poll. It is race-free
by construction:

```python
async with notify.wait_yield(task_id) as wakeup:   # register BEFORE reading
    while True:
        out, done = await _fetch()                 # read Mongo
        if out or done:
            return {"yields": out}                  # new values OR terminal
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"yields": []}
        await asyncio.wait_for(wakeup.wait(), timeout=remaining)
        wakeup.clear()
```

- `_fetch` projects `{"yields": {"$slice": [after_seq + 1, ...]}, "result_json": 1}`,
  so already-consumed values are never re-transferred, and returns a `done`
  flag set when `result_json` is present.
- `done` is what stops the consumer from waiting out the full `wait_seconds`
  after a stream ends: the moment the terminal envelope exists, the handler
  returns empty and the client reads the result.
- The `_Waiter` context manager registers the `asyncio.Event` **before** the
  first Mongo read, so a `signal_yield` racing with the read still sets the
  event and the subsequent `wait()` returns immediately — no missed wakeup.

### The doorbell — `NotifyHub`

[notify.py](../../cloud_poc/backend/app/notify.py) runs three RabbitMQ topic
exchanges; streaming adds the third:

| Exchange | Routing key | Wakes |
|----------|-------------|-------|
| `offwork.tasks` | `<user_id>` | `/claim` waiters |
| `offwork.results` | `<task_id>` | `/result` waiters |
| `offwork.yields` | `<task_id>` | `get_yields` long-poll waiters |

Each API pod owns one exclusive auto-delete queue per exchange bound to `#`,
so every replica sees every signal. `signal_yield` first wakes local waiters
(`_wake`, covering the single-replica case without an AMQP round-trip) then
publishes an empty message keyed by `task_id`. `put_result` signals **both**
`offwork.results` and `offwork.yields`, so a streaming consumer wakes on
completion and re-reads to find the terminal envelope.

If RabbitMQ is unreachable (broker down, or `aio_pika` missing), `NotifyHub`
runs in **in-process mode**: local-only wakeups, no cross-replica signals, and
a worst-case extra wakeup latency bounded by the long-poll deadline.
Correctness is preserved by that deadline; only cross-replica promptness
degrades. With the API at `replicas: 1` this is moot today, and RabbitMQ keeps
it correct when scaled out.

---

## 5. Invariants

- **No polling.** Every wait is a doorbell; the only sleep is `local`'s 20 Hz
  dev tick.
- **Ordering & contiguity.** `seq` is zero-based and monotonic; the client
  raises on a gap.
- **No late-joiner replay.** Yields are not persisted for consumers that start
  late.
- **Terminal also rings the yield doorbell.** `send_result` / `put_result`
  wake streaming consumers so they stop promptly instead of waiting out the
  long-poll.
- **No return value; retries off.** Streaming tasks return nothing and are not
  retried.
- **Sync generators unsupported.** They raise an explicit error (planned v2).
- **Ledger untouched.** `put_yield` never writes the append-only usage ledger.
- **Encoding.** Values are `json.dumps(value, cls=_TaskEncoder)` and decoded
  with `_resolve(json.loads(s), {})`, matching the result encoder.
