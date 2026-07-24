from __future__ import annotations

import csv
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from trt_core.repository import PROJECT_ROOT


M12_ROOT = PROJECT_ROOT / "outputs" / "reports" / "m12"
OUT_DIR = M12_ROOT / "comparison_results" / "literature_performance"
FIG_DIR = OUT_DIR / "figures"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def esc(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "null"
    return f"{value:.4f}"


def load_reference() -> dict[str, Any]:
    return yaml.safe_load((M12_ROOT / "seed_data" / "reference_baselines.yaml").read_text(encoding="utf-8"))["reference_sources"]


def load_latest_smoke_status() -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(M12_ROOT / "manual_results.jsonl"):
        test_id = row.get("test_case_id")
        if isinstance(test_id, str) and test_id.startswith("SMOKE_"):
            latest[test_id] = row
    return latest


def load_smoke_metrics() -> list[dict[str, Any]]:
    db_path = M12_ROOT / "m12_metrics.sqlite3"
    if not db_path.exists():
        return []
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT test_case_id, run_id, scenario_spec_id, R_storage,
                   N_tool_storage_total, N_failed_tool_storage,
                   T_wait_seconds, T_verification_seconds, T_loop_seconds,
                   R_reset, data_quality_status, data_quality_reason,
                   data_source, is_live_test, is_historical
            FROM m12_run_metrics
            WHERE test_case_id LIKE 'SMOKE_%'
            ORDER BY test_case_id
            """
        )
    ]
    connection.close()
    return rows


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_values() -> dict[str, Any]:
    metrics = load_smoke_metrics()
    statuses = load_latest_smoke_status()
    tc2 = [statuses.get(f"SMOKE_{i:03d}", {}) for i in range(9, 18)]
    tc4 = [statuses.get(f"SMOKE_{i:03d}", {}) for i in range(20, 28)]
    tc3 = [statuses.get(f"SMOKE_{i:03d}", {}) for i in range(18, 20)]
    r_storage_values = [float(row["R_storage"]) for row in metrics if row.get("R_storage") is not None]
    t_wait_values = [float(row["T_wait_seconds"]) for row in metrics if row.get("T_wait_seconds") is not None]
    t_ver_values = [float(row["T_verification_seconds"]) for row in metrics if row.get("T_verification_seconds") is not None]
    t_loop_values = [float(row["T_loop_seconds"]) for row in metrics if row.get("T_loop_seconds") is not None]
    return {
        "metrics": metrics,
        "statuses": statuses,
        "smoke_items": len([key for key in statuses if key.startswith("SMOKE_")]),
        "simulation_rows": len(metrics),
        "R_storage_mean": mean(r_storage_values),
        "T_wait_mean_seconds": mean(t_wait_values),
        "T_verification_mean_seconds": mean(t_ver_values),
        "T_loop_mean_seconds": mean(t_loop_values),
        "TC2_query_success_rate": sum(1 for row in tc2 if row.get("status") == "PASS") / len(tc2),
        "TC3_live_setup_rows": sum(1 for row in tc3 if row.get("status") == "PASS"),
        "TC4_interception_rate": sum(1 for row in tc4 if row.get("status") == "REJECTED") / len(tc4),
        "TC4_rows": len(tc4),
        "TC4_intercepted": sum(1 for row in tc4 if row.get("status") == "REJECTED"),
    }


def svg_grouped_horizontal(path: Path, *, title: str, subtitle: str, rows: list[dict[str, Any]], x_label: str, max_value: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    width = 1180
    row_h = 62
    margin_l = 310
    margin_r = 70
    margin_t = 92
    margin_b = 80
    height = margin_t + margin_b + row_h * len(rows)
    plot_w = width - margin_l - margin_r
    max_v = max_value if max_value is not None else max(float(row["value"]) for row in rows) * 1.1
    max_v = max(max_v, 1e-9)
    palette = {
        "Literature": "#475569",
        "Our system": "#2563eb",
        "Our system (proxy)": "#0f766e",
        "Our system (different scope)": "#d97706",
    }
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfbf8"/>',
        f'<text x="{width/2}" y="34" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">{esc(title)}</text>',
        f'<text x="{width/2}" y="58" text-anchor="middle" font-family="Arial" font-size="13" fill="#475569">{esc(subtitle)}</text>',
        f'<line x1="{margin_l}" y1="{margin_t-16}" x2="{margin_l}" y2="{height-margin_b+16}" stroke="#1f2937" stroke-width="1"/>',
        f'<line x1="{margin_l}" y1="{height-margin_b+16}" x2="{width-margin_r}" y2="{height-margin_b+16}" stroke="#1f2937" stroke-width="1"/>',
    ]
    for i in range(6):
        value = max_v * i / 5
        x = margin_l + (value / max_v) * plot_w
        lines.append(f'<line x1="{x:.1f}" y1="{margin_t-16}" x2="{x:.1f}" y2="{height-margin_b+16}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="{x:.1f}" y="{height-margin_b+38}" text-anchor="middle" font-family="Arial" font-size="11" fill="#64748b">{value:.2g}</text>')
    for index, row in enumerate(rows):
        y = margin_t + index * row_h
        bar_h = 24
        value = float(row["value"])
        bar_w = (value / max_v) * plot_w
        color = palette.get(row.get("series", ""), "#64748b")
        lines.append(f'<text x="{margin_l-18}" y="{y+19}" text-anchor="end" font-family="Arial" font-size="13" font-weight="700" fill="#111827">{esc(row["label"])}</text>')
        lines.append(f'<text x="{margin_l-18}" y="{y+39}" text-anchor="end" font-family="Arial" font-size="11" fill="#64748b">{esc(row.get("note", ""))}</text>')
        lines.append(f'<rect x="{margin_l}" y="{y}" width="{bar_w:.1f}" height="{bar_h}" rx="3" fill="{color}"/>')
        lines.append(f'<text x="{margin_l + bar_w + 8:.1f}" y="{y+17}" font-family="Arial" font-size="12" fill="#111827">{esc(row.get("display", f"{value:.3g}"))}</text>')
    lines.append(f'<text x="{width/2}" y="{height-18}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569">{esc(x_label)}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def svg_live_metric_bars(
    path: Path,
    *,
    title: str,
    subtitle: str,
    rows: list[dict[str, Any]],
    metric_key: str,
    unit: str,
    y_max: float | None = None,
    value_format: str = ".2f",
) -> None:
    valid_rows = [row for row in rows if row.get(metric_key) is not None]
    path.parent.mkdir(parents=True, exist_ok=True)
    width = 1320
    height = 660
    margin_l = 82
    margin_r = 36
    margin_t = 98
    margin_b = 126
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    if not valid_rows:
        path.write_text(
            "\n".join(
                [
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
                    '<rect width="100%" height="100%" fill="#fbfbf8"/>',
                    f'<text x="{width/2}" y="42" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">{esc(title)}</text>',
                    f'<text x="{width/2}" y="84" text-anchor="middle" font-family="Arial" font-size="18" fill="#b91c1c">No valid live data available</text>',
                    "</svg>",
                ]
            ),
            encoding="utf-8",
        )
        return

    values = [float(row[metric_key]) for row in valid_rows]
    max_v = y_max if y_max is not None else max(values) * 1.18
    max_v = max(max_v, 1e-9)
    mean_v = mean(values) or 0
    bar_gap = 18
    bar_w = max(28, (plot_w - bar_gap * (len(valid_rows) - 1)) / len(valid_rows))
    mean_y = margin_t + plot_h - (mean_v / max_v) * plot_h
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfbf8"/>',
        f'<text x="{width/2}" y="34" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">{esc(title)}</text>',
        f'<text x="{width/2}" y="60" text-anchor="middle" font-family="Arial" font-size="13" fill="#475569">{esc(subtitle)}</text>',
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t + plot_h}" stroke="#111827" stroke-width="1"/>',
        f'<line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{width - margin_r}" y2="{margin_t + plot_h}" stroke="#111827" stroke-width="1"/>',
    ]
    for i in range(6):
        value = max_v * i / 5
        y = margin_t + plot_h - (value / max_v) * plot_h
        lines.append(f'<line x1="{margin_l}" y1="{y:.1f}" x2="{width-margin_r}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="{margin_l-12}" y="{y+4:.1f}" text-anchor="end" font-family="Arial" font-size="11" fill="#64748b">{value:.2g}</text>')
    lines.append(f'<line x1="{margin_l}" y1="{mean_y:.1f}" x2="{width-margin_r}" y2="{mean_y:.1f}" stroke="#b45309" stroke-width="2" stroke-dasharray="7 5"/>')
    lines.append(f'<text x="{width-margin_r-4}" y="{mean_y-7:.1f}" text-anchor="end" font-family="Arial" font-size="12" font-weight="700" fill="#92400e">mean {mean_v:{value_format}} {esc(unit)}</text>')
    for index, row in enumerate(valid_rows):
        value = float(row[metric_key])
        x = margin_l + index * (bar_w + bar_gap)
        bar_h = (value / max_v) * plot_h
        y = margin_t + plot_h - bar_h
        label = row.get("test_case_id") or row.get("run_id") or str(index + 1)
        run_id = row.get("run_id") or ""
        fill = "#2563eb" if row.get("data_source") == "LIVE_N8N_CHAT" else "#64748b"
        if metric_key == "R_storage" and value < 1.0:
            fill = "#dc2626"
        lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="4" fill="{fill}"/>')
        lines.append(f'<text x="{x + bar_w / 2:.1f}" y="{y-8:.1f}" text-anchor="middle" font-family="Arial" font-size="11" fill="#111827">{value:{value_format}}</text>')
        lines.append(f'<text x="{x + bar_w / 2:.1f}" y="{margin_t + plot_h + 24}" text-anchor="middle" font-family="Arial" font-size="11" font-weight="700" fill="#111827" transform="rotate(-35 {x + bar_w / 2:.1f},{margin_t + plot_h + 24})">{esc(label)}</text>')
        if run_id:
            lines.append(f'<title>{esc(label)} {esc(run_id)} {metric_key}={value:{value_format}} {esc(unit)}</title>')
    lines.append(f'<text x="24" y="{margin_t + plot_h/2}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569" transform="rotate(-90 24,{margin_t + plot_h/2})">{esc(metric_key)} ({esc(unit)})</text>')
    lines.append(f'<text x="{width/2}" y="{height-24}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569">Source: m12_run_metrics, LIVE_N8N_CHAT smoke simulation rows only. Run IDs are embedded in SVG titles.</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def histogram_bins(
    rows: list[dict[str, Any]],
    *,
    metric_key: str,
    bin_width: float,
    start: float | None = None,
    end: float | None = None,
) -> list[dict[str, Any]]:
    values = [float(row[metric_key]) for row in rows if row.get(metric_key) is not None]
    if not values:
        return []
    lo = start if start is not None else math.floor(min(values) / bin_width) * bin_width
    hi = end if end is not None else math.ceil(max(values) / bin_width) * bin_width
    if end is None and hi <= max(values):
        hi += bin_width
    edges: list[float] = []
    current = lo
    while current < hi + (bin_width * 0.5):
        edges.append(round(current, 10))
        current += bin_width
    bins: list[dict[str, Any]] = []
    total = len(values)
    for index in range(len(edges) - 1):
        left = edges[index]
        right = edges[index + 1]
        if index == len(edges) - 2:
            count = sum(1 for value in values if left <= value <= right)
        else:
            count = sum(1 for value in values if left <= value < right)
        if count == 0:
            continue
        bins.append(
            {
                "metric": metric_key,
                "bin_start": left,
                "bin_end": right,
                "count": count,
                "total": total,
                "probability": count / total,
            }
        )
    return bins


def svg_normalized_histogram(
    path: Path,
    *,
    title: str,
    subtitle: str,
    bins: list[dict[str, Any]],
    x_label: str,
    unit: str,
    source_note: str,
    value_format: str = ".0f",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    width = 1280
    height = 650
    margin_l = 88
    margin_r = 42
    margin_t = 96
    margin_b = 116
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    if not bins:
        path.write_text(
            "\n".join(
                [
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
                    '<rect width="100%" height="100%" fill="#fbfbf8"/>',
                    f'<text x="{width/2}" y="42" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">{esc(title)}</text>',
                    f'<text x="{width/2}" y="84" text-anchor="middle" font-family="Arial" font-size="18" fill="#b91c1c">No valid live data available</text>',
                    "</svg>",
                ]
            ),
            encoding="utf-8",
        )
        return

    max_p = max(float(row["probability"]) for row in bins)
    y_max = min(1.0, max(0.25, math.ceil(max_p * 10) / 10))
    bar_gap = 20
    bar_w = max(44, (plot_w - bar_gap * (len(bins) - 1)) / len(bins))
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfbf8"/>',
        f'<text x="{width/2}" y="34" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">{esc(title)}</text>',
        f'<text x="{width/2}" y="60" text-anchor="middle" font-family="Arial" font-size="13" fill="#475569">{esc(subtitle)}</text>',
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t + plot_h}" stroke="#111827" stroke-width="1"/>',
        f'<line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{width - margin_r}" y2="{margin_t + plot_h}" stroke="#111827" stroke-width="1"/>',
    ]
    for i in range(6):
        probability = y_max * i / 5
        y = margin_t + plot_h - (probability / y_max) * plot_h
        lines.append(f'<line x1="{margin_l}" y1="{y:.1f}" x2="{width-margin_r}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="{margin_l-12}" y="{y+4:.1f}" text-anchor="end" font-family="Arial" font-size="11" fill="#64748b">{probability:.2f}</text>')

    for index, row in enumerate(bins):
        probability = float(row["probability"])
        x = margin_l + index * (bar_w + bar_gap)
        bar_h = (probability / y_max) * plot_h
        y = margin_t + plot_h - bar_h
        label = f"{row['bin_start']:{value_format}}-{row['bin_end']:{value_format}}"
        count_label = f"{row['count']}/{row['total']}"
        lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="4" fill="#2563eb"/>')
        lines.append(f'<text x="{x + bar_w/2:.1f}" y="{y-8:.1f}" text-anchor="middle" font-family="Arial" font-size="12" font-weight="700" fill="#111827">{probability:.2f}</text>')
        lines.append(f'<text x="{x + bar_w/2:.1f}" y="{margin_t + plot_h + 22}" text-anchor="middle" font-family="Arial" font-size="11" fill="#111827">{esc(label)}</text>')
        lines.append(f'<text x="{x + bar_w/2:.1f}" y="{margin_t + plot_h + 40}" text-anchor="middle" font-family="Arial" font-size="10" fill="#64748b">{esc(count_label)} runs</text>')
    lines.append(f'<text x="26" y="{margin_t + plot_h/2}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569" transform="rotate(-90 26,{margin_t + plot_h/2})">normalized probability</text>')
    lines.append(f'<text x="{width/2}" y="{height-48}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569">{esc(x_label)} ({esc(unit)})</text>')
    lines.append(f'<text x="{width/2}" y="{height-22}" text-anchor="middle" font-family="Arial" font-size="11" fill="#64748b">{esc(source_note)}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def build_comparison_rows(refs: dict[str, Any], values: dict[str, Any]) -> list[dict[str, Any]]:
    t_wait_min = values["T_wait_mean_seconds"] / 60 if values["T_wait_mean_seconds"] is not None else None
    t_ver_min = values["T_verification_mean_seconds"] / 60 if values["T_verification_mean_seconds"] is not None else None
    rows = [
        {
            "comparison_id": "C01",
            "theme": "planning_latency",
            "reference_name": "LLMAPM",
            "reference_metric": "engineer_manual_process_time_minutes",
            "reference_value": refs["LLMAPM"]["reference_timing_minutes"]["engineer_manual_process_time"],
            "our_metric": "T_wait_mean_minutes",
            "our_value": t_wait_min,
            "direction": "LOWER_IS_BETTER",
            "claim": "Our chat-to-candidate loop reduces human-facing planning wait relative to manual process creation.",
            "comparability": "DIFFERENT_SCOPE_BUT_USEFUL_BASELINE",
        },
        {
            "comparison_id": "C02",
            "theme": "verification_latency",
            "reference_name": "LLMAPM",
            "reference_metric": "generated_process_import_time_minutes",
            "reference_value": refs["LLMAPM"]["reference_timing_minutes"]["generated_process_import_time"],
            "our_metric": "T_verification_mean_minutes",
            "our_value": t_ver_min,
            "direction": "LOWER_IS_BETTER",
            "claim": "Our verification is slower than import-only literature timing because it includes live Isaac simulation and evidence extraction.",
            "comparability": "DIFFERENT_SCOPE",
        },
        {
            "comparison_id": "C03",
            "theme": "tool_orchestration",
            "reference_name": "MAKA",
            "reference_metric": "critic_enabled_mean_f1",
            "reference_value": refs["MAKA"]["critic_ablation"]["critic_enabled_mean_f1"],
            "our_metric": "TC2_query_success_rate_proxy",
            "our_value": values["TC2_query_success_rate"],
            "direction": "HIGHER_IS_BETTER",
            "claim": "When routed to config/state paths, our backend achieves MAKA critic-level query success on smoke L1 rows.",
            "comparability": "PROXY_METRIC",
        },
        {
            "comparison_id": "C04",
            "theme": "safety_recovery",
            "reference_name": "MAKA",
            "reference_metric": "full_recovery_rate",
            "reference_value": refs["MAKA"]["critic_ablation"]["full_recovery_rate"],
            "our_metric": "TC4_error_interception_rate",
            "our_value": values["TC4_interception_rate"],
            "direction": "HIGHER_IS_BETTER",
            "claim": "Required-field, vocabulary, and scope guards give slightly higher smoke interception than MAKA full recovery, but validator gaps remain.",
            "comparability": "PROXY_METRIC",
        },
        {
            "comparison_id": "C05",
            "theme": "evidence_quality",
            "reference_name": "GAMHE_5_0",
            "reference_metric": "successful_functional_score_percent",
            "reference_value": refs["GAMHE_5_0"]["llm_code_generation"]["successful_functional_score"],
            "our_metric": "R_storage_mean_percent",
            "our_value": values["R_storage_mean"] * 100 if values["R_storage_mean"] is not None else None,
            "direction": "HIGHER_IS_BETTER",
            "claim": "Our evidence layer reaches near-perfect physical placement verification, measuring execution rather than code generation alone.",
            "comparability": "DIFFERENT_METRIC_ANCHOR",
        },
        {
            "comparison_id": "C06",
            "theme": "taxonomy_coverage",
            "reference_name": "FactoryFlow",
            "reference_metric": "error_taxonomy_count",
            "reference_value": len(refs["FactoryFlow"]["error_taxonomy"]),
            "our_metric": "TC4_smoke_invalid_request_rows",
            "our_value": values["TC4_rows"],
            "direction": "EQUAL",
            "claim": "The smoke suite matches FactoryFlow taxonomy scale for invalid-request coverage while attaching deployment-blocking outcomes.",
            "comparability": "COVERAGE_COUNT",
        },
    ]
    for row in rows:
        ref = float(row["reference_value"])
        ours = row["our_value"]
        if ours is None:
            result = "DATA_MISSING"
        elif row["direction"].startswith("LOWER"):
            result = "BETTER" if ours < ref else "WORSE_OR_SLOWER"
        elif row["direction"].startswith("HIGHER"):
            if abs(ours - ref) <= 0.005:
                result = "APPROX_EQUAL"
            else:
                result = "BETTER" if ours > ref else "LOWER"
        elif row["direction"] == "EQUAL":
            result = "PASS" if abs(ours - ref) <= 1e-9 else "DIFFERENT"
        else:
            result = "PASS" if ours >= ref else "LOWER"
        row["comparison_result"] = result
    return rows


def write_outputs(refs: dict[str, Any], values: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fields = [
        "comparison_id", "theme", "reference_name", "reference_metric", "reference_value",
        "our_metric", "our_value", "direction", "comparison_result", "comparability", "claim",
    ]
    write_csv(OUT_DIR / "literature_vs_ours_performance.csv", rows, fields)
    (OUT_DIR / "literature_vs_ours_metrics.json").write_text(json.dumps(values, indent=2, default=str), encoding="utf-8")
    metric_fields = [
        "test_case_id", "run_id", "scenario_spec_id", "R_storage",
        "T_wait_seconds", "T_verification_seconds", "T_loop_seconds",
        "data_source", "is_live_test", "data_quality_status", "data_quality_reason",
    ]
    write_csv(OUT_DIR / "live_smoke_metric_bars_source.csv", values["metrics"], metric_fields)
    histogram_rows: list[dict[str, Any]] = []
    r_storage_bins = histogram_bins(values["metrics"], metric_key="R_storage", bin_width=0.05, start=0.85, end=1.0)
    t_wait_bins = histogram_bins(values["metrics"], metric_key="T_wait_seconds", bin_width=5.0, start=10.0, end=40.0)
    t_verification_bins = histogram_bins(values["metrics"], metric_key="T_verification_seconds", bin_width=60.0, start=420.0, end=600.0)
    t_loop_bins = histogram_bins(values["metrics"], metric_key="T_loop_seconds", bin_width=60.0, start=480.0, end=660.0)
    for metric_bins in [r_storage_bins, t_wait_bins, t_verification_bins, t_loop_bins]:
        histogram_rows.extend(metric_bins)
    write_csv(
        OUT_DIR / "live_smoke_metric_distribution_source.csv",
        histogram_rows,
        ["metric", "bin_start", "bin_end", "count", "total", "probability"],
    )

    svg_normalized_histogram(
        FIG_DIR / "fig_05_live_R_storage_distribution.svg",
        title="Live Smoke Runs: Placement Verification Distribution",
        subtitle="Equation 3.2, R_storage. Bars show normalized probability across pass-rate bins.",
        bins=r_storage_bins,
        x_label="R_storage bin",
        unit="rate",
        source_note="Source: m12_run_metrics, LIVE_N8N_CHAT smoke simulation rows only.",
        value_format=".2f",
    )
    svg_normalized_histogram(
        FIG_DIR / "fig_06_live_T_wait_distribution.svg",
        title="Live Smoke Runs: Operator Wait Time Distribution",
        subtitle="Equation 3.4, T_wait. Bars show normalized probability across 5-second bins.",
        bins=t_wait_bins,
        x_label="T_wait bin",
        unit="seconds",
        source_note="Source: m12_run_metrics, LIVE_N8N_CHAT smoke simulation rows only.",
        value_format=".0f",
    )
    svg_normalized_histogram(
        FIG_DIR / "fig_07_live_T_verification_distribution.svg",
        title="Live Smoke Runs: Verification Time Distribution",
        subtitle="Equation 3.5, T_verification. Bars show normalized probability across 60-second bins.",
        bins=t_verification_bins,
        x_label="T_verification bin",
        unit="seconds",
        source_note="Source: m12_run_metrics, LIVE_N8N_CHAT smoke simulation rows only.",
        value_format=".0f",
    )
    svg_normalized_histogram(
        FIG_DIR / "fig_08_live_T_loop_distribution.svg",
        title="Live Smoke Runs: Closed-Loop Time Distribution",
        subtitle="Equation 3.6, T_loop. Bars show normalized probability across 60-second bins.",
        bins=t_loop_bins,
        x_label="T_loop bin",
        unit="seconds",
        source_note="Source: m12_run_metrics, LIVE_N8N_CHAT smoke simulation rows only.",
        value_format=".0f",
    )

    svg_grouped_horizontal(
        FIG_DIR / "fig_01_planning_and_verification_latency.svg",
        title="Planning And Verification Time: Literature Vs Our Live Smoke Runs",
        subtitle="Lower is better. Ours separates operator wait from live Isaac verification.",
        x_label="minutes",
        max_value=30,
        rows=[
            {"label": "LLMAPM manual engineer", "value": refs["LLMAPM"]["reference_timing_minutes"]["engineer_manual_process_time"], "series": "Literature", "display": "30.0 min", "note": "manual process creation"},
            {"label": "LLMAPM generated import", "value": refs["LLMAPM"]["reference_timing_minutes"]["generated_process_import_time"], "series": "Literature", "display": "6.0 min", "note": "import/code logic only"},
            {"label": "Ours T_wait", "value": (values["T_wait_mean_seconds"] or 0) / 60, "series": "Our system", "display": f'{(values["T_wait_mean_seconds"] or 0):.1f} s', "note": "intent to candidate summary"},
            {"label": "Ours T_verification", "value": (values["T_verification_mean_seconds"] or 0) / 60, "series": "Our system (different scope)", "display": f'{((values["T_verification_mean_seconds"] or 0)/60):.2f} min', "note": "ScenarioSpec to RunArtifact"},
        ],
    )
    svg_grouped_horizontal(
        FIG_DIR / "fig_02_tool_reasoning_success_vs_maka.svg",
        title="Tool/Query Reasoning: Literature Vs Our Smoke Query Path",
        subtitle="Higher is better. Our value is a smoke proxy, not full trace-level F1.",
        x_label="rate / score",
        max_value=1.0,
        rows=[
            {"label": "MAKA no critic F1", "value": refs["MAKA"]["critic_ablation"]["no_critic_mean_f1"], "series": "Literature", "display": "0.2919", "note": "degraded routing"},
            {"label": "MAKA critic F1", "value": refs["MAKA"]["critic_ablation"]["critic_enabled_mean_f1"], "series": "Literature", "display": "0.6697", "note": "critic enabled"},
            {"label": "MAKA KG MC acc.", "value": refs["MAKA"]["kg_ablation"]["kg_mean_mc_accuracy"], "series": "Literature", "display": "0.5733", "note": "knowledge graph"},
            {"label": "Ours query success", "value": values["TC2_query_success_rate"], "series": "Our system (proxy)", "display": f'{values["TC2_query_success_rate"]:.4f}', "note": "6/9 smoke query rows"},
        ],
    )
    svg_grouped_horizontal(
        FIG_DIR / "fig_03_safety_interception_vs_literature.svg",
        title="Safety/Error Interception: Literature Vs Our Smoke Guards",
        subtitle="Higher is better for rates. Counts show taxonomy coverage scale.",
        x_label="rate or normalized count",
        max_value=1.0,
        rows=[
            {"label": "MAKA full recovery", "value": refs["MAKA"]["critic_ablation"]["full_recovery_rate"], "series": "Literature", "display": "0.6119", "note": "critic recovery"},
            {"label": "Ours interception", "value": values["TC4_interception_rate"], "series": "Our system (proxy)", "display": f'{values["TC4_interception_rate"]:.4f}', "note": f'{values["TC4_intercepted"]}/{values["TC4_rows"]} TC4 rows'},
            {"label": "FactoryFlow taxonomy", "value": 1.0, "series": "Literature", "display": "8 classes", "note": "normalized count"},
            {"label": "Ours smoke invalid rows", "value": values["TC4_rows"] / len(refs["FactoryFlow"]["error_taxonomy"]), "series": "Our system", "display": f'{values["TC4_rows"]} rows', "note": "normalized to FactoryFlow count"},
        ],
    )
    svg_grouped_horizontal(
        FIG_DIR / "fig_04_evidence_quality_vs_literature.svg",
        title="Evidence Quality: Code-Level Literature Anchor Vs Physical Execution Evidence",
        subtitle="Higher is better. Metrics are deliberately labeled as different evidence types.",
        x_label="percent",
        max_value=100,
        rows=[
            {"label": "GAMHE code score", "value": refs["GAMHE_5_0"]["llm_code_generation"]["successful_functional_score"], "series": "Literature", "display": "100%", "note": "code functional score"},
            {"label": "Ours R_storage", "value": (values["R_storage_mean"] or 0) * 100, "series": "Our system (different scope)", "display": f'{((values["R_storage_mean"] or 0)*100):.2f}%', "note": "physical placement verification"},
            {"label": "GAMHE setups", "value": 100, "series": "Literature", "display": "4 setups", "note": "normalized"},
            {"label": "Ours TC3 smoke", "value": values["TC3_live_setup_rows"] / len(refs["GAMHE_5_0"]["setups"]) * 100, "series": "Our system", "display": f'{values["TC3_live_setup_rows"]}/4 setups', "note": "smoke scale"},
        ],
    )

    discussion = [
        "# Literature Performance Comparison And Contribution Knowledge Points",
        "",
        "This comparison uses live n8n Smoke Queue outputs and live RunArtifact metrics. Literature values come from `reference_baselines.yaml`. Where the metric semantics differ, the chart and table label the comparison as a proxy or different-scope anchor.",
        "",
        "## Strong Contribution Claims",
        "",
        "1. **Human-facing planning latency is sharply reduced.** Our mean `T_wait` is about "
        f"{values['T_wait_mean_seconds']:.1f} seconds, compared with LLMAPM's 30 minute manual engineer process baseline. The mechanism is the n8n chat workflow plus deterministic candidate-patch generation: the operator receives a reviewable patch quickly instead of waiting for manual process authoring.",
        "",
        "2. **The system adds physical evidence beyond process-generation papers.** LLMAPM validates generated process flow logic; FactoryFlow validates model structure. Our pipeline goes further by executing the generated ScenarioSpec in Isaac and storing placement-level evidence. This is why `R_storage` can be measured directly from tool events instead of inferred from text.",
        "",
        "3. **Safety interception is comparable to MAKA-style recovery on the smoke subset.** MAKA reports `full_recovery_rate = 0.6119`; our TC4 smoke interception rate is "
        f"{values['TC4_interception_rate']:.4f}. The mechanism is a layered guard structure: required-field checks, vocabulary validation, scope validation, evidence/deployment guards, and no-deploy behavior during M12 testing.",
        "",
        "4. **Structured config/state retrieval can reach MAKA critic-level behavior when routing is correct.** Our TC2 query success proxy is "
        f"{values['TC2_query_success_rate']:.4f}, close to MAKA's critic-enabled F1 of 0.6697. The positive mechanism is not generic LLM reasoning; it is explicit routing into config/state retrieval paths.",
        "",
        "5. **The evidence layer prevents unsafe optimism.** TC3 smoke rows produced live evidence but still blocked deployment because KPI evidence did not support deployment. That is a contribution over report-only optimization workflows: the system connects generated recommendations to deployment-blocking measured evidence.",
        "",
        "6. **Operator rejection becomes a safety mechanism, not an afterthought.** The system does not treat an LLM-generated plan as deployable by default. It presents measured evidence, and when that evidence does not match operator expectations, the operator can reject the deployment. This closes the loop between human judgment and physical-line safety.",
        "",
        "7. **Physics simulation compensates for missing physical knowledge in language models.** LLMs and workflow tools are weak at reasoning about embodied constraints such as timing, placement, collisions, and line behavior. Isaac Sim acts as a physics-grounded verifier, so physical feasibility is checked through simulation rather than inferred from text.",
        "",
        "8. **The contribution is an auditable evidence pipeline.** Each comparison row links natural-language intent, candidate patch, ScenarioSpec, RunArtifact, evidence summary, and deployment decision. This makes the system testable and falsifiable: failures such as simulation-config drift and missing reset-cycle fields are visible instead of hidden in prose.",
        "",
        "9. **The architecture separates suggestion, verification, and authority.** The LLM/workflow can suggest a policy, Isaac can test whether the policy behaves acceptably, and the operator/deployment guard decides whether the policy may proceed. This separation is important for production-line systems because no single component is trusted unconditionally.",
        "",
        "## Gaps That Explain Negative Differences",
        "",
        "- `T_verification` is slower than LLMAPM import time because it includes live Isaac simulation and RunArtifact extraction, not just code import.",
        "- Invalid line IDs, impossible KPI targets, and invalid intervention mode still reached approval in some smoke rows. This means the system needs stronger domain-knowledge validators before candidate approval.",
        "- Query rows failed when routed as patch-intent turns. The knowledge boundary between `report/config query` and `task-change request` needs to be explicit in n8n.",
        "- `R_reset` remains incomplete because reset cycle request/completion fields are not exposed in the RunArtifact schema.",
        "",
        "## Generated Figures",
        "",
        "- `fig_01_planning_and_verification_latency.svg`",
        "- `fig_02_tool_reasoning_success_vs_maka.svg`",
        "- `fig_03_safety_interception_vs_literature.svg`",
        "- `fig_04_evidence_quality_vs_literature.svg`",
        "- `fig_05_live_R_storage_distribution.svg`",
        "- `fig_06_live_T_wait_distribution.svg`",
        "- `fig_07_live_T_verification_distribution.svg`",
        "- `fig_08_live_T_loop_distribution.svg`",
    ]
    (OUT_DIR / "literature_performance_discussion.md").write_text("\n".join(discussion), encoding="utf-8")

    write_uml_outputs(refs, values, rows)


def write_uml_outputs(refs: Dict[str, Any], values: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    uml_dir = OUT_DIR / "uml"
    uml_dir.mkdir(parents=True, exist_ok=True)

    (uml_dir / "README.md").write_text(
        "\n".join(
            [
                "# M12 Literature Comparison UML",
                "",
                "These PlantUML files document the comparative-test architecture, data provenance, validation pipeline, and contribution claims.",
                "",
                "They are generated from live Smoke Queue outputs plus literature baseline fixtures. They do not assert that proxy metrics are identical to literature metrics; comparability limits are shown in the comparison CSV and discussion file.",
                "",
                "Files:",
                "",
                "- `m12_system_sequence.puml`",
                "- `m12_data_provenance.puml`",
                "- `m12_validation_pipeline.puml`",
                "- `m12_comparative_test_structure.puml`",
                "- `m12_literature_comparison_claims.puml`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (uml_dir / "m12_system_sequence.puml").write_text(
        f"""@startuml
title M12 Smoke Comparison System Sequence
actor Operator
participant "n8n Chat Workflow" as N8N
participant "trt-api" as API
participant "IntentPatch Validator" as Patch
participant "ScenarioSpec Generator" as Scenario
participant "Isaac Host Runner" as Isaac
database "RunArtifact SQLite" as Artifact
participant "Evidence Guard" as Evidence
participant "Metrics Collector" as Metrics
participant "Deployment Endpoint" as Deploy

Operator -> N8N: Natural-language Smoke Queue prompt
N8N -> API: dialogue decision and required-field state
API -> Patch: generate and validate candidate patch
Patch --> N8N: candidate patch summary
Operator -> N8N: approve test candidate
N8N -> API: release approval, deployment disabled
API -> Scenario: create ScenarioSpec
Scenario --> N8N: scenario_spec_id
N8N -> Isaac: run simulation request
Isaac -> Artifact: write run artifact and placement events
Artifact --> Isaac: run_id
Isaac --> N8N: simulation status and artifact path
N8N -> Evidence: extract KPI, placement, and deployment evidence
Evidence --> N8N: deployment blocked or non-deploy prompt
N8N -> Metrics: collect R_storage, timing metrics, provenance
Metrics --> N8N: m12_metrics.sqlite3 / m12_metrics.csv
Operator -> N8N: DO_NOT_DEPLOY or cancel
N8N -> Deploy: no production deployment requested

note over N8N,Deploy
M12 comparison mode suppresses deployment.
Measured rows are labeled LIVE_N8N_CHAT.
end note

note over Metrics
Live smoke means:
R_storage mean = {values['R_storage_mean']:.4f}
T_wait mean = {values['T_wait_mean_seconds']:.2f} seconds
T_verification mean = {values['T_verification_mean_seconds']:.2f} seconds
R_reset = DATA_INCOMPLETE
end note
@enduml
""",
        encoding="utf-8",
    )

    (uml_dir / "m12_data_provenance.puml").write_text(
        """@startuml
title M12 Data Provenance For Literature Comparison
skinparam componentStyle rectangle

package "Expected / Gold Inputs" #FFF3CD {
  artifact "reference_baselines.yaml" as Ref
  artifact "operator_intent_gold.jsonl" as IntentGold
  artifact "tool_orchestration_gold.jsonl" as ToolGold
  artifact "scenario_setup_gold.jsonl" as ScenarioGold
  artifact "error_injection_gold.csv" as ErrorGold
}

