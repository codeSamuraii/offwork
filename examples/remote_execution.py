import math
import yaml

import pyfuse
from pyfuse import trace

pyfuse.connect("redis://localhost:6379")

def add(a: int, b: int) -> int:
    with open('sample.yml', 'r') as f:
        yml = yaml.safe_load(f)
    print(yml)
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    return math.sqrt(add(a**2, b**2))

future = hypotenuse.run(3.0, 4.0)
print(future.result())  # 5.0
