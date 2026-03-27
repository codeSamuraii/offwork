"""Save a serialized graph to disk, load it later, and reconstruct."""

import csv
import tempfile
from pathlib import Path

from pyfuse import reconstruct, serialize, trace


@trace
def normalize(text: str) -> str:
    """Strip and lowercase text."""
    return text.strip().lower()


@trace
def parse_row(row: list[str]) -> list[str]:
    """Normalize each cell in a CSV row."""
    return [normalize(cell) for cell in row]


@trace
def parse_csv(data: str) -> list[list[str]]:
    """Parse and normalize a CSV document."""
    rows = list(csv.reader(data.splitlines()))
    return [parse_row(row) for row in rows]


if __name__ == "__main__":
    # Serialize and save to a file
    graph_json = serialize()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        f.write(graph_json)
        path = Path(f.name)

    print(f"Graph saved to {path}")

    # Later: load and reconstruct
    loaded = path.read_text()
    source = reconstruct(loaded, "parse_csv")

    print("\n=== Reconstructed from file ===")
    print(source)

    path.unlink()