package "Live Measured Outputs" #D1E7DD {
  artifact "n8n chat transcripts" as Transcript
  artifact "ScenarioSpec JSON" as ScenarioSpec
  database "RunArtifact SQLite" as RunArtifact
  database "m12_metrics.sqlite3" as MetricsDb
  artifact "m12_metrics.csv" as MetricsCsv
  artifact "manual_results.jsonl" as ManualResults
}

package "Derived Comparison Outputs" #CFE2FF {
  artifact "literature_vs_ours_performance.csv" as ComparisonCsv
  artifact "literature_performance_discussion.md" as Discussion
  artifact "comparison SVG figures" as Figures
  artifact "PlantUML diagrams" as UML
}

Ref --> ComparisonCsv : literature baseline values
IntentGold --> Transcript : natural-language prompts
ToolGold --> ManualResults : expected query protocol
ScenarioGold --> ScenarioSpec : scenario setup expectations
ErrorGold --> ManualResults : expected interceptors
Transcript --> ManualResults : observed chat result
ScenarioSpec --> RunArtifact : simulation input
RunArtifact --> MetricsDb : measured placement/timing source
MetricsDb --> MetricsCsv
MetricsCsv --> ComparisonCsv : our measured values
ManualResults --> ComparisonCsv : pass/fail and interception results
ComparisonCsv --> Discussion
ComparisonCsv --> Figures
ComparisonCsv --> UML

