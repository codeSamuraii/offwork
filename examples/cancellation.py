"""Task cancellation with pyfuse.

Demonstrates cancelling a pending or in-progress task.

Usage:
    # Terminal 1 -- start a worker
    pyfuse worker --backend redis://localhost:6379 --tmp

    # Terminal 2 -- run this script
    python -m pyfuse run examples/cancellation.py
"""

import asyncio

import pyfuse
from pyfuse import trace, TaskCancelled

pyfuse.connect("local://localhost:9748")


@trace
async def slow_computation(n: int) -> int:
    """A deliberately slow function."""
    try:
        total = 0
        for _ in range(n):
            await asyncio.sleep(1.0)
            total += 1
        return total
    finally:
        if total < n:
            print("Task cancelled")



async def main() -> None:
    # Start a slow task
    future = await slow_computation.start(10)
    print(f"Task submitted: {future.task_id}")

    # Wait briefly, then cancel it
    await asyncio.sleep(3.0)
    print("Cancelling task...")
    await future.cancel()

    # Awaiting a cancelled task raises TaskCancelled
    try:
        result = await future
    except TaskCancelled:
        print("Task was cancelled successfully!")

    # Verify the status
    status = await future.status()
    print(f"Task status: {status}")  # "cancelled"


asyncio.run(main())
