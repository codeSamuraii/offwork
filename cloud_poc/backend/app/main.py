"""FastAPI app for the local pyfuse cloud proof-of-concept."""

import os
import time
import logging
import asyncio
from typing import Any
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

logging.basicConfig(
    level=os.environ.get("PYFUSE_CLOUD_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

from fastapi import Body, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from pyfuse.core.task import Task
from pyfuse.worker.result import ResultEnvelope

from .config import Settings
from .security import generate_api_key, hash_password, require_api_key, require_user, verify_password
from .orchestrator import WorkerOrchestrator

settings = Settings()
logger = logging.getLogger(__name__)


class _DB:
    def __init__(self, client: MongoClient[Any], name: str) -> None:
        self._db = client[name]
        self.users: Collection[Any] = self._db["users"]
        self.tasks: Collection[Any] = self._db["tasks"]
        self.schedules: Collection[Any] = self._db["schedules"]
        self.throttles: Collection[Any] = self._db["throttles"]

    def ensure_indexes(self) -> None:
        self.users.create_index("email", unique=True)
        self.users.create_index("api_key", unique=True)
        self.tasks.create_index([("user_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)])
        self.tasks.create_index("task_id", unique=True)
        self.schedules.create_index([("user_id", ASCENDING), ("schedule_id", ASCENDING)], unique=True)
        self.throttles.create_index([("user_id", ASCENDING), ("function_name", ASCENDING)], unique=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def deployment_name_for(user_id: str) -> str:
    return f"pyfuse-worker-{user_id[:12]}"


def broker_url(api_key: str) -> str:
    return f"{settings.broker_public_base_url}?api_key={api_key}"


async def authenticated_user(request: Request) -> dict[str, Any]:
    api_key = await require_api_key(request)
    return await require_user(request, api_key)


async def mark_user_active(user: dict[str, Any]) -> None:
    app.state.db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_worker_activity_at": utc_now()}},
    )


async def worker_reaper() -> None:
    logger.info("reaper started  idle_threshold=%ds", settings.worker_idle_seconds)
    while True:
        cutoff = utc_now() - timedelta(seconds=settings.worker_idle_seconds)
        idle_users = list(
            app.state.db.users.find(
                {
                    "last_worker_activity_at": {"$lt": cutoff},
                    "$expr": {
                        "$gt": [
                            {"$ifNull": ["$last_worker_activity_at", 0]},
                            {"$ifNull": ["$reaped_at", 0]},
                        ]
                    },
                },
                {"_id": 1},
            )
        )
        if idle_users:
            logger.info("reaper found %d idle user(s)", len(idle_users))
        for user in idle_users:
            user_id = str(user["_id"])
            try:
                await asyncio.to_thread(
                    app.state.orchestrator.scale_worker,
                    deployment_name_for(user_id),
                    0,
                )
                app.state.db.users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"reaped_at": utc_now()}},
                )
                logger.info("reaper scaled user=%s to 0", user_id)
            except RuntimeError:
                logger.warning("reaper failed to scale user=%s", user_id, exc_info=True)
        await asyncio.sleep(max(settings.task_poll_interval, 1.0))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "control-plane starting  mongo=%s db=%s public_url=%s internal_url=%s",
        settings.mongodb_uri,
        settings.mongodb_database,
        settings.broker_public_base_url,
        settings.broker_internal_base_url,
    )
    client: MongoClient[Any] = MongoClient(settings.mongodb_uri, tz_aware=True)
    _app.state.mongo_client = client
    _app.state.db = _DB(client, settings.mongodb_database)
    _app.state.db.ensure_indexes()
    _app.state.orchestrator = WorkerOrchestrator(settings)
    _app.state.reaper = asyncio.create_task(worker_reaper())
    logger.info("control-plane ready")
    try:
        yield
    finally:
        logger.info("control-plane shutting down")
        reaper = getattr(_app.state, "reaper", None)
        if reaper is not None:
            reaper.cancel()
            try:
                await reaper
            except asyncio.CancelledError:
                pass
        _app.state.mongo_client.close()


app = FastAPI(
    title="pyfuse cloud proof-of-concept",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/users/register")
async def register_user(payload: dict[str, str] = Body(...)) -> dict[str, Any]:
    email = payload.get("email", "").strip().lower()
    password = payload.get("password", "")
    if not email or not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="email and password are required",
        )

    created_at = utc_now()
    api_key = generate_api_key()
    user_doc = {
        "email": email,
        "password_hash": hash_password(password),
        "api_key": api_key,
        "created_at": created_at,
        "last_worker_activity_at": created_at,
    }
    try:
        result = app.state.db.users.insert_one(user_doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered") from exc

    user_id = str(result.inserted_id)
    logger.info("register user=%s id=%s", email, user_id)
    await asyncio.to_thread(
        app.state.orchestrator.ensure_worker,
        deployment_name_for(user_id),
        api_key,
    )
    return {
        "user_id": user_id,
        "email": email,
        "api_key": api_key,
        "broker_url": broker_url(api_key),
    }


@app.post("/api/v1/users/login")
async def login_user(payload: dict[str, str] = Body(...)) -> dict[str, Any]:
    email = payload.get("email", "").strip().lower()
    password = payload.get("password", "")
    if not email or not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="email and password are required",
        )
    user = app.state.db.users.find_one({"email": email})
    if user is None or not verify_password(password, str(user["password_hash"])):
        logger.info("login failed user=%s", email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    user_id = str(user["_id"])
    api_key = str(user["api_key"])
    logger.info("login user=%s id=%s", email, user_id)
    return {
        "user_id": user_id,
        "email": email,
        "api_key": api_key,
        "broker_url": broker_url(api_key),
    }


@app.get("/api/v1/users/me")
async def current_user(request: Request) -> dict[str, Any]:
    user = await authenticated_user(request)
    return {
        "id": user["id"],
        "email": user["email"],
        "created_at": user["created_at"],
        "last_worker_activity_at": user.get("last_worker_activity_at"),
        "broker_url": broker_url(str(user["api_key"])),
    }


@app.get("/api/v1/usage/summary")
async def usage_summary(request: Request) -> dict[str, Any]:
    user = await authenticated_user(request)
    tasks = list(
        app.state.db.tasks.find(
            {"user_id": user["id"]},
            {"status": 1, "task_bytes": 1, "result_bytes": 1, "created_at": 1},
        )
    )
    summary = {
        "total_tasks": len(tasks),
        "queued_tasks": 0,
        "running_tasks": 0,
        "completed_tasks": 0,
        "failed_tasks": 0,
        "cancelled_tasks": 0,
        "total_task_bytes": 0,
        "total_result_bytes": 0,
        "last_submission_at": None,
    }
    for task in tasks:
        status_name = task.get("status")
        if status_name == "queued":
            summary["queued_tasks"] += 1
        elif status_name == "running":
            summary["running_tasks"] += 1
        elif status_name == "completed":
            summary["completed_tasks"] += 1
        elif status_name in {"error", "throttled"}:
            summary["failed_tasks"] += 1
        elif status_name == "cancelled":
            summary["cancelled_tasks"] += 1
        summary["total_task_bytes"] += int(task.get("task_bytes", 0))
        summary["total_result_bytes"] += int(task.get("result_bytes", 0))
        created_at = task.get("created_at")
        if created_at is not None and (
            summary["last_submission_at"] is None or created_at > summary["last_submission_at"]
        ):
            summary["last_submission_at"] = created_at
    return summary


@app.get("/api/v1/usage/tasks")
async def usage_tasks(request: Request, limit: int = 25) -> list[dict[str, Any]]:
    user = await authenticated_user(request)
    docs = (
        app.state.db.tasks.find({"user_id": user["id"]})
        .sort("created_at", DESCENDING)
        .limit(min(limit, 100))
    )
    return [
        {
            "task_id": doc["task_id"],
            "function_name": doc.get("function_name"),
            "status": doc.get("status"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
            "task_bytes": doc.get("task_bytes", 0),
            "result_bytes": doc.get("result_bytes", 0),
        }
        for doc in docs
    ]


@app.post("/api/v1/broker/tasks")
async def submit_task(request: Request, payload: dict[str, str] = Body(...)) -> dict[str, Any]:
    user = await authenticated_user(request)
    task_json = payload["task_json"]
    task = Task.from_json(task_json)
    now = utc_now()
    app.state.db.tasks.update_one(
        {"task_id": task.task_id},
        {
            "$set": {
                "task_id": task.task_id,
                "user_id": user["id"],
                "task_json": task_json,
                "function_name": task.function_name,
                "status": "queued",
                "cancelled": False,
                "created_at": now,
                "updated_at": now,
                "task_bytes": len(task_json.encode("utf-8")),
            }
        },
        upsert=True,
    )
    await mark_user_active(user)
    logger.info(
        "submit user=%s task=%s fn=%s bytes=%d",
        user["id"], task.task_id, task.function_name, len(task_json.encode("utf-8")),
    )
    await asyncio.to_thread(
        app.state.orchestrator.ensure_worker,
        deployment_name_for(user["id"]),
        str(user["api_key"]),
    )
    await asyncio.to_thread(
        app.state.orchestrator.scale_worker,
        deployment_name_for(user["id"]),
        1,
    )
    return {"task_id": task.task_id}


@app.post("/api/v1/broker/tasks/claim")
async def claim_task(
    request: Request,
    payload: dict[str, float] = Body(default_factory=dict),
) -> Response:
    user = await authenticated_user(request)
    deadline = time.monotonic() + float(payload.get("wait_seconds", 0.0))
    while True:
        now = utc_now()
        doc = app.state.db.tasks.find_one_and_update(
            {"user_id": user["id"], "status": "queued", "cancelled": False},
            {"$set": {"status": "running", "updated_at": now, "started_at": now}},
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            await mark_user_active(user)
            logger.info(
                "claim user=%s task=%s fn=%s",
                user["id"], doc["task_id"], doc.get("function_name"),
            )
            return {"task_json": doc["task_json"]}
        if time.monotonic() >= deadline:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        await asyncio.sleep(settings.task_poll_interval)


@app.post("/api/v1/broker/tasks/{task_id}/result")
async def put_result(
    task_id: str,
    request: Request,
    payload: dict[str, str] = Body(...),
) -> dict[str, bool]:
    user = await authenticated_user(request)
    result_json = payload["result_json"]
    envelope = ResultEnvelope.from_json(result_json)
    status_name = {
        "ok": "completed",
        "error": "error",
        "cancelled": "cancelled",
        "throttled": "throttled",
    }.get(envelope.status, envelope.status)
    now = utc_now()
    app.state.db.tasks.update_one(
        {"task_id": task_id, "user_id": user["id"]},
        {
            "$set": {
                "result_json": result_json,
                "status": status_name,
                "updated_at": now,
                "finished_at": now,
                "result_bytes": len(result_json.encode("utf-8")),
            }
        },
    )
    await mark_user_active(user)
    logger.info(
        "result user=%s task=%s status=%s bytes=%d",
        user["id"], task_id, status_name, len(result_json.encode("utf-8")),
    )
    return {"ok": True}


@app.get("/api/v1/broker/tasks/{task_id}/result")
async def get_result(
    task_id: str,
    request: Request,
    wait_seconds: float = 0.0,
) -> Response:
    user = await authenticated_user(request)
    deadline = time.monotonic() + wait_seconds
    while True:
        doc = app.state.db.tasks.find_one(
            {"task_id": task_id, "user_id": user["id"]},
            {"result_json": 1},
        )
        if doc and isinstance(doc.get("result_json"), str):
            return {"result_json": doc["result_json"]}
        if time.monotonic() >= deadline:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        await asyncio.sleep(settings.task_poll_interval)


@app.post("/api/v1/broker/tasks/{task_id}/heartbeat")
async def put_heartbeat(task_id: str, request: Request) -> dict[str, bool]:
    user = await authenticated_user(request)
    now = utc_now()
    app.state.db.tasks.update_one(
        {"task_id": task_id, "user_id": user["id"]},
        {"$set": {"heartbeat_at": now, "updated_at": now}},
    )
    await mark_user_active(user)
    return {"ok": True}


@app.get("/api/v1/broker/tasks/{task_id}/heartbeat")
async def get_heartbeat(task_id: str, request: Request) -> Response:
    user = await authenticated_user(request)
    doc = app.state.db.tasks.find_one(
        {"task_id": task_id, "user_id": user["id"]},
        {"heartbeat_at": 1},
    )
    heartbeat_at = doc.get("heartbeat_at") if doc else None
    if heartbeat_at is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return {"heartbeat": heartbeat_at.timestamp()}


@app.post("/api/v1/broker/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> dict[str, bool]:
    user = await authenticated_user(request)
    now = utc_now()
    app.state.db.tasks.update_one(
        {"task_id": task_id, "user_id": user["id"]},
        {
            "$set": {
                "cancelled": True,
                "status": "cancelled",
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    logger.info("cancel user=%s task=%s", user["id"], task_id)
    return {"cancelled": True}


@app.get("/api/v1/broker/tasks/{task_id}/cancel")
async def is_cancelled(task_id: str, request: Request) -> dict[str, bool]:
    user = await authenticated_user(request)
    doc = app.state.db.tasks.find_one(
        {"task_id": task_id, "user_id": user["id"]},
        {"cancelled": 1},
    )
    return {"cancelled": bool(doc and doc.get("cancelled"))}


@app.post("/api/v1/broker/tasks/{task_id}/progress")
async def put_progress(
    task_id: str,
    request: Request,
    payload: dict[str, str] = Body(...),
) -> dict[str, bool]:
    user = await authenticated_user(request)
    now = utc_now()
    app.state.db.tasks.update_one(
        {"task_id": task_id, "user_id": user["id"]},
        {"$set": {"progress_json": payload["progress_json"], "updated_at": now}},
    )
    await mark_user_active(user)
    return {"ok": True}


@app.get("/api/v1/broker/tasks/{task_id}/progress")
async def get_progress(task_id: str, request: Request) -> Response:
    user = await authenticated_user(request)
    doc = app.state.db.tasks.find_one(
        {"task_id": task_id, "user_id": user["id"]},
        {"progress_json": 1},
    )
    if doc is None or "progress_json" not in doc:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return {"progress_json": doc["progress_json"]}


@app.post("/api/v1/broker/schedules/{schedule_id}/cancel")
async def cancel_schedule(schedule_id: str, request: Request) -> dict[str, bool]:
    user = await authenticated_user(request)
    app.state.db.schedules.update_one(
        {"user_id": user["id"], "schedule_id": schedule_id},
        {"$set": {"cancelled": True, "updated_at": utc_now()}},
        upsert=True,
    )
    return {"cancelled": True}


@app.get("/api/v1/broker/schedules/{schedule_id}/cancel")
async def is_schedule_cancelled(schedule_id: str, request: Request) -> dict[str, bool]:
    user = await authenticated_user(request)
    doc = app.state.db.schedules.find_one(
        {"user_id": user["id"], "schedule_id": schedule_id},
        {"cancelled": 1},
    )
    return {"cancelled": bool(doc and doc.get("cancelled"))}


@app.get("/api/v1/broker/throttle/check")
async def check_throttle(function_name: str, request: Request) -> dict[str, bool]:
    user = await authenticated_user(request)
    doc = app.state.db.throttles.find_one(
        {"user_id": user["id"], "function_name": function_name},
        {"expire_at": 1},
    )
    expire_at = doc.get("expire_at") if doc else None
    return {"allowed": expire_at is None or expire_at <= utc_now()}


@app.post("/api/v1/broker/throttle/record")
async def record_throttle(
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> dict[str, bool]:
    user = await authenticated_user(request)
    app.state.db.throttles.update_one(
        {"user_id": user["id"], "function_name": str(payload["function_name"])},
        {
            "$set": {
                "expire_at": utc_now() + timedelta(seconds=float(payload["throttle_seconds"])),
                "updated_at": utc_now(),
            }
        },
        upsert=True,
    )
    return {"ok": True}
