"""Redis-backed task queue with a producer pushing tasks and a worker executing them.

Requires a Redis server running on localhost:6379.
Install the redis package: pip install redis

Usage:
    # Terminal 1 – start the worker
    python examples/redis_worker.py worker

    # Terminal 2 – push tasks and read back results
    python examples/redis_worker.py push
"""
import sys
import json
import math
import time
import uuid
import logging

from pyfuse import FuseWorker, serialize, trace

# logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

QUEUE_KEY = "pyfuse:tasks"

# ---------------------------------------------------------------------------
# Example functions to trace
# ---------------------------------------------------------------------------


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    """Compute the hypotenuse of a right triangle."""
    return math.sqrt(add(a**2, b**2))


def say_hello() -> str:
    """Return a greeting prefix."""
    return "Hello! "

@trace
def greet(name: str) -> str:
    """Return a greeting."""
    return say_hello() + name


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------


def make_task(
    function_name: str, args: list, task_id: str | None = None
) -> str:
    """Build a JSON task envelope containing the serialized graph."""
    return json.dumps({
        "id": task_id or uuid.uuid4().hex[:8],
        "graph": serialize(),
        "function": function_name,
        "args": args,
    })


# ---------------------------------------------------------------------------
# Client – pushes tasks then waits for results
# ---------------------------------------------------------------------------


def push_tasks() -> None:
    """Push tasks and block-read results as they arrive."""
    import redis

    r = redis.Redis()

    tasks = [
        ("add",        [3, 4]),
        ("hypotenuse", [3.0, 4.0]),
        ("greet",      ["pyfuse"]),
        ("say_hello",  []),
    ]

    task_ids: list[str] = []
    for func_name, args in tasks:
        tid = uuid.uuid4().hex[:8]
        task_ids.append(tid)
        r.rpush(QUEUE_KEY, make_task(func_name, args, tid))
        print(f"  pushed   {func_name}({', '.join(map(repr, args))})")

    print(f"\n{len(tasks)} tasks queued — waiting for results …\n")

    # Read results back – one per task, keyed by task id
    pending = list(task_ids)
    while pending:
        result_keys = [f"pyfuse:result:{tid}" for tid in pending]
        popped = r.blpop(result_keys, timeout=30)  # type: ignore[misc]
        if popped is None:
            print("\n  Timed out waiting for results.")
            break
        key, raw = popped
        entry = json.loads(raw)
        # Remove the task id we just received from pending
        tid = key.decode().removeprefix("pyfuse:result:")
        pending.remove(tid)
        print(
            f"  result   {entry['function']}({', '.join(map(repr, entry['args']))}) "
            f"= {entry['result']!r}  ({entry['elapsed_ms']}ms)"
        )

    if not pending:
        print("\nAll results received.")


# ---------------------------------------------------------------------------
# Worker – pops tasks, reconstructs + executes, returns results
# ---------------------------------------------------------------------------


def run_worker() -> None:
    """Block-pop tasks from Redis, execute them, push results back."""
    import redis

    r = redis.Redis()
    worker = FuseWorker(auto_install=False)

    print("Worker listening on", QUEUE_KEY, flush=True)
    print("Press Ctrl+C to stop.\n", flush=True)

    try:
        while True:
            _, raw = r.blpop(QUEUE_KEY)  # type: ignore[misc]
            envelope = json.loads(raw)

            task_id = envelope["id"]
            func_name = envelope["function"]
            graph = envelope["graph"]
            args = envelope["args"]

            t0 = time.perf_counter()
            result = worker.execute(graph, func_name, *args)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

            cache = worker.cache_info()
            print(
                f"  executed  {func_name:<12}  "
                f"{elapsed_ms:6.2f}ms  "
                f"cache size: {cache['size']}",
                flush=True,
            )

            # Push result to a per-task key so the client can read it back
            result_key = f"pyfuse:result:{task_id}"
            r.rpush(
                result_key,
                json.dumps({
                    "function": func_name,
                    "args": args,
                    "result": result,
                    "elapsed_ms": elapsed_ms,
                }),
            )
            r.expire(result_key, 60)  # auto-cleanup after 60s

    except KeyboardInterrupt:
        print("\nWorker stopped.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("push", "worker"):
        print("Usage: python redis_worker.py [push|worker]")
        sys.exit(1)

    if sys.argv[1] == "push":
        push_tasks()
    else:
        run_worker()
