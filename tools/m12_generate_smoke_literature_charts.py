from __future__ import annotations

import csv
import json
import math
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from trt_core.repository import PROJECT_ROOT


M12_ROOT = PROJECT_ROOT / "outputs" / "reports" / "m12"
DEFAULT_RUN_DIR = M12_ROOT / "automated_smoke_n8n_run_20260706_193732"
DEFAULT_OUTPUT = DEFAULT_RUN_DIR / "smoke_literature_charts"


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


def build_rows(run_dir: Path, db_path: Path) -> list[dict[str, Any]]:
    results = read_csv(run_dir / "full_n8n_results_latest.csv")
    reviewed_path = run_dir / "human_reviewed" / "m12_smoke_human_reviewed.csv"
    reviewed = {row["test_id"]: row for row in read_csv(reviewed_path)} if reviewed_path.exists() else {}
    metrics = load_metrics_by_run(db_path)
    rows: list[dict[str, Any]] = []
    for result in results:
        test_id = result["test_id"]
        combined = load_combined(result.get("combined_execution_json", ""))
        timing = timing_from_combined(combined)
        run_id = result.get("run_id", "")
        scenario_spec_id = result.get("scenario_spec_id", "")
        metric = metrics.get(run_id, {})
        db_verification = fnum(metric.get("T_verification_seconds"))
        row = {
            "test_id": test_id,
            "packet_test_id": result.get("packet_test_id", ""),
            "suite": result.get("suite", ""),
            "automated_status": result.get("status", ""),
            "human_binary_status": reviewed.get(test_id, {}).get("human_binary_status", ""),
            "run_id": run_id,
            "scenario_spec_id": scenario_spec_id,
            "R_storage": fnum(metric.get("R_storage")),
            "N_tool_storage_total": metric.get("N_tool_storage_total"),
            "N_tool_storage_passed": metric.get("N_tool_storage_passed"),
            "N_failed_tool_storage": metric.get("N_failed_tool_storage"),
            "T_wait_seconds": timing["T_wait_seconds"],
            "T_verification_seconds": db_verification,
            "T_verification_source": "m12_run_metrics.sqlite3_startup_excluded" if db_verification is not None else "DATA_INCOMPLETE",
            "T_loop_seconds": timing["T_loop_seconds"],
            "data_source": "LIVE_N8N_CHAT",
            "metric_data_quality_status": metric.get("data_quality_status", "DATA_INCOMPLETE" if run_id else "NO_RUN"),
        }
        rows.append(row)
    return rows


