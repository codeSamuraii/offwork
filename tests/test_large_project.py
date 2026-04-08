"""End-to-end test for the large multi-file example project.

Validates that pyfuse correctly discovers, serializes, reconstructs,
and executes a deep dependency graph that spans multiple modules when
only the edge functions carry ``@trace``.
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest

from pyfuse import FuseWorker, graph, pack, reconstruct, serialize
from pyfuse._store import FuseStore

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

# Modules that must be reloaded to re-trigger @trace against a fresh graph.
_EXAMPLE_MODULES = [
    "examples.large_project.models",
    "examples.large_project.parsers",
    "examples.large_project.math_utils",
    "examples.large_project.validators",
    "examples.large_project.transforms",
    "examples.large_project.formatters",
    "examples.large_project.pipeline",
]


@pytest.fixture(autouse=True)
def _reload_example_modules() -> None:
    """Re-import the example pipeline so @trace runs against the fresh graph."""
    for mod_name in _EXAMPLE_MODULES:
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    for mod_name in _EXAMPLE_MODULES:
        importlib.import_module(mod_name)


def _get_ingest_func():
    """Return the current traced ``ingest_and_analyze`` wrapper."""
    mod = sys.modules["examples.large_project.pipeline"]
    return mod.ingest_and_analyze


class TestAutoDiscovery:
    """Auto-discovery should pull in all transitive dependencies."""

    def test_traced_entry_points_registered(self) -> None:
        g = graph()
        names = set(g.nodes)
        assert any("ingest_and_analyze" in n for n in names)
        assert any("run_report" in n for n in names)

    def test_deep_helpers_discovered(self) -> None:
        g = graph()
        names = set(g.nodes)
        # Functions from validators.py (3 levels deep)
        assert any("is_numeric" in n for n in names)
        assert any("clean_value" in n for n in names)
        assert any("validate_row" in n for n in names)
        # Functions from transforms.py
        assert any("compute_column_stats" in n for n in names)
        assert any("add_rank_column" in n for n in names)
        # Functions from formatters.py
        assert any("build_summary" in n for n in names)
        assert any("format_report" in n for n in names)

    def test_minimum_node_count(self) -> None:
        """The graph should have many nodes from auto-discovery."""
        g = graph()
        assert len(g.nodes) >= 15


class TestSerialization:
    """Serialize / deserialize the full and sub graphs."""

    def test_full_graph_roundtrip(self) -> None:
        graph_json = serialize()
        data = json.loads(graph_json)
        assert data["version"] == "0.3.0"
        assert len(data["objects"]) >= 15

    def test_subgraph_smaller_than_full(self) -> None:
        full = serialize()
        sub = serialize(_get_ingest_func())
        assert len(sub) <= len(full)

    def test_subgraph_contains_deep_deps(self) -> None:
        sub = serialize(_get_ingest_func())
        store = FuseStore.from_json(sub)
        ref_names = set(store.refs.keys())
        # Deep transitive deps must be present
        assert any("is_numeric" in r for r in ref_names)
        assert any("clean_value" in r for r in ref_names)

    def test_store_from_json_roundtrip(self) -> None:
        original = serialize()
        store = FuseStore.from_json(original)
        roundtripped = store.to_json()
        assert json.loads(original) == json.loads(roundtripped)


class TestReconstruction:
    """Reconstructed source must be self-contained and ordered."""

    def test_reconstruct_contains_all_functions(self) -> None:
        sub = serialize(_get_ingest_func())
        source = reconstruct(sub, "ingest_and_analyze")
        assert "def ingest_and_analyze" in source
        assert "def full_report" in source
        assert "def analyze_table" in source
        assert "def is_numeric" in source
        assert "def clean_value" in source

    def test_topological_order(self) -> None:
        sub = serialize(_get_ingest_func())
        source = reconstruct(sub, "ingest_and_analyze")
        assert source.index("def is_numeric") < source.index(
            "def extract_numeric_column"
        )
        assert source.index("def full_report") < source.index(
            "def ingest_and_analyze"
        )

    def test_imports_present(self) -> None:
        sub = serialize(_get_ingest_func())
        source = reconstruct(sub, "ingest_and_analyze")
        assert "import csv" in source
        assert "import json" in source

    def test_class_method_reconstruction(self) -> None:
        full = serialize()
        source = reconstruct(full, "run_report")
        assert "class AnalyticsPipeline:" in source
        assert "def run_report" in source


class TestWorkerExecution:
    """FuseWorker should execute reconstructed functions correctly."""

    def test_execute_standalone_function(self) -> None:
        sub = serialize(_get_ingest_func())
        worker = FuseWorker(auto_install=False)
        result = worker.execute(
            sub, "ingest_and_analyze", SAMPLE_CSV, NUMERIC_COLS, RANK_COL
        )
        assert "ANALYTICS REPORT" in result
        assert "score" in result
        assert "Alice" in result

    def test_worker_caching(self) -> None:
        sub = serialize(_get_ingest_func())
        worker = FuseWorker(auto_install=False)
        worker.execute(
            sub, "ingest_and_analyze", SAMPLE_CSV, NUMERIC_COLS, RANK_COL
        )
        assert worker.cache_info()["size"] == 1
        worker.execute(
            sub, "ingest_and_analyze", SAMPLE_CSV, NUMERIC_COLS, RANK_COL
        )
        assert worker.cache_info()["size"] == 1  # cache hit

    def test_pack_and_run(self) -> None:
        func = _get_ingest_func()
        task = pack(func, SAMPLE_CSV, NUMERIC_COLS, RANK_COL)
        worker = FuseWorker(auto_install=False)
        result = worker.run(task)
        assert "ANALYTICS REPORT" in result

    def test_results_match(self) -> None:
        """Direct worker.execute and task.run must return identical output."""
        func = _get_ingest_func()
        sub = serialize(func)
        worker = FuseWorker(auto_install=False)
        direct = worker.execute(
            sub, "ingest_and_analyze", SAMPLE_CSV, NUMERIC_COLS, RANK_COL
        )
        task = pack(func, SAMPLE_CSV, NUMERIC_COLS, RANK_COL)
        via_task = worker.run(task)
        assert direct == via_task
