"""Chunked CSV transform with pandas, fanned out across workers.

A daily ETL pattern: split a large CSV into row-ranges, send each chunk
to a worker, merge the partial results locally.  Each task is pure --
bytes in, dict out.

The traced entry point ``summarize_chunk`` calls three plain helpers
(``_load``, ``_clean``, ``_aggregate``).  None of them are decorated;
away picks them up by walking the call graph of the entry point and
sends them along with the task.

Usage:
    away worker --backend redis://localhost:6379 --tmp
    python -m away run --tmp examples/csv_etl.py
"""

import asyncio
import csv
import io
import random

import pandas as pd

import away
from away import trace

away.connect("local://localhost:9748")


# --- helpers (auto-discovered) --------------------------------------------

def _load(csv_bytes: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(csv_bytes))


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["amount"])
    df = df.assign(amount=df["amount"].astype(float))
    return df[df["amount"] >= 0]


def _aggregate(df: pd.DataFrame) -> dict[str, float | int]:
    if df.empty:
        return {"rows": 0, "sum": 0.0, "min": 0.0, "max": 0.0}
    return {
        "rows": int(len(df)),
        "sum": float(df["amount"].sum()),
        "min": float(df["amount"].min()),
        "max": float(df["amount"].max()),
    }


# --- entry point ----------------------------------------------------------

@trace
def summarize_chunk(csv_bytes: bytes) -> dict[str, float | int]:
    """Clean a CSV chunk and return per-chunk aggregates."""
    return _aggregate(_clean(_load(csv_bytes)))


# --- local-only test data + dispatch -------------------------------------

def make_synthetic_csv(rows: int, seed: int) -> bytes:
    rng = random.Random(seed)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "amount", "category"])
    for i in range(rows):
        amount = "" if rng.random() < 0.02 else f"{rng.gauss(50, 20):.2f}"
        w.writerow([i, amount, rng.choice(["a", "b", "c"])])
    return buf.getvalue().encode()


def split_csv(blob: bytes, chunks: int) -> list[bytes]:
    text = blob.decode()
    header, _, body = text.partition("\n")
    lines = body.splitlines()
    step = max(1, len(lines) // chunks)
    return [
        "\n".join([header, *lines[i : i + step]]).encode()
        for i in range(0, len(lines), step)
    ]


async def main() -> None:
    blob = make_synthetic_csv(rows=50_000, seed=7)
    chunks = split_csv(blob, chunks=8)
    print(f"Dispatching {len(chunks)} chunks ({len(blob) // 1024} KiB total)")

    partials = await summarize_chunk.map([(c,) for c in chunks])

    total_rows = sum(p["rows"] for p in partials)
    total_sum = sum(p["sum"] for p in partials)
    print(f"rows kept: {total_rows}")
    print(f"sum:       {total_sum:.2f}")
    print(f"avg:       {total_sum / total_rows:.2f}")
    print(f"min/max:   {min(p['min'] for p in partials):.2f} / "
          f"{max(p['max'] for p in partials):.2f}")


if __name__ == "__main__":
    asyncio.run(main())
