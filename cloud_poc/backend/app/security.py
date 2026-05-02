"""Security helpers for the local cloud proof-of-concept."""

import os
import hmac
import hashlib
import secrets
from typing import Any

from fastapi import HTTPException, Request, status


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    real_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), real_salt, 600_000)
    return f"{real_salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    salt_hex, digest_hex = encoded.split(":", 1)
    actual = hash_password(password, salt=bytes.fromhex(salt_hex)).split(":", 1)[1]
    return hmac.compare_digest(actual, digest_hex)


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


async def require_api_key(request: Request) -> str:
    api_key = request.headers.get("X-Pyfuse-API-Key") or request.query_params.get("api_key")
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")
    return api_key


async def require_user(request: Request, api_key: str | None = None) -> dict[str, Any]:
    key = api_key or await require_api_key(request)
    user = request.app.state.db.users.find_one({"api_key": key}, {"password_hash": 0})
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    user["id"] = str(user["_id"])
    return user
