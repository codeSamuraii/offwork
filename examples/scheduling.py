"""Task scheduling with offwork.

Demonstrates:
- run_in(delay)   — execute after a delay
- run_at(datetime) — execute at a specific time
- run_every(freq)  — recurring execution with cancellation

Usage:
    # Terminal 1 -- start a worker
    offwork worker --backend local://localhost:9748 --tmp

    # Terminal 2 -- run this script
    offwork run examples/scheduling.py
"""

import asyncio
from datetime import datetime, timedelta

import offwork

offwork.connect("local://localhost:9748")


@offwork.task
def greet(name: str) -> str:
    return f"Hello, {name}! (executed at {datetime.now():%H:%M:%S})"


@offwork.task
def tick(n: int) -> str:
    return f"Tick #{n} at {datetime.now():%H:%M:%S}"


async def main() -> None:
    print(f"Now: {datetime.now():%H:%M:%S}\n")

    # 1. run_in — execute after a 2-second delay
    print("— run_in(2 seconds) —")
    result = await greet.submit("Alice", run_in=timedelta(seconds=2))
    print(result)

    # 2. run_at — execute at a specific time (3 seconds from now)
    print("\n— run_at(now + 3s) —")
    target = datetime.now() + timedelta(seconds=3)
    print(f"Scheduled for: {target:%H:%M:%S}")
    result = await greet.submit("Bob", run_at=target)
    print(result)

    # 3. run_every — recurring execution every 2 seconds
    print("\n— run_every(2 seconds) for ~6 seconds —")
    schedule = await tick.submit(1, run_every=timedelta(seconds=2))
    print(f"Schedule started: {schedule.schedule_id}")
    await asyncio.sleep(6)

    # Cancel the recurring schedule
    await schedule.cancel()
    print(f"Schedule cancelled: {schedule.schedule_id}")


asyncio.run(main())
