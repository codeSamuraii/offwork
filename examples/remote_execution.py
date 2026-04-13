import asyncio
import math

import pyfuse
from pyfuse import trace

pyfuse.connect("local://localhost:9748")

def add(a: int, b: int) -> int:
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

async def main() -> None:
    result = await hypotenuse.run(3.0, 4.0)
    print(result)  # 5.0

asyncio.run(main())
