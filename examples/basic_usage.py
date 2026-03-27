"""Basic pyfuse usage: trace functions, serialize, and reconstruct."""

import csv
import json

from pyfuse import reconstruct, serialize, trace


@trace
def parse_csv(csv_data: str) -> dict:
    """Parses CSV data and returns a dictionary with row indices as keys."""
    reader = csv.reader(csv_data.splitlines())
    rows = list(reader)
    if not rows:
        return {}
    headers = rows[0]
    return {i: dict(zip(headers, row)) for i, row in enumerate(rows[1:])}


@trace
def to_json(data: dict) -> str:
    """Converts a dictionary to a JSON string."""
    return json.dumps(data, indent=2)


@trace
def csv_to_json(csv_data: str) -> str:
    """Full pipeline: parse CSV then convert to JSON."""
    table = parse_csv(csv_data)
    return to_json(table)


if __name__ == "__main__":
    # Serialize the full dependency graph
    graph = serialize()
    print("=== Serialized graph ===")
    print(graph)
    print()

    # Reconstruct source for csv_to_json (includes all dependencies)
    source = reconstruct(graph, "csv_to_json")
    print("=== Reconstructed: csv_to_json ===")
    print(source)

    # Reconstruct source for parse_csv alone (only its own dependencies)
    source = reconstruct(graph, "parse_csv")
    print("=== Reconstructed: parse_csv ===")
    print(source)
