"""Statistical analysis and anomaly detection."""

import math
import statistics

from pyfuse import trace

from tests.fixtures.stress_test_module.transformers import group_by_sensor


class StatisticalAnalyzer:

    def _percentiles(self, values: list[float]) -> dict[str, float]:
        if len(values) < 2:
            return {"p25": 0.0, "p50": 0.0, "p75": 0.0}
        quantiles = statistics.quantiles(values, n=4)
        return {
            "p25": round(quantiles[0], 4),
            "p50": round(quantiles[1], 4),
            "p75": round(quantiles[2], 4),
        }

    def _basic_stats(self, values: list[float]) -> dict[str, float]:
        if not values:
            return {"mean": 0.0, "median": 0.0, "stdev": 0.0, "min": 0.0, "max": 0.0}
        result = {
            "mean": round(statistics.mean(values), 4),
            "median": round(statistics.median(values), 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
        }
        if len(values) >= 2:
            result["stdev"] = round(statistics.stdev(values), 4)
        else:
            result["stdev"] = 0.0
        return result

    def _detect_trends(self, values: list[float]) -> str:
        if len(values) < 3:
            return "insufficient_data"
        n = len(values)
        x_mean = (n - 1) / 2.0
        y_mean = statistics.mean(values)
        numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        if denominator == 0:
            return "flat"
        slope = numerator / denominator
        normalized = slope / (y_mean if y_mean != 0 else 1.0)
        if normalized > 0.01:
            return "increasing"
        if normalized < -0.01:
            return "decreasing"
        return "stable"

    def analyze_sensor(
        self, sensor_id: str, readings: list[dict]
    ) -> dict[str, object]:
        values = [r["value"] for r in readings]
        return {
            "sensor_id": sensor_id,
            "count": len(values),
            "stats": self._basic_stats(values),
            "percentiles": self._percentiles(values),
            "trend": self._detect_trends(values),
        }

    @trace
    def analyze_all(self, measurements: list[dict]) -> dict[str, dict]:
        groups = group_by_sensor(measurements)
        results: dict[str, dict] = {}
        for sensor_id, readings in groups.items():
            results[sensor_id] = self.analyze_sensor(sensor_id, readings)
        return results


class AnomalyDetector:

    def _zscore(self, values: list[float], threshold: float = 3.0) -> list[int]:
        if len(values) < 3:
            return []
        mean = statistics.mean(values)
        stdev = statistics.stdev(values)
        if stdev == 0:
            return []
        return [
            i for i, v in enumerate(values)
            if abs(v - mean) / stdev > threshold
        ]

    def _rate_of_change(self, values: list[float], max_delta: float) -> list[int]:
        anomalies: list[int] = []
        for i in range(1, len(values)):
            if abs(values[i] - values[i - 1]) > max_delta:
                anomalies.append(i)
        return anomalies

    @trace
    def detect(self, sensor_id: str, measurements: list[dict]) -> list[dict]:
        values = [m["value"] for m in measurements]

        lo = min(values) if values else 0.0
        hi = max(values) if values else 0.0
        max_delta = (hi - lo) * 0.5 if hi != lo else 1.0

        zscore_indices = self._zscore(values, threshold=2.5)
        roc_indices = self._rate_of_change(values, max_delta)

        all_indices = sorted(set(zscore_indices) | set(roc_indices))
        results: list[dict] = []
        for idx in all_indices:
            results.append({
                "sensor_id": sensor_id,
                "index": idx,
                "value": values[idx],
                "timestamp": measurements[idx].get("timestamp", ""),
                "zscore": idx in zscore_indices,
                "rate_of_change": idx in roc_indices,
            })
        return results


def classify_severity(anomaly_count: int, total_count: int) -> str:
    if total_count == 0:
        return "unknown"
    ratio = anomaly_count / total_count
    if ratio > 0.2:
        return "critical"
    if ratio > 0.1:
        return "high"
    if ratio > 0.03:
        return "medium"
    return "low"


def build_report(
    title: str,
    stats: dict[str, dict],
    anomalies: list[dict],
    measurements: list[dict],
) -> dict:
    severity = classify_severity(len(anomalies), len(measurements))
    summary: dict[str, float] = {}
    all_values = [m["value"] for m in measurements if isinstance(m.get("value"), (int, float))]
    if all_values:
        summary["global_mean"] = round(statistics.mean(all_values), 4)
        summary["global_min"] = min(all_values)
        summary["global_max"] = max(all_values)
    summary["sensor_count"] = len(stats)
    summary["anomaly_count"] = len(anomalies)

    return {
        "title": title,
        "severity": severity,
        "summary": summary,
        "stats": stats,
        "anomalies": anomalies,
        "measurement_count": len(measurements),
    }


def analyze_measurements(measurements: list[dict]) -> dict[str, dict]:
    analyzer = StatisticalAnalyzer()
    return analyzer.analyze_all(measurements)


def detect_anomalies(measurements: list[dict]) -> list[dict]:
    detector = AnomalyDetector()
    groups = group_by_sensor(measurements)
    all_anomalies: list[dict] = []
    for sensor_id, readings in groups.items():
        anomalies = detector.detect(sensor_id, readings)
        all_anomalies.extend(anomalies)
    return all_anomalies