def five_equal_bins(values: list[float]) -> list[dict[str, Any]]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if math.isclose(lo, hi):
        span = max(1.0, abs(lo) * 0.1)
        lo -= span / 2
        hi += span / 2
    width = (hi - lo) / 5
    bins: list[dict[str, Any]] = []
    total = len(values)
    for i in range(5):
        left = lo + i * width
        right = hi if i == 4 else lo + (i + 1) * width
        if i == 4:
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
    margin_l = 330
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
        bar_w = max(0, (value / max_value) * plot_w)
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
        f'<text x="{width/2}" y="34" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">Figure 5. Placement Verification Pass Rate By Smoke Run</text>',
        f'<text x="{width/2}" y="60" text-anchor="middle" font-family="Arial" font-size="13" fill="#475569">Equation 3.2. Run-level bars preserve raw placement counts, which is more appropriate than a distribution for this small smoke sample.</text>',
    ]
    if not data:
        lines.append(f'<text x="{width/2}" y="{height/2}" text-anchor="middle" font-family="Arial" font-size="18" fill="#b91c1c">No valid live R_storage data available</text>')
        lines.append("</svg>")
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    max_v = 1.0
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
        failed = fnum(row.get("N_failed_tool_storage")) or 0
        total = fnum(row.get("N_tool_storage_total")) or 0
        color = "#2563eb" if math.isclose(value, 1.0) else "#dc2626"
        lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="4" fill="{color}"/>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{y-8:.1f}" text-anchor="middle" font-family="Arial" font-size="11" font-weight="700" fill="#111827">{value:.2f}</text>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{margin_t+plot_h+22}" text-anchor="middle" font-family="Arial" font-size="11" fill="#111827" transform="rotate(-35 {x+bar_w/2:.1f},{margin_t+plot_h+22})">{esc(row["test_id"])}</text>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{margin_t+plot_h+72}" text-anchor="middle" font-family="Arial" font-size="10" fill="#64748b">{int(total-failed)}/{int(total)} pass</text>')
        lines.append(f'<title>{esc(row["test_id"])} {esc(row["run_id"])} R_storage={value:.3f}; passed={int(total-failed)}, failed={int(failed)}, total={int(total)}</title>')
    lines.append(f'<text x="26" y="{margin_t+plot_h/2}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569" transform="rotate(-90 26,{margin_t+plot_h/2})">R_storage</text>')
    lines.append(f'<text x="{width/2}" y="{height-24}" text-anchor="middle" font-family="Arial" font-size="11" fill="#64748b">Source: m12_run_metrics.sqlite3 joined by RunArtifact ID; LIVE_N8N_CHAT smoke simulation rows only.</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def svg_time_distribution(path: Path, *, figure_no: int, title: str, metric: str, values: list[float], unit: str, source_note: str) -> list[dict[str, Any]]:
    bins = five_equal_bins(values)
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
        f'<text x="{width/2}" y="34" text-anchor="middle" font-family="Arial" font-size="24" font-weight="700" fill="#111827">Figure {figure_no}. {esc(title)}</text>',
        f'<text x="{width/2}" y="60" text-anchor="middle" font-family="Arial" font-size="13" fill="#475569">Five equal-width time bins. Bars show normalized probability; vertical markers show mean and maximum.</text>',
    ]
    if not bins:
        lines.append(f'<text x="{width/2}" y="{height/2}" text-anchor="middle" font-family="Arial" font-size="18" fill="#b91c1c">No valid live data available</text>')
        lines.append("</svg>")
        path.write_text("\n".join(lines), encoding="utf-8")
        return []
    max_p = max(0.2, max(row["probability"] for row in bins))
    max_p = min(1.0, math.ceil(max_p * 10) / 10)
    x_lo = bins[0]["bin_start"]
    x_hi = bins[-1]["bin_end"]
    x_span = max(1e-9, x_hi - x_lo)
    bar_gap = 22
    bar_w = max(64, (plot_w - bar_gap * 4) / 5)
    lines.extend([
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t+plot_h}" stroke="#111827"/>',
        f'<line x1="{margin_l}" y1="{margin_t+plot_h}" x2="{width-margin_r}" y2="{margin_t+plot_h}" stroke="#111827"/>',
    ])
    for i in range(6):
        p = max_p * i / 5
        y = margin_t + plot_h - (p / max_p) * plot_h
        lines.append(f'<line x1="{margin_l}" y1="{y:.1f}" x2="{width-margin_r}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        lines.append(f'<text x="{margin_l-12}" y="{y+4:.1f}" text-anchor="end" font-family="Arial" font-size="11" fill="#64748b">{p:.2f}</text>')
    for i, row in enumerate(bins):
        x = margin_l + i * (bar_w + bar_gap)
        bar_h = (row["probability"] / max_p) * plot_h
        y = margin_t + plot_h - bar_h
        label = f'{row["bin_start"]:.1f}-{row["bin_end"]:.1f}'
        lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="4" fill="#2563eb"/>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{y-8:.1f}" text-anchor="middle" font-family="Arial" font-size="12" font-weight="700" fill="#111827">{row["probability"]:.2f}</text>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{margin_t+plot_h+24}" text-anchor="middle" font-family="Arial" font-size="11" fill="#111827">{esc(label)}</text>')
        lines.append(f'<text x="{x+bar_w/2:.1f}" y="{margin_t+plot_h+42}" text-anchor="middle" font-family="Arial" font-size="10" fill="#64748b">{row["count"]}/{row["total"]}</text>')
    mean_v = mean(values) or 0.0
    max_v = max(values)
    for marker, color, label, dy in [(mean_v, "#b45309", f"mean {mean_v:.1f}s", -10), (max_v, "#991b1b", f"max {max_v:.1f}s", 16)]:
        x = margin_l + ((marker - x_lo) / x_span) * plot_w
        lines.append(f'<line x1="{x:.1f}" y1="{margin_t}" x2="{x:.1f}" y2="{margin_t+plot_h}" stroke="{color}" stroke-width="2" stroke-dasharray="7 5"/>')
        lines.append(f'<text x="{min(width-margin_r-4, max(margin_l+4, x+4)):.1f}" y="{margin_t+dy:.1f}" font-family="Arial" font-size="12" font-weight="700" fill="{color}">{esc(label)}</text>')
    lines.append(f'<text x="26" y="{margin_t+plot_h/2}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569" transform="rotate(-90 26,{margin_t+plot_h/2})">normalized probability</text>')
    lines.append(f'<text x="{width/2}" y="{height-50}" text-anchor="middle" font-family="Arial" font-size="12" fill="#475569">{esc(metric)} time bin ({esc(unit)})</text>')
    lines.append(f'<text x="{width/2}" y="{height-24}" text-anchor="middle" font-family="Arial" font-size="11" fill="#64748b">{esc(source_note)}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")
    return [{"metric": metric, **row, "mean": mean_v, "max": max_v} for row in bins]


def convert_svg_to_png(svg_path: Path) -> None:
    png_path = svg_path.with_suffix(".png")
    converter = Path("C:/msys64/mingw64/bin/rsvg-convert.exe")
    if not converter.exists():
        return
    subprocess.run([str(converter), "-o", str(png_path), str(svg_path)], check=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate literature comparison charts for the reviewed M12 smoke run.")
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--db", default=str(M12_ROOT / "m12_metrics.sqlite3"))
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_ROOT / run_dir
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    fig_dir = output / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    refs = yaml.safe_load((M12_ROOT / "seed_data" / "reference_baselines.yaml").read_text(encoding="utf-8"))["reference_sources"]
    rows = build_rows(run_dir, Path(args.db) if Path(args.db).is_absolute() else PROJECT_ROOT / args.db)
    write_csv(
        output / "smoke_chart_source_data.csv",
        rows,
        [
            "test_id", "packet_test_id", "suite", "automated_status", "human_binary_status",
            "run_id", "scenario_spec_id", "R_storage", "N_tool_storage_total", "N_tool_storage_passed",
            "N_failed_tool_storage", "T_wait_seconds", "T_verification_seconds", "T_verification_source",
            "T_loop_seconds", "data_source", "metric_data_quality_status",
        ],
    )
    tc2 = [row for row in rows if row["suite"] == "TC2"]
    tc3 = [row for row in rows if row["suite"] == "TC3"]
    tc4 = [row for row in rows if row["suite"] == "TC4"]
    sim_rows = [row for row in rows if row.get("run_id")]
    r_storage_values = [row["R_storage"] for row in sim_rows if row.get("R_storage") is not None]
    t_wait_values = [row["T_wait_seconds"] for row in rows if row.get("T_wait_seconds") is not None]
    t_ver_values = [row["T_verification_seconds"] for row in sim_rows if row.get("T_verification_seconds") is not None]
    t_loop_values = [row["T_loop_seconds"] for row in rows if row.get("T_loop_seconds") is not None]
    tc2_pass = sum(1 for row in tc2 if row["human_binary_status"] == "PASS")
    tc4_pass = sum(1 for row in tc4 if row["human_binary_status"] == "PASS")

    figures: list[Path] = []
    svg_grouped_horizontal(
        fig_dir / "fig_01_planning_and_verification_latency_smoke.svg",
        title="Figure 1. Planning And Verification Time: Literature Vs This Study's Smoke Run",
        subtitle="Lower is better. Literature anchors are process-generation/import times; this study uses live chat and Isaac-derived smoke timings.",
        x_label="minutes",
        max_value=30,
        rows=[
            {"label": "LLMAPM import", "value": refs["LLMAPM"]["reference_timing_minutes"]["generated_process_import_time"], "series": "Literature", "display": "6 min", "note": "reference generated-process import"},
            {"label": "LLMAPM manual", "value": refs["LLMAPM"]["reference_timing_minutes"]["engineer_manual_process_time"], "series": "Literature", "display": "30 min", "note": "reference engineer manual process"},
            {"label": "This study's T_wait mean", "value": (mean(t_wait_values) or 0) / 60, "series": "This study", "display": f"{(mean(t_wait_values) or 0):.1f}s", "note": "automated chat wait proxy"},
            {"label": "This study's T_verification mean", "value": (mean(t_ver_values) or 0) / 60, "series": "This study (different scope)", "display": f"{(mean(t_ver_values) or 0):.1f}s", "note": "ScenarioSpec file to RunArtifact file"},
        ],
    )
    figures.append(fig_dir / "fig_01_planning_and_verification_latency_smoke.svg")
    svg_grouped_horizontal(
        fig_dir / "fig_02_tool_reasoning_success_vs_maka_smoke.svg",
        title="Figure 2. Tool/Query Reasoning: Literature Vs This Study's Smoke Chat Path",
        subtitle="Higher is better. This study uses human-reviewed binary outcomes from smoke TC2 rows.",
        x_label="rate / score",
        max_value=1.0,
        rows=[
            {"label": "MAKA no critic F1", "value": refs["MAKA"]["critic_ablation"]["no_critic_mean_f1"], "series": "Literature", "display": "0.2919", "note": "reference degraded routing"},
            {"label": "MAKA critic F1", "value": refs["MAKA"]["critic_ablation"]["critic_enabled_mean_f1"], "series": "Literature", "display": "0.6697", "note": "reference critic enabled"},
            {"label": "This study's TC2 pass", "value": tc2_pass / len(tc2), "series": "This study (proxy)", "display": f"{tc2_pass}/{len(tc2)}", "note": "human-reviewed smoke query rows"},
        ],
    )
    figures.append(fig_dir / "fig_02_tool_reasoning_success_vs_maka_smoke.svg")
    svg_grouped_horizontal(
        fig_dir / "fig_03_safety_interception_vs_literature_smoke.svg",
        title="Figure 3. Safety/Error Interception: Literature Vs This Study's Smoke Guards",
        subtitle="Higher is better. This study counts clarification/refusal before deployment as successful interception.",
        x_label="rate or normalized coverage",
        max_value=1.0,
        rows=[
            {"label": "MAKA full recovery", "value": refs["MAKA"]["critic_ablation"]["full_recovery_rate"], "series": "Literature", "display": "0.6119", "note": "reference recovery rate"},
            {"label": "This study's TC4 pass", "value": tc4_pass / len(tc4), "series": "This study (proxy)", "display": f"{tc4_pass}/{len(tc4)}", "note": "human-reviewed smoke error rows"},
            {"label": "This study's coverage", "value": min(1.0, len(tc4) / len(refs["FactoryFlow"]["error_taxonomy"])), "series": "This study", "display": f"{len(tc4)} rows", "note": "normalized to FactoryFlow taxonomy"},
        ],
    )
    figures.append(fig_dir / "fig_03_safety_interception_vs_literature_smoke.svg")
    svg_grouped_horizontal(
        fig_dir / "fig_04_evidence_quality_vs_literature_smoke.svg",
        title="Figure 4. Evidence Quality: Literature Anchors Vs This Study's Physical Evidence",
        subtitle="Higher is better. This study measures physical placement evidence from live smoke RunArtifacts.",
        x_label="percent / normalized score",
        max_value=100,
        rows=[
            {"label": "GAMHE code score", "value": refs["GAMHE_5_0"]["llm_code_generation"]["successful_functional_score"], "series": "Literature", "display": "100", "note": "reference functional code score"},
            {"label": "This study's live metric rows", "value": len([r for r in sim_rows if r.get("R_storage") is not None]) / max(1, len(sim_rows)) * 100, "series": "This study", "display": f'{len([r for r in sim_rows if r.get("R_storage") is not None])}/{len(sim_rows)}', "note": "RunArtifacts with R_storage"},
            {"label": "This study's R_storage mean", "value": (mean(r_storage_values) or 0) * 100, "series": "This study", "display": f"{(mean(r_storage_values) or 0):.3f}", "note": "placement verification pass rate"},
            {"label": "This study's TC3 smoke", "value": len([r for r in tc3 if r["human_binary_status"] == "PASS"]) / len(refs["GAMHE_5_0"]["setups"]) * 100, "series": "This study (proxy)", "display": f'{len([r for r in tc3 if r["human_binary_status"] == "PASS"])}/4 setups', "note": "smoke scale"},
        ],
    )
    figures.append(fig_dir / "fig_04_evidence_quality_vs_literature_smoke.svg")
    svg_r_storage_by_run(fig_dir / "fig_05_live_R_storage_by_smoke_run.svg", sim_rows)
    figures.append(fig_dir / "fig_05_live_R_storage_by_smoke_run.svg")
    distribution_rows: list[dict[str, Any]] = []
    distribution_rows += svg_time_distribution(
        fig_dir / "fig_06_live_T_wait_distribution_smoke.svg",
        figure_no=6,
        title="Operator Wait Time Distribution",
        metric="T_wait_seconds",
        values=t_wait_values,
        unit="seconds",
        source_note="Source: combined execution JSON turn timestamps; LIVE_N8N_CHAT smoke rows.",
    )
    figures.append(fig_dir / "fig_06_live_T_wait_distribution_smoke.svg")
    distribution_rows += svg_time_distribution(
        fig_dir / "fig_07_live_T_verification_distribution_smoke.svg",
        figure_no=7,
        title="Verification Time Distribution",
        metric="T_verification_seconds",
        values=t_ver_values,
        unit="seconds",
        source_note="Source: ScenarioSpec and RunArtifact file timestamps for smoke simulation rows.",
    )
    figures.append(fig_dir / "fig_07_live_T_verification_distribution_smoke.svg")
    distribution_rows += svg_time_distribution(
        fig_dir / "fig_08_live_T_loop_distribution_smoke.svg",
        figure_no=8,
        title="Closed-Loop Elapsed Time Distribution",
        metric="T_loop_seconds",
        values=t_loop_values,
        unit="seconds",
        source_note="Source: combined execution JSON turn timestamps; automated no-deploy smoke loop.",
    )
    figures.append(fig_dir / "fig_08_live_T_loop_distribution_smoke.svg")
    write_csv(output / "smoke_time_distribution_source.csv", distribution_rows, ["metric", "bin_start", "bin_end", "count", "total", "probability", "mean", "max"])
    for figure in figures:
        convert_svg_to_png(figure)
    summary = {
        "status": "OK",
        "output": str(output),
        "figures": [str(path) for path in figures],
        "png_generated": [str(path.with_suffix(".png")) for path in figures if path.with_suffix(".png").exists()],
        "n_rows": len(rows),
        "simulation_rows": len(sim_rows),
        "R_storage_mean": mean(r_storage_values),
        "T_wait_mean_seconds": mean(t_wait_values),
        "T_verification_mean_seconds": mean(t_ver_values),
        "T_loop_mean_seconds": mean(t_loop_values),
        "TC2_human_pass_rate": tc2_pass / len(tc2) if tc2 else None,
        "TC4_human_pass_rate": tc4_pass / len(tc4) if tc4 else None,
    }
    (output / "smoke_chart_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
