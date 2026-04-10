"""Run functions on a remote worker -- functions, methods, and third-party deps.

The worker has no prior knowledge of the code. It reconstructs the source,
installs missing packages, and executes the function.

Requires Redis on localhost:6379.  Install: pip install redis

Usage:
    # Terminal 1 -- start a worker
    python -m pyfuse worker --backend redis://localhost:6379

    # Terminal 2 -- run this script
    python examples/remote_execution.py
"""

import math

import pyfuse
from pyfuse import trace
from pyfuse.worker.deps import install_package_as


# -- Plain functions ---------------------------------------------------------


def add(a: int, b: int) -> int:
    return a + b


@trace
def hypotenuse(a: float, b: float) -> float:
    """Calls add() -- the dependency is serialized automatically."""
    return math.sqrt(add(a**2, b**2))


# -- Class methods -----------------------------------------------------------


class Greeter:
    @trace
    def greet(self, name: str) -> str:
        return self.format_greeting(f"Hello, {name}!")

    def format_greeting(self, msg: str) -> str:
        return f"*** {msg} ***"


# -- Third-party package dependency ------------------------------------------

# install_package_as tells the worker which pip package to install
# for an import that doesn't match the package name.
# At runtime this is a no-op -- the import runs normally.
with install_package_as("PyYAML"):
    import yaml


@trace
def to_yaml(data: object) -> str:
    """The worker will auto-install PyYAML before executing this."""
    return yaml.dump(data, default_flow_style=False)


# -- Retry and timeout -------------------------------------------------------


@trace(timeout=10, retries=2)
def fragile_add(a: int, b: int) -> int:
    """Retried up to 2 times, each attempt limited to 10 seconds."""
    return a + b


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pyfuse.connect("redis://localhost:6379")

    # Plain functions
    print("--- Functions ---")
    print(f"  hypotenuse(3, 4)    = {hypotenuse.run(3.0, 4.0).result()}")

    # Class methods
    print("\n--- Class methods ---")
    g = Greeter()
    print(f"  greet('pyfuse')     = {g.greet.run(g, 'pyfuse').result()}")

    # Third-party package
    print("\n--- Third-party deps ---")
    print(f"  to_yaml(dict)       =\n{to_yaml.run({'framework': 'pyfuse', 'version': 2}).result()}")

    # Retry + timeout
    print("--- Retry + timeout ---")
    future = fragile_add.run(10, 20)
    print(f"  fragile_add(10, 20) = {future.result()}")

    pyfuse.disconnect()
