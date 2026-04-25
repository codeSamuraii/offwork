"""Async HTTP fan-out from a remote worker.

Each traced call opens its own ``httpx.AsyncClient`` (HTTP/2, connection
pool, async DNS) and probes a list of URLs concurrently.  This is a
faithful test of the worker's event-loop integration: dozens of TCP
sockets, TLS handshakes, and async iterators per task.

The client is created *inside* the async function so it binds to the
worker's running loop.  The ``async with`` block guarantees the pool is
torn down before the task returns.

Usage:
    pyfuse worker --backend redis://localhost:6379 --tmp
    python -m pyfuse run --tmp examples/httpx_concurrent_scrape.py
"""

import asyncio
from typing import Any

import httpx

import pyfuse
from pyfuse import trace, progress

pyfuse.connect("redis://localhost:6379")


URLS = [
    "https://example.com",
    "https://www.python.org",
    "https://httpbin.org/get",
    "https://httpbin.org/status/200",
    "https://httpbin.org/status/404",
    "https://httpbin.org/delay/1",
    "https://httpbin.org/headers",
    "https://httpbin.org/uuid",
]


@trace(timeout=30, retries=1)
async def probe_many(urls: list[str], concurrency: int = 4) -> list[dict[str, Any]]:
    """Hit every URL once, capture status / size / latency."""
    sem = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        async def probe(url: str) -> dict[str, Any]:
            loop = asyncio.get_running_loop()
            start = loop.time()
            try:
                async with sem:
                    r = await client.get(url)
                return {
                    "url": url,
                    "status": r.status_code,
                    "bytes": len(r.content),
                    "elapsed_ms": round((loop.time() - start) * 1000, 1),
                }
            except Exception as exc:
                return {
                    "url": url,
                    "status": None,
                    "error": type(exc).__name__,
                    "elapsed_ms": round((loop.time() - start) * 1000, 1),
                }

        tasks = [asyncio.create_task(probe(u)) for u in urls]
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            results.append(await coro)
            progress(i, len(urls), message=f"{i}/{len(urls)} probed")

    return results


async def main() -> None:
    handle = await probe_many.start(URLS, concurrency=4)
    while not await handle.done():
        p = await handle.progress()
        if p is not None:
            print(p)
        await asyncio.sleep(0.3)

    rows = await handle
    print(f"\nProbed {len(rows)} URLs:")
    for row in sorted(rows, key=lambda r: r["elapsed_ms"]):
        status = row.get("status") or row.get("error")
        print(f"  {row['elapsed_ms']:>7.1f}ms  {str(status):<5}  {row['url']}")


if __name__ == "__main__":
    asyncio.run(main())
