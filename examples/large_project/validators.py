"""Data validation and cleaning functions."""

from __future__ import annotations

from examples.large_project.models import DataTable


def is_numeric(value: str) -> bool:
    """Check if a string can be interpreted as a number."""
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def clean_value(value: str) -> str:
    """Strip whitespace and normalize empty strings."""
    cleaned = value.strip()
    return cleaned if cleaned else ""


def validate_row(row: list[str], expected_cols: int) -> bool:
    """Check that a row has the expected number of columns."""
    return len(row) == expected_cols


def remove_invalid_rows(table: DataTable) -> DataTable:
    """Remove rows that don't match the header column count."""
    expected = table.num_cols()
    valid_rows = [row for row in table.rows if validate_row(row, expected)]
    return DataTable(table.headers, valid_rows)


def clean_table(table: DataTable) -> DataTable:
    """Strip whitespace from all cells and remove invalid rows."""
    cleaned_rows = [[clean_value(cell) for cell in row] for row in table.rows]
    cleaned = DataTable(table.headers, cleaned_rows)
    return remove_invalid_rows(cleaned)


def extract_numeric_column(table: DataTable, col_name: str) -> list[float]:
    """Extract a column's values as floats, skipping non-numeric entries."""
    raw = table.column(col_name)
    return [float(v) for v in raw if is_numeric(v)]


def detect_outliers(
    values: list[float], threshold: float = 2.0
) -> list[tuple[int, float]]:
    """Return indices and values of outliers beyond `threshold` std devs."""
    if len(values) < 2:
        return []
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    sd = variance**0.5
    if sd == 0:
        return []
    return [(i, v) for i, v in enumerate(values) if abs(v - mean) / sd > threshold]
