"""Remote execution with pyfuse -- the simplest path.

Requires a Redis server running on localhost:6379.
Install the redis package: pip install redis

Usage:
    # Terminal 1 -- start a worker
    python -m pyfuse worker --backend redis://localhost:6379

    # Terminal 2 -- run this script
    python examples/remote_execution.py
"""
import math

import pyfuse
from pyfuse import trace


@trace
def add(a: int, b: int) -> int:
    return a + b


@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))


@trace
def greet(name: str) -> str:
    return f"Hello, {name}!"


if __name__ == "__main__":
    pyfuse.connect("redis://localhost:6379")

    futures = [
        ("add(3, 4)", add.run(3, 4)),
        ("hypotenuse(3, 4)", hypotenuse.run(3.0, 4.0)),
        ("greet('pyfuse')", greet.run("pyfuse")),
    ]

    for label, future in futures:
        print(f"  {label} = {future.result()}")

    pyfuse.disconnect()
