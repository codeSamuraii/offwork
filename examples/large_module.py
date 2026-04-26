"""Run a large multi-file module on a remote worker.

The stress_test_module has 47 functions across 7 files, 3 classes, and deep
dependency chains. Only the entry point below is decorated with @trace --
pyfuse discovers everything else automatically.

Requires Redis on localhost:6379.  Install: pip install redis

Usage:
    # Terminal 1 -- start a worker
    pyfuse worker --backend redis://localhost:6379

    # Terminal 2 -- run this script
    python examples/large_module.py
"""
import asyncio
import sys
from pathlib import Path

# Make ``tests.fixtures.stress_test_module`` importable when running this
# script directly from a checkout, without needing PYTHONPATH gymnastics.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pyfuse
from pyfuse import trace

from tests.fixtures.stress_test_module.analyzers import build_report, detect_anomalies, analyze_measurements
from tests.fixtures.stress_test_module.formatters import format_text_report
from tests.fixtures.stress_test_module.generators import inject_anomalies, generate_measurements
from tests.fixtures.stress_test_module.validators import validate_measurements
from tests.fixtures.stress_test_module.transformers import compute_deltas, normalize_units, clean_measurements


@trace
def full_sensor_report(sensor_count: int, readings_per_sensor: int, seed: int = 42) -> str:
    """
    Calls functions from multiple modules, with complex dependency chains.
    pyfuse captures their source and dependencies automatically.
    """
    measurements = generate_measurements(sensor_count, readings_per_sensor, seed)
    measurements = inject_anomalies(measurements, anomaly_rate=0.05, seed=seed + 1)
    validate_measurements(measurements)

    cleaned = clean_measurements(measurements)
    normalized = normalize_units(cleaned, {"fahrenheit": "celsius"})
    deltas = compute_deltas(normalized)

    stats = analyze_measurements(normalized)
    anomalies = detect_anomalies(normalized)

    report = build_report(
        f"Sensor Report ({sensor_count} sensors, seed={seed})",
        stats, anomalies, normalized,
    )
    report["delta_count"] = len(deltas)
    return format_text_report(report)


async def main() -> None:
    pyfuse.connect("redis://localhost:6379")
    report = await full_sensor_report.run(3, 50, seed=42)
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