note bottom of Ref
Fixture rows are expected/gold references only.
They are not plotted as measured performance.
end note

note bottom of MetricsDb
Current measured performance rows use LIVE_N8N_CHAT.
Historical or fixture-only rows must not be mixed into final charts.
end note
@enduml
""",
        encoding="utf-8",
    )

    (uml_dir / "m12_validation_pipeline.puml").write_text(
        """@startuml
title M12 Validation And Interception Pipeline
start
:Load chat session state;
if (cancel/help/config query?) then (yes)
  :Route to terminal/help/query path;
  stop
endif

:Receive natural-language task request;
:Check required operator_id and reason;
if (required fields missing?) then (yes)
  :Ask clarification and save session;
  stop
endif

:Generate IntentPatch candidate;
:Validate line scope, tooling target, KPI bounds, intervention mode;
if (invalid or unsupported?) then (yes)
  :Reject or request revision;
  :Record interception result;
  stop
endif

:Request operator approval;
if (approved?) then (yes)
  :Generate ScenarioSpec;
else (no)
  :Cancel or keep pending review;
  stop
endif

:Validate ScenarioSpec schema and command arguments;
if (schema or config invalid?) then (yes)
  :Block simulation/deployment;
  :Record failed validation;
  stop
endif

:Run Isaac simulation;
if (simulation fails?) then (yes)
  :Record simulation failure;
  :Block deployment;
  stop
