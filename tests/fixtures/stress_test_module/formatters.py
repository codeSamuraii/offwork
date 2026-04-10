"""Output formatting and reporting."""

import datetime
import json
import textwrap


def _format_header(title: str, width: int = 60) -> str:
    padded = f" {title} "
    return padded.center(width, "=")


def _format_stat_line(name: str, value: float, precision: int = 2) -> str:
    return f"  {name:<20s}: {value:.{precision}f}"


def _format_anomaly_row(anomaly: dict) -> str:
    sensor = anomaly.get("sensor_id", "?")
    idx = anomaly.get("index", "?")
    value = anomaly.get("value", "?")
    flags: list[str] = []
    if anomaly.get("zscore"):
        flags.append("zscore")
    if anomaly.get("rate_of_change"):
        flags.append("rate_of_change")
    return f"  [{sensor}#{idx}] value={value} flags={','.join(flags)}"


def format_text_report(report: dict) -> str:
    lines: list[str] = []
    lines.append(_format_header(report.get("title", "Report")))
    lines.append("")

    severity = report.get("severity", "unknown")
    lines.append(f"  Severity: {severity}")
    lines.append(f"  Measurements: {report.get('measurement_count', 0)}")
    lines.append("")

    summary = report.get("summary", {})
    if summary:
        lines.append(_format_header("Summary", 40))
        for key, value in sorted(summary.items()):
            if isinstance(value, float):
                lines.append(_format_stat_line(key, value))
            else:
                lines.append(f"  {key:<20s}: {value}")
        lines.append("")

    stats = report.get("stats", {})
    if stats:
        lines.append(_format_header("Per-Sensor Statistics", 40))
        for sensor_id, sensor_stats in sorted(stats.items()):
            lines.append(f"\n  --- {sensor_id} ---")
            basic = sensor_stats.get("stats", {})
            for k, v in sorted(basic.items()):
                lines.append(_format_stat_line(k, v))
            trend = sensor_stats.get("trend", "unknown")
            lines.append(f"  {'trend':<20s}: {trend}")
        lines.append("")

    anomalies = report.get("anomalies", [])
    if anomalies:
        lines.append(_format_header("Anomalies", 40))
        for a in anomalies[:20]:
            lines.append(_format_anomaly_row(a))
        if len(anomalies) > 20:
            lines.append(f"  ... and {len(anomalies) - 20} more")
        lines.append("")

    lines.append(_format_header("End of Report"))
    return "\n".join(lines)


def _serialize_report(report: dict) -> dict:
    result: dict = {}
    for key, value in report.items():
        if isinstance(value, datetime.datetime):
            result[key] = value.isoformat()
        elif isinstance(value, dict):
            result[key] = _serialize_report(value)
        elif isinstance(value, list):
            result[key] = [
                _serialize_report(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def format_json_report(report: dict) -> str:
    serializable = _serialize_report(report)
    serializable["generated_at"] = datetime.datetime.now(
        tz=datetime.timezone.utc
    ).isoformat()
    return json.dumps(serializable, indent=2)
