"""Stress-test pyfuse with a multi-file module (47 nodes, 3 classes, 7 files).

Tests serialize → reconstruct → Worker.run for each entry point, verifying
that the reconstructed code is self-contained and produces correct results.
"""

import importlib
import json

import pytest

from pyfuse import Graph, pack, reconstruct, serialize
from pyfuse._worker import Worker

import tests.fixtures.stress_test_module.validators as _validators_mod
import tests.fixtures.stress_test_module.analyzers as _analyzers_mod
import tests.fixtures.stress_test_module.pipeline as _pipeline_mod
from tests.fixtures.stress_test_module.generators import generate_measurements


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reload_traced_modules():
    """Re-run @trace registrations against the fresh graph."""
    importlib.reload(_validators_mod)
    importlib.reload(_analyzers_mod)
    importlib.reload(_pipeline_mod)


@pytest.fixture()
def worker() -> Worker:
    return Worker(auto_install=False)


@pytest.fixture()
def sample_measurements() -> list[dict]:
    return generate_measurements(2, 15, seed=42)


# ── helpers ──────────────────────────────────────────────────────────


def _exec_source(source: str) -> dict:
    ns: dict = {}
    exec(compile(source, "<reconstructed>", "exec"), ns)
    return ns


# ── Phase 1: module works without pyfuse ─────────────────────────────


class TestStandaloneModule:

    def test_generate_and_validate(self):
        measurements = generate_measurements(3, 20, seed=42)
        assert len(measurements) == 60
        result = _validators_mod.validate_measurements(measurements)
        assert result["is_valid"] is True
        assert result["total"] == 60

    def test_clean_and_analyze(self):
        from tests.fixtures.stress_test_module.transformers import clean_measurements

        measurements = generate_measurements(2, 10, seed=7)
        cleaned = clean_measurements(measurements)
        assert len(cleaned) == 20

        stats = _analyzers_mod.analyze_measurements(cleaned)
        assert len(stats) == 2

        anomalies = _analyzers_mod.detect_anomalies(cleaned)
        assert isinstance(anomalies, list)

    def test_full_pipeline_local(self):
        result = _pipeline_mod.run_full_pipeline(
            sensor_count=2, readings_per_sensor=10, seed=42,
        )
        assert isinstance(result, str)
        assert len(result) > 100

    def test_analysis_only_local(self, sample_measurements):
        result = _pipeline_mod.run_analysis_only(sample_measurements)
        parsed = json.loads(result)
        assert parsed["title"] == "Analysis Only"
        assert "severity" in parsed

    def test_validation_report_local(self, sample_measurements):
        result = _pipeline_mod.run_validation_report(sample_measurements)
        assert "Valid:" in result

    def test_closure_filter_local(self, sample_measurements):
        result = _pipeline_mod.high_value_analysis(sample_measurements)
        parsed = json.loads(result)
        assert parsed["label"] == "high_value"
        assert "count" in parsed


# ── Phase 2: serialization and reconstruction ────────────────────────


class TestSerialization:

    def test_full_pipeline_object_count(self):
        data = json.loads(serialize(_pipeline_mod.run_full_pipeline))
        assert len(data["objects"]) >= 35

    def test_full_pipeline_no_cross_module_imports(self):
        source = reconstruct(
            serialize(_pipeline_mod.run_full_pipeline), "run_full_pipeline",
        )
        assert "from tests.fixtures.stress_test_module" not in source

    def test_full_pipeline_functions_present(self):
        source = reconstruct(
            serialize(_pipeline_mod.run_full_pipeline), "run_full_pipeline",
        )
        expected = [
            # generators
            "generate_measurements", "_make_sensor_id", "_generate_value",
            "_generate_timestamp", "inject_anomalies",
            # transformers
            "clean_measurements", "_interpolate_missing", "_apply_moving_average",
            "_remove_outliers", "normalize_units", "_celsius_to_fahrenheit",
            "_fahrenheit_to_celsius", "group_by_sensor", "compute_deltas",
            # validators
            "validate_measurements", "validate_batch", "validate_single",
            "_check_unit", "_check_timestamp", "_check_value_range",
            "_check_sensor_id", "_detect_batch_anomalies",
            # analyzers
            "analyze_all", "analyze_sensor", "_percentiles", "_basic_stats",
            "_detect_trends", "detect", "_zscore", "_rate_of_change",
            "classify_severity", "build_report",
            "analyze_measurements", "detect_anomalies",
            # formatters
            "format_text_report", "_format_header", "_format_stat_line",
            "_format_anomaly_row",
        ]
        for name in expected:
            assert f"def {name}" in source, f"Missing function: {name}"

    def test_full_pipeline_classes_present(self):
        source = reconstruct(
            serialize(_pipeline_mod.run_full_pipeline), "run_full_pipeline",
        )
        for cls in ["MeasurementValidator", "StatisticalAnalyzer", "AnomalyDetector"]:
            assert f"class {cls}:" in source, f"Missing class: {cls}"

    def test_full_pipeline_stdlib_imports_preserved(self):
        source = reconstruct(
            serialize(_pipeline_mod.run_full_pipeline), "run_full_pipeline",
        )
        for imp in [
            "import hashlib", "import math", "import random",
            "import datetime", "import statistics", "import copy",
            "import collections", "import itertools",
        ]:
            assert imp in source, f"Missing import: {imp}"

    def test_validate_batch_self_method_chain(self):
        source = reconstruct(
            serialize(_validators_mod.MeasurementValidator.validate_batch),
            "validate_batch",
        )
        assert "class MeasurementValidator:" in source
        for name in [
            "validate_batch", "validate_single",
            "_check_unit", "_check_timestamp", "_check_value_range",
            "_check_sensor_id",
        ]:
            assert f"def {name}" in source
        assert "def _detect_batch_anomalies" in source

    def test_analyze_all_cross_module_inlined(self):
        source = reconstruct(
            serialize(_analyzers_mod.StatisticalAnalyzer.analyze_all),
            "analyze_all",
        )
        assert "def group_by_sensor" in source
        assert "from tests.fixtures.stress_test_module.transformers" not in source
        assert "import itertools" in source

    def test_closure_variables_captured(self):
        source = reconstruct(
            serialize(_pipeline_mod.high_value_analysis), "threshold_filter",
        )
        assert "100.0" in source
        assert "'high_value'" in source or '"high_value"' in source
        assert "def clean_measurements" in source
        assert "def _apply_filter" in source


