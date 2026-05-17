"""Orchestration entry points -- only this file uses @offwork.task."""

import json

import offwork

from tests.fixtures.stress_test_module.analyzers import (
    analyze_measurements,
    build_report,
    detect_anomalies,
)
from tests.fixtures.stress_test_module.formatters import format_json_report, format_text_report
from tests.fixtures.stress_test_module.generators import generate_measurements, inject_anomalies
from tests.fixtures.stress_test_module.transformers import (
    clean_measurements,
    compute_deltas,
    normalize_units,
)
from tests.fixtures.stress_test_module.validators import validate_measurements


@offwork.task
def run_full_pipeline(
    sensor_count: int = 5,
    readings_per_sensor: int = 100,
    seed: int = 42,
) -> str:
    measurements = generate_measurements(sensor_count, readings_per_sensor, seed)
    measurements = inject_anomalies(measurements, anomaly_rate=0.05, seed=seed + 1)

    validation = validate_measurements(measurements)

    cleaned = clean_measurements(measurements)
    normalized = normalize_units(cleaned, {"fahrenheit": "celsius"})
    deltas = compute_deltas(normalized)

    stats = analyze_measurements(normalized)
    anomalies = detect_anomalies(normalized)

    report = build_report(
        title=f"Full Pipeline Report ({sensor_count} sensors)",
        stats=stats,
        anomalies=anomalies,
        measurements=normalized,
    )
    report["validation"] = validation
    report["delta_count"] = len(deltas)

    return format_text_report(report)


@offwork.task
def run_analysis_only(measurements: list[dict]) -> str:
    cleaned = clean_measurements(measurements)
    stats = analyze_measurements(cleaned)
    anomalies = detect_anomalies(cleaned)
    report = build_report(
        title="Analysis Only",
        stats=stats,
        anomalies=anomalies,
        measurements=cleaned,
    )
    return format_json_report(report)


@offwork.task
def run_validation_report(measurements: list[dict]) -> str:
    validation = validate_measurements(measurements)
    lines = [
        f"Valid: {validation['is_valid']}",
        f"Total: {validation['total']}",
        f"Invalid: {validation['invalid_count']}",
    ]
    if validation["errors"]:
        lines.append("Errors:")
        for e in validation["errors"][:10]:
            lines.append(f"  - {e}")
    if validation["warnings"]:
        lines.append("Warnings:")
        for w in validation["warnings"][:10]:
            lines.append(f"  - {w}")
    return "\n".join(lines)


def _apply_filter(measurements: list[dict], cutoff: float) -> list[dict]:
    return [m for m in measurements if m.get("value", 0) > cutoff]


def _make_threshold_pipeline(threshold: float, label: str):
    @offwork.task
    def threshold_filter(measurements: list[dict]) -> str:
        cleaned = clean_measurements(measurements)
        filtered = _apply_filter(cleaned, threshold)
        return json.dumps({"label": label, "count": len(filtered), "total": len(cleaned)})

    return threshold_filter


high_value_analysis = _make_threshold_pipeline(100.0, "high_value")
