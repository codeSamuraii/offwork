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


def add(a: int, b: int) -> int:
    return a + b


@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))


@trace
async def async_add(a: int, b: int) -> int:
    return a + b


async def main() -> None:
    # 1. Await a single task
    result = await hypotenuse.arun(3.0, 4.0)
    print(f"hypotenuse(3, 4) = {result}")  # 5.0

    # 2. Await via Result.__await__
    result = await hypotenuse.run(5.0, 12.0)
    print(f"hypotenuse(5, 12) = {result}")  # 13.0

    # 3. Batch with .amap() -- submits all, awaits all concurrently
    results = await hypotenuse.amap([(3.0, 4.0), (5.0, 12.0), (8.0, 15.0)])
    print(f"amap results = {results}")  # [5.0, 13.0, 17.0]

    # 4. asyncio.gather -- submit multiple different tasks concurrently
    r1, r2 = await asyncio.gather(
        hypotenuse.run(6.0, 8.0),
        async_add.run(10, 20),
    )
    print(f"gather: hypotenuse={r1}, async_add={r2}")  # 10.0, 30


asyncio.run(main())
