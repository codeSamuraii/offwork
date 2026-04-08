"""Analytics pipeline – only edge functions are ``@trace``-decorated.

The internal helpers (parsing, validation, math, formatting) are plain
Python functions spread across multiple modules.  PyFuse's auto-discovery
walks the call graph from the traced entry-points and pulls in every
transitive dependency automatically.

Dependency chains exercised here
--------------------------------
``AnalyticsPipeline.run_report``
  → ``full_report``
    → ``analyze_table``
      → ``compute_all_stats``  →  ``extract_numeric_column``  →  ``is_numeric``
      → ``detect_outliers``    →  ``extract_numeric_column``
    → ``prepare_and_rank``
      → ``prepare_data``       →  ``clean_table``  →  ``remove_invalid_rows`` / ``clean_value``
      → ``add_rank_column``    →  ``extract_numeric_column``
    → ``build_summary``
    → ``stats_to_json``  →  ``ColumnStats.to_dict``
    → ``table_to_json``  →  ``DataTable.to_dict_list``
    → ``format_report``

``ingest_and_analyze``
  → ``parse_csv_text``  →  ``DataTable``
  → ``full_report``     →  … (same subgraph as above)
"""

from __future__ import annotations

from examples.large_project.formatters import (
    build_summary,
    format_report,
    stats_to_json,
    table_to_json,
)
from examples.large_project.models import ColumnStats, DataTable
from examples.large_project.parsers import parse_csv_text
from examples.large_project.transforms import (
    add_rank_column,
    compute_all_stats,
    filter_outliers,
    prepare_data,
)
from examples.large_project.validators import detect_outliers, extract_numeric_column

from pyfuse import trace


# ── Internal (untraced) helpers ─────────────────────────────────────────────


def analyze_table(
    table: DataTable,
    numeric_cols: list[str],
) -> tuple[dict[str, ColumnStats], dict[str, list[tuple[int, float]]]]:
    """Compute statistics and detect outliers for every numeric column."""
    stats = compute_all_stats(table, numeric_cols)
    outlier_report: dict[str, list[tuple[int, float]]] = {}
    for col in numeric_cols:
        values = extract_numeric_column(table, col)
        outlier_report[col] = detect_outliers(values)
    return stats, outlier_report


def prepare_and_rank(table: DataTable, rank_col: str) -> DataTable:
    """Clean, validate, and add a rank column."""
    cleaned = prepare_data(table)
    return add_rank_column(cleaned, rank_col)


def full_report(
    table: DataTable,
    numeric_cols: list[str],
    rank_col: str,
) -> str:
    """End-to-end analysis: stats → outliers → rank → formatted report."""
    stats, outlier_report = analyze_table(table, numeric_cols)
    ranked = prepare_and_rank(table, rank_col)
    filtered = filter_outliers(ranked, rank_col)

    summary = build_summary(filtered, stats, outlier_report)
    s_json = stats_to_json(stats)
    t_json = table_to_json(filtered)
    return format_report(summary, s_json, t_json)


# ── Traced edge functions ──────────────────────────────────────────────────


@trace
def ingest_and_analyze(raw_csv: str, numeric_cols: list[str], rank_col: str) -> str:
    """Parse raw CSV text and produce a full analytics report.

    This is the main entry-point.  The entire dependency tree – parsers,
    validators, math helpers, transforms, formatters – is reachable from
    here and will be auto-discovered by pyfuse.
    """
    table = parse_csv_text(raw_csv)
    return full_report(table, numeric_cols, rank_col)


class AnalyticsPipeline:
    """Wraps the same logic inside a class for ``self.method()`` tracing."""

    @trace
    def run_report(
        self,
        raw_csv: str,
        numeric_cols: list[str],
        rank_col: str,
    ) -> str:
        """Class-based entry-point that delegates to ``full_report``."""
        table = parse_csv_text(raw_csv)
        return full_report(table, numeric_cols, rank_col)
