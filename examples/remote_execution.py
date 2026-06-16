import asyncio
import math

import offwork

offwork.connect("local://localhost:9748")

def inverse(x: float) -> float:
    return 1 / x

@offwork.task
def inverse_root(n: float) -> float:
    return inverse(math.sqrt(n))

async def main() -> None:
    result = await inverse_root.run(4)
    print(result)  # 0.5

asyncio.run(main())
