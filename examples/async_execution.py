"""Async remote execution with offwork.

Demonstrates:
- Awaiting a single remote task with .run()
- Fire-and-forget with .submit() + await later
- Concurrent batch execution with .map()
- asyncio.gather with multiple tasks
"""
import asyncio
import math

import offwork

offwork.connect("local://localhost:9748")

async def inverse(x: float) -> float:
    return 1 / x

@offwork.task
async def inverse_root(n: float) -> float:
    return await inverse(math.sqrt(n))

async def main() -> None:
    # 1. Run and get result directly
    result = await inverse_root.run(4)
    print(f"inverse_root(4) = {result}")  # 0.5

    # 2. Submit task, get Result handle, await later
    future = await inverse_root.submit(100)
    print(f"inverse_root(100) -> {future}", flush=True)
    await asyncio.sleep(3)  # do other work while waiting

    result = await future
    print(f"                  =  {result}")  # 0.1

    # 3. Batch with .map() -- submits all, awaits all
    results = await inverse_root.map([(4,), (16,), (100,)])
    print(f"map results = {results}")  # [0.5, 0.25, 0.1]

    # 4. asyncio.gather -- submit multiple tasks, await concurrently
    gathered = await asyncio.gather(
        inverse_root.run(4),
        inverse_root.run(16),
        inverse_root.run(100),
    )
    print(f"gather: {gathered}")  # [0.5, 0.25, 0.1]


asyncio.run(main())
