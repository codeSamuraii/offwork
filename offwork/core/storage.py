"""Persistent per-worker storage path.

A worker may expose a directory that survives between task executions
(e.g. a per-user volume mounted into the worker container). Tasks read
its location through :func:`storage_path` rather than hard-coding a path,
so the same code runs locally and on a hosted broker.
"""

import os
from pathlib import Path

#: Environment variable a worker sets to advertise its persistent storage
#: directory. Hosted brokers point this at a per-user volume.
STORAGE_ENV = "OFFWORK_STORAGE"


def storage_path(*parts: str) -> Path:
    """Return the persistent storage directory, creating it if needed.

    Files written under the returned directory survive between task
    executions on workers that provide persistent storage. When no
    storage is configured (``OFFWORK_STORAGE`` unset) this falls back to
    ``./offwork-storage`` in the worker's working directory.

    Optional *parts* are joined onto the base path::

        offwork.storage_path()                 # the storage root
        offwork.storage_path("models")         # a subdirectory
        offwork.storage_path("data", "in.csv") # a file path

    The storage root is created on access. The returned path is absolute.
    Joined *parts* are not created — call ``.mkdir(parents=True)`` on the
    result yourself if you need a subdirectory.
    """
    base = Path(os.environ.get(STORAGE_ENV, "offwork-storage")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return base.joinpath(*parts).resolve()
