import math
import yaml
import textwrap
from io import StringIO

import pyfuse
from pyfuse import trace

pyfuse.connect("shm://localhost:9847")

def load_config() -> dict:
    config_content = textwrap.dedent(
    """
    test_yaml:
        host: localhost
        port: 5432
        name: mydb
    """
    )
    yml = yaml.safe_load(StringIO(config_content))
    return yml

def add(a: int, b: int) -> int:
    return a + b

@trace
def hypotenuse(a: float, b: float) -> float:
    load_config()
    return math.sqrt(add(a**2, b**2))

future = hypotenuse.run(3.0, 4.0)
print(future.result())  # 5.0
