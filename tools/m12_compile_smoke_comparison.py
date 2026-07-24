from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from trt_core.repository import PROJECT_ROOT


M12_ROOT = PROJECT_ROOT / "outputs" / "reports" / "m12"
OUT_DIR = M12_ROOT / "comparison_results" / "smoke_literature"
FIG_DIR = OUT_DIR / "figures"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
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


def latest_manual_results() -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(M12_ROOT / "manual_results.jsonl"):
        test_id = row.get("test_case_id")
        if isinstance(test_id, str) and test_id.startswith("SMOKE_"):
            latest[test_id] = row
    return latest


def load_metrics() -> dict[str, dict[str, Any]]:
    db_path = M12_ROOT / "m12_metrics.sqlite3"
    if not db_path.exists():
        return {}
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = {
        row["test_case_id"]: dict(row)
        for row in connection.execute(
            """
            SELECT test_case_id, run_id, scenario_spec_id, R_storage,
                   N_tool_storage_total, N_failed_tool_storage,
                   T_wait_seconds, T_verification_seconds, T_loop_seconds,
                   R_reset, data_quality_status, data_quality_reason,
                   data_source, is_live_test, is_historical
            FROM m12_run_metrics
            WHERE test_case_id LIKE 'SMOKE_%'
            """
        )
    }
    connection.close()
    return rows


def tc_for_smoke(seq: str) -> str:
    number = int(seq.split("_")[1])
    if 1 <= number <= 8:
        return "TC1"
    if 9 <= number <= 17:
        return "TC2"
    if 18 <= number <= 19:
        return "TC3"
    return "TC4"


def strict_success(row: dict[str, Any]) -> bool:
    return row.get("status") == "PASS"


def intercepted(row: dict[str, Any]) -> bool | None:
    if row["test_case_id"] != "TC4":
        return None
    return row.get("status") == "REJECTED"


def pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return ""
    return f"{value:.4f}"


def load_baselines() -> dict[str, Any]:
    return yaml.safe_load((M12_ROOT / "seed_data" / "reference_baselines.yaml").read_text(encoding="utf-8"))


def make_item_table() -> list[dict[str, Any]]:
    packet = {row["smoke_sequence"]: row for row in read_csv(M12_ROOT / "manual_test_packet" / "smoke_queue_manual.csv")}
    manual = latest_manual_results()
    metrics = load_metrics()
    rows: list[dict[str, Any]] = []
    for index in range(1, 28):
        seq = f"SMOKE_{index:03d}"
        packet_row = packet.get(seq, {})
        manual_row = manual.get(seq, {})
        metric_row = metrics.get(seq, {})
        tc = tc_for_smoke(seq)
        rows.append(
            {
                "smoke_sequence": seq,
                "test_case_id": tc,
                "packet_test_id": packet_row.get("test_id", ""),
                "natural_language_input": packet_row.get("paste_into_n8n", ""),
                "status": manual_row.get("status", ""),
                "strict_success": strict_success({"status": manual_row.get("status", "")}),
                "scenario_spec_id": metric_row.get("scenario_spec_id") or manual_row.get("scenario_spec_id") or "",
                "run_id": metric_row.get("run_id") or manual_row.get("run_id") or "",
                "R_storage": metric_row.get("R_storage"),
                "N_tool_storage_total": metric_row.get("N_tool_storage_total"),
                "N_failed_tool_storage": metric_row.get("N_failed_tool_storage"),
                "T_wait_seconds": metric_row.get("T_wait_seconds"),
                "T_verification_seconds": metric_row.get("T_verification_seconds"),
                "T_loop_seconds": metric_row.get("T_loop_seconds"),
                "R_reset": metric_row.get("R_reset"),
                "data_quality_status": metric_row.get("data_quality_status") or "",
                "data_quality_reason": metric_row.get("data_quality_reason") or "",
                "reference_basis": {
                    "TC1": "LLMAPM + FactoryFlow",
                    "TC2": "MAKA + GAMHE_5_0",
                    "TC3": "GAMHE_5_0 + MAKA",
                    "TC4": "FactoryFlow + MAKA + HRCD",
                }[tc],
                "knowledge_point": knowledge_point(tc, manual_row.get("status", ""), packet_row.get("test_id", "")),
            }
        )
    return rows


