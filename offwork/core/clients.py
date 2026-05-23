"""Per-worker TOFU registry of known client identities.

The worker maintains a JSON file under ``~/.offwork/known_clients.json``
mapping each ``client_id`` it has accepted at least one task from to the
Ed25519 public key it first saw.  Subsequent submissions must present
the same public key; mismatches are rejected with
:class:`IdentityMismatchError`.

The same file also carries a ``revoked`` flag per client_id, used by
``offwork clients revoke <id>``.
"""

import json
import time
import logging
import threading
from pathlib import Path
from dataclasses import dataclass, asdict

from offwork.core.errors import IdentityMismatchError

logger = logging.getLogger(__name__)

_DEFAULT_KEY_DIR = Path.home() / ".offwork"
_FILE_NAME = "known_clients.json"


@dataclass
class ClientEntry:
    """One row of the known-clients registry."""

    client_id: str
    pubkey: str  # 64 hex chars
    first_seen: float
    last_seen: float
    revoked: bool = False


class KnownClients:
    """TOFU + denylist registry.  All operations are thread-safe."""

    def __init__(self, key_dir: Path | None = None) -> None:
        d = key_dir or _DEFAULT_KEY_DIR
        d.mkdir(parents=True, exist_ok=True)
        self._path = d / _FILE_NAME
        self._lock = threading.Lock()
        self._entries: dict[str, ClientEntry] = self._load()

    # -- Persistence -------------------------------------------------------

    def _load(self) -> dict[str, ClientEntry]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s (%s) — starting empty", self._path, exc)
            return {}
        out: dict[str, ClientEntry] = {}
        for cid, row in raw.items():
            try:
                out[cid] = ClientEntry(**row)
            except TypeError:
                logger.warning("Dropping malformed entry for %s", cid)
        return out

    def _save_locked(self) -> None:
        data = {cid: asdict(e) for cid, e in self._entries.items()}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.chmod(0o600)
        tmp.replace(self._path)

    # -- Public API --------------------------------------------------------

    def register_or_verify(self, client_id: str, pubkey_hex: str) -> str:
        """Pin a client_id to a public key on first sight; verify on later sights.

        Returns ``"new"`` when the client was just registered (TOFU) and
        ``"known"`` when it matched an existing pin.  Raises
        :class:`IdentityMismatchError` if the stored pubkey differs.
        """
        with self._lock:
            entry = self._entries.get(client_id)
            now = time.time()
            if entry is None:
                self._entries[client_id] = ClientEntry(
                    client_id=client_id,
                    pubkey=pubkey_hex,
                    first_seen=now,
                    last_seen=now,
                )
                self._save_locked()
                logger.info("TOFU-registered new client %s", client_id[:8])
                return "new"
            if entry.pubkey != pubkey_hex:
                raise IdentityMismatchError(
                    f"Client {client_id[:8]} previously presented a different "
                    f"public key — rejecting submission"
                )
            entry.last_seen = now
            self._save_locked()
            return "known"

    def is_revoked(self, client_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(client_id)
            return entry is not None and entry.revoked

    def revoke(self, client_id: str) -> bool:
        """Mark *client_id* as revoked.  Returns True if a change was made."""
        with self._lock:
            entry = self._entries.get(client_id)
            if entry is None or entry.revoked:
                return False
            entry.revoked = True
            self._save_locked()
            logger.info("Revoked client %s", client_id[:8])
            return True

    def approve(self, client_id: str) -> bool:
        """Clear the revoked flag for *client_id*."""
        with self._lock:
            entry = self._entries.get(client_id)
            if entry is None or not entry.revoked:
                return False
            entry.revoked = False
            self._save_locked()
            logger.info("Approved (un-revoked) client %s", client_id[:8])
            return True

    def list_clients(self) -> list[ClientEntry]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e.first_seen)

    def get(self, client_id: str) -> ClientEntry | None:
        with self._lock:
            return self._entries.get(client_id)

    @property
    def path(self) -> Path:
        return self._path
