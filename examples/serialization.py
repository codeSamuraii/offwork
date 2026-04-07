"""Serialize functions and class methods into self-contained JSON graphs.

No external services needed -- this example runs standalone.

Usage:
    python examples/serialization.py
"""

import csv
import json

from pyfuse import reconstruct, serialize, trace


# -- Standalone functions with auto-discovered dependencies ------------------


def normalize(text: str) -> str:
    """Strip and lowercase -- not decorated, but auto-discovered."""
    return text.strip().lower()


def parse_csv(raw: str) -> list[dict[str, str]]:
    """Parse CSV text into a list of row dicts with normalized values."""
    reader = csv.reader(raw.splitlines())
    rows = list(reader)
    if not rows:
        return []
    headers = rows[0]
    return [
        {normalize(h): normalize(v) for h, v in zip(headers, row)}
        for row in rows[1:]
    ]


@trace
def csv_to_json(raw: str) -> str:
    """Full pipeline: CSV string in, JSON string out."""
    return json.dumps(parse_csv(raw), indent=2)


# -- Class methods with self.method() dependencies --------------------------


class TextProcessor:
    def clean(self, text: str) -> str:
        return text.strip().lower()

    def word_count(self, text: str) -> int:
        return len(self.clean(text).split())

    @trace
    def summarize(self, text: str) -> dict:
        cleaned = self.clean(text)
        return {
            "words": self.word_count(text),
            "chars": len(cleaned),
            "preview": cleaned[:50],
        }


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # --- Function serialization ---
    graph_json = serialize(csv_to_json)

    print("=== Reconstructed: csv_to_json ===")
    print(reconstruct(graph_json, "csv_to_json"))
    # normalize() is auto-discovered and included even though it has no @trace

    # --- Class method serialization ---
    graph_json = serialize()

    print("=== Reconstructed: summarize ===")
    print(reconstruct(graph_json, "summarize"))
    # All three methods are included, wrapped in the TextProcessor class
