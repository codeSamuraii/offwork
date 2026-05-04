"""End-to-end smoke test for the cloud broker.

Registers (or reuses) a user, submits a task, and waits for the result.
The control plane provisions a Kubernetes worker pod on demand.
"""

import asyncio
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pyfuse
from pyfuse import trace

API_BASE = "http://localhost:8000"


def register(email: str, password: str) -> dict[str, str]:
    payload = json.dumps({"email": email, "password": password}).encode()
    req = Request(
        f"{API_BASE}/api/v1/users/register",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req) as resp:
        return json.loads(resp.read().decode())


@trace
def add(a: int, b: int) -> int:
    return a + b


async def main() -> None:
    email = sys.argv[1] if len(sys.argv) > 1 else "smoke@example.com"
    password = "smokepassword123"
    try:
        info = register(email, password)
    except HTTPError as exc:
        if exc.code != 409:
            raise
        print(f"[i] User {email} already exists; this script needs a fresh email.")
        sys.exit(1)
    print(f"[+] Registered {info['email']}; broker: {info['broker_url']}")

    pyfuse.connect(info["broker_url"])
    print("[+] Submitting task...")
    result = await asyncio.wait_for(add.run(2, 40), timeout=300)
    print(f"[+] add(2, 40) = {result}")


if __name__ == "__main__":
    asyncio.run(main())
