"""Nightly backup job, scheduled via run_every.

The traced function ``snapshot_directory`` is small.  It composes three
plain helpers -- ``_archive``, ``_compress``, ``_upload`` -- which are
not themselves decorated.  pyfuse follows the calls, ships the source
of all three, and the worker executes the whole pipeline as one task.

The client just schedules the job and exits; the worker pool runs it on
the chosen cadence.

Replace the ``_upload`` body with real ``boto3`` calls when wiring this
into production.

Usage:
    pyfuse worker --backend redis://localhost:6379 --tmp
    python -m pyfuse run --tmp examples/scheduled_backup.py
"""

import asyncio
import gzip
import hashlib
import io
import os
import tarfile
from datetime import timedelta
from typing import Any

import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")


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


async def main() -> None:
    schedule = await snapshot_directory.run_every(
        timedelta(hours=24),
        "/var/lib/myapp/data",
        "my-backups",
        "myapp/daily",
    )
    print(f"Scheduled daily backup: {schedule.schedule_id}")
    # The script ends here; the worker keeps running the schedule.


if __name__ == "__main__":
    asyncio.run(main())
