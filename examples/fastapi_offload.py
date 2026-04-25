"""Use pyfuse to offload heavy work from a FastAPI request handler.

A common production pattern: the web app stays light and responsive, while
CPU-bound or slow I/O work is shipped to a worker pool over Redis.  The
request handler awaits the result and returns it.

Endpoints:
    GET  /                  -- healthcheck
    POST /jobs/sync         -- await the worker, return result
    POST /jobs/async        -- start the task, return task_id
    GET  /jobs/{task_id}    -- poll status / result

Usage:
    pyfuse worker --backend redis://localhost:6379 --tmp
    uvicorn examples.fastapi_offload:app --reload
    curl -X POST localhost:8000/jobs/sync -d '{"text":"hello WORLD"}' \
         -H 'content-type: application/json'
"""

import asyncio
import hashlib
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import pyfuse
from pyfuse import trace, Result


# --- traced workloads ------------------------------------------------------

@trace
def normalize(text: str) -> dict[str, Any]:
    """Cheap demo workload -- runs on the worker."""
    cleaned = " ".join(text.split()).lower()
    return {
        "length": len(cleaned),
        "sha1": hashlib.sha1(cleaned.encode()).hexdigest(),
        "preview": cleaned[:80],
    }


@trace(timeout=30, retries=2)
def cpu_bound(n: int) -> int:
    """Pretend-CPU work to demonstrate timeout / retry semantics."""
    total = 0
    for i in range(n):
        total += (i * i) % 97
    return total


# --- FastAPI app -----------------------------------------------------------

class JobRequest(BaseModel):
    text: str = ""
    n: int = 1_000_000


app = FastAPI(title="pyfuse offload demo")
_pending: dict[str, Result[Any]] = {}


@app.on_event("startup")
async def _startup() -> None:
    # Connecting at startup avoids a per-request handshake.
    pyfuse.connect("redis://localhost:6379")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await pyfuse.disconnect()


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs/sync")
async def submit_sync(req: JobRequest) -> dict[str, Any]:
    """Block the request until the worker returns."""
    try:
        return await asyncio.wait_for(normalize.run(req.text), timeout=10)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="worker timeout") from exc


@app.post("/jobs/async")
async def submit_async(req: JobRequest) -> dict[str, str]:
    """Fire-and-forget; return the task_id for polling."""
    handle = await cpu_bound.start(req.n)
    _pending[handle.task_id] = handle
    return {"task_id": handle.task_id}


@app.get("/jobs/{task_id}")
async def poll(task_id: str) -> dict[str, Any]:
    handle = _pending.get(task_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="unknown task")
    if not await handle.done():
        return {"task_id": task_id, "status": "pending"}
    try:
        result = await handle
    except Exception as exc:
        return {"task_id": task_id, "status": "error", "detail": str(exc)}
    finally:
        _pending.pop(task_id, None)
    return {"task_id": task_id, "status": "ok", "result": result}
