import asyncio
import math
from typing import overload

import offwork
from offwork import trace

offwork.connect("local://localhost:9748")

@overload
def add(a: int, b: int) -> int: ...

@overload
def add(a: float, b: float) -> float: ...

def add(a: int | float, b: int | float) -> int | float:
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

async def main() -> None:
    result = await hypotenuse.run(3.0, 4.0)
    print(result)  # 5.0

asyncio.run(main())
