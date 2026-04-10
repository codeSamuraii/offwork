"""Synthetic sensor data generation."""

import copy
import datetime
import hashlib
import math
import random


def _make_sensor_id(prefix: str, index: int) -> str:
    raw = f"{prefix}-{index:04d}"
    suffix = hashlib.md5(raw.encode()).hexdigest()[:4]
    return f"{raw}-{suffix}"


def _unit_ranges() -> dict[str, tuple[float, float]]:
    return {
        "celsius": (-40.0, 120.0),
        "fahrenheit": (-40.0, 248.0),
        "psi": (0.0, 3000.0),
        "bar": (0.0, 207.0),
        "rpm": (0.0, 15000.0),
        "kwh": (0.0, 500.0),
    }


def _generate_value(unit: str, rng: random.Random) -> float:
    ranges = _unit_ranges()
    lo, hi = ranges.get(unit, (0.0, 100.0))
    base = rng.uniform(lo, hi)
    seasonal = math.sin(rng.random() * math.pi) * (hi - lo) * 0.05
    return round(base + seasonal, 2)


def _generate_timestamp(base_date: datetime.datetime, offset_hours: int) -> str:
    ts = base_date + datetime.timedelta(hours=offset_hours)
    return ts.isoformat()


def _sensor_config() -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    prefixes = ("TEMP", "PRES", "ROTA", "ENRG")
    prefix_to_units = {
        "TEMP": ("celsius", "fahrenheit"),
        "PRES": ("psi", "bar"),
        "ROTA": ("rpm",),
        "ENRG": ("kwh",),
    }
    return prefixes, prefix_to_units


def generate_measurements(
    sensor_count: int,
    readings_per_sensor: int,
    seed: int = 42,
) -> list[dict]:
    rng = random.Random(seed)
    base_date = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    prefixes, prefix_to_units = _sensor_config()
    measurements: list[dict] = []

    for i in range(sensor_count):
        prefix = prefixes[i % len(prefixes)]
        sensor_id = _make_sensor_id(prefix, i)
        unit = rng.choice(prefix_to_units[prefix])

        for j in range(readings_per_sensor):
            measurements.append({
                "timestamp": _generate_timestamp(base_date, j * 6),
                "sensor_id": sensor_id,
                "value": _generate_value(unit, rng),
                "unit": unit,
            })

    return measurements


def inject_anomalies(
    measurements: list[dict],
    anomaly_rate: float = 0.05,
    seed: int = 99,
) -> list[dict]:
    rng = random.Random(seed)
    result = copy.deepcopy(measurements)
    ranges = _unit_ranges()

    for m in result:
        if rng.random() < anomaly_rate:
            lo, hi = ranges.get(m["unit"], (0.0, 100.0))
            span = hi - lo
            if rng.random() < 0.5:
                m["value"] = round(hi + span * rng.uniform(0.5, 2.0), 2)
            else:
                m["value"] = round(lo - span * rng.uniform(0.5, 2.0), 2)

    return result
