"""Persistent storage on offwork cloud.

Demonstrates:
- ``@offwork.task(storage=True)`` to mount a per-user volume on the worker
- ``offwork.storage_path()`` for reading and writing files that survive
  across task runs (and worker scale-to-zero cycles)

Requires a hosted WebSocket broker (``ws://`` or ``wss://``). Redis,
RabbitMQ, and ``local://`` backends reject ``storage=True``.

Setup::

    export OFFWORK_BACKEND=wss://offwork.live/api/v1/broker/ws
    export OFFWORK_API_KEY=<your key>

Run::

    python examples/persistent_storage.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import offwork
from offwork.core.errors import StorageNotSupportedError

_BACKEND = os.environ.get(
    "OFFWORK_BACKEND",
    "wss://offwork.live/api/v1/broker/ws",
)


@offwork.task(storage=True)
def touch_counter(label: str) -> dict:
    """Increment a JSON counter stored on the worker's persistent volume."""
    path = offwork.storage_path("demo_counter.json")
    data: dict[str, object]
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"count": 0, "labels": []}

    data["count"] = int(data.get("count", 0)) + 1
    labels = list(data.get("labels", []))
    labels.append(label)
    data["labels"] = labels[-5:]  # keep the last few labels
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


@offwork.task(storage=True)
def read_counter() -> dict:
    """Read the counter without modifying it."""
    path = offwork.storage_path("demo_counter.json")
    if not path.exists():
        return {"count": 0, "labels": [], "updated_at": None}
    return json.loads(path.read_text(encoding="utf-8"))


async def main() -> None:
    try:
        offwork.connect(_BACKEND)
    except Exception as exc:  # noqa: BLE001 — show a friendly setup hint
        print(f"Could not connect to {_BACKEND}: {exc}")
        print("Set OFFWORK_BACKEND and OFFWORK_API_KEY for your cloud broker.")
        return

    print(f"broker: {_BACKEND.split('?')[0]}")
    print("Writing to the worker's /storage mount …\n")

    try:
        after_write = await touch_counter.run("persistent_storage demo")
        after_read = await read_counter.run()
    except StorageNotSupportedError as exc:
        print(f"storage not available: {exc}")
        return

    print("touch_counter returned:")
    print(json.dumps(after_write, indent=2))
    print("\nread_counter returned:")
    print(json.dumps(after_read, indent=2))
    print(
        "\nRun this script again — the count should increase and the volume "
        "persists even if the worker pod was idle-reaped between runs."
    )


if __name__ == "__main__":
    asyncio.run(main())