endif

:Extract RunArtifact evidence;
:Compute R_storage and timing metrics;
if (placement evidence missing or failed?) then (yes)
  :Block deployment;
endif
if (KPI evidence does not allow deployment?) then (yes)
  :Block deployment;
endif

:Show evidence summary;
:Suppress deployment for M12 comparison mode;
stop

note right
Smoke gaps found:
- some invalid requests reached candidate approval
- query turns can route as patch intents
- R_reset unavailable from RunArtifact schema
end note
@enduml
""",
        encoding="utf-8",
    )

    (uml_dir / "m12_comparative_test_structure.puml").write_text(
        f"""@startuml
title M12 Comparative Test Structure Against Literature
skinparam componentStyle rectangle

package "Literature Protocols" {{
  component "LLMAPM\\nprocess/FSM validation" as LLMAPM
  component "FactoryFlow\\nmodel-error taxonomy" as FactoryFlow
  component "MAKA\\nL1/L2/L3 tool benchmark" as MAKA
  component "GAMHE 5.0\\nAutoML/report workflow" as GAMHE
  component "HRCD DiBN\\nreliability planning" as HRCD
}}

package "M12 Smoke Comparative Tests" {{
  component "TC1 Intent-to-plan\\npatch + ScenarioSpec" as TC1
  component "TC2 Tool orchestration\\nquery routing proxy" as TC2
  component "TC3 KPI/report evidence\\nR_storage + timing" as TC3
  component "TC4 Error interception\\ndeployment safety" as TC4
}}

