"""Inspect email attachments on workers, fanned out from a local poller.

Two concerns kept apart:

* The **poller** (this script's main loop) is stateful: it owns the IMAP
  connection and tracks which UIDs were already seen.  It stays local.
* The **per-attachment work** is pure: bytes in, structured result out.
  That's the offwork task.

The traced entry point ``process_attachment`` is small.  It calls three
plain helpers -- ``_classify``, ``_extract_text``, ``_score_risk`` --
that are not decorated.  offwork discovers them by walking the AST and
ships their source as part of the same task envelope.

Usage:
    offwork worker --backend redis://localhost:6379 --tmp
    python examples/email_attachments.py

The script synthesizes a handful of emails with attachments in-memory
(no IMAP server needed), dispatches each attachment to the worker, and
prints the verdict.
"""

import asyncio
import hashlib
import re
from email import message_from_bytes
from email.message import EmailMessage, Message
from typing import Any

import offwork
from offwork import trace

offwork.connect("local://localhost:9748")


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


# --- local-only mailbox plumbing -----------------------------------------

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


def synthesize_inbox() -> list[Message]:
    """Build a small in-memory inbox with attachments for the demo."""
    samples: list[tuple[str, str, list[tuple[str, bytes]]]] = [
        (
            "alice@example.com",
            "Lunch tomorrow?",
            [("photo.jpg", b"\xff\xd8\xff" + b"\x00" * 4096)],
        ),
        (
            "billing@vendor.com",
            "Invoice INV-9921",
            [("invoice.pdf", b"%PDF-1.4 invoice total: $1,250.00 due upon receipt")],
        ),
        (
            "noreply@scary.example",
            "URGENT: please review attached",
            [("urgent_invoice.scr", b"MZ" + b"\x00" * 2048)],
        ),
        (
            "ops@partner.com",
            "Q1 deck and notes",
            [
                ("deck.pdf", b"%PDF-1.4 strategy review Q1"),
                ("notes.txt", b"meeting notes go here"),
            ],
        ),
    ]

    inbox: list[Message] = []
    for sender, subject, atts in samples:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = "you@example.com"
        msg["Subject"] = subject
        msg.set_content("See attached.")
        for name, payload in atts:
            msg.add_attachment(
                payload, maintype="application", subtype="octet-stream", filename=name,
            )
        inbox.append(message_from_bytes(bytes(msg)))
    return inbox


async def main() -> None:
    messages = synthesize_inbox()
    attachments = [a for m in messages for a in iter_attachments(m)]
    print(f"Inbox: {len(messages)} messages, {len(attachments)} attachments")

    results = await process_attachment.map(attachments)
    for r in results:
        flag = "!!" if r["risk"] >= 50 else "ok"
        print(f"  [{flag}] {r['filename']:<24} {r['kind']:<10} "
              f"size={r['size']:<6} risk={r['risk']}")


if __name__ == "__main__":
    asyncio.run(main())
