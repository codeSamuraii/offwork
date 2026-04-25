"""Real-world MongoDB usage with pyfuse.

DB clients are stateful: connection pools, sockets, background heartbeat
threads.  Two rules to keep in mind when running them on a remote worker:

1. Open the client *inside* the traced function, not at module level.
   pyfuse caches the reconstructed namespace per subgraph, so a module-level
   client would survive across calls -- with stale sockets, wrong loop, etc.

2. Close the client before returning.  Otherwise the worker leaks a pool
   and a heartbeat thread per task.

The example runs three independent tasks against a real MongoDB and uses
``pymongo``'s aggregation framework so the round-trip is non-trivial.

Usage:
    # Start a Mongo + worker:
    docker run -d -p 27017:27017 --name pyfuse-mongo mongo:7
    pyfuse worker --backend redis://localhost:6379 --tmp

    # Run the script (will pip-install pymongo on the worker):
    python -m pyfuse run --tmp examples/mongodb_client.py
"""

import asyncio
import random
from contextlib import contextmanager
from typing import Any, Iterator

import pymongo

import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")

MONGO_URL = "mongodb://localhost:27017"
DB_NAME = "pyfuse_demo"


@contextmanager
def mongo_db(url: str, db_name: str) -> Iterator[Any]:
    """Open a client, yield the database, always close on exit."""
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
    try:
        yield client[db_name]
    finally:
        client.close()


@trace
def seed_events(n: int, seed: int = 0) -> int:
    """Insert *n* synthetic events, return the inserted count."""
    rng = random.Random(seed)
    levels = ["info", "warn", "error"]
    docs = [
        {
            "level": rng.choice(levels),
            "service": f"svc-{rng.randint(0, 4)}",
            "latency_ms": round(rng.gauss(120, 40), 2),
        }
        for _ in range(n)
    ]
    with mongo_db(MONGO_URL, DB_NAME) as db:
        db.events.drop()
        result = db.events.insert_many(docs)
        return len(result.inserted_ids)


@trace
def aggregate_by_service() -> list[dict[str, Any]]:
    """Run an aggregation pipeline and return the result."""
    pipeline = [
        {"$match": {"level": {"$in": ["warn", "error"]}}},
        {
            "$group": {
                "_id": "$service",
                "count": {"$sum": 1},
                "p95_latency": {"$max": "$latency_ms"},
            }
        },
        {"$sort": {"count": -1}},
    ]
    with mongo_db(MONGO_URL, DB_NAME) as db:
        return [
            {"service": r["_id"], "count": r["count"], "p95": r["p95_latency"]}
            for r in db.events.aggregate(pipeline)
        ]


@trace
def purge_events() -> int:
    """Drop the collection.  Returns the previous document count."""
    with mongo_db(MONGO_URL, DB_NAME) as db:
        before = db.events.count_documents({})
        db.events.drop()
        return before


async def main() -> None:
    inserted = await seed_events.run(2000, seed=42)
    print(f"Inserted {inserted} events")

    summary = await aggregate_by_service.run()
    print("Top noisy services:")
    for row in summary:
        print(f"  {row['service']:<8} count={row['count']:<4} p95={row['p95']}ms")

    removed = await purge_events.run()
    print(f"Cleaned up {removed} events")


if __name__ == "__main__":
    asyncio.run(main())
