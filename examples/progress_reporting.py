"""Progress reporting with pyfuse.

Demonstrates how a long-running task can report progress
back to the client in real time.

Usage:
    # Terminal 1 -- start a worker
    pyfuse worker --backend redis://localhost:6379 --tmp

    # Terminal 2 -- run this script
    pyfuse run examples/progress_reporting.py
"""

import asyncio
import time

import pyfuse
from pyfuse import trace, progress

pyfuse.connect("local://localhost:9748")


def process_item(item: str) -> str:
    """Simulate work on a single item."""
    time.sleep(0.3)
    return item.upper()


@trace
def batch_process(items: list[str]) -> list[str]:
    """Process items one by one, reporting progress after each."""
    results = []
    total = len(items)
    for i, item in enumerate(items):
        results.append(process_item(item))
        progress(i + 1, total, message=f"Processed '{item}'")
    return results


async def main() -> None:
    items = ["alpha", "bravo", "charlie", "delta", "echo"]

    # Start the task (returns immediately)
    future = await batch_process.start(items)
    print(f"Task submitted: {future.task_id}")

    # Poll for progress until done
    while not await future.done():
        p = await future.progress()
        if p is not None:
            print(p)
        await asyncio.sleep(0.5)

    # Get the final result
    result = await future
    print(f"\nDone! Results: {result}")


asyncio.run(main())
