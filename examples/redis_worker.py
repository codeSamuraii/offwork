"""Redis-backed task queue with a producer pushing tasks and a worker executing them.

Requires a Redis server running on localhost:6379.
Install the redis package: pip install redis

Usage:
    # Terminal 1 – start the worker
    python examples/redis_worker.py worker

    # Terminal 2 – push tasks
    python examples/redis_worker.py push
"""

import json
import sys
import time
import csv as csv_mod

from pyfuse import FuseWorker, serialize, trace

QUEUE_KEY = "pyfuse:tasks"
RESULTS_KEY = "pyfuse:results"

# ---------------------------------------------------------------------------
# Traced functions – these are the "user code" that gets serialized
# ---------------------------------------------------------------------------


@trace
def parse_csv(raw: str) -> list[dict[str, str]]:
    """Parse a CSV string into a list of row dicts."""
    reader = csv_mod.DictReader(raw.splitlines())
    return list(reader)


@trace
def summarize(raw: str) -> dict:
    """Parse CSV and return a summary with row count and column names."""
    rows = parse_csv(raw)
    if not rows:
        return {"row_count": 0, "columns": []}
    return {"row_count": len(rows), "columns": list(rows[0].keys())}


# ---------------------------------------------------------------------------
# Task helpers
# ---------------------------------------------------------------------------

SAMPLE_CSV = "name,age,city\nAlice,30,Paris\nBob,25,London\nCharlie,35,Tokyo"


def make_task(function_name: str, args: list) -> str:
    """Build a JSON task envelope containing the serialized graph."""
    graph = serialize()
    return json.dumps({
        "graph": graph,
        "function": function_name,
        "args": args,
    })


# ---------------------------------------------------------------------------
# Producer – pushes tasks onto the Redis queue
# ---------------------------------------------------------------------------


def push_tasks() -> None:
    """Push a few tasks to the Redis queue."""
    import redis

    r = redis.Redis()

    tasks = [
        ("parse_csv", [SAMPLE_CSV]),
        ("summarize", [SAMPLE_CSV]),
        ("parse_csv", ["col1,col2\n1,2\n3,4"]),
    ]

    for func_name, args in tasks:
        task = make_task(func_name, args)
        r.rpush(QUEUE_KEY, task)
        print(f"  pushed  {func_name}({args[0][:30]}...)")

    print(f"\n{len(tasks)} tasks queued.")


# ---------------------------------------------------------------------------
# Worker – pops tasks, reconstructs + executes, stores results
# ---------------------------------------------------------------------------


def run_worker() -> None:
    """Block-pop tasks from Redis, execute them, and store results."""
    import redis

    r = redis.Redis()
    worker = FuseWorker(auto_install=False)

    print("Worker listening on", QUEUE_KEY)
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            # BLPOP blocks until a task is available (timeout 0 = forever)
            _, raw = r.blpop(QUEUE_KEY)  # type: ignore[misc]
            envelope = json.loads(raw)

            func_name = envelope["function"]
            graph = envelope["graph"]
            args = envelope["args"]

            t0 = time.perf_counter()
            result = worker.execute(graph, func_name, *args)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            # Store the result
            entry = json.dumps({
                "function": func_name,
                "result": result,
                "elapsed_ms": round(elapsed_ms, 2),
            })
            r.rpush(RESULTS_KEY, entry)

            cache = worker.cache_info()
            print(
                f"  executed  {func_name:<12}  "
                f"{elapsed_ms:6.2f}ms  "
                f"cache size: {cache['size']}"
            )

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
