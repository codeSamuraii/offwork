"""Driver: run an example file against the cloud broker.

Usage:
    python cloud_poc/run_example.py <broker_url> <example_path>

Monkey-patches ``pyfuse.connect`` so each example connects to the cloud broker
instead of the URL it has hard-coded.
"""

import sys
import runpy

import pyfuse

cloud_url = sys.argv[1]
example_path = sys.argv[2]

_original_connect = pyfuse.connect


def _patched_connect(_url: str) -> None:
    return _original_connect(cloud_url)


pyfuse.connect = _patched_connect  # type: ignore[assignment]

runpy.run_path(example_path, run_name="__main__")
