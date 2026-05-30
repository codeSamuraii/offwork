"""CPU and memory stress task for cloud_poc.

Submits a task that burns CPU for a given duration while repeatedly
allocating and releasing chunks of memory.

Usage:
    BROKER_URL="http://localhost:8000/api/v1/broker?api_key=<key>" \
        python stress_task.py [cpu_seconds] [mem_mib_per_iter] [mem_iters]

Arguments (positional, all optional):
    cpu_seconds       Seconds to keep the CPU busy     (default: 10)
    mem_mib_per_iter  MiB to allocate per iteration    (default: 64)
    mem_iters         Number of allocation iterations  (default: 8)
"""

import asyncio
import os
import sys

import offwork
from offwork import progress

broker_url = os.environ.get("BROKER_URL")
if not broker_url:
    print("error: BROKER_URL is not set", file=sys.stderr)
    sys.exit(1)

offwork.connect(broker_url)


def _burn_cpu(seconds: float) -> int:
    """Busy-loop for *seconds* wall time. Returns iteration count."""
    import time
    import hashlib

    deadline = time.monotonic() + seconds
    data = b"offwork-stress" * 256  # ~3.5 KB
    iters = 0
    while time.monotonic() < deadline:
        hashlib.sha256(data).digest()
        iters += 1
    return iters


def _alloc_mib(mib: float) -> int:
    """Allocate *mib* MiB, touch every page, return checksum byte."""
    size = int(mib * 1024 * 1024)
    chunk = bytearray(size)
    # Touch every 4096-byte page so the OS actually faults the memory in.
    for i in range(0, size, 4096):
        chunk[i] = (i // 4096) & 0xFF
    return chunk[-1]


@offwork.task
def stress(cpu_seconds: float, mem_mib_per_iter: float, mem_iters: int) -> dict:
    """Stress the CPU and memory on the worker.

    Args:
        cpu_seconds:      Wall-clock seconds to spend hashing.
        mem_mib_per_iter: MiB to allocate and touch per memory iteration.
        mem_iters:        How many times to allocate / release that block.

    Returns a summary dict with hash iteration count and total memory touched.
    """
    import time

    start = time.monotonic()
    total_steps = mem_iters  # N CPU phase + N mem phases

    checksums = []
    for i in range(mem_iters):
        # --- CPU phase ---
        progress(i, total_steps, message=f"Iter {i + 1}/{mem_iters}")
        hash_iters = _burn_cpu(cpu_seconds)
        checksums.append(_alloc_mib(mem_mib_per_iter))

    elapsed = time.monotonic() - start
    total_mib = mem_mib_per_iter * mem_iters
    return {
        "elapsed_seconds": round(elapsed, 2),
        "cpu_seconds_requested": cpu_seconds,
        "hash_iterations": hash_iters,
        "memory_iterations": mem_iters,
        "mem_mib_per_iter": mem_mib_per_iter,
        "total_mem_touched_mib": total_mib,
        "checksum": sum(checksums) & 0xFF,
    }


async def main() -> None:
    cpu_seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    mem_mib_per_iter = float(sys.argv[2]) if len(sys.argv) > 2 else 64.0
    mem_iters = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    print(f"submitting stress task: cpu={cpu_seconds}s  mem={mem_mib_per_iter}MiB x {mem_iters} iters")
    future = await stress.start(cpu_seconds, mem_mib_per_iter, mem_iters)
    print(f"task id: {future.task_id}")

    while not await future.done():
        p = await future.progress()
        if p is not None:
            print(f"  [{p.current}/{p.total}] {p.message}")
        await asyncio.sleep(2.0)

    result = await future
    print("\nresult:")
    for k, v in result.items():
        print(f"  {k}: {v}")


asyncio.run(main())
