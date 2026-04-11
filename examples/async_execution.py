"""Async remote execution with pyfuse.

Demonstrates:
- Awaiting a single remote task
- Concurrent batch execution with .amap()
- asyncio.gather with multiple tasks
"""
import asyncio
import math

import pyfuse
from pyfuse import trace

pyfuse.connect("shm://localhost:9847")

async def async_add(a: int, b: int) -> int:
    return a + b

@trace
async def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(await async_add(a**2, b**2))

async def main() -> None:
    # 1. Await a single task
    result = await hypotenuse.arun(3.0, 4.0)
    print(f"hypotenuse(3, 4) = {result}")  # 5.0

    # 2. Await via Result.__await__
    result = await hypotenuse.run(5.0, 12.0)
    print(f"hypotenuse(5, 12) = {result}")  # 13.0

    # 3. Launch and get results later
    future = hypotenuse.run(8.0, 15.0)
    print("hypotenuse(8, 15) = ...", end='\r')
    await asyncio.sleep(3)  # do other work while waiting

    result = await future
    print(f"hypotenuse(8, 15) = {result}")  # 17.0

    # 4. Batch with .amap() -- submits all, awaits all concurrently
    results = await hypotenuse.amap([(3.0, 4.0), (5.0, 12.0), (8.0, 15.0)])
    print(f"amap results = {results}")  # [5.0, 13.0, 17.0]

    # 5. asyncio.gather -- submit multiple different tasks concurrently
    results = await asyncio.gather(
        hypotenuse.run(3.0, 4.0),
        hypotenuse.run(5.0, 12.0),
        hypotenuse.run(8.0, 15.0),
    )
    print(f"gather: {results}")  # [5.0, 13.0, 17.0]


asyncio.run(main())
