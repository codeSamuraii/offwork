"""Output formatting functions."""

from __future__ import annotations

import json

from examples.large_project.models import ColumnStats, DataTable


def stats_to_json(stats: dict[str, ColumnStats]) -> str:
    """Serialize column statistics to a JSON string."""
    payload = {name: cs.to_dict() for name, cs in stats.items()}
    return json.dumps(payload, indent=2)


def table_to_json(table: DataTable) -> str:
    """Serialize a DataTable to a JSON array of objects."""
    return json.dumps(table.to_dict_list(), indent=2)


def table_to_csv(table: DataTable) -> str:
    """Serialize a DataTable to CSV text."""
    lines = [",".join(table.headers)]
    for row in table.rows:
        lines.append(",".join(row))
    return "\n".join(lines)


def build_summary(
    table: DataTable,
    stats: dict[str, ColumnStats],
    outlier_report: dict[str, list[tuple[int, float]]],
) -> str:
    """Build a human-readable summary report."""
    parts: list[str] = []
    parts.append(f"Dataset: {table.num_rows()} rows x {table.num_cols()} columns")
    parts.append(f"Columns: {', '.join(table.headers)}")
    parts.append("")

    for col_name, cs in stats.items():
        d = cs.to_dict()
        parts.append(f"  {col_name}:")
        parts.append(f"    count={d['count']}  mean={d['mean']}  var={d['variance']}")
        parts.append(f"    min={d['min']}  max={d['max']}")

    parts.append("")
    for col_name, outliers in outlier_report.items():
        if outliers:
            parts.append(f"  Outliers in {col_name}: {len(outliers)}")
            for idx, val in outliers[:5]:
                parts.append(f"    row {idx}: {val}")
        else:
            parts.append(f"  No outliers in {col_name}")

    return "\n".join(parts)


def format_report(
    summary: str,
    stats_json: str,
    table_json: str,
) -> str:
    """Combine all report sections into one output string."""
    sep = "=" * 60
    return "\n".join([
        sep,
        "ANALYTICS REPORT",
        sep,
        "",
        summary,
        "",
        sep,
        "STATISTICS (JSON)",
        sep,
        stats_json,
        "",
        sep,
        "DATA (JSON)",
        sep,
        table_json,
    ])
