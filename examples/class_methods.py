"""Tracing class methods with self.method() dependency detection."""

import csv
import json

from pyfuse import reconstruct, serialize, trace


class DataPipeline:
    @trace
    def read(self, raw: str) -> list[list[str]]:
        """Read CSV data into rows."""
        return list(csv.reader(raw.splitlines()))

    @trace
    def transform(self, rows: list[list[str]]) -> list[dict[str, str]]:
        """Convert rows to list of dicts using first row as headers."""
        if not rows:
            return []
        headers = rows[0]
        return [dict(zip(headers, row)) for row in rows[1:]]

    @trace
    def export(self, raw: str) -> str:
        """Full pipeline: read, transform, export as JSON."""
        rows = self.read(raw)
        records = self.transform(rows)
        return json.dumps(records, indent=2)


if __name__ == "__main__":
    graph = serialize()

    # Reconstructing 'export' pulls in 'read' and 'transform',
    # all wrapped in the DataPipeline class
    source = reconstruct(graph, "export")
    print("=== Reconstructed: export ===")
    print(source)

    # Reconstructing 'read' gives just that method in its class
    source = reconstruct(graph, "read")
    print("=== Reconstructed: read ===")
    print(source)
