"""CSV and text parsing utilities."""

from __future__ import annotations

import csv
import io

from examples.large_project.models import DataTable


def parse_csv_text(raw: str) -> DataTable:
    """Parse raw CSV text into a DataTable."""
    reader = csv.reader(io.StringIO(raw))
    all_rows = list(reader)
    if not all_rows:
        return DataTable([], [])
    headers = [h.strip() for h in all_rows[0]]
    data = [row for row in all_rows[1:] if any(cell.strip() for cell in row)]
    return DataTable(headers, data)


def split_sections(raw: str, delimiter: str = "---") -> list[str]:
    """Split text into sections by a delimiter line."""
    sections: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        if line.strip() == delimiter:
            if current:
                sections.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return sections


def extract_key_value_pairs(text: str, sep: str = "=") -> dict[str, str]:
    """Parse lines of 'key=value' into a dictionary."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        if sep in line:
            key, _, value = line.partition(sep)
            result[key.strip()] = value.strip()
    return result
