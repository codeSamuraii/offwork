"""Run the large project example.

Demonstrates pyfuse's dependency analysis on a multi-file codebase where
only the edge functions carry ``@trace``.  The script:

1. Traces two entry-points (a standalone function and a class method).
2. Serializes the graph and prints the discovered dependency count.
3. Reconstructs executable source for each entry-point.
4. Executes the reconstructed functions through a ``FuseWorker``.
5. Packs and runs a ``Task`` to verify the full worker pipeline.
"""

from __future__ import annotations

from pyfuse import FuseWorker, graph, pack, reconstruct, serialize

# Importing the module registers the @trace functions.
from examples.large_project.pipeline import (  # noqa: F401
    AnalyticsPipeline,
    ingest_and_analyze,
)

SAMPLE_CSV = (
    "name,score,age\n"
    "Alice,88.5,30\n"
    "Bob,72.0,25\n"
    "Charlie,95.2,35\n"
    "Diana,64.8,28\n"
    "Eve,91.0,32\n"
)
NUMERIC_COLS = ["score", "age"]
RANK_COL = "score"


def main() -> None:
    # ── 1.  Inspect the traced graph ──────────────────────────────────────
    g = graph()
    print("=== Traced nodes ===")
    for name, node in sorted(g.nodes.items()):
        deps = ", ".join(node.dependencies) if node.dependencies else "(none)"
        print(f"  {name}  →  {deps}")
    print()

    # ── 2.  Serialize ─────────────────────────────────────────────────────
    full_json = serialize()
    sub_json = serialize(ingest_and_analyze)
    print(f"Full graph JSON length : {len(full_json):>6,} bytes")
    print(f"Subgraph JSON length   : {len(sub_json):>6,} bytes")
    print()

    # ── 3.  Reconstruct source ────────────────────────────────────────────
    source = reconstruct(sub_json, "ingest_and_analyze")
    func_defs = [l for l in source.splitlines() if l.startswith("def ")]
    print(f"=== Reconstructed source has {len(func_defs)} function defs ===")
    for fd in func_defs:
        print(f"  {fd}")
    print()

    # ── 4.  Worker execution ──────────────────────────────────────────────
    worker = FuseWorker(auto_install=False)
    result = worker.execute(
        sub_json, "ingest_and_analyze", SAMPLE_CSV, NUMERIC_COLS, RANK_COL
    )
    print("=== Worker result (first 400 chars) ===")
    print(result[:400])
    print("...")
    print()

    # ── 5.  Pack + run via Task ───────────────────────────────────────────
    task = pack(ingest_and_analyze, SAMPLE_CSV, NUMERIC_COLS, RANK_COL)
    task_result = worker.run(task)
    assert task_result == result, "Task result must match direct execution"
    print("=== Task round-trip: OK ===")
    print()

    # ── 6.  Mermaid visualization ─────────────────────────────────────────
    print("=== Dependency graph (Mermaid) ===")
    print(g.to_mermaid())


if __name__ == "__main__":
    main()
