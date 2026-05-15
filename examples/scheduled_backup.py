"""Backup job, scheduled via run_every.

The traced function ``snapshot_directory`` is small.  It composes three
plain helpers -- ``_archive``, ``_compress``, ``_upload`` -- which are
not themselves decorated.  seeya follows the calls, ships the source
of all three, and the worker executes the whole pipeline as one task.

Replace the ``_upload`` body with real ``boto3`` calls when wiring this
into production.

Usage:
    seeya worker --backend redis://localhost:6379 --tmp
    python examples/scheduled_backup.py

The script generates a small directory of fake data in ``/tmp``,
schedules a recurring backup, lets it fire a couple of times, then
cancels the schedule and exits.
"""

import asyncio
import gzip
import hashlib
import io
import os
import tarfile
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import seeya
from seeya import trace

seeya.connect("local://localhost:9748")


# --- helpers (auto-discovered) --------------------------------------------

def _archive(src_dir: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(src_dir, arcname=os.path.basename(src_dir.rstrip("/")))
    return buf.getvalue()


def _compress(blob: bytes, level: int = 6) -> bytes:
    return gzip.compress(blob, compresslevel=level)


def _upload(bucket: str, key: str, blob: bytes) -> None:
    """Stub.  Replace with ``boto3.client('s3').put_object(...)``."""
    print(f"  [upload] s3://{bucket}/{key} ({len(blob)} bytes)")


# --- entry point ----------------------------------------------------------

@trace(timeout=600, retries=2, retry_delay=10.0)
def snapshot_directory(src_dir: str, bucket: str, prefix: str) -> dict[str, Any]:
    """Tar+gzip *src_dir*, upload, return a manifest entry."""
    compressed = _compress(_archive(src_dir))
    digest = hashlib.sha256(compressed).hexdigest()
    key = f"{prefix}/{digest[:12]}.tar.gz"
    _upload(bucket, key, compressed)
    return {
        "bucket": bucket,
        "key": key,
        "size_bytes": len(compressed),
        "sha256": digest,
    }


# --- demo driver ---------------------------------------------------------

def _populate_sample_dir() -> str:
    """Create a small directory of fake data and return its path."""
    root = Path(tempfile.mkdtemp(prefix="seeya_backup_"))
    (root / "config.yaml").write_text("env: prod\nversion: 1.2.3\n")
    (root / "users.csv").write_text("id,name\n1,alice\n2,bob\n3,carol\n")
    (root / "logs").mkdir()
    (root / "logs" / "app.log").write_text("started\n" * 100)
    return str(root)


async def main() -> None:
    src = _populate_sample_dir()
    print(f"Source dir: {src}")

    schedule = await snapshot_directory.run_every(
        timedelta(seconds=5),
        src,
        "my-backups",
        "myapp/demo",
    )
    print(f"Scheduled backup every 5s: {schedule.schedule_id}")

    # Let the schedule fire a couple of times, then cancel.
    await asyncio.sleep(12)
    await schedule.cancel()
    print(f"Cancelled schedule: {schedule.schedule_id}")


if __name__ == "__main__":
    asyncio.run(main())
