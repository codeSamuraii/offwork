"""Data models for the analytics pipeline."""

from __future__ import annotations


class ColumnStats:
    """Statistics for a single data column."""

    def __init__(self, name: str, values: list[float]) -> None:
        self.name = name
        self.values = values
        self.count = len(values)

    def mean(self) -> float:
        if not self.values:
            return 0.0
        return sum(self.values) / len(self.values)

    def variance(self) -> float:
        if len(self.values) < 2:
            return 0.0
        m = self.mean()
        return sum((v - m) ** 2 for v in self.values) / (len(self.values) - 1)

    def min_val(self) -> float:
        return min(self.values) if self.values else 0.0

    def max_val(self) -> float:
        return max(self.values) if self.values else 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "mean": round(self.mean(), 4),
            "variance": round(self.variance(), 4),
            "min": self.min_val(),
            "max": self.max_val(),
        }


class DataTable:
    """A simple table with named columns."""

    def __init__(self, headers: list[str], rows: list[list[str]]) -> None:
        self.headers = headers
        self.rows = rows

    def column(self, name: str) -> list[str]:
        idx = self.headers.index(name)
        return [row[idx] for row in self.rows if idx < len(row)]

    def num_rows(self) -> int:
        return len(self.rows)

    def num_cols(self) -> int:
        return len(self.headers)

    def select(self, *names: str) -> DataTable:
        indices = [self.headers.index(n) for n in names]
        new_headers = list(names)
        new_rows = [[row[i] for i in indices if i < len(row)] for row in self.rows]
        return DataTable(new_headers, new_rows)

    def to_dict_list(self) -> list[dict[str, str]]:
        return [dict(zip(self.headers, row)) for row in self.rows]
