"""Async DB clients (Motor / asyncpg-style) on a pyfuse worker.

Async clients are bound to the event loop they were created on.  Running
them on a worker is fine *as long as* you create them inside the traced
async function -- pyfuse's worker awaits the coroutine on its own loop,
so a freshly-created client picks up the right loop automatically.

This example uses Motor (async MongoDB driver).  The pattern is identical
for ``asyncpg``, ``aioredis``, ``aiomysql``, etc.

Usage:
    docker run -d -p 27017:27017 --name pyfuse-mongo mongo:7
    pyfuse worker --backend redis://localhost:6379 --tmp
    python -m pyfuse run --tmp examples/async_db_client.py
"""

import asyncio
from typing import Any

import motor.motor_asyncio as motor

import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")

MONGO_URL = "mongodb://localhost:27017"
DB_NAME = "pyfuse_demo_async"


@trace
async def upsert_user(user_id: int, name: str, score: float) -> str:
    """Async upsert -- exercises the worker's event loop end-to-end."""
    client = motor.AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    try:
        db = client[DB_NAME]
        await db.users.update_one(
            {"_id": user_id},
            {"$set": {"name": name, "score": score}},
            upsert=True,
        )
        return f"upserted user {user_id}"
    finally:
        client.close()


@trace
async def top_users(limit: int = 5) -> list[dict[str, Any]]:
    """Concurrent reads via asyncio.gather inside the worker loop."""
    client = motor.AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    try:
        db = client[DB_NAME]
        cursor = db.users.find({}, {"_id": 1, "name": 1, "score": 1})
        cursor = cursor.sort("score", -1).limit(limit)
        return [doc async for doc in cursor]
    finally:
        client.close()


async def main() -> None:
    # Fan-out a batch of upserts -- each runs as its own remote task,
    # each with its own client / loop on the worker side.
    users = [(i, f"user-{i}", float(i * 7 % 31)) for i in range(20)]
    acks = await upsert_user.map(users)
    print(f"{len(acks)} upserts acknowledged")

    leaderboard = await top_users.run(5)
    print("Leaderboard:")
    for row in leaderboard:
        print(f"  {row['name']:<10} score={row['score']}")


if __name__ == "__main__":
    asyncio.run(main())
