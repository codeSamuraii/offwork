"""Input validation for sensor measurements."""

import datetime
import re
import statistics

from away import trace

VALID_UNITS = frozenset({"celsius", "fahrenheit", "psi", "bar", "rpm", "kwh"})
SENSOR_ID_PATTERN = re.compile(r"^[A-Z]{4}-\d{4}-[0-9a-f]{4}$")
MAX_MEASUREMENT_AGE_DAYS = 90

_VALUE_RANGES = {
    "celsius": (-50.0, 150.0),
    "fahrenheit": (-58.0, 302.0),
    "psi": (-100.0, 5000.0),
    "bar": (-10.0, 350.0),
    "rpm": (-500.0, 20000.0),
    "kwh": (-50.0, 1000.0),
}


def _detect_batch_anomalies(values: list[float]) -> list[str]:
    if len(values) < 3:
        return []
    warnings: list[str] = []
    mean = statistics.mean(values)
    stdev = statistics.stdev(values)
    if stdev > 0:
        outlier_count = sum(1 for v in values if abs(v - mean) > 3 * stdev)
        if outlier_count > 0:
            warnings.append(
                f"Batch contains {outlier_count} statistical outliers "
                f"(>{3}*stdev from mean)"
            )
    spread = max(values) - min(values)
    if spread > 0 and stdev / spread < 0.01:
        warnings.append("Very low variance relative to range")
    return warnings


class MeasurementValidator:

    def _check_unit(self, unit: str) -> str | None:
        valid_units = frozenset({"celsius", "fahrenheit", "psi", "bar", "rpm", "kwh"})
        if unit not in valid_units:
            return f"Invalid unit '{unit}', expected one of {sorted(valid_units)}"
        return None

    def _check_timestamp(self, timestamp: str) -> str | None:
        try:
            datetime.datetime.fromisoformat(timestamp)
        except (ValueError, TypeError):
            return f"Invalid ISO timestamp: '{timestamp}'"
        return None

    def _check_value_range(self, value: float, unit: str) -> str | None:
        value_ranges = {
            "celsius": (-50.0, 150.0),
            "fahrenheit": (-58.0, 302.0),
            "psi": (-100.0, 5000.0),
            "bar": (-10.0, 350.0),
            "rpm": (-500.0, 20000.0),
            "kwh": (-50.0, 1000.0),
        }
        lo, hi = value_ranges.get(unit, (-1e9, 1e9))
        if not lo <= value <= hi:
            return f"Value {value} out of plausible range [{lo}, {hi}] for {unit}"
        return None

    def _check_sensor_id(self, sensor_id: str) -> str | None:
        pattern = re.compile(r"^[A-Z]{4}-\d{4}-[0-9a-f]{4}$")
        if not pattern.match(sensor_id):
            return f"Invalid sensor ID format: '{sensor_id}'"
        return None

    def validate_single(self, measurement: dict) -> dict:
        errors: list[str] = []
        warnings: list[str] = []

        err = self._check_unit(measurement.get("unit", ""))
        if err:
            errors.append(err)

        err = self._check_timestamp(measurement.get("timestamp", ""))
        if err:
            errors.append(err)

        err = self._check_sensor_id(measurement.get("sensor_id", ""))
        if err:
            errors.append(err)

        unit = measurement.get("unit", "")
        value = measurement.get("value", 0.0)
        err = self._check_value_range(value, unit)
        if err:
            warnings.append(err)

        return {
            "is_valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }

    @trace
    def validate_batch(self, measurements: list[dict]) -> dict:
        all_errors: list[str] = []
        all_warnings: list[str] = []
        invalid_count = 0

        for i, m in enumerate(measurements):
            result = self.validate_single(m)
            if not result["is_valid"]:
                invalid_count += 1
                for e in result["errors"]:
                    all_errors.append(f"[{i}] {e}")
            all_warnings.extend(result["warnings"])

        values = [m.get("value", 0.0) for m in measurements if isinstance(m.get("value"), (int, float))]
        batch_warnings = _detect_batch_anomalies(values)
        all_warnings.extend(batch_warnings)

        return {
            "is_valid": invalid_count == 0,
            "total": len(measurements),
            "invalid_count": invalid_count,
            "errors": all_errors,
            "warnings": all_warnings,
        }


def validate_measurements(measurements: list[dict]) -> dict:
    validator = MeasurementValidator()
    return validator.validate_batch(measurements)
