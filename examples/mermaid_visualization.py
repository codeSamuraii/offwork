"""Visualize the dependency graph as a Mermaid flowchart."""

import csv
import json

from pyfuse import analyze, trace


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
    graph = analyze()

    # Full graph
    print("=== Full graph ===")
    print(graph.to_mermaid())

    # Subgraph scoped to csv_to_json
    print("=== Subgraph: csv_to_json ===")
    print(graph.to_mermaid(csv_to_json))

    # Left-to-right layout
    print("=== Left-to-right ===")
    print(graph.to_mermaid(direction="LR"))
