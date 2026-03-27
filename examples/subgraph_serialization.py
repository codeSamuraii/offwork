"""Serialize only a subgraph instead of the full registry."""

import csv
import json
import os

from pyfuse import FuseGraph, reconstruct, serialize, trace


@trace
def read_file(path: str) -> str:
    """Read a file from disk."""
    with open(path) as f:
        return f.read()


@trace
def parse_csv(data: str) -> list[list[str]]:
    """Parse CSV text into rows."""
    return list(csv.reader(data.splitlines()))


@trace
def to_json(data: object) -> str:
    """Serialize data to JSON."""
    return json.dumps(data, indent=2)


@trace
def get_env(key: str) -> str:
    """Read an environment variable."""
    return os.getenv(key, "")


if __name__ == "__main__":
    # Serialize the full graph (all 4 functions)
    full_graph = serialize()
    print(f"Full graph has {full_graph.count('qualified_name')} nodes")

    # Serialize only parse_csv and its dependencies (just parse_csv itself)
    sub_graph = serialize(parse_csv)
    print(f"parse_csv subgraph has {sub_graph.count('qualified_name')} node(s)")
    print()

    # You can also pass the function by name
    sub_graph2 = FuseGraph.default().serialize("to_json")
    source = reconstruct(sub_graph2, "to_json")
    print("=== Reconstructed: to_json (from subgraph) ===")
    print(source)
