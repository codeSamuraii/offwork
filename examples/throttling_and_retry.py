"""Throttling and retry with offwork.

Demonstrates:
- @offwork.task(throttle=timedelta) — rate-limit executions
- @offwork.task(retries=N, retry_delay=T) — retry with exponential backoff
- Combining throttle + retry

Usage:
    # Terminal 1 -- start a worker
    offwork worker --backend local://localhost:9748 --tmp

    # Terminal 2 -- run this script
    offwork run examples/throttling_and_retry.py
"""

import asyncio
import random
from datetime import timedelta

import offwork
from offwork import ThrottleError

offwork.connect("local://localhost:9748")


# --- Throttling: at most once every 5 seconds ---

@offwork.task(throttle=timedelta(seconds=5))
def expensive_query(query: str) -> str:
    return f"Result for '{query}'"


# --- Retry: 3 attempts with exponential backoff ---

@offwork.task(retries=3, retry_delay=0.5)
def flaky_operation(x: int) -> int:
    if random.random() < 0.5:
        raise RuntimeError("Random failure!")
    return x * 2


async def main() -> None:
    # 1. Throttling demo
    print("— Throttle demo (5s cooldown) —")
    result = await expensive_query.run("first call")
    print(f"  Call 1: {result}")

    try:
        result = await expensive_query.run("second call (too soon)")
        print(f"  Call 2: {result}")
    except ThrottleError:
        print("  Call 2: ThrottleError — rate limited!")

    # Wait for cooldown
    print("  Waiting 5s for cooldown...")
    await asyncio.sleep(5)

    result = await expensive_query.run("third call (after cooldown)")
    print(f"  Call 3: {result}")

    # 2. Retry demo
    print("\n— Retry demo (3 retries, 0.5s base delay) —")
    try:
        result = await flaky_operation.run(21)
        print(f"  Result: {result}")
    except Exception as exc:
        print(f"  Failed after retries: {exc}")


asyncio.run(main())
