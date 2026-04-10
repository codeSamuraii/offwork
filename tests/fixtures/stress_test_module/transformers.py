"""Data cleaning and transformation utilities."""

import collections
import itertools
import math
import statistics


def _celsius_to_fahrenheit(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def _fahrenheit_to_celsius(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


def normalize_units(
    measurements: list[dict], target_unit_map: dict[str, str]
) -> list[dict]:
    result: list[dict] = []
    for m in measurements:
        current = m["unit"]
        target = target_unit_map.get(current)
        if target and current == "celsius" and target == "fahrenheit":
            result.append({**m, "value": round(_celsius_to_fahrenheit(m["value"]), 2), "unit": target})
        elif target and current == "fahrenheit" and target == "celsius":
            result.append({**m, "value": round(_fahrenheit_to_celsius(m["value"]), 2), "unit": target})
        else:
            result.append(dict(m))
    return result


def _interpolate_missing(values: list[float | None]) -> list[float]:
    result = list(values)
    for i, v in enumerate(result):
        if v is not None and not math.isfinite(v):
            result[i] = None
    # Forward-fill then backward-fill
    last_valid: float | None = None
    for i, v in enumerate(result):
        if v is not None:
            last_valid = v
        elif last_valid is not None:
            result[i] = last_valid
    # Backward fill for leading Nones
    next_valid: float | None = None
    for i in range(len(result) - 1, -1, -1):
        if result[i] is not None:
            next_valid = result[i]
        elif next_valid is not None:
            result[i] = next_valid
    return [v if v is not None else 0.0 for v in result]


def _apply_moving_average(values: list[float], window: int = 5) -> list[float]:
    if len(values) <= window:
        return list(values)
    buf: collections.deque[float] = collections.deque(maxlen=window)
    result: list[float] = []
    for v in values:
        buf.append(v)
        result.append(round(statistics.mean(buf), 4))
    return result


def _remove_outliers(values: list[float], sigma: float = 3.0) -> list[float]:
    if len(values) < 3:
        return list(values)
    mean = statistics.mean(values)
    stdev = statistics.stdev(values)
    if stdev == 0:
        return list(values)
    return [v for v in values if abs(v - mean) <= sigma * stdev]


def clean_measurements(measurements: list[dict]) -> list[dict]:
    by_sensor: dict[str, list[dict]] = {}
    for m in measurements:
        by_sensor.setdefault(m["sensor_id"], []).append(m)

    result: list[dict] = []
    for sensor_id, readings in by_sensor.items():
        values = [r["value"] for r in readings]
        values = _interpolate_missing(values)
        values = _apply_moving_average(values, window=3)
        cleaned_values = _remove_outliers(values)

        for reading, cleaned_val in zip(readings, cleaned_values):
            result.append({**reading, "value": cleaned_val})

    return result


def group_by_sensor(measurements: list[dict]) -> dict[str, list[dict]]:
    sorted_data = sorted(measurements, key=lambda m: m["sensor_id"])
    groups: dict[str, list[dict]] = {}
    for key, group in itertools.groupby(sorted_data, key=lambda m: m["sensor_id"]):
        groups[key] = list(group)
    return groups


def compute_deltas(measurements: list[dict]) -> list[dict]:
    groups = group_by_sensor(measurements)
    result: list[dict] = []
    for sensor_id, readings in groups.items():
        sorted_readings = sorted(readings, key=lambda r: r["timestamp"])
        for i in range(1, len(sorted_readings)):
            prev_val = sorted_readings[i - 1]["value"]
            curr_val = sorted_readings[i]["value"]
            delta = round(curr_val - prev_val, 4)
            result.append({
                "sensor_id": sensor_id,
                "timestamp": sorted_readings[i]["timestamp"],
                "value": curr_val,
                "delta": delta,
                "unit": sorted_readings[i]["unit"],
            })
    return result
