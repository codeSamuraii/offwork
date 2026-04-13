"""Async remote execution with pyfuse.

Demonstrates:
- Awaiting a single remote task with .run()
- Fire-and-forget with .start() + await later
- Concurrent batch execution with .map()
- asyncio.gather with multiple tasks
"""
import asyncio
import math

import pyfuse
from pyfuse import trace

pyfuse.connect("local://localhost:9748")

async def add(a: int, b: int) -> int:
    return a + b

@trace
async def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(await add(a**2, b**2))

async def main() -> None:
    # 1. Run and get result directly
    result = await hypotenuse.run(3.0, 4.0)
    print(f"hypotenuse(3, 4) = {result}")  # 5.0

    # 2. Start task, get Result handle, await later
    future = await hypotenuse.start(8.0, 15.0)
    print(f"hypotenuse(8, 15) -> {future}", flush=True)
    await asyncio.sleep(3)  # do other work while waiting

    result = await future
    print(f"                  =  {result}")  # 17.0

    # 3. Batch with .map() -- submits all, awaits all
    results = await hypotenuse.map([(3.0, 4.0), (5.0, 12.0), (8.0, 15.0)])
    print(f"map results = {results}")  # [5.0, 13.0, 17.0]

    # 4. asyncio.gather -- submit multiple tasks, await concurrently
    results = await asyncio.gather(
        hypotenuse.run(3.0, 4.0),
        hypotenuse.run(5.0, 12.0),
        hypotenuse.run(8.0, 15.0),
    )
    print(f"gather: {results}")  # [5.0, 13.0, 17.0]


asyncio.run(main())
