"""FastAPI integration example for pyfuse.

Demonstrates how to use pyfuse with FastAPI to submit remote tasks
from HTTP endpoints.

Start a worker in one terminal:
    pyfuse worker --backend local://localhost:9748 --tmp

Run the FastAPI app in another:
    pip install fastapi uvicorn
    uvicorn examples.fastapi_app:app --reload

Then call the endpoint:
    curl -X POST http://localhost:8000/compute -H 'Content-Type: application/json' -d '{"a": 3.0, "b": 4.0}'
"""

import math

from fastapi import FastAPI

import pyfuse
from pyfuse import trace
from pyfuse.integrations.asgi import pyfuse_lifespan


# ── pyfuse setup ──────────────────────────────────────────────────────

def add(a: float, b: float) -> float:
    return a + b


@trace
def hypotenuse(a: float, b: float) -> float:
    """Compute the hypotenuse — executed on a remote worker."""
    return math.sqrt(add(a**2, b**2))


# ── FastAPI app ───────────────────────────────────────────────────────

app = FastAPI(
    title="pyfuse + FastAPI example",
    lifespan=pyfuse_lifespan("local://localhost:9748"),
)


@app.post("/compute")
async def compute(a: float = 3.0, b: float = 4.0) -> dict[str, object]:
    """Submit a hypotenuse computation to the remote worker."""
    result = await hypotenuse.run(a, b)
    return {"a": a, "b": b, "hypotenuse": result}


@app.post("/compute/start")
async def compute_start(a: float = 3.0, b: float = 4.0) -> dict[str, str]:
    """Submit a task and return the task ID without waiting."""
    future = await hypotenuse.start(a, b)
    return {"task_id": future.task_id, "status": "submitted"}
