"""File descriptors and raw sockets in remote tasks.

Two things to watch out for:

1. **No shared filesystem.**  The worker runs in a different process (and
   often a different host or container).  Any path the function touches
   must exist on the *worker*.  Use ``tempfile`` to create scratch space
   inside the function, or stream bytes through arguments / return values.

2. **Don't leak descriptors.**  pyfuse keeps the reconstructed function in
   a long-lived namespace; a socket or file opened at module level would
   linger across calls.  Always open inside the function and close before
   returning (``with`` blocks make this trivial).

This script demonstrates both: a TCP echo round-trip done inside the
worker, and a tempfile-based hashing pipeline.
"""

import asyncio
import hashlib
import os
import socket
import tempfile

import pyfuse
from pyfuse import trace

pyfuse.connect("local://localhost:9748")


@trace
def echo_roundtrip(payload: bytes, port: int = 0) -> bytes:
    """Spin up a tiny echo server in a thread, connect, exchange bytes."""
    import threading

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    bound_port = server.getsockname()[1]

    def serve() -> None:
        with server:
            conn, _ = server.accept()
            with conn:
                data = conn.recv(len(payload))
                conn.sendall(data)

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    with socket.create_connection(("127.0.0.1", bound_port), timeout=2.0) as cli:
        cli.sendall(payload)
        received = b""
        while len(received) < len(payload):
            chunk = cli.recv(4096)
            if not chunk:
                break
            received += chunk
    t.join(timeout=2.0)
    return received


@trace
def hash_in_chunks(blob: bytes, chunk_size: int = 64 * 1024) -> dict[str, object]:
    """Round-trip ``blob`` through a tempfile, hash it as we re-read."""
    fd, path = tempfile.mkstemp(prefix="pyfuse-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)

        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        return {"size": size, "sha256": h.hexdigest(), "path": path}
    finally:
        os.unlink(path)


async def main() -> None:
    payload = b"the quick brown fox" * 100
    echoed = await echo_roundtrip.run(payload)
    print(f"echo ok: {echoed == payload} ({len(echoed)} bytes)")

    blob = os.urandom(256 * 1024)
    info = await hash_in_chunks.run(blob)
    expected = hashlib.sha256(blob).hexdigest()
    print(f"hash ok: {info['sha256'] == expected} (size={info['size']})")
    print(f"  worker tempfile path was: {info['path']}")


if __name__ == "__main__":
    asyncio.run(main())