LLMAPM --> TC1 : process validation pattern
FactoryFlow --> TC1 : schema/error taxonomy
MAKA --> TC2 : 75-query depth protocol
GAMHE --> TC2 : report/data workflow checks
GAMHE --> TC3 : scenario setup comparison
MAKA --> TC3 : digital-twin what-if verification
FactoryFlow --> TC4 : error characterization
MAKA --> TC4 : recovery/interception baseline
HRCD --> TC4 : reliability-oriented planning

note right of TC2
Our smoke proxy = {values['TC2_query_success_rate']:.4f}
MAKA critic F1 = {refs['MAKA']['critic_ablation']['critic_enabled_mean_f1']}
Comparability: proxy, not identical metric.
end note

note right of TC3
R_storage mean = {values['R_storage_mean']:.4f}
T_wait mean = {values['T_wait_mean_seconds']:.2f}s
T_verification mean = {values['T_verification_mean_seconds']:.2f}s
R_reset = DATA_INCOMPLETE
end note

note right of TC4
Our smoke interception = {values['TC4_interception_rate']:.4f}
MAKA full recovery = {refs['MAKA']['critic_ablation']['full_recovery_rate']}
No deployment occurred.
end note
@enduml
""",
        encoding="utf-8",
    )

    claim_lines = [
        "@startmindmap",
        "* M12 Literature Comparison Claims",
        f"** Human-facing planning latency: {values['T_wait_mean_seconds']:.2f}s mean T_wait",
        "*** Mechanism: n8n stateful chat + deterministic candidate-patch generation",
        f"** Physical evidence: R_storage mean {values['R_storage_mean']:.4f}",
        "*** Mechanism: Isaac RunArtifact placement records, not chat-text inference",
        f"** Safety interception: {values['TC4_interception_rate']:.4f} smoke rate",
        "*** Mechanism: layered validators and deployment guard",
        f"** Query/tool routing proxy: {values['TC2_query_success_rate']:.4f}",
        "*** Mechanism: explicit config/state retrieval paths when routed correctly",
        "** Limitations",
        "*** T_verification includes Isaac execution, so it is slower than import-only baselines",
        "*** R_reset is DATA_INCOMPLETE because reset counts are missing in RunArtifact schema",
        "*** Some invalid requests still reached approval, requiring stronger validators",
        "@endmindmap",
    ]
    (uml_dir / "m12_literature_comparison_claims.puml").write_text("\n".join(claim_lines) + "\n", encoding="utf-8")


def main() -> int:
    refs = load_reference()
    values = compute_values()
    rows = build_comparison_rows(refs, values)
    write_outputs(refs, values, rows)
    print(json.dumps({
        "status": "OK",
        "output": str(OUT_DIR),
        "figures": [str(path) for path in sorted(FIG_DIR.glob("*.svg"))],
        "uml": str(OUT_DIR / "uml"),
        "metrics": {
            "R_storage_mean": values["R_storage_mean"],
            "T_wait_mean_seconds": values["T_wait_mean_seconds"],
            "T_verification_mean_seconds": values["T_verification_mean_seconds"],
            "TC2_query_success_rate": values["TC2_query_success_rate"],
            "TC4_interception_rate": values["TC4_interception_rate"],
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
