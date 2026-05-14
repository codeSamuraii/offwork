"""Task cancellation with away.

Demonstrates cancelling a pending or in-progress task.

Usage:
    # Terminal 1 -- start a worker
    away worker --backend redis://localhost:6379 --tmp

    # Terminal 2 -- run this script
    away run examples/cancellation.py
"""

import asyncio

import away
from away import trace, TaskCancelled

away.connect("local://localhost:9748")


@trace
async def slow_computation(n: int) -> int:
    """A deliberately slow function."""
    total = 0
    try:
        for _ in range(n):
            await asyncio.sleep(1.0)
            total += 1
        return total
    finally:
        if total < n:
            print(f"Task interrupted after {total}/{n} iterations")


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
        print(f"Unexpected result: {result}")
    except TaskCancelled:
        print("Task was cancelled successfully!")

    # Verify the status
    status = await future.status()
    print(f"Task status: {status}")  # "cancelled"


if __name__ == "__main__":
    asyncio.run(main())
