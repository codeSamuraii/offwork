"""Data transformation functions."""

from __future__ import annotations

from examples.large_project.math_utils import normalize, z_scores
from examples.large_project.models import ColumnStats, DataTable
from examples.large_project.validators import (
    clean_table,
    detect_outliers,
    extract_numeric_column,
)


def compute_column_stats(table: DataTable, col_name: str) -> ColumnStats:
    """Compute statistics for a numeric column."""
    values = extract_numeric_column(table, col_name)
    return ColumnStats(col_name, values)


def compute_all_stats(
    table: DataTable, numeric_cols: list[str]
) -> dict[str, ColumnStats]:
    """Compute statistics for all specified numeric columns."""
    return {col: compute_column_stats(table, col) for col in numeric_cols}


def normalize_column(table: DataTable, col_name: str) -> list[float]:
    """Normalize a numeric column to [0, 1]."""
    values = extract_numeric_column(table, col_name)
    return normalize(values)


def standardize_column(table: DataTable, col_name: str) -> list[float]:
    """Standardize a numeric column to z-scores."""
    values = extract_numeric_column(table, col_name)
    return z_scores(values)


def filter_outliers(table: DataTable, col_name: str, threshold: float = 2.0) -> DataTable:
    """Remove rows where a column value is an outlier."""
    values = extract_numeric_column(table, col_name)
    outlier_indices = {idx for idx, _ in detect_outliers(values, threshold)}
    col_idx = table.headers.index(col_name)

    kept_rows: list[list[str]] = []
    numeric_index = 0
    for row in table.rows:
        if col_idx < len(row):
            try:
                float(row[col_idx])
                if numeric_index not in outlier_indices:
                    kept_rows.append(row)
                numeric_index += 1
            except ValueError:
                kept_rows.append(row)
        else:
            kept_rows.append(row)
    return DataTable(table.headers, kept_rows)


def prepare_data(table: DataTable) -> DataTable:
    """Clean and validate a raw data table."""
    return clean_table(table)


def add_rank_column(table: DataTable, col_name: str) -> DataTable:
    """Add a rank column based on a numeric column (ascending)."""
    values = extract_numeric_column(table, col_name)
    sorted_indices = sorted(range(len(values)), key=lambda i: values[i])
    rank_map = {idx: rank + 1 for rank, idx in enumerate(sorted_indices)}

    new_headers = table.headers + [f"{col_name}_rank"]
    col_idx = table.headers.index(col_name)
    new_rows: list[list[str]] = []
    numeric_idx = 0
    for row in table.rows:
        if col_idx < len(row):
            try:
                float(row[col_idx])
                rank = rank_map.get(numeric_idx, 0)
                new_rows.append(row + [str(rank)])
                numeric_idx += 1
            except ValueError:
                new_rows.append(row + [""])
        else:
            new_rows.append(row + [""])
    return DataTable(new_headers, new_rows)
