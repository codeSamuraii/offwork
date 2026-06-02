"""Progress reporting with offwork.

Demonstrates how a long-running task can report progress
back to the client in real time.

Usage:
    # Terminal 1 -- start a worker
    offwork worker --backend local://localhost:9748 --tmp

    # Terminal 2 -- run this script
    offwork run examples/progress_reporting.py
"""

import asyncio
import time

import offwork
from offwork import progress

offwork.connect("local://localhost:9748")


def process_item(item: str) -> str:
    """Simulate work on a single item."""
    time.sleep(0.3)
    return item.upper()


@offwork.task
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
    future = await batch_process.submit(items)
    print(f"Task submitted: {future.task_id}")

    # Stream progress updates until the task finishes
    async for p in future.progress():
        print(f"Progress: {p.percent}% ({p.current}/{p.total}) - {p.message}")

    # Get the final result
    result = await future
    print(f"\nDone! Results: {result}")


asyncio.run(main())
