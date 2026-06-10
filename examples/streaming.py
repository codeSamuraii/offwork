"""Streaming results with an async generator task.

An ``async def ... yield`` task streams each value back to the client as
it is produced. Consume it with ``async for`` via ``.stream(...)`` —
values arrive in order, and an exception raised mid-stream surfaces from
the loop.

Usage:
    # Terminal 1 -- start a worker
    offwork worker --backend local://localhost:9748 --tmp

    # Terminal 2 -- run this script
    offwork run examples/streaming.py
"""

import asyncio

import offwork

offwork.connect("local://localhost:9748")


@offwork.task
async def tail_lines(count: int):
    """Yield log-like lines one at a time, simulating a live feed."""
    for i in range(count):
        await asyncio.sleep(0.2)
        yield f"line {i + 1}: event-{i:03d}"


async def main() -> None:
    # Iterate directly — the task is submitted on the first `async for`.
    async for line in tail_lines.stream(5):
        print(f"received: {line}")

    # Or await the handle first to keep a reference (e.g. to cancel).
    stream = await tail_lines.stream(3)
    print(f"\nStreaming task: {stream.task_id}")
    async for line in stream:
        print(f"received: {line}")


asyncio.run(main())
