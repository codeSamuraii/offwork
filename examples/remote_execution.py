import math

import pyfuse
from pyfuse import trace

pyfuse.connect("shm://localhost:9847")

def add(a: int, b: int) -> int:
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

future = hypotenuse.run(3.0, 4.0)
print(future.result())  # 5.0