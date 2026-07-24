from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from trt_core.repository import PROJECT_ROOT


M12_ROOT = PROJECT_ROOT / "outputs" / "reports" / "m12"
SOURCE_RUN_DIR = M12_ROOT / "automated_smoke_n8n_run_20260706_193732"
DEFAULT_OUTPUT = M12_ROOT / "reviewed_trial_20260706"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def esc(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fnum(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (e - s).total_seconds())


def short(value: str, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def load_reference() -> dict[str, Any]:
    return yaml.safe_load((M12_ROOT / "seed_data" / "reference_baselines.yaml").read_text(encoding="utf-8"))["reference_sources"]


def load_metrics_by_run(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = {
            str(row["run_id"]): dict(row)
            for row in connection.execute(
                """
                SELECT run_id, scenario_spec_id, R_storage, R_reset,
                       N_tool_storage_total, N_tool_storage_passed, N_failed_tool_storage,
                       C_reset_requested, C_reset_completed,
                       T_wait_seconds, T_verification_seconds, T_loop_seconds,
                       data_quality_status, data_quality_reason, data_source, is_live_test
                FROM m12_run_metrics
                WHERE run_id IS NOT NULL AND run_id != ''
                """
            )
        }
    finally:
        connection.close()
    return rows


def load_combined(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def timing_from_combined(combined: dict[str, Any]) -> dict[str, float | None]:
    turns = combined.get("turns") if isinstance(combined, dict) else None
    if not isinstance(turns, list) or not turns:
        return {"T_wait_seconds": None, "T_loop_seconds": None}
    first_started = turns[0].get("started_at_utc")
    last_completed = turns[-1].get("completed_at_utc")
    candidate_completed = None
    for turn in turns:
        text = str(turn.get("text", "")).lower()
        if any(
            token in text
            for token in [
                "candidate patch passed validation",
                "cannot be processed",
                "cannot be completed",
                "requires revision",
                "needs revision",
                "please revise",
                "please clarify",
                "could not tell what task change",
                "current kpi settings",
                "latest task requirement table",
                "state record details",
            ]
        ):
            candidate_completed = turn.get("completed_at_utc")
            break
    return {
        "T_wait_seconds": seconds_between(first_started, candidate_completed),
        "T_loop_seconds": seconds_between(first_started, last_completed),
    }


def file_elapsed_seconds(start_path: Path, end_path: Path) -> float | None:
    if not start_path.exists() or not end_path.exists():
        return None
    return max(0.0, end_path.stat().st_mtime - start_path.stat().st_mtime)


def transcript_response_excerpt(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    websocket: list[str] = []
    for match in re.finditer(r'"websocketText"\s*:\s*"((?:\\.|[^"])*)"', text):
        raw = match.group(1)
        try:
            websocket.append(json.loads(f'"{raw}"'))
        except json.JSONDecodeError:
            websocket.append(raw)
    if websocket:
        return short(websocket[-1], 360)
    messages = []
    for line in text.splitlines():
        if line.startswith("message: ") or line.startswith("operator_message: "):
            messages.append(line.split(": ", 1)[1])
    return short(messages[-1] if messages else "", 360)


def load_packet_rows(packet_dir: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for filename in [
        "tc1_intent_plan_manual.csv",
        "tc2_tool_orchestration_manual.csv",
        "tc3_kpi_report_manual.csv",
        "tc4_error_interception_manual.csv",
    ]:
        path = packet_dir / filename
        if not path.exists():
            continue
        for row in read_csv(path):
            rows[row["test_id"]] = row
    return rows


def build_rows(source_run_dir: Path, db_path: Path) -> list[dict[str, Any]]:
    results = read_csv(source_run_dir / "full_n8n_results_latest.csv")
    reviewed = {
        row["test_id"]: row
        for row in read_csv(source_run_dir / "human_reviewed" / "m12_smoke_human_reviewed.csv")
    }
    packet_rows = load_packet_rows(M12_ROOT / "manual_test_packet")
    metrics = load_metrics_by_run(db_path)
    rows: list[dict[str, Any]] = []
    for idx, result in enumerate(results, start=1):
        raw_id = result["test_id"]
        test_label = f"T{idx:02d}"
        packet = packet_rows.get(result.get("packet_test_id", ""), {})
        combined = load_combined(result.get("combined_execution_json", ""))
        timing = timing_from_combined(combined)
        run_id = result.get("run_id", "")
        scenario_spec_id = result.get("scenario_spec_id", "")
        metric = metrics.get(run_id, {})
        spec_path = PROJECT_ROOT / "outputs" / "scenario_specs" / f"{scenario_spec_id}.json"
        artifact_path = PROJECT_ROOT / "outputs" / "run_artifacts" / f"{run_id}.sqlite"
        file_verification = file_elapsed_seconds(spec_path, artifact_path) if run_id and scenario_spec_id else None
        db_verification = fnum(metric.get("T_verification_seconds"))
        if db_verification is not None and db_verification <= 0:
            db_verification = None
        transcript_path = source_run_dir / "manual_transcripts_snapshot" / f"{raw_id}.txt"
        total = fnum(metric.get("N_tool_storage_total"))
        failed = fnum(metric.get("N_failed_tool_storage"))
        passed = None if total is None or failed is None else max(0.0, total - failed)
        rows.append(
            {
                "test_label": test_label,
                "packet_test_id": result.get("packet_test_id", ""),
                "suite": result.get("suite", ""),
                "operator_instruction": packet.get("paste_into_n8n", ""),
                "system_response_excerpt": transcript_response_excerpt(transcript_path),
                "automated_status": result.get("status", ""),
                "reviewed_status": reviewed.get(raw_id, {}).get("human_binary_status", ""),
                "review_reason": reviewed.get(raw_id, {}).get("human_review_reason", ""),
                "scenario_spec_id": scenario_spec_id,
                "run_id": run_id,
                "R_storage": fnum(metric.get("R_storage")),
                "N_tool_storage_total": total,
                "N_tool_storage_passed": passed,
                "N_failed_tool_storage": failed,
                "T_wait_seconds": timing["T_wait_seconds"],
                "T_verification_seconds": db_verification if db_verification is not None else file_verification,
                "T_verification_source": "metrics database lifecycle timestamps" if db_verification is not None else ("ScenarioSpec-to-RunArtifact file timestamps" if file_verification is not None else "DATA_INCOMPLETE"),
                "T_loop_seconds": timing["T_loop_seconds"],
                "data_source": "LIVE_N8N_CHAT",
                "metric_data_quality_status": metric.get("data_quality_status", "DATA_INCOMPLETE" if run_id else "NO_RUN"),
            }
        )
    return rows


def equal_width_bins(values: list[float], *, minimum_bins: int = 6) -> list[dict[str, Any]]:
    if not values:
        return []
    bin_count = max(minimum_bins, min(10, math.ceil(math.sqrt(len(values))) + 3))
    lo = min(values)
    hi = max(values)
    if math.isclose(lo, hi):
        span = max(1.0, abs(lo) * 0.1)
        lo -= span / 2
        hi += span / 2
    width = (hi - lo) / bin_count
    total = len(values)
    bins = []
    for i in range(bin_count):
        left = lo + i * width
        right = hi if i == bin_count - 1 else lo + (i + 1) * width
        if i == bin_count - 1:
            count = sum(1 for value in values if left <= value <= right)
        else:
            count = sum(1 for value in values if left <= value < right)
        bins.append(
            {
                "bin_start": left,
                "bin_end": right,
                "count": count,
                "total": total,
                "probability": count / total if total else 0.0,
            }
        )
    return bins


def svg_grouped_horizontal(path: Path, *, title: str, subtitle: str, x_label: str, rows: list[dict[str, Any]], max_value: float) -> None:
    width = 1220
    row_h = 64
    margin_l = 340
    margin_r = 72
    margin_t = 92
    margin_b = 82
    height = margin_t + margin_b + len(rows) * row_h
    plot_w = width - margin_l - margin_r
    palette = {
        "Literature": "#475569",
        "This study": "#2563eb",
        "This study (proxy)": "#0f766e",
        "This study (different scope)": "#d97706",
    }
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfbf8"/>',
        f'<text x="{width/2}" y="34" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">{esc(title)}</text>',
        f'<text x="{width/2}" y="60" text-anchor="middle" font-family="Arial" font-size="13" fill="#475569">{esc(subtitle)}</text>',
        f'<line x1="{margin_l}" y1="{margin_t-14}" x2="{margin_l}" y2="{height-margin_b+16}" stroke="#111827"/>',
        f'<line x1="{margin_l}" y1="{height-margin_b+16}" x2="{width-margin_r}" y2="{height-margin_b+16}" stroke="#111827"/>',
    ]
    for i in range(6):
        value = max_value * i / 5
        x = margin_l + (value / max_value) * plot_w
        lines.append(f'<line x1="{x:.1f}" y1="{margin_t-14}" x2="{x:.1f}" y2="{height-margin_b+16}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="{x:.1f}" y="{height-margin_b+38}" text-anchor="middle" font-family="Arial" font-size="11" fill="#64748b">{value:.2g}</text>')
    for i, row in enumerate(rows):
        y = margin_t + i * row_h
        value = float(row["value"])
        bar_w = max(0.0, (value / max_value) * plot_w)
        color = palette.get(row.get("series", ""), "#64748b")
        lines.append(f'<text x="{margin_l-18}" y="{y+18}" text-anchor="end" font-family="Arial" font-size="13" font-weight="700" fill="#111827">{esc(row["label"])}</text>')
        lines.append(f'<text x="{margin_l-18}" y="{y+38}" text-anchor="end" font-family="Arial" font-size="11" fill="#64748b">{esc(row.get("note", ""))}</text>')
        lines.append(f'<rect x="{margin_l}" y="{y}" width="{bar_w:.1f}" height="24" rx="3" fill="{color}"/>')
        lines.append(f'<text x="{margin_l + bar_w + 8:.1f}" y="{y+17}" font-family="Arial" font-size="12" fill="#111827">{esc(row.get("display", f"{value:.3g}"))}</text>')
    lines.append(f'<text x="{width/2}" y="{height-20}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569">{esc(x_label)}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def svg_r_storage_by_run(path: Path, rows: list[dict[str, Any]]) -> None:
    data = [row for row in rows if row.get("R_storage") is not None]
    width = 1300
    height = 670
    margin_l = 84
    margin_r = 42
    margin_t = 96
    margin_b = 134
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfbf8"/>',
        f'<text x="{width/2}" y="34" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">Placement Verification Pass Rate By Run</text>',
        f'<text x="{width/2}" y="60" text-anchor="middle" font-family="Arial" font-size="13" fill="#475569">Equation 3.2. Run-level bars preserve raw placement counts, which is more appropriate than a distribution for this sample size.</text>',
    ]
    if not data:
        lines.append(f'<text x="{width/2}" y="{height/2}" text-anchor="middle" font-family="Arial" font-size="18" fill="#b91c1c">No valid live R_storage data available</text>')
        lines.append("</svg>")
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    mean_v = mean([float(row["R_storage"]) for row in data]) or 0.0
    bar_gap = 18
    bar_w = max(34, (plot_w - bar_gap * (len(data) - 1)) / len(data))
    lines.extend([
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t+plot_h}" stroke="#111827"/>',
        f'<line x1="{margin_l}" y1="{margin_t+plot_h}" x2="{width-margin_r}" y2="{margin_t+plot_h}" stroke="#111827"/>',
    ])
    for i in range(6):
        value = i / 5
        y = margin_t + plot_h - value * plot_h
        lines.append(f'<line x1="{margin_l}" y1="{y:.1f}" x2="{width-margin_r}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="{margin_l-12}" y="{y+4:.1f}" text-anchor="end" font-family="Arial" font-size="11" fill="#64748b">{value:.1f}</text>')
    mean_y = margin_t + plot_h - mean_v * plot_h
    lines.append(f'<line x1="{margin_l}" y1="{mean_y:.1f}" x2="{width-margin_r}" y2="{mean_y:.1f}" stroke="#b45309" stroke-width="2" stroke-dasharray="7 5"/>')
    lines.append(f'<text x="{width-margin_r-4}" y="{mean_y-8:.1f}" text-anchor="end" font-family="Arial" font-size="12" font-weight="700" fill="#92400e">mean {mean_v:.3f}</text>')
    for i, row in enumerate(data):
        value = float(row["R_storage"])
        x = margin_l + i * (bar_w + bar_gap)
        bar_h = value * plot_h
        y = margin_t + plot_h - bar_h
        total = fnum(row.get("N_tool_storage_total")) or 0
        failed = fnum(row.get("N_failed_tool_storage")) or 0
        color = "#2563eb" if math.isclose(value, 1.0) else "#dc2626"
        lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="4" fill="{color}"/>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{y-8:.1f}" text-anchor="middle" font-family="Arial" font-size="11" font-weight="700" fill="#111827">{value:.2f}</text>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{margin_t+plot_h+22}" text-anchor="middle" font-family="Arial" font-size="11" fill="#111827" transform="rotate(-35 {x+bar_w/2:.1f},{margin_t+plot_h+22})">{esc(row["test_label"])}</text>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{margin_t+plot_h+72}" text-anchor="middle" font-family="Arial" font-size="10" fill="#64748b">{int(total-failed)}/{int(total)} pass</text>')
    lines.append(f'<text x="26" y="{margin_t+plot_h/2}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569" transform="rotate(-90 26,{margin_t+plot_h/2})">R_storage</text>')
    lines.append(f'<text x="{width/2}" y="{height-24}" text-anchor="middle" font-family="Arial" font-size="11" fill="#64748b">Source: metrics database joined by RunArtifact ID; live chat trial rows only.</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def svg_time_distribution(path: Path, *, title: str, metric: str, values: list[float], source_note: str) -> list[dict[str, Any]]:
    bins = equal_width_bins(values)
    width = 1280
    height = 670
    margin_l = 88
    margin_r = 42
    margin_t = 98
    margin_b = 122
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfbf8"/>',
        f'<text x="{width/2}" y="34" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">{esc(title)}</text>',
        f'<text x="{width/2}" y="60" text-anchor="middle" font-family="Arial" font-size="13" fill="#475569">Equal-width time bins. Bars show normalized probability; vertical markers show mean and maximum.</text>',
    ]
    if not bins:
        lines.append(f'<text x="{width/2}" y="{height/2}" text-anchor="middle" font-family="Arial" font-size="18" fill="#b91c1c">No valid live data available</text>')
        lines.append("</svg>")
        path.write_text("\n".join(lines), encoding="utf-8")
        return []
    max_p = min(1.0, max(0.2, math.ceil(max(row["probability"] for row in bins) * 10) / 10))
    x_lo = bins[0]["bin_start"]
    x_hi = bins[-1]["bin_end"]
    x_span = max(1e-9, x_hi - x_lo)
    bar_gap = 14
    bar_w = max(48, (plot_w - bar_gap * (len(bins) - 1)) / len(bins))
    lines.extend([
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t+plot_h}" stroke="#111827"/>',
        f'<line x1="{margin_l}" y1="{margin_t+plot_h}" x2="{width-margin_r}" y2="{margin_t+plot_h}" stroke="#111827"/>',
    ])
    for i in range(6):
        probability = max_p * i / 5
        y = margin_t + plot_h - (probability / max_p) * plot_h
        lines.append(f'<line x1="{margin_l}" y1="{y:.1f}" x2="{width-margin_r}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="{margin_l-12}" y="{y+4:.1f}" text-anchor="end" font-family="Arial" font-size="11" fill="#64748b">{probability:.2f}</text>')
    for i, row in enumerate(bins):
        x = margin_l + i * (bar_w + bar_gap)
        bar_h = (row["probability"] / max_p) * plot_h
        y = margin_t + plot_h - bar_h
        label = f'{row["bin_start"]:.1f}-{row["bin_end"]:.1f}'
        lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="4" fill="#2563eb"/>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{y-8:.1f}" text-anchor="middle" font-family="Arial" font-size="12" font-weight="700" fill="#111827">{row["probability"]:.2f}</text>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{margin_t+plot_h+24}" text-anchor="middle" font-family="Arial" font-size="10" fill="#111827">{esc(label)}</text>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{margin_t+plot_h+42}" text-anchor="middle" font-family="Arial" font-size="10" fill="#64748b">{row["count"]}/{row["total"]}</text>')
    mean_v = mean(values) or 0.0
    max_v = max(values)
    for marker, color, label, dy in [(mean_v, "#b45309", f"mean {mean_v:.1f}s", -10), (max_v, "#991b1b", f"max {max_v:.1f}s", 16)]:
        x = margin_l + ((marker - x_lo) / x_span) * plot_w
        lines.append(f'<line x1="{x:.1f}" y1="{margin_t}" x2="{x:.1f}" y2="{margin_t+plot_h}" stroke="{color}" stroke-width="2" stroke-dasharray="7 5"/>')
        lines.append(f'<text x="{min(width-margin_r-4, max(margin_l+4, x+4)):.1f}" y="{margin_t+dy:.1f}" font-family="Arial" font-size="12" font-weight="700" fill="{color}">{esc(label)}</text>')
    lines.append(f'<text x="26" y="{margin_t+plot_h/2}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569" transform="rotate(-90 26,{margin_t+plot_h/2})">normalized probability</text>')
    lines.append(f'<text x="{width/2}" y="{height-50}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569">{esc(metric)} bin (seconds)</text>')
    lines.append(f'<text x="{width/2}" y="{height-24}" text-anchor="middle" font-family="Arial" font-size="11" fill="#64748b">{esc(source_note)}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")
    return [{"metric": metric, **row, "mean": mean_v, "max": max_v} for row in bins]


def fmt_num(value: Any, digits: int = 2) -> str:
    number = fnum(value)
    return "null" if number is None else f"{number:.{digits}f}"


def markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for index, row in enumerate(rows):
        lines.append("| " + " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) + " |")
        if index == 0:
            lines.append("| " + " | ".join("-" * widths[i] for i in range(len(row))) + " |")
    return "\n".join(lines)


def write_report(output: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    suite_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        suite = row["suite"]
        suite_counts.setdefault(suite, {"PASS": 0, "FAIL": 0})
        if row["reviewed_status"] in {"PASS", "FAIL"}:
            suite_counts[suite][row["reviewed_status"]] += 1
    metric_rows = [row for row in rows if row.get("run_id")]
    detail_rows = [
        [
            "Trial",
            "Suite",
            "Operator instruction",
            "System response excerpt",
            "Reviewed result",
            "Reason",
        ]
    ]
    for row in rows:
        detail_rows.append(
            [
                row["test_label"],
                row["suite"],
                short(row["operator_instruction"], 120),
                short(row["system_response_excerpt"], 150),
                row["reviewed_status"],
                short(row["review_reason"], 170),
            ]
        )
    metric_detail = [["Trial", "RunArtifact", "R_storage", "Placement passed/total", "T_wait (s)", "T_verification (s)", "T_loop (s)", "Verification source"]]
    for row in metric_rows:
        total = fnum(row.get("N_tool_storage_total"))
        passed = fnum(row.get("N_tool_storage_passed"))
        placement = "null" if total is None or total == 0 else f"{int(passed or 0)}/{int(total)}"
        metric_detail.append(
            [
                row["test_label"],
                f'`{row["run_id"]}`',
                fmt_num(row.get("R_storage"), 2),
                placement,
                fmt_num(row.get("T_wait_seconds"), 2),
                fmt_num(row.get("T_verification_seconds"), 2),
                fmt_num(row.get("T_loop_seconds"), 2),
                row.get("T_verification_source", ""),
            ]
        )
    lines = [
        "# Milestone 12 Reviewed Trial Engineering Technical Report",
        "",
        "Date generated: 2026-07-09",
        "",
        "## 1. Purpose",
        "",
        "This document summarizes the reviewed Milestone 12 trial run used before a larger comparison campaign. The run exercised natural-language operator requests, n8n chat routing, TRT patch review, ScenarioSpec generation, Isaac Sim execution, evidence extraction, and deployment-safety checks.",
        "",
        "The final result uses reviewed binary adjudication rather than the raw automated status. The automated runner is treated as an evidence collector and first-pass classifier.",
        "",
        "## 2. Result Summary",
        "",
        f"- Reviewed PASS: {summary['pass_count']}",
        f"- Reviewed FAIL: {summary['fail_count']}",
        f"- Reviewed pass rate: {summary['pass_rate']:.4f}",
        f"- Live simulation rows: {summary['simulation_rows']}",
        f"- Mean R_storage: {fmt_num(summary['R_storage_mean'], 4)}",
        f"- Mean T_wait: {fmt_num(summary['T_wait_mean_seconds'], 2)} seconds",
        f"- Mean T_verification: {fmt_num(summary['T_verification_mean_seconds'], 2)} seconds",
        f"- Mean T_loop: {fmt_num(summary['T_loop_mean_seconds'], 2)} seconds",
        "",
        "## 3. Suite-Level Outcomes",
        "",
        markdown_table([["Suite", "PASS", "FAIL"]] + [[suite, str(counts["PASS"]), str(counts["FAIL"])] for suite, counts in sorted(suite_counts.items())]),
        "",
        "## 4. Figures",
        "",
        "The figures below are SVG-only. Placement verification is shown by run rather than as a distribution, because the metric is a pass rate with meaningful per-run raw counts. Timing distributions use equal-width bins and include mean and maximum markers.",
        "",
        "### Planning And Verification Time",
        "",
        "![Planning And Verification Time](figures/fig_01_planning_and_verification_latency.svg)",
        "",
        "### Tool/Query Reasoning",
        "",
        "![Tool Query Reasoning](figures/fig_02_tool_reasoning_success_vs_maka.svg)",
        "",
        "### Safety/Error Interception",
        "",
        "![Safety Error Interception](figures/fig_03_safety_interception_vs_literature.svg)",
        "",
        "### Evidence Quality",
        "",
        "![Evidence Quality](figures/fig_04_evidence_quality_vs_literature.svg)",
        "",
        "### Placement Verification Pass Rate By Run",
        "",
        "![Placement Verification Pass Rate By Run](figures/fig_05_R_storage_by_run.svg)",
        "",
        "### Operator Wait Time Distribution",
        "",
        "![Operator Wait Time Distribution](figures/fig_06_T_wait_distribution.svg)",
        "",
        "### Verification Time Distribution",
        "",
        "![Verification Time Distribution](figures/fig_07_T_verification_distribution.svg)",
        "",
        "### Closed-Loop Elapsed Time Distribution",
        "",
        "![Closed Loop Time Distribution](figures/fig_08_T_loop_distribution.svg)",
        "",
        "## 5. Engineering Findings",
        "",
        "The system successfully answered several configuration and state queries that the automated trace scorer originally marked as failed. This shows that internal tool-trace matching is too brittle to serve as the final experimental judgment.",
        "",
        "The system also showed clear validator gaps. Invalid or unsafe values such as an out-of-range line identifier, an impossible throughput target, and an unsupported intervention mode reached candidate approval. These should be deterministic validation failures before any approval path.",
        "",
        "The strongest evidence contribution is the ability to connect natural-language planning with physical simulation artifacts. However, formal lifecycle event logging must be strengthened before this can be treated as a complete final comparison dataset.",
        "",
        "## Appendix A. Metric Calculation Method",
        "",
        "### A.1 Placement Verification Pass Rate",
        "",
        "`R_storage = (N_tool_storage_total - N_failed_tool_storage) / N_tool_storage_total`",
        "",
        "For each run, `N_tool_storage_total` and `N_failed_tool_storage` were read from `m12_run_metrics.sqlite3`. If `N_tool_storage_total` was zero, `R_storage` was treated as `null` and not plotted as a successful placement result.",
        "",
        "### A.2 Operator Wait Time",
        "",
        "`T_wait = T_candidate_or_answer_ready - T_first_operator_turn`",
        "",
        "The trial did not yet persist complete formal M12 event-log timestamps for every row. Therefore, `T_wait` was calculated from the combined n8n execution JSON as a proxy: first chat turn start to the first candidate, answer, clarification, or revision response that completed the operator-facing step.",
        "",
        "### A.3 Verification Time",
        "",
        "`T_verification = T_artifact_created - T_scenario_created`",
        "",
        "The report prefers a positive formal `m12_run_metrics.T_verification_seconds` value computed from lifecycle timestamps. File timestamps from `outputs/scenario_specs/<scenario_spec_id>.json` and `outputs/run_artifacts/<run_id>.sqlite` are used only as a fallback when lifecycle timestamps are missing or non-positive. This matters because copied/regenerated files can make modification-time deltas larger than the actual closed-loop chat duration, while incomplete lifecycle rows can produce zero-duration artifacts.",
        "",
        "### A.4 Closed-Loop Elapsed Time",
        "",
        "`T_loop = T_last_recorded_test_turn - T_first_operator_turn`",
        "",
        "The trial did not perform deployment. `T_loop` is therefore an automated no-deploy test-loop proxy from combined execution turn timestamps, not a human deployment-review duration. It should be compared with `T_verification` only when both values come from the same lifecycle source; otherwise the report labels the proxy source explicitly.",
        "",
        "The summary mean for `T_loop` is calculated over all reviewed trial rows that have chat-turn timestamps, including fast query, clarification, and rejection cases. The summary mean for `T_verification` is calculated only over simulation rows. Therefore the two summary means are not a nested timing comparison; use Appendix C for row-level simulation comparisons.",
        "",
        "### A.5 Reviewed Pass/Fail",
        "",
        "A row was marked `PASS` if the actual test purpose was satisfied. It was marked `FAIL` if the system did not satisfy the test purpose, if a safety-critical invalid value reached candidate approval, or if missing evidence prevented proving success.",
        "",
        "## Appendix B. Detailed Test Record",
        "",
        markdown_table(detail_rows),
        "",
        "## Appendix C. Metric Rows Used For Figures",
        "",
        markdown_table(metric_detail),
        "",
        "## Appendix D. Data Quality Notes",
        "",
        "- Raw automated statuses were not used as final results without review.",
        "- Timing values are real measurements from stored execution/file timestamps, but several are proxies because formal event rows were incomplete.",
        "- Rows with `R_storage = null` had no coordinate-based placement records and were not treated as successful placement evidence.",
        "- The trial did not deploy to a production line.",
        "- Source CSV files retain internal raw identifiers for traceability, but the report uses neutral trial labels.",
    ]
    (output / "M12_REVIEWED_TRIAL_ENGINEERING_TECHNICAL_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate corrected M12 reviewed trial report and SVG charts.")
    parser.add_argument("--source-run-dir", default=str(SOURCE_RUN_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--db", default=str(M12_ROOT / "m12_metrics.sqlite3"))
    args = parser.parse_args()
    source_run_dir = Path(args.source_run_dir)
    if not source_run_dir.is_absolute():
        source_run_dir = PROJECT_ROOT / source_run_dir
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    fig_dir = output / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    refs = load_reference()
    rows = build_rows(source_run_dir, db_path)
    write_csv(
        output / "reviewed_trial_source_data.csv",
        rows,
        [
            "test_label", "packet_test_id", "suite", "operator_instruction",
            "system_response_excerpt", "automated_status", "reviewed_status", "review_reason",
            "scenario_spec_id", "run_id", "R_storage", "N_tool_storage_total", "N_tool_storage_passed",
            "N_failed_tool_storage", "T_wait_seconds", "T_verification_seconds", "T_verification_source",
            "T_loop_seconds", "data_source", "metric_data_quality_status",
        ],
    )
    tc2 = [row for row in rows if row["suite"] == "TC2"]
    tc3 = [row for row in rows if row["suite"] == "TC3"]
    tc4 = [row for row in rows if row["suite"] == "TC4"]
    sim_rows = [row for row in rows if row["run_id"]]
    r_storage = [row["R_storage"] for row in sim_rows if row["R_storage"] is not None]
    t_wait = [row["T_wait_seconds"] for row in rows if row["T_wait_seconds"] is not None]
    t_ver = [row["T_verification_seconds"] for row in sim_rows if row["T_verification_seconds"] is not None]
    t_loop = [row["T_loop_seconds"] for row in rows if row["T_loop_seconds"] is not None]
    pass_count = sum(1 for row in rows if row["reviewed_status"] == "PASS")
    fail_count = sum(1 for row in rows if row["reviewed_status"] == "FAIL")
    tc2_pass = sum(1 for row in tc2 if row["reviewed_status"] == "PASS")
    tc4_pass = sum(1 for row in tc4 if row["reviewed_status"] == "PASS")

    svg_grouped_horizontal(
        fig_dir / "fig_01_planning_and_verification_latency.svg",
        title="Planning And Verification Time: Literature Vs This Study's Trial Run",
        subtitle="Lower is better. Literature anchors are process-generation/import times; this study uses live chat and Isaac-derived timings.",
        x_label="minutes",
        max_value=30,
        rows=[
            {"label": "LLMAPM generated import", "value": refs["LLMAPM"]["reference_timing_minutes"]["generated_process_import_time"], "series": "Literature", "display": "6 min", "note": "import/code logic only"},
            {"label": "LLMAPM manual engineer", "value": refs["LLMAPM"]["reference_timing_minutes"]["engineer_manual_process_time"], "series": "Literature", "display": "30 min", "note": "manual process creation"},
            {"label": "This study's T_wait mean", "value": (mean(t_wait) or 0) / 60, "series": "This study", "display": f"{(mean(t_wait) or 0):.1f}s", "note": "automated chat wait proxy"},
            {"label": "This study's T_verification mean", "value": (mean(t_ver) or 0) / 60, "series": "This study (different scope)", "display": f"{(mean(t_ver) or 0):.1f}s", "note": "ScenarioSpec file to RunArtifact file"},
        ],
    )
    svg_grouped_horizontal(
        fig_dir / "fig_02_tool_reasoning_success_vs_maka.svg",
        title="Tool/Query Reasoning: Literature Vs This Study's Chat Path",
        subtitle="Higher is better. This study uses reviewed binary outcomes from query rows.",
        x_label="rate / score",
        max_value=1.0,
        rows=[
            {"label": "MAKA no critic F1", "value": refs["MAKA"]["critic_ablation"]["no_critic_mean_f1"], "series": "Literature", "display": "0.2919", "note": "reference degraded routing"},
            {"label": "MAKA critic F1", "value": refs["MAKA"]["critic_ablation"]["critic_enabled_mean_f1"], "series": "Literature", "display": "0.6697", "note": "reference critic enabled"},
            {"label": "MAKA KG MC acc.", "value": refs["MAKA"]["kg_ablation"]["kg_mean_mc_accuracy"], "series": "Literature", "display": "0.5733", "note": "reference KG multiple-choice accuracy"},
            {"label": "This study's TC2 pass", "value": tc2_pass / len(tc2), "series": "This study (proxy)", "display": f"{tc2_pass}/{len(tc2)}", "note": "reviewed query rows"},
        ],
    )
    svg_grouped_horizontal(
        fig_dir / "fig_03_safety_interception_vs_literature.svg",
        title="Safety/Error Interception: Literature Vs This Study's Guards",
        subtitle="Higher is better. This study counts clarification/refusal before deployment as successful interception.",
        x_label="rate or normalized coverage",
        max_value=1.0,
        rows=[
            {"label": "MAKA full recovery", "value": refs["MAKA"]["critic_ablation"]["full_recovery_rate"], "series": "Literature", "display": "0.6119", "note": "reference recovery rate"},
            {"label": "FactoryFlow taxonomy", "value": 1.0, "series": "Literature", "display": f'{len(refs["FactoryFlow"]["error_taxonomy"])} classes', "note": "reference error-taxonomy coverage"},
            {"label": "This study's TC4 pass", "value": tc4_pass / len(tc4), "series": "This study (proxy)", "display": f"{tc4_pass}/{len(tc4)}", "note": "reviewed error rows"},
            {"label": "This study's coverage", "value": min(1.0, len(tc4) / len(refs["FactoryFlow"]["error_taxonomy"])), "series": "This study", "display": f"{len(tc4)} rows", "note": "normalized to FactoryFlow taxonomy"},
        ],
    )
    svg_grouped_horizontal(
        fig_dir / "fig_04_evidence_quality_vs_literature.svg",
        title="Evidence Quality: Literature Anchors Vs This Study's Physical Evidence",
        subtitle="Higher is better. This study measures physical placement evidence from live RunArtifacts.",
        x_label="percent / normalized score",
        max_value=100,
        rows=[
            {"label": "GAMHE code score", "value": refs["GAMHE_5_0"]["llm_code_generation"]["successful_functional_score"], "series": "Literature", "display": "100", "note": "reference functional code score"},
            {"label": "GAMHE setups", "value": 100, "series": "Literature", "display": f'{len(refs["GAMHE_5_0"]["setups"])} setups', "note": "reference optimisation setup count"},
            {"label": "This study's metric rows", "value": len([r for r in sim_rows if r["R_storage"] is not None]) / max(1, len(sim_rows)) * 100, "series": "This study", "display": f'{len([r for r in sim_rows if r["R_storage"] is not None])}/{len(sim_rows)}', "note": "RunArtifacts with R_storage"},
            {"label": "This study's R_storage mean", "value": (mean(r_storage) or 0) * 100, "series": "This study", "display": f"{(mean(r_storage) or 0):.3f}", "note": "placement verification pass rate"},
            {"label": "This study's TC3 trial", "value": len([r for r in tc3 if r["reviewed_status"] == "PASS"]) / len(refs["GAMHE_5_0"]["setups"]) * 100, "series": "This study (proxy)", "display": f'{len([r for r in tc3 if r["reviewed_status"] == "PASS"])}/4 setups', "note": "trial scale"},
        ],
    )
    svg_r_storage_by_run(fig_dir / "fig_05_R_storage_by_run.svg", sim_rows)
    distribution_rows: list[dict[str, Any]] = []
    distribution_rows += svg_time_distribution(
        fig_dir / "fig_06_T_wait_distribution.svg",
        title="Operator Wait Time Distribution",
        metric="T_wait_seconds",
        values=t_wait,
        source_note="Source: combined execution JSON turn timestamps; live chat trial rows.",
    )
    distribution_rows += svg_time_distribution(
        fig_dir / "fig_07_T_verification_distribution.svg",
        title="Verification Time Distribution",
        metric="T_verification_seconds",
        values=t_ver,
        source_note="Source: ScenarioSpec and RunArtifact file timestamps for simulation rows.",
    )
    distribution_rows += svg_time_distribution(
        fig_dir / "fig_08_T_loop_distribution.svg",
        title="Closed-Loop Elapsed Time Distribution",
        metric="T_loop_seconds",
        values=t_loop,
        source_note="Source: combined execution JSON turn timestamps; automated no-deploy loop.",
    )
    write_csv(output / "time_distribution_source.csv", distribution_rows, ["metric", "bin_start", "bin_end", "count", "total", "probability", "mean", "max"])
    summary = {
        "rows": len(rows),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "pass_rate": pass_count / len(rows),
        "simulation_rows": len(sim_rows),
        "R_storage_mean": mean(r_storage),
        "T_wait_mean_seconds": mean(t_wait),
        "T_verification_mean_seconds": mean(t_ver),
        "T_loop_mean_seconds": mean(t_loop),
        "output": str(output),
    }
    (output / "reviewed_trial_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_report(output, rows, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
