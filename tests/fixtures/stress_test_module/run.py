"""Run the stress-test module through away: serialize, reconstruct, execute on a Worker.

Usage:
    python tests/fixtures/stress_test_module/run.py
"""

import asyncio

from away import pack, serialize, reconstruct, Graph
from away.worker.worker import Worker

from tests.fixtures.stress_test_module import pipeline
from tests.fixtures.stress_test_module.generators import generate_measurements


async def async_main() -> None:
    # ── 1. Run the full pipeline locally (normal call) ───────────────
    print("--- Local call ---")
    report = pipeline.run_full_pipeline(sensor_count=3, readings_per_sensor=20, seed=42)
    print(report[:200], "...\n")

    # ── 2. Inspect what away captured ──────────────────────────────
    graph = Graph.default()
    print(f"--- Graph: {len(graph.nodes)} nodes ---")
    for qname in sorted(graph.nodes):
        node = graph.nodes[qname]
        tag = "class" if node.owner_class else "func"
        print(f"  [{tag}] {qname}  ({len(node.dependencies)} deps)")
    print()

    # ── 3. Serialize and reconstruct ─────────────────────────────────
    graph_json = serialize(pipeline.run_full_pipeline)
    source = reconstruct(graph_json, "run_full_pipeline")
    print(f"--- Reconstructed source: {source.count(chr(10))} lines ---")
    for line in source.splitlines()[:15]:
        print(f"  {line}")
    print("  ...\n")

    # ── 4. Execute on a Worker (simulates remote execution) ──────────
    print("--- Worker execution ---")
    task = pack(pipeline.run_full_pipeline, 3, 20, seed=42)
    print(f"  Task ID:  {task.task_id}")
    print(f"  Function: {task.function_name}")

    worker = Worker(auto_install=False)
    result = await worker.run(task)
    print(f"  Output:   {len(result)} chars")
    print(f"  Cache:    {worker.cache_info()}")
    print()

    # ── 5. Same for a sub-pipeline ───────────────────────────────────
    print("--- Worker: run_analysis_only ---")
    measurements = generate_measurements(2, 10, seed=7)
    task = pack(pipeline.run_analysis_only, measurements)
    result = await worker.run(task)
    print(f"  Output:   {result[:80]}...")
    print(f"  Cache:    {worker.cache_info()}")
    print()

    # ── 6. Closure-captured pipeline ─────────────────────────────────
    print("--- Worker: high_value_analysis (closure) ---")
    task = pack(pipeline.high_value_analysis, measurements)
    result = await worker.run(task)
    print(f"  Output:   {result}")
    print()

    print("Done.")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