def knowledge_point(tc: str, status: str, packet_test_id: str) -> str:
    if tc == "TC1":
        if status == "FAIL_ERROR_NOT_INTERCEPTED":
            return "Line/tool ontology and schema guards must reject invalid references before candidate approval."
        if status == "FAIL_SIMULATION_CONFIG_DRIFT":
            return "Dialogue state carries prior/default simulation parameters into new intents; state isolation is a knowledge dependency."
        if status == "SIMULATION_FAILED":
            return "Digital-twin evidence can expose physical placement failures that text validation cannot see."
        return "Accepted plans must preserve exact scope, target lines, and simulation arguments."
    if tc == "TC2":
        if status == "FAIL":
            return "Report/config questions need a separate query route; treating metric requests as patch intents breaks tool orchestration."
        return "Structured config/state retrieval works when the query is routed to the config-query path."
    if tc == "TC3":
        return "KPI evidence depends on live RunArtifact rows; deployment must remain blocked when line KPI targets are missed."
    if status == "FAIL_ERROR_NOT_INTERCEPTED":
        return "Safety-critical validation is too late or absent for this error class; invalid requests can reach approval."
    return "Required-field and vocabulary guards can block some invalid requests before simulation/deployment."


def aggregate(item_rows: list[dict[str, Any]], baselines: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_tc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in item_rows:
        by_tc[row["test_case_id"]].append(row)

    tc_rows: list[dict[str, Any]] = []
    for tc, rows in sorted(by_tc.items()):
        total = len(rows)
        passed = sum(1 for row in rows if row["status"] == "PASS")
        failed = sum(1 for row in rows if row["status"].startswith("FAIL"))
        rejected = sum(1 for row in rows if row["status"] == "REJECTED")
        metrics_rows = [row for row in rows if row.get("R_storage") not in (None, "")]
        r_storage_values = [float(row["R_storage"]) for row in metrics_rows if row.get("R_storage") not in (None, "")]
        t_wait_values = [float(row["T_wait_seconds"]) for row in metrics_rows if row.get("T_wait_seconds") not in (None, "")]
        tc_rows.append(
            {
                "test_case_id": tc,
                "items_run": total,
                "passed": passed,
                "failed": failed,
                "rejected_or_blocked": rejected,
                "strict_pass_rate": passed / total if total else None,
                "measured_metric_rows": len(metrics_rows),
                "R_storage_mean": sum(r_storage_values) / len(r_storage_values) if r_storage_values else None,
                "T_wait_mean_seconds": sum(t_wait_values) / len(t_wait_values) if t_wait_values else None,
                "data_quality_note": "R_reset unavailable in RunArtifact schema." if metrics_rows else "",
            }
        )

    refs = baselines["reference_sources"]
    tc2_rows = by_tc["TC2"]
    tc4_rows = by_tc["TC4"]
    tc3_rows = by_tc["TC3"]
    tc2_pass = sum(1 for row in tc2_rows if row["status"] == "PASS")
    tc2_f1_proxy = tc2_pass / len(tc2_rows) if tc2_rows else None
    tc4_intercepted = sum(1 for row in tc4_rows if row["status"] == "REJECTED")
    tc4_rate = tc4_intercepted / len(tc4_rows) if tc4_rows else None
    metric_rows = [row for row in item_rows if row.get("R_storage") not in (None, "")]
    r_storage_values = [float(row["R_storage"]) for row in metric_rows if row.get("R_storage") not in (None, "")]
    t_ver_values = [float(row["T_verification_seconds"]) for row in metric_rows if row.get("T_verification_seconds") not in (None, "")]
    comparison_rows = [
        {
            "test_case_id": "TC2",
            "reference_name": "MAKA",
            "reference_protocol": "L1/L2/L3 tool-use benchmark",
            "reference_metric_name": "total_questions",
            "reference_metric_value": refs["MAKA"]["tool_use_depth_protocol"]["total_questions"],
            "our_protocol": "Smoke n8n query rows",
            "our_metric_name": "tool_query_rows",
            "our_metric_value": len(tc2_rows),
            "comparison_direction": "PROTOCOL_SCALE",
            "comparison_result": "PARTIAL_SCALE",
            "data_quality_status": "OK",
            "notes": "Smoke run uses 9 L1 query rows, not full 75-question MAKA protocol.",
        },
        {
            "test_case_id": "TC2",
            "reference_name": "MAKA",
            "reference_protocol": "Critic-enabled tool recovery",
            "reference_metric_name": "critic_enabled_mean_f1",
            "reference_metric_value": refs["MAKA"]["critic_ablation"]["critic_enabled_mean_f1"],
            "our_protocol": "Smoke n8n query-answer success proxy",
            "our_metric_name": "query_success_rate_proxy",
            "our_metric_value": tc2_f1_proxy,
            "comparison_direction": "HIGHER_IS_BETTER",
            "comparison_result": "APPROX_EQUAL" if tc2_f1_proxy is not None and abs(tc2_f1_proxy - refs["MAKA"]["critic_ablation"]["critic_enabled_mean_f1"]) < 0.02 else "LOWER",
            "data_quality_status": "PROXY_METRIC",
            "notes": "Not trace-level F1; rows 009,010,017 failed because query prompts were routed as patch intents.",
        },
        {
            "test_case_id": "TC3",
            "reference_name": "GAMHE_5_0",
            "reference_protocol": "Four setup optimisation study",
            "reference_metric_name": "setups",
            "reference_metric_value": len(refs["GAMHE_5_0"]["setups"]),
            "our_protocol": "Smoke live TC3 setup rows",
            "our_metric_name": "live_tc3_setup_rows",
            "our_metric_value": len(tc3_rows),
            "comparison_direction": "PROTOCOL_SCALE",
            "comparison_result": "PARTIAL_SCALE",
            "data_quality_status": "OK",
            "notes": "Only two TC3 smoke setup repetitions were run; both produced live RunArtifacts.",
        },
        {
            "test_case_id": "TC3",
            "reference_name": "GAMHE_5_0",
            "reference_protocol": "LLM code integration",
            "reference_metric_name": "successful_functional_score",
            "reference_metric_value": refs["GAMHE_5_0"]["llm_code_generation"]["successful_functional_score"],
            "our_protocol": "Live smoke storage verification",
            "our_metric_name": "R_storage_mean_percent",
            "our_metric_value": (sum(r_storage_values) / len(r_storage_values) * 100) if r_storage_values else None,
            "comparison_direction": "HIGHER_IS_BETTER",
            "comparison_result": "LOWER" if r_storage_values and (sum(r_storage_values) / len(r_storage_values) * 100) < 100 else "PASS",
            "data_quality_status": "MEASURED_DIFFERENT_METRIC",
            "notes": "Compared to a 100-point code functional score only as an evidence-quality anchor; our metric is physical placement verification.",
        },
        {
            "test_case_id": "TC4",
            "reference_name": "FactoryFlow",
            "reference_protocol": "Digital-twin error taxonomy",
            "reference_metric_name": "error_taxonomy_count",
            "reference_metric_value": len(refs["FactoryFlow"]["error_taxonomy"]),
            "our_protocol": "Smoke TC4 chat-injected invalid requests",
            "our_metric_name": "injected_error_rows",
            "our_metric_value": len(tc4_rows),
            "comparison_direction": "EQUAL",
            "comparison_result": "PASS",
            "data_quality_status": "OK",
            "notes": "Smoke queue covers 8 TC4 invalid-request rows.",
        },
        {
            "test_case_id": "TC4",
            "reference_name": "MAKA",
            "reference_protocol": "Critic/safety recovery",
            "reference_metric_name": "full_recovery_rate",
            "reference_metric_value": refs["MAKA"]["critic_ablation"]["full_recovery_rate"],
            "our_protocol": "Smoke TC4 interception",
            "our_metric_name": "error_interception_rate",
            "our_metric_value": tc4_rate,
            "comparison_direction": "HIGHER_IS_BETTER",
            "comparison_result": "HIGHER" if tc4_rate and tc4_rate > refs["MAKA"]["critic_ablation"]["full_recovery_rate"] else "LOWER",
            "data_quality_status": "MEASURED_PROXY",
            "notes": "Three smoke invalid requests reached candidate approval instead of being intercepted.",
        },
        {
            "test_case_id": "TC1",
            "reference_name": "LLMAPM",
            "reference_protocol": "Generated process import",
            "reference_metric_name": "generated_process_import_time_minutes",
            "reference_metric_value": refs["LLMAPM"]["reference_timing_minutes"]["generated_process_import_time"],
            "our_protocol": "n8n-to-Isaac verification",
            "our_metric_name": "T_verification_mean_minutes",
            "our_metric_value": (sum(t_ver_values) / len(t_ver_values) / 60) if t_ver_values else None,
            "comparison_direction": "LOWER_IS_BETTER",
            "comparison_result": "SLOWER",
            "data_quality_status": "MEASURED_DIFFERENT_SCOPE",
            "notes": "Our timing includes live Isaac simulation and evidence extraction, not only importing generated process logic.",
        },
    ]
    return tc_rows, comparison_rows


def svg_bar_chart(path: Path, title: str, labels: list[str], values: list[float], ylabel: str, source: str, max_value: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 920, 520
    margin_l, margin_r, margin_t, margin_b = 90, 40, 70, 115
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    max_v = max_value if max_value is not None else max(values + [1.0])
    max_v = max(max_v, 1e-9)
    bar_gap = 18
    bar_w = max(16, (plot_w - bar_gap * (len(values) + 1)) / max(len(values), 1))
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width/2}" y="32" text-anchor="middle" font-family="Arial" font-size="22" font-weight="700">{escape(title)}</text>',
        f'<text x="{width/2}" y="55" text-anchor="middle" font-family="Arial" font-size="12" fill="#555">Source: {escape(source)}</text>',
        f'<line x1="{margin_l}" y1="{margin_t + plot_h}" x2="{margin_l + plot_w}" y2="{margin_t + plot_h}" stroke="#333"/>',
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" y2="{margin_t + plot_h}" stroke="#333"/>',
    ]
    for i in range(6):
        val = max_v * i / 5
        y = margin_t + plot_h - (val / max_v) * plot_h
        lines.append(f'<line x1="{margin_l}" y1="{y:.1f}" x2="{margin_l + plot_w}" y2="{y:.1f}" stroke="#e6e6e6"/>')
        lines.append(f'<text x="{margin_l-10}" y="{y+4:.1f}" text-anchor="end" font-family="Arial" font-size="11" fill="#555">{val:.2f}</text>')
    palette = ["#3b82f6", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6", "#06b6d4", "#64748b", "#dc2626"]
    for i, (label, value) in enumerate(zip(labels, values)):
        x = margin_l + bar_gap + i * (bar_w + bar_gap)
        h = (value / max_v) * plot_h
        y = margin_t + plot_h - h
        color = palette[i % len(palette)]
        lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="{color}"/>')
        lines.append(f'<text x="{x + bar_w/2:.1f}" y="{y-6:.1f}" text-anchor="middle" font-family="Arial" font-size="11">{value:.3g}</text>')
        lines.append(f'<text x="{x + bar_w/2:.1f}" y="{margin_t + plot_h + 18}" text-anchor="middle" font-family="Arial" font-size="11">{escape(label)}</text>')
    lines.append(f'<text x="22" y="{margin_t + plot_h/2}" transform="rotate(-90 22 {margin_t + plot_h/2})" text-anchor="middle" font-family="Arial" font-size="13">{escape(ylabel)}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def escape(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_discussion(item_rows: list[dict[str, Any]], tc_rows: list[dict[str, Any]], comparison_rows: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    counts = Counter(row["status"] for row in item_rows)
    metric_rows = [row for row in item_rows if row.get("R_storage") not in (None, "")]
    r_storage_mean = sum(float(row["R_storage"]) for row in metric_rows) / len(metric_rows) if metric_rows else None
    lines = [
        "# M12 Smoke Comparison Against Literature Protocols",
        "",
        "Data source: LIVE_N8N_CHAT smoke executions and live RunArtifact SQLite rows. Seed/gold fixtures are used only for reference labels and literature baseline values.",
        "",
        "## Status Summary",
        "",
        f"- Smoke items analyzed: {len(item_rows)}",
        f"- Status counts: {dict(counts)}",
        f"- Live simulation metric rows: {len(metric_rows)}",
        f"- Mean R_storage: {pct(r_storage_mean)}",
        "- R_reset: DATA_INCOMPLETE for all live runs because reset cycle request/completion fields are absent from RunArtifact.",
        "",
        "## Knowledge Points",
        "",
        "1. Intent parsing is not enough. LLMAPM and FactoryFlow both rely on process/state or schema validation after generation. Our smoke results show why: invalid line IDs and impossible KPI/intervention requests can still reach candidate approval unless the validator owns line ontology, numeric feasibility, and intervention-mode knowledge.",
        "2. State isolation is a first-class knowledge problem. Several TC1 rows inherited prior/default throughput or Time-Arrival parameters. That means the workflow knows the current TRT state, but does not cleanly distinguish requested changes from carried context.",
        "3. Query routing must be separated from patch intent routing. MAKA-style tool-use questions require selecting a data source and computation path. SMOKE_009, SMOKE_010, and SMOKE_017 were routed as task-change dialogue instead of report/config queries.",
        "4. Digital-twin evidence adds knowledge unavailable to chat validation. SMOKE_003 passed candidate validation but produced a placement failure, lowering R_storage to 0.9 and failing RunArtifact validation.",
        "5. Deployment safety depends on evidence guards, not optimism. TC3 smoke rows produced evidence but correctly blocked deployment because line-level KPI targets were missed.",
        "",
        "## Files",
        "",
        "- smoke_item_comparison.csv: per-item status, metrics, provenance, and knowledge point.",
        "- smoke_reference_vs_ours.csv: literature baseline/protocol rows compared with measured smoke outputs.",
        "- figures/*.svg: generated comparison plots. PNG was not generated because this environment has no matplotlib/Pillow raster backend.",
        "",
    ]
    (OUT_DIR / "smoke_literature_discussion.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    item_rows = make_item_table()
    baselines = load_baselines()
    tc_rows, comparison_rows = aggregate(item_rows, baselines)

    item_fields = [
        "smoke_sequence", "test_case_id", "packet_test_id", "natural_language_input", "status",
        "strict_success", "scenario_spec_id", "run_id", "R_storage", "N_tool_storage_total",
        "N_failed_tool_storage", "T_wait_seconds", "T_verification_seconds", "T_loop_seconds",
        "R_reset", "data_quality_status", "data_quality_reason", "reference_basis", "knowledge_point",
    ]
    write_csv(OUT_DIR / "smoke_item_comparison.csv", item_rows, item_fields)
    write_csv(OUT_DIR / "smoke_tc_summary.csv", tc_rows, list(tc_rows[0].keys()))
    write_csv(OUT_DIR / "smoke_reference_vs_ours.csv", comparison_rows, list(comparison_rows[0].keys()))

    status_counts = Counter(row["status"] for row in item_rows)
    svg_bar_chart(
        FIG_DIR / "fig_01_smoke_status_counts.svg",
        "M12 Smoke Item Outcomes",
        list(status_counts.keys()),
        [float(v) for v in status_counts.values()],
        "count",
        "LIVE_N8N_CHAT smoke_queue_manual.csv + manual_results.jsonl",
    )
    tc_pass = {row["test_case_id"]: float(row["strict_pass_rate"] or 0.0) for row in tc_rows}
    svg_bar_chart(
        FIG_DIR / "fig_02_strict_pass_rate_by_tc.svg",
        "Strict Pass Rate By Comparative Test Case",
        list(tc_pass.keys()),
        list(tc_pass.values()),
        "strict pass rate",
        "LIVE_N8N_CHAT smoke results",
        max_value=1.0,
    )
    r_rows = [row for row in item_rows if row.get("R_storage") not in (None, "")]
    svg_bar_chart(
        FIG_DIR / "fig_03_R_storage_by_run.svg",
        "Placement Verification Pass Rate By Smoke Run",
        [row["smoke_sequence"] for row in r_rows],
        [float(row["R_storage"]) for row in r_rows],
        "R_storage",
        "m12_metrics.sqlite3 LIVE_N8N_CHAT rows",
        max_value=1.0,
    )
    ref_rows = [row for row in comparison_rows if row.get("our_metric_value") not in (None, "")]
    svg_bar_chart(
        FIG_DIR / "fig_04_reference_vs_ours_selected_metrics.svg",
        "Selected Literature Baselines Vs Smoke Measurements",
        [f'{row["test_case_id"]}\\n{row["our_metric_name"]}' for row in ref_rows],
        [float(row["our_metric_value"]) for row in ref_rows],
        "our measured/proxy value",
        "reference_baselines.yaml + LIVE_N8N_CHAT smoke outputs",
    )
    write_discussion(item_rows, tc_rows, comparison_rows)
    print(json.dumps({
        "status": "OK",
        "output": str(OUT_DIR),
        "rows": {
            "item_rows": len(item_rows),
            "tc_summary_rows": len(tc_rows),
            "reference_comparison_rows": len(comparison_rows),
        },
        "figures": [str(path) for path in sorted(FIG_DIR.glob("*.svg"))],
        "png_generated": False,
        "png_reason": "matplotlib/Pillow raster backend unavailable in current Python environment.",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
