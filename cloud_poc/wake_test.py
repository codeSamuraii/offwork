"""Wake-from-zero test: re-submit after the worker has been scaled down."""

import asyncio
import json
import sys
from urllib.request import Request, urlopen

import pyfuse
from pyfuse import trace

API_KEY = sys.argv[1]


@trace
def cube(x: int) -> int:
    return x * x * x


async def main() -> None:
    req = Request(
        "http://localhost:8000/api/v1/users/me",
        headers={"X-Pyfuse-API-Key": API_KEY},
    )
    info = json.loads(urlopen(req).read())
    print(f"[+] user: {info['email']}")
    pyfuse.connect(info["broker_url"])
    print("[+] submitting cube(7)...")
    result = await asyncio.wait_for(cube.run(7), timeout=300)
    print(f"[+] cube(7) = {result}")


if __name__ == "__main__":
    asyncio.run(main())