# ── Phase 3: reconstructed code executes correctly ───────────────────


class TestReconstructedExecution:

    def test_full_pipeline_executes(self):
        source = reconstruct(
            serialize(_pipeline_mod.run_full_pipeline), "run_full_pipeline",
        )
        ns = _exec_source(source)
        result = ns["run_full_pipeline"](
            sensor_count=2, readings_per_sensor=10, seed=42,
        )
        assert isinstance(result, str)
        assert len(result) > 100

    def test_validate_batch_executes(self):
        source = reconstruct(
            serialize(_validators_mod.MeasurementValidator.validate_batch),
            "validate_batch",
        )
        ns = _exec_source(source)
        validator = ns["MeasurementValidator"]()
        result = validator.validate_batch([{
            "unit": "celsius", "value": 25.0,
            "timestamp": "2025-01-01T00:00:00+00:00",
            "sensor_id": "TEMP-0000-1234",
        }])
        assert result["is_valid"] is True

    def test_analyze_all_executes(self):
        source = reconstruct(
            serialize(_analyzers_mod.StatisticalAnalyzer.analyze_all),
            "analyze_all",
        )
        ns = _exec_source(source)
        analyzer = ns["StatisticalAnalyzer"]()
        result = analyzer.analyze_all([
            {"sensor_id": "S1", "value": 10.0},
            {"sensor_id": "S1", "value": 20.0},
            {"sensor_id": "S1", "value": 30.0},
        ])
        assert "S1" in result
        assert result["S1"]["stats"]["mean"] == 20.0

    def test_closure_executes(self):
        source = reconstruct(
            serialize(_pipeline_mod.high_value_analysis), "threshold_filter",
        )
        ns = _exec_source(source)
        result = json.loads(ns["threshold_filter"]([
            {"sensor_id": "S1", "value": 50.0, "unit": "celsius",
             "timestamp": "2025-01-01T00:00:00"},
            {"sensor_id": "S1", "value": 150.0, "unit": "celsius",
             "timestamp": "2025-01-01T06:00:00"},
        ]))
        assert result["label"] == "high_value"

    def test_analysis_only_executes(self):
        source = reconstruct(
            serialize(_pipeline_mod.run_analysis_only), "run_analysis_only",
        )
        ns = _exec_source(source)
        sample = generate_measurements(2, 10, seed=7)
        parsed = json.loads(ns["run_analysis_only"](sample))
        assert parsed["title"] == "Analysis Only"
        assert "severity" in parsed


# ── Phase 4: Worker execution (simulates remote) ─────────────────────


class TestWorkerExecution:

    def test_full_pipeline(self, worker):
        task = pack(_pipeline_mod.run_full_pipeline, 2, 10, seed=42)
        result = worker.run(task)
        assert isinstance(result, str)
        assert len(result) > 100

    def test_full_pipeline_matches_local(self, worker):
        local = _pipeline_mod.run_full_pipeline(
            sensor_count=2, readings_per_sensor=10, seed=42,
        )
        task = pack(_pipeline_mod.run_full_pipeline, 2, 10, seed=42)
        remote = worker.run(task)
        assert local == remote

    def test_analysis_only(self, worker, sample_measurements):
        task = pack(_pipeline_mod.run_analysis_only, sample_measurements)
        result = json.loads(worker.run(task))
        assert result["title"] == "Analysis Only"

    def test_validation_report(self, worker, sample_measurements):
        task = pack(_pipeline_mod.run_validation_report, sample_measurements)
        result = worker.run(task)
        assert "Valid:" in result

    def test_closure_filter(self, worker, sample_measurements):
        task = pack(_pipeline_mod.high_value_analysis, sample_measurements)
        result = json.loads(worker.run(task))
        assert result["label"] == "high_value"
        assert result["count"] <= result["total"]

    def test_worker_caching(self, worker):
        task1 = pack(_pipeline_mod.run_full_pipeline, 2, 10, seed=42)
        worker.run(task1)
        assert worker.cache_info()["size"] == 1

        task2 = pack(_pipeline_mod.run_full_pipeline, 3, 5, seed=99)
        worker.run(task2)
        assert worker.cache_info()["size"] == 1

        task3 = pack(_pipeline_mod.run_analysis_only, [])
        worker.run(task3)
        assert worker.cache_info()["size"] == 2

    def test_multiple_entry_points(self, worker, sample_measurements):
        results = {}
        for name, task in [
            ("full", pack(_pipeline_mod.run_full_pipeline, 2, 10, seed=42)),
            ("analysis", pack(_pipeline_mod.run_analysis_only, sample_measurements)),
            ("validation", pack(_pipeline_mod.run_validation_report, sample_measurements)),
            ("closure", pack(_pipeline_mod.high_value_analysis, sample_measurements)),
        ]:
            results[name] = worker.run(task)

        assert len(results["full"]) > 100
        assert json.loads(results["analysis"])["title"] == "Analysis Only"
        assert "Valid:" in results["validation"]
        assert json.loads(results["closure"])["label"] == "high_value"
