"""Length-prefixed JSON wire protocol shared by host and guest agent.

The format is identical to the one used by the local backend
(:mod:`pyfuse.worker.backends.local`): a 4-byte big-endian length
header followed by a UTF-8 JSON payload.

This module is intentionally dependency-free (stdlib only) so it can be
shipped into the container without installing pyfuse there.
"""

import json
import struct
from typing import Any

_HEADER = struct.Struct("!I")  # 4-byte big-endian unsigned int


def encode(obj: dict[str, Any]) -> bytes:
    """Serialise *obj* to a length-prefixed JSON frame."""
    payload = json.dumps(obj, separators=(",", ":")).encode()
    return _HEADER.pack(len(payload)) + payload


def decode_header(data: bytes) -> int:
    """Return the payload length from a 4-byte header."""
    (length,) = _HEADER.unpack(data)
    return length


HEADER_SIZE: int = _HEADER.size


# -- asyncio helpers (used by both host and guest) --------------------------

async def async_send(writer: Any, obj: dict[str, Any]) -> None:
    """Send a length-prefixed JSON message on an :class:`asyncio.StreamWriter`."""
    writer.write(encode(obj))
    await writer.drain()


async def async_recv(reader: Any) -> dict[str, Any]:
    """Receive a length-prefixed JSON message from an :class:`asyncio.StreamReader`.

    Raises :class:`asyncio.IncompleteReadError` on EOF.
    """
    raw = await reader.readexactly(HEADER_SIZE)
    length = decode_header(raw)
    data = await reader.readexactly(length)
    result: dict[str, Any] = json.loads(data)
    return result
