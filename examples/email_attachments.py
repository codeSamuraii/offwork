"""Poll an IMAP mailbox locally, fan attachment work out to workers.

Two concerns kept apart:

* The **poller** (this script's main loop) is stateful: it owns the IMAP
  connection and tracks which UIDs were already seen.  It stays local.
* The **per-attachment work** is pure: bytes in, structured result out.
  That's the pyfuse task.

The traced entry point ``process_attachment`` is small.  It calls three
plain helpers -- ``_classify``, ``_extract_text``, ``_score_risk`` --
that are not decorated.  pyfuse discovers them by walking the AST and
ships their source as part of the same task envelope.

Usage:
    pyfuse worker --backend redis://localhost:6379 --tmp
    python -m pyfuse run --tmp examples/email_attachments.py
"""

import asyncio
import hashlib
import imaplib
import re
from email import message_from_bytes
from email.message import Message
from typing import Any

import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")


# --- helpers (auto-discovered) --------------------------------------------

DANGEROUS_EXTS = {".exe", ".js", ".vbs", ".scr", ".bat"}
SUSPICIOUS_KEYWORDS = ("invoice", "wire", "urgent", "password")


def _classify(filename: str) -> str:
    name = filename.lower()
    for ext in DANGEROUS_EXTS:
        if name.endswith(ext):
            return "executable"
    if name.endswith((".pdf", ".doc", ".docx")):
        return "document"
    if name.endswith((".png", ".jpg", ".jpeg", ".gif")):
        return "image"
    return "other"


def _extract_text(payload: bytes, kind: str) -> str:
    if kind != "document":
        return ""
    # Stub: real version would shell out to pdftotext / antiword / etc.
    text = payload.decode("utf-8", errors="ignore")
    return re.sub(r"\s+", " ", text)[:2000]


def _score_risk(filename: str, kind: str, text: str) -> int:
    score = 0
    if kind == "executable":
        score += 80
    if any(kw in text.lower() for kw in SUSPICIOUS_KEYWORDS):
        score += 25
    if any(kw in filename.lower() for kw in SUSPICIOUS_KEYWORDS):
        score += 15
    return min(score, 100)


# --- entry point ----------------------------------------------------------

@trace
def process_attachment(filename: str, payload: bytes) -> dict[str, Any]:
    """Inspect a single attachment.  Calls the helpers above on the worker."""
    kind = _classify(filename)
    text = _extract_text(payload, kind)
    return {
        "filename": filename,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "kind": kind,
        "risk": _score_risk(filename, kind, text),
    }


# --- local-only IMAP plumbing --------------------------------------------

def iter_attachments(msg: Message) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get("Content-Disposition") is None:
            continue
        name = part.get_filename()
        payload = part.get_payload(decode=True)
        if name and isinstance(payload, bytes):
            out.append((name, payload))
    return out


def fetch_unseen(host: str, user: str, password: str) -> list[Message]:
    cli = imaplib.IMAP4_SSL(host)
    try:
        cli.login(user, password)
        cli.select("INBOX")
        _, data = cli.search(None, "UNSEEN")
        msgs: list[Message] = []
        for uid in data[0].split():
            _, parts = cli.fetch(uid, "(RFC822)")
            raw = parts[0][1] if parts and parts[0] else None
            if isinstance(raw, bytes):
                msgs.append(message_from_bytes(raw))
        return msgs
    finally:
        cli.logout()


async def main() -> None:
    host, user, password = "imap.example.com", "you@example.com", "secret"

    while True:
        try:
            messages = await asyncio.to_thread(fetch_unseen, host, user, password)
        except Exception as exc:
            print(f"poll failed: {exc}")
            await asyncio.sleep(30)
            continue

        attachments = [a for m in messages for a in iter_attachments(m)]
        if not attachments:
            await asyncio.sleep(30)
            continue

        results = await process_attachment.map(attachments)
        for r in results:
            flag = "!!" if r["risk"] >= 50 else "ok"
            print(f"  [{flag}] {r['filename']:<40} {r['kind']:<10} risk={r['risk']}")

        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
