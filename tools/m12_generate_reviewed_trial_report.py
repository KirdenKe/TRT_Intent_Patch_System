from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trt_core.repository import PROJECT_ROOT
from trt_core.experiment_evaluation import (
    CHECKPOINTS,
    auto_human_metrics,
    completion_metrics,
)


M12_ROOT = PROJECT_ROOT / "outputs" / "reports" / "m12"
SOURCE_RUN_DIR = M12_ROOT / "automated_smoke_n8n_run_20260706_193732"
DEFAULT_OUTPUT = M12_ROOT / "reviewed_trial_20260706"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def optional_bool(value: Any) -> bool | None:
    normalized = str(value or "").strip().lower()
    if value is True or normalized in {"true", "1", "yes", "pass", "partial"}:
        return True
    if value is False or normalized in {"false", "0", "no", "fail", "fail_expected"}:
        return False
    return None


def load_manual_reviews(source_run_dir: Path) -> dict[str, dict[str, Any]]:
    reviews: dict[str, dict[str, Any]] = {}
    verdicts = source_run_dir / "manual_review_verdicts.csv"
    if verdicts.exists():
        for row in read_csv(verdicts):
            reviews[row["test_id"]] = {
                "manual_result": row.get("manual_verdict"),
                "manual_reason": row.get("manual_reason"),
                "reviewer_type": row.get("manual_review_method") or "EVIDENCE_BASED_MANUAL_REVIEW",
                "reviewed_at_utc": row.get("reviewed_at_utc"),
                "failure_cause_code": row.get("failure_source_code"),
                "failure_stage": row.get("failure_stage"),
                "correction_method": row.get("correction_method"),
                "outcome_class": row.get("workflow_outcome_class"),
                "checkpoints": {cp: optional_bool(row.get(cp)) for cp in CHECKPOINTS},
            }
    legacy = source_run_dir / "human_reviewed" / "m12_smoke_human_reviewed.csv"
    if legacy.exists():
        for row in read_csv(legacy):
            reviews[row["test_id"]] = {
                "manual_result": row.get("human_binary_status"),
                "manual_reason": row.get("human_review_reason"),
                "reviewer_type": "CODEX_SEMANTIC_REVIEW",
                "reviewed_at_utc": row.get("reviewed_at_utc"),
            }
    for path in (source_run_dir / "semantic_reviews.jsonl", M12_ROOT / "semantic_reviews.jsonl"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            test_id = str(row.get("test_id") or "")
            if test_id:
                reviews[test_id] = {
                    "manual_result": row.get("review_result"),
                    "manual_reason": row.get("review_reason"),
                    "reviewer_type": row.get("reviewer_type"),
                    "reviewed_at_utc": row.get("reviewed_at_utc"),
                    "failure_cause_code": row.get("failure_cause"),
                    "correction_method": row.get("correction_method"),
                }
    return reviews


def read_full_transcript(path: Path) -> str:
    if not path.exists():
        return "DATA_INCOMPLETE - transcript file was not found."
    return path.read_text(encoding="utf-8", errors="replace").strip()


def finalized_outcome(
    *,
    recorded: str,
    manual_result: str,
    manual_correction_used: bool | None,
    failure_stage: str,
    failure_cause_code: str,
) -> str:
    """Classify completion of the test objective, including negative cases."""

    if manual_result == "PASS":
        return "MANUALLY_ASSISTED_SUCCESS" if manual_correction_used else "AUTONOMOUS_SUCCESS"
    if manual_result != "FAIL":
        return recorded or "EVALUATION_INCOMPLETE"
    if failure_cause_code == "MANUAL_REJECTION" or failure_stage.lower() == "deployment":
        return "MANUAL_REJECTION"
    if failure_stage.lower() in {"runner_exception", "backend_injection", "system", "api"}:
        return "SYSTEM_ERROR"
    if failure_stage.lower() in {"simulation", "isaac_runtime", "scenario_generation"}:
        return "SIMULATION_FAILURE"
    if failure_stage.lower() in {"dialogue", "intent_validation", "required_fields"}:
        return "INPUT_FAILURE"
    return "VALIDATION_FAILURE"


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


def unique_metric_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one validated metric row per selected live simulation run."""

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        run_id = str(row.get("run_id") or "")
        if not run_id or run_id in seen or not row.get("include_in_metric_figures", True):
            continue
        if row.get("suite") not in {"TC1", "TC3"}:
            continue
        if row.get("T_verification_wall_seconds") is None:
            continue
        seen.add(run_id)
        selected.append(row)
    return selected


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
                       T_wait_seconds, T_verification_seconds,
                       T_verification_wall_seconds, T_isaac_startup_seconds,
                       verification_timing_source, T_loop_seconds, loop_review_definition,
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
    reviewed = load_manual_reviews(source_run_dir)
    packet_rows = load_packet_rows(M12_ROOT / "manual_test_packet")
    metrics = load_metrics_by_run(db_path)
    rows: list[dict[str, Any]] = []
    for idx, result in enumerate(results, start=1):
        raw_id = result["test_id"]
        test_label = f"T{idx:02d}"
        review = reviewed.get(raw_id, {})
        packet = packet_rows.get(result.get("packet_test_id", ""), {})
        combined = load_combined(result.get("combined_execution_json", ""))
        timing = timing_from_combined(combined)
        run_id = result.get("run_id", "")
        scenario_spec_id = result.get("scenario_spec_id", "")
        metric = metrics.get(run_id, {})
        db_verification = fnum(metric.get("T_verification_seconds"))
        if db_verification is not None and db_verification <= 0:
            db_verification = None
        transcript_value = str(result.get("transcript") or "")
        transcript_path = Path(transcript_value) if transcript_value else source_run_dir / "manual_transcripts_snapshot" / f"{raw_id}.txt"
        if not transcript_path.is_absolute():
            transcript_path = PROJECT_ROOT / transcript_path
        total = fnum(metric.get("N_tool_storage_total"))
        failed = fnum(metric.get("N_failed_tool_storage"))
        passed = None if total is None or failed is None else max(0.0, total - failed)
        manual_result = str(review.get("manual_result") or result.get("manual_result", "")).upper()
        manual_correction_used = optional_bool(result.get("manual_correction_used"))
        failure_stage = str(review.get("failure_stage") or result.get("failure_stage") or "")
        failure_cause_code_value = str(review.get("failure_cause_code") or result.get("failure_cause_code", ""))
        outcome_class = str(review.get("outcome_class") or "") or finalized_outcome(
            recorded=str(result.get("outcome_class") or "EVALUATION_INCOMPLETE"),
            manual_result=manual_result,
            manual_correction_used=manual_correction_used,
            failure_stage=failure_stage,
            failure_cause_code=failure_cause_code_value,
        )
        rows.append(
            {
                "test_label": test_label,
                "test_id": raw_id,
                "packet_test_id": result.get("packet_test_id", ""),
                "suite": result.get("suite", ""),
                "operator_instruction": packet.get("paste_into_n8n", ""),
                "system_response_excerpt": transcript_response_excerpt(transcript_path),
                "full_transcript": read_full_transcript(transcript_path),
                "expected_criteria": json.dumps(packet, sort_keys=True),
                "automated_status": result.get("status", ""),
                "automated_result": result.get("automated_result") or result.get("status", ""),
                "reviewed_status": manual_result,
                "manual_result": manual_result,
                "review_reason": review.get("manual_reason") or result.get("rejection_reason", ""),
                "reviewer_type": review.get("reviewer_type") or ("UNREVIEWED" if not result.get("manual_result") else "UNSPECIFIED_MANUAL_REVIEW"),
                "reviewed_at_utc": review.get("reviewed_at_utc"),
                "outcome_class": outcome_class,
                "manual_correction_used": manual_correction_used,
                "manual_intervention_required": optional_bool(result.get("manual_intervention_required")),
                "failure_stage": failure_stage,
                "failure_cause": result.get("failure_cause", ""),
                "failure_cause_code": failure_cause_code_value,
                "correction_method": review.get("correction_method") or result.get("correction_method", ""),
                "checkpoints": review.get("checkpoints") or {cp: optional_bool(result.get(cp)) for cp in CHECKPOINTS},
                "scenario_spec_id": scenario_spec_id,
                "run_id": run_id,
                "strategy_batch_id": result.get("strategy_batch_id", ""),
                "candidate_count": result.get("candidate_count", ""),
                "candidate_run_ids": result.get("candidate_run_ids", ""),
                "selected_candidate_strategy_id": result.get("selected_candidate_strategy_id", ""),
                "selection_objective_id": result.get("selection_objective_id", ""),
                "selection_objective_score": result.get("selection_objective_score", ""),
                "R_storage": fnum(metric.get("R_storage")),
                "N_tool_storage_total": total,
                "N_tool_storage_passed": passed,
                "N_failed_tool_storage": failed,
                "R_reset": fnum(metric.get("R_reset")),
                "C_reset_requested": fnum(metric.get("C_reset_requested")),
                "C_reset_completed": fnum(metric.get("C_reset_completed")),
                "T_wait_seconds": fnum(metric.get("T_wait_seconds")),
                "T_verification_seconds": db_verification,
                "T_verification_source": "metrics database with measured Isaac startup excluded" if db_verification is not None else "DATA_INCOMPLETE",
                "T_verification_wall_seconds": fnum(metric.get("T_verification_wall_seconds")),
                "T_isaac_startup_seconds": fnum(metric.get("T_isaac_startup_seconds")),
                "T_loop_seconds": fnum(metric.get("T_loop_seconds")),
                "loop_review_definition": metric.get("loop_review_definition"),
                "include_in_metric_figures": result.get("suite", "") in {"TC1", "TC3"} and bool(metric),
                "data_source": "LIVE_N8N_CHAT",
                "metric_data_quality_status": metric.get("data_quality_status", "DATA_INCOMPLETE" if run_id else "NO_RUN"),
                "chat_session_id": result.get("chat_session_id", ""),
                "n8n_execution_ids": result.get("n8n_execution_ids", ""),
                "combined_execution_json": result.get("combined_execution_json", ""),
                "transcript_path": str(transcript_path),
            }
        )
    extension_results_path = source_run_dir / "smoke_extensions" / "smoke_extension_results.csv"
    extension_packet_path = M12_ROOT / "manual_test_packet" / "smoke_extension_tc5_tc7.csv"
    if extension_results_path.exists() and extension_packet_path.exists():
        extension_results = {row["extension_id"]: row for row in read_csv(extension_results_path)}
        extension_packets = {row["extension_id"]: row for row in read_csv(extension_packet_path)}
        repetition_path = source_run_dir / "smoke_extensions" / "llm_generation" / "llm_generation_repetitions.jsonl"
        repetitions = [
            json.loads(line)
            for line in repetition_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ] if repetition_path.exists() else []
        core_by_id = {row["test_id"]: row for row in rows}
        for index, extension_id in enumerate(("SMOKE_028", "SMOKE_029", "SMOKE_030"), start=28):
            result = extension_results.get(extension_id)
            packet = extension_packets.get(extension_id)
            review = reviewed.get(extension_id, {})
            if not result or not packet:
                continue
            suite = packet["test_case_id"]
            source_row = core_by_id.get(packet.get("source_smoke_sequence", ""), {})
            metric = metrics.get(result.get("run_id", ""), {})
            if suite == "TC6":
                evidence_rows = [row for row in repetitions if "TC6" in row.get("test_case_ids", [])]
            elif suite == "TC7":
                evidence_rows = [row for row in repetitions if "TC7" in row.get("test_case_ids", [])]
            else:
                evidence_rows = []
            full_evidence = (
                source_row.get("full_transcript", "")
                if suite == "TC5"
                else "\n".join(json.dumps(row, sort_keys=True) for row in evidence_rows)
            )
            manual_result = str(review.get("manual_result") or "").upper()
            rows.append(
                {
                    "test_label": f"T{index:02d}",
                    "test_id": extension_id,
                    "packet_test_id": suite,
                    "suite": suite,
                    "operator_instruction": packet.get("natural_language_trigger", ""),
                    "system_response_excerpt": short(result.get("measurements_json", ""), 360),
                    "full_transcript": full_evidence or "DATA_INCOMPLETE - detailed extension evidence missing.",
                    "expected_criteria": json.dumps(packet, sort_keys=True),
                    "automated_status": result.get("status", ""),
                    "automated_result": result.get("status", ""),
                    "reviewed_status": manual_result,
                    "manual_result": manual_result,
                    "review_reason": review.get("manual_reason", ""),
                    "reviewer_type": review.get("reviewer_type", "UNREVIEWED"),
                    "reviewed_at_utc": review.get("reviewed_at_utc"),
                    "outcome_class": review.get("outcome_class") or "EVALUATION_INCOMPLETE",
                    "manual_correction_used": False,
                    "manual_intervention_required": False,
                    "failure_stage": review.get("failure_stage", ""),
                    "failure_cause": review.get("manual_reason", ""),
                    "failure_cause_code": review.get("failure_cause_code", ""),
                    "correction_method": review.get("correction_method", ""),
                    "checkpoints": review.get("checkpoints") or {cp: None for cp in CHECKPOINTS},
                    "scenario_spec_id": result.get("scenario_spec_id", ""),
                    "run_id": result.get("run_id", ""),
                    "strategy_batch_id": source_row.get("strategy_batch_id", ""),
                    "candidate_count": source_row.get("candidate_count", ""),
                    "candidate_run_ids": source_row.get("candidate_run_ids", ""),
                    "selected_candidate_strategy_id": source_row.get("selected_candidate_strategy_id", ""),
                    "selection_objective_id": source_row.get("selection_objective_id", ""),
                    "selection_objective_score": source_row.get("selection_objective_score", ""),
                    "R_storage": fnum(metric.get("R_storage")),
                    "N_tool_storage_total": fnum(metric.get("N_tool_storage_total")),
                    "N_tool_storage_passed": fnum(metric.get("N_tool_storage_passed")),
                    "N_failed_tool_storage": fnum(metric.get("N_failed_tool_storage")),
                    "R_reset": fnum(metric.get("R_reset")),
                    "C_reset_requested": fnum(metric.get("C_reset_requested")),
                    "C_reset_completed": fnum(metric.get("C_reset_completed")),
                    "T_wait_seconds": fnum(metric.get("T_wait_seconds")),
                    "T_verification_seconds": fnum(metric.get("T_verification_seconds")),
                    "T_verification_source": "metrics database with measured Isaac startup excluded" if metric else "NOT_APPLICABLE",
                    "T_verification_wall_seconds": fnum(metric.get("T_verification_wall_seconds")),
                    "T_isaac_startup_seconds": fnum(metric.get("T_isaac_startup_seconds")),
                    "T_loop_seconds": fnum(metric.get("T_loop_seconds")),
                    "loop_review_definition": metric.get("loop_review_definition"),
                    "include_in_metric_figures": False,
                    "data_source": metric.get("data_source") or result.get("data_source", "DATA_MISSING"),
                    "metric_data_quality_status": metric.get("data_quality_status") or result.get("data_quality_status", "DATA_INCOMPLETE"),
                    "chat_session_id": source_row.get("chat_session_id", ""),
                    "n8n_execution_ids": source_row.get("n8n_execution_ids", ""),
                    "combined_execution_json": source_row.get("combined_execution_json", ""),
                    "transcript_path": source_row.get("transcript_path", "") if suite == "TC5" else str(repetition_path),
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


WORKSTATIONS = [
    ["Operator chat", "Receive intent and review decisions", "Natural language", "Clarify, approve, reject, or revise", "Chat turns and review decisions", "Human wording and response latency"],
    ["Intent generator / trt-api", "Convert intent into a validated patch", "Chat turn and current TRT", "Classify, extract, normalize, validate", "IntentPatch or clarification", "Supported schema and validators"],
    ["Supervisor / reconciliation", "Align approved requirements with state", "Released TRT and state records", "Reconcile affected lines and constraints", "Reconciliation plan", "Not a production scheduler"],
    ["Candidate strategy generator", "Propose distinct policy alternatives", "Released TRT, aligned state, Time-Arrival state", "Generate 2-8 schema-valid candidates", "Candidate strategy batch", "One configured LLM endpoint per batch"],
    ["Scenario adapter", "Compile each candidate", "Candidate strategy and scene contract", "Generate and validate ScenarioSpec", "Executable ScenarioSpec", "Cannot repair an infeasible objective"],
    ["Isaac Sim digital twin", "Evaluate physical execution", "ScenarioSpec", "Simulate placement, ordering, reset, and timing", "RunArtifact and raw SQLite evidence", "Simulation fidelity and startup cost"],
    ["Evidence and selector", "Gate and rank outcomes", "RunArtifacts and KPI constraints", "Reject constraint failures; rank eligible candidates by throughput", "Selected candidate or refinement request", "Depends on complete evidence"],
]


CASE_OBJECTIVES = {
    "TC1": ["Natural-language intent and executable-plan correctness", "Incorrect extraction, schema, line scope, or policy semantics", "Validate CP0-CP4 before evidence ranking"],
    "TC2": ["Tool and evidence-query orchestration", "Wrong tool, argument, dependency order, or fabricated answer", "Compare actual trace and answer with fixed L1/L2/L3 gold requirements"],
    "TC3": ["KPI, timing, and graph-ready evidence", "Missing or invalid RunArtifact metrics and constraint evidence", "Run physical what-if scenarios and collect Equations 3.2-3.6"],
    "TC4": ["Deployment-relevant error interception", "Unsafe or invalid state proceeds beyond its required gate", "Inject a defined error and verify interception before deployment"],
    "TC5": ["Human review and closed-loop timing", "Lifecycle events or review completion are missing", "Measure operator-facing wait, verification, and loop time"],
    "TC6": ["Single-model repeated-generation stability", "Same prompt produces malformed, incomplete, or semantically inconsistent output", "Repeat the same prompt with the same model and server presets"],
    "TC7": ["Direct cross-model structured-generation benchmark", "Model-dependent format, semantic, consistency, latency, or token differences", "Send identical fixtures, prompt, context, and schema directly to Gemma, Qwen, and Llama with equal repetitions"],
}


def checkpoint_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for cp in CHECKPOINTS:
        entered = [row for row in rows if row["checkpoints"].get(cp) is not None]
        passed = sum(row["checkpoints"].get(cp) is True for row in entered)
        result[cp] = {"entered": len(entered), "passed": passed, "rate": passed / len(entered) if entered else None}
    return result


def overall_compliance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if row["suite"] in {"TC1", "TC2", "TC3", "TC5"}]
    passed = 0
    for row in eligible:
        applicable = [value for cp, value in row["checkpoints"].items() if cp != "CP6" and value is not None]
        if applicable and all(applicable) and row.get("manual_result") == "PASS":
            passed += 1
    return {
        "entered": len(eligible),
        "passed": passed,
        "rate": passed / len(eligible) if eligible else None,
        "mandatory_criteria": "All recorded applicable CP0-CP5 checks pass, required ScenarioSpec/RunArtifact evidence exists, no mandatory KPI or safety constraint fails, and manual review is PASS. TC4 negative/error-injection cases are reported separately.",
    }


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


def write_detailed_appendix(output: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Appendix: Detailed Trial Evidence",
        "",
        "This appendix contains every evaluated case. `Manual review` denotes a recorded semantic adjudication; reviewer provenance is retained so a Codex or engineer review is not misrepresented as an operator deployment decision.",
        "",
    ]
    for row in rows:
        objective = CASE_OBJECTIVES.get(row["suite"], ["Not registered", "Not registered", "Not registered"])
        checkpoint_rows = [["Checkpoint", "Result", "Test object", "Pass criterion"]]
        for cp in CHECKPOINTS:
            value = row["checkpoints"][cp]
            checkpoint_rows.append([
                cp,
                "PASS" if value is True else ("FAIL" if value is False else "NOT ENTERED / DATA_INCOMPLETE"),
                CHECKPOINTS[cp]["test_object"],
                CHECKPOINTS[cp]["pass_criteria"],
            ])
        lines.extend([
            f"## {row['test_label']} - {row['suite']} / {row['packet_test_id']}",
            "",
            "### Test Objective",
            "",
            f"- Purpose: {objective[0]}",
            f"- Expected problem: {objective[1]}",
            f"- Methodology/checkpoint: {objective[2]}",
            "",
            "### Operator Instruction",
            "",
            row["operator_instruction"] or "DATA_INCOMPLETE - operator instruction missing.",
            "",
            "### Expected Criteria And Stop Rule",
            "",
            "```json",
            row["expected_criteria"],
            "```",
            "",
            "### Full Recorded Interaction",
            "",
            "```text",
            row["full_transcript"].replace("```", "'''"),
            "```",
            "",
            "### Checkpoint Results",
            "",
            markdown_table(checkpoint_rows),
            "",
            "### Automated And Manual Review",
            "",
            f"- Automated result: `{row['automated_result'] or 'DATA_INCOMPLETE'}`",
            f"- Manual review result: `{row['manual_result'] or 'DATA_INCOMPLETE'}`",
            f"- Reviewer type: `{row['reviewer_type']}`",
            f"- Review reason: {row['review_reason'] or 'DATA_INCOMPLETE'}",
            f"- Outcome class: `{row['outcome_class']}`",
            f"- Manual correction used: `{row['manual_correction_used']}`",
            "",
            "### Failure And Correction Evidence",
            "",
            f"- Failure stage: `{row['failure_stage'] or 'NONE_RECORDED'}`",
            f"- Failure source: `{row['failure_cause_code'] or 'NONE_RECORDED'}`",
            f"- Detailed reason: {row['failure_cause'] or 'No failure detail recorded.'}",
            f"- Correction method: {row['correction_method'] or 'No correction recorded.'}",
            "",
            "### IDs, Provenance, And Metrics",
            "",
            f"- Chat session ID: `{row['chat_session_id'] or 'null'}`",
            f"- n8n execution IDs: `{row['n8n_execution_ids'] or 'null'}`",
            f"- ScenarioSpec ID: `{row['scenario_spec_id'] or 'null'}`",
            f"- RunArtifact ID: `{row['run_id'] or 'null'}`",
            f"- Strategy batch ID: `{row['strategy_batch_id'] or 'null'}`",
            f"- Candidate count: `{row['candidate_count'] or 'null'}`",
            f"- Candidate RunArtifact IDs: `{row['candidate_run_ids'] or 'null'}`",
            f"- System-selected candidate: `{row['selected_candidate_strategy_id'] or 'null'}`",
            f"- Selection objective: `{row['selection_objective_id'] or 'null'}`; score=`{row['selection_objective_score'] or 'null'}`",
            f"- Data source: `{row['data_source']}`",
            f"- R_storage: `{fmt_num(row['R_storage'], 6)}` from passed=`{fmt_num(row['N_tool_storage_passed'], 0)}` / total=`{fmt_num(row['N_tool_storage_total'], 0)}`",
            f"- R_reset: `{fmt_num(row['R_reset'], 6)}` from completed=`{fmt_num(row['C_reset_completed'], 0)}` / requested=`{fmt_num(row['C_reset_requested'], 0)}`",
            f"- T_wait: `{fmt_num(row['T_wait_seconds'], 6)}` seconds",
            f"- T_verification wall interval: `{fmt_num(row['T_verification_wall_seconds'], 6)}` seconds",
            f"- Excluded Isaac startup/model-loading interval: `{fmt_num(row['T_isaac_startup_seconds'], 6)}` seconds",
            f"- T_verification after startup exclusion: `{fmt_num(row['T_verification_seconds'], 6)}` seconds; source: {row['T_verification_source']}",
            f"- T_loop: `{fmt_num(row['T_loop_seconds'], 6)}` seconds; review definition: `{row['loop_review_definition'] or 'DATA_INCOMPLETE'}`",
            f"- Data quality: `{row['metric_data_quality_status']}`",
            "",
        ])
    (output / "APPENDIX_DETAILED_TRIAL_EVIDENCE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(
    output: Path,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    source_run_dir: Path,
) -> None:
    suite_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        suite = row["suite"]
        suite_counts.setdefault(suite, {"PASS": 0, "FAIL": 0})
        if row["reviewed_status"] in {"PASS", "FAIL"}:
            suite_counts[suite][row["reviewed_status"]] += 1
    metric_rows = unique_metric_rows(rows)
    detail_rows = [
        [
            "Trial",
            "Suite",
            "Operator instruction",
            "System response excerpt",
            "Manual review result",
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
    metric_detail = [[
        "Trial", "RunArtifact", "R_storage", "Placement passed/total", "R_reset",
        "Reset completed/requested", "T_wait (s)", "Verification wall (s)",
        "Excluded startup (s)", "T_verification (s)", "T_loop (s)",
        "Loop review definition", "Verification source",
    ]]
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
                fmt_num(row.get("R_reset"), 2),
                (
                    "null"
                    if row.get("C_reset_requested") in {None, 0, 0.0}
                    else f"{int(row.get('C_reset_completed') or 0)}/{int(row['C_reset_requested'])}"
                ),
                fmt_num(row.get("T_wait_seconds"), 2),
                fmt_num(row.get("T_verification_wall_seconds"), 2),
                fmt_num(row.get("T_isaac_startup_seconds"), 2),
                fmt_num(row.get("T_verification_seconds"), 2),
                fmt_num(row.get("T_loop_seconds"), 2),
                row.get("loop_review_definition") or "DATA_INCOMPLETE",
                row.get("T_verification_source", ""),
            ]
        )
    checkpoints = checkpoint_summary(rows)
    completion = completion_metrics(rows)
    judgments = auto_human_metrics(rows)
    compliance = overall_compliance(rows)
    outcome_counts: dict[str, int] = {}
    for row in rows:
        outcome_counts[row["outcome_class"]] = outcome_counts.get(row["outcome_class"], 0) + 1
    checkpoint_table = [["Checkpoint", "Object", "Entered", "Passed", "Pass rate", "Mode"]]
    for cp, definition in CHECKPOINTS.items():
        values = checkpoints[cp]
        checkpoint_table.append([
            cp,
            definition["test_object"],
            str(values["entered"]),
            str(values["passed"]),
            fmt_num(values["rate"], 4),
            "Manual review" if definition["mode"] == "MANUAL" else definition["mode"],
        ])
    case_table = [["Case", "Purpose", "Expected problem", "Methodology"]] + [
        [case, values[0], values[1], values[2]] for case, values in CASE_OBJECTIVES.items()
    ]
    failure_table = [["Trial", "Failure stage", "Failure source", "Automated result", "Manual result", "Rejection reason", "Correction method"]]
    for row in rows:
        if row["manual_result"] == "FAIL" or row["failure_cause_code"]:
            failure_table.append([
                row["test_label"],
                row["failure_stage"] or "DATA_INCOMPLETE",
                row["failure_cause_code"] or "DATA_INCOMPLETE",
                row["automated_result"] or "DATA_INCOMPLETE",
                row["manual_result"] or "DATA_INCOMPLETE",
                short(row["review_reason"] or row["failure_cause"], 160),
                short(row["correction_method"], 120) or "Not recorded",
            ])
    disagreement_ids = set(judgments["automated_pass_manual_fail"] + judgments["automated_fail_manual_pass"])
    disagreement_table = [["Trial", "Automated", "Manual review", "Reason"]] + [
        [row["test_label"], row["automated_result"], row["manual_result"], short(row["review_reason"], 180)]
        for row in rows if row.get("test_id") in disagreement_ids
    ]
    llm_roots = [
        source_run_dir / "smoke_extensions" / "llm_generation",
        M12_ROOT / "smoke_extensions" / "llm_generation",
        M12_ROOT / "llm_comparison",
    ]
    llm_root = next(
        (path for path in llm_roots if (path / "llm_generation_benchmark_manifest.json").exists()),
        llm_roots[0],
    )
    llm_manifest = llm_root / "llm_generation_benchmark_manifest.json"
    tc7_csv = llm_root / "tc7_model_comparison_results.csv"
    tc7_rows = read_csv(tc7_csv) if tc7_csv.exists() else []
    tc7_manual_csv = llm_root / "tc7_model_comparison_manual_review.csv"
    tc7_manual = {
        row["model"]: row for row in read_csv(tc7_manual_csv)
    } if tc7_manual_csv.exists() else {}
    llm_status = (
        "Measured benchmark manifest: `llm_generation_benchmark_manifest.json`"
        if llm_manifest.exists()
        else "DATA_INCOMPLETE - TC6/TC7 are defined, but no post-improvement model benchmark has been run."
    )
    lines = [
        "# Milestone 12 Reviewed Trial Engineering Technical Report",
        "",
        f"Date generated: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
        "",
        "## 1. Purpose",
        "",
        "This document summarizes the manually reviewed Milestone 12 trial run used before a larger comparison campaign. The run exercised natural-language operator requests, n8n chat routing, TRT patch review, ScenarioSpec generation, Isaac Sim execution, evidence extraction, deployment-safety checks, lifecycle timing, repeated generation, and cross-model comparison.",
        "",
        "The final result uses manual-review binary adjudication rather than the raw automated status. The automated runner is treated as an evidence collector and first-pass classifier; reviewer provenance remains explicit.",
        "",
        "## 2. Result Summary",
        "",
        f"- Manual-review PASS: {summary['pass_count']}",
        f"- Manual-review FAIL: {summary['fail_count']}",
        f"- Manual-review pass rate: {summary['pass_rate']:.4f}",
        f"- Live simulation rows: {summary['simulation_rows']}",
        f"- Mean R_storage: {fmt_num(summary['R_storage_mean'], 4)}",
        f"- Mean T_wait: {fmt_num(summary['T_wait_mean_seconds'], 2)} seconds",
        f"- Mean T_verification: {fmt_num(summary['T_verification_mean_seconds'], 2)} seconds",
        f"- Mean T_loop: {fmt_num(summary['T_loop_mean_seconds'], 2)} seconds",
        f"- Autonomous success rate: {fmt_num(completion['autonomous_success_rate'], 4)}",
        f"- Assisted completion rate: {fmt_num(completion['assisted_completion_rate'], 4)}",
        f"- Overall completion rate: {fmt_num(completion['overall_completion_rate'], 4)}",
        "",
        "Outcome classes are reported separately: Autonomous Success, Manually Assisted Success, Validation Failure, Input Failure, Simulation Failure, System Error, Manual Rejection, and Evaluation Incomplete when evidence is insufficient. A manually assisted completion is never counted as autonomous success.",
        "",
        markdown_table([["Outcome class", "Count"]] + [[name, str(count)] for name, count in sorted(outcome_counts.items())]),
        "",
        "## 3. Test Environment And Workstations",
        "",
        markdown_table([["Workstation", "Function", "Input", "Task", "Output", "Major limitations"]] + WORKSTATIONS),
        "",
        "Different workstations perform different ownership-scoped tasks. Policy ordering may vary only within the candidate-strategy fields allowed by schema; operator-locked targets, task meaning, KPI constraints, line scope, and Time-Arrival values cannot be substituted or transferred by the candidate generator.",
        "",
        "## 4. Case Studies And Research Checkpoints",
        "",
        markdown_table(case_table),
        "",
        "## 5. Checkpoint Performance",
        "",
        markdown_table(checkpoint_table),
        "",
        "A checkpoint denominator includes only cases that entered that checkpoint. `NOT ENTERED` is not silently converted to pass or fail. In negative interception tests, `FAIL_EXPECTED` means the deliberately invalid input failed that checkpoint as intended; the test case itself may still pass when the system intercepts it at the required stage.",
        "",
        "## 6. Automated Checks And Manual Review",
        "",
        f"- Auto-check pass rate: {fmt_num(judgments['automated_pass_rate'], 4)}",
        f"- Manual-review pass rate: {fmt_num(judgments['manual_verification_pass_rate'], 4)}",
        f"- Auto-manual agreement rate: {fmt_num(judgments['auto_human_agreement_rate'], 4)}",
        "",
        "Cases where automated and manual results disagree:",
        "",
        markdown_table(disagreement_table) if len(disagreement_table) > 1 else "No recorded disagreements, or manual review is incomplete.",
        "",
        "## 7. Overall Compliance Pass Rate",
        "",
        f"Overall Compliance Pass Rate = {compliance['passed']} / {compliance['entered']} = {fmt_num(compliance['rate'], 4)}.",
        "",
        f"Mandatory criteria: {compliance['mandatory_criteria']}",
        "",
        "## 8. LLM Stability And Model Comparison",
        "",
        llm_status,
        "",
        "TC6 records repeated-output JSON accuracy, required-field completeness, intent-classification consistency, field-content consistency, semantic accuracy, output variants, latency, tokens, prompt version, server sampling provenance, and hardware description.",
        "",
        "### 8.1 TC7 Direct Cross-Model Benchmark",
        "",
        "TC7 uses Gemma, Qwen, and Llama as benchmark models. Every model receives the same natural-language fixture, system prompt, state context, JSON Schema, and number of repetitions. The only intentional differences are the model identifier and its matching endpoint. The benchmark calls the three live model endpoints directly; it does not traverse n8n, does not invoke the trt-api HTTP service, does not run Isaac Sim, and does not attempt deployment.",
        "",
        "The preliminary protocol contains one fixture repeated three times per model, for nine measured generations. Automated schema and gold-field scores are reported separately from manual semantic review. TC7 evaluates model behavior under this study's fixed structured-generation contract; it does not claim end-to-end n8n model interchangeability.",
        "",
        "Client requests do not set temperature, top-p, top-k, min-p, presence penalty, or repetition penalty. Unreported server preset values remain null. Exact request, prompt, and schema hashes are retained in the benchmark request snapshots.",
        "",
        markdown_table(
            [["Model", "Fixtures", "Repetitions", "JSON schema accuracy", "Required-field completeness", "Semantic accuracy", "Mean latency (s)", "Max latency (s)", "Manual semantic review"]]
            + [
                [
                    row.get("model", ""),
                    row.get("fixtures", ""),
                    row.get("repetitions_per_fixture", ""),
                    row.get("json_format_accuracy", ""),
                    row.get("required_field_completeness_rate", ""),
                    row.get("semantic_accuracy", ""),
                    row.get("average_generation_seconds", ""),
                    row.get("maximum_generation_seconds", ""),
                    (
                        f"COMPLETED: {tc7_manual[row.get('model', '')].get('strict_semantic_passes', '')}/"
                        f"{tc7_manual[row.get('model', '')].get('repetitions', '')} strict pass"
                        if row.get("model", "") in tc7_manual
                        else row.get("manual_semantic_review_status", "PENDING_MANUAL_REVIEW")
                    ),
                ]
                for row in tc7_rows
            ]
        ) if tc7_rows else "DATA_INCOMPLETE - TC7 has not been executed after the latest system improvements. No cross-model values are reported.",
        "",
        "## 9. Suite-Level Outcomes",
        "",
        markdown_table([["Suite", "PASS", "FAIL"]] + [[suite, str(counts["PASS"]), str(counts["FAIL"])] for suite, counts in sorted(suite_counts.items())]),
        "",
        "## 10. Figures",
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
        "### Checkpoint Pass Rates",
        "",
        "![Checkpoint Pass Rates](figures/fig_09_checkpoint_pass_rates.svg)",
        "",
        "### Automated And Manual Review Rates",
        "",
        "![Automated And Manual Review Rates](figures/fig_10_automated_manual_review_rates.svg)",
        "",
        "### Outcome Classification",
        "",
        "![Outcome Classification](figures/fig_11_outcome_classification.svg)",
        "",
        "## 11. Timing Scope",
        "",
        "`T_verification_wall = T_artifact_created - T_scenario_created` is the measured end-to-end verification wall interval. The reported thesis value is `T_verification = T_verification_wall - T_isaac_startup`, so the measured initial Isaac pre-rendering and model-loading interval is excluded.",
        "",
        "The startup boundary is reconstructed from the initial host-timestamped Isaac warning sequence. The first GPU memory-budget warning terminates the initial startup sequence; when it is absent, the initial articulation-warning burst is used. Repeated articulation warnings near process shutdown are explicitly ignored. The remaining interval includes post-startup initialization, strategy simulation, artifact persistence, and inseparable evidence-pipeline overhead, so it is broader than pure physics-step time.",
        "",
        "## 12. Failure Sources",
        "",
        markdown_table(failure_table) if len(failure_table) > 1 else "No failed cases were recorded.",
        "",
        "## 13. Engineering Findings",
        "",
        "The system successfully answered several configuration and state queries that the automated trace scorer originally marked as failed. This shows that internal tool-trace matching is too brittle to serve as the final experimental judgment.",
        "",
        "The system also showed validator and routing gaps. A negative production-line count reached candidate approval, while the negative-throughput and unsupported-intervention cases were blocked. A valid 99-line task-table query was incorrectly routed as a production modification instead of being answered as an information request. These distinctions matter: large positive values are not inherently invalid, whereas physically meaningless negative line counts require deterministic interception.",
        "",
        "The strongest evidence contribution is the ability to connect natural-language planning with physical simulation artifacts and to retain human refusal as a deployment-safety gate. The corrected lifecycle reconstruction supplies formal events for selected live runs, but rows without a post-evidence review or reset evidence remain explicitly incomplete.",
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
        "`T_wait = T_summary_created - T_intent_created`",
        "",
        "For selected live runs, `T_intent_created` is the start of the n8n `Receive Operator Intent` node and `T_summary_created` is the completion of `Chat Candidate Patch Summary`. Both timestamps come from preserved n8n `runData`; chat text is not used to invent timing values.",
        "",
        "### A.3 Verification Time",
        "",
        "`T_verification_wall = T_artifact_created - T_scenario_created`",
        "",
        "`T_verification = T_verification_wall - T_isaac_startup`",
        "",
        "`T_isaac_startup` begins when the host runner launches the Isaac command and ends at the terminal marker in the initial startup-warning sequence. The preferred terminal marker is the first GPU memory-budget warning; otherwise the initial articulation-warning burst is used. The two articulation warnings match any `Env<id>`. Shutdown-time repetitions are ignored. Host UTC timestamps are preferred, and an Isaac internal timestamp is accepted only as an explicit fallback. If the boundary is unavailable, `T_verification` remains null and is labelled `DATA_INCOMPLETE`.",
        "",
        "### A.4 Closed-Loop Elapsed Time",
        "",
        "`T_loop = T_review_end - T_intent_created`",
        "",
        "For a run that produced a RunArtifact, `T_review_end` must be a post-evidence candidate, deployment, or final operator review event. A pre-simulation candidate approval is not accepted as loop completion. If no post-evidence review was recorded, `T_loop` is null rather than a shorter proxy interval.",
        "",
        "The `T_wait`, `T_verification`, and `T_loop` figure datasets contain only unique selected live simulation rows. A TC5 timing extension that reuses an existing RunArtifact remains in the appendix but is excluded from metric figures to avoid double counting.",
        "",
        "### A.5 Manual Review Pass/Fail",
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
        "- Timing values are reconstructed from preserved n8n node-run timestamps, ScenarioSpec/RunArtifact events, and host-runner logs. Missing lifecycle boundaries remain null.",
        "- `T_verification` excludes the measured initial Isaac startup/model-loading interval; its wall interval and excluded startup value are retained separately.",
        "- `T_loop` is null when no post-evidence review event exists, so it cannot become artificially shorter than verification time.",
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
    source_rows = [{**row, **row["checkpoints"]} for row in rows]
    write_csv(
        output / "reviewed_trial_source_data.csv",
        source_rows,
        [
            "test_label", "test_id", "packet_test_id", "suite", "operator_instruction",
            "expected_criteria", "full_transcript", "transcript_path", "system_response_excerpt",
            "automated_status", "automated_result", "reviewed_status", "manual_result", "review_reason",
            "reviewer_type", "reviewed_at_utc", "outcome_class", "manual_correction_used",
            "manual_intervention_required", "CP0", "CP1", "CP2", "CP3", "CP4", "CP5", "CP6",
            "failure_stage", "failure_cause", "failure_cause_code", "correction_method",
            "scenario_spec_id", "run_id", "strategy_batch_id", "candidate_count", "candidate_run_ids",
            "selected_candidate_strategy_id", "selection_objective_id", "selection_objective_score",
            "R_storage", "N_tool_storage_total", "N_tool_storage_passed",
            "N_failed_tool_storage", "R_reset", "C_reset_requested", "C_reset_completed",
            "T_wait_seconds", "T_verification_wall_seconds", "T_isaac_startup_seconds",
            "T_verification_seconds", "T_verification_source", "T_loop_seconds",
            "loop_review_definition", "include_in_metric_figures", "data_source",
            "metric_data_quality_status", "chat_session_id",
            "n8n_execution_ids", "combined_execution_json",
        ],
    )
    tc2 = [row for row in rows if row["suite"] == "TC2"]
    tc3 = [row for row in rows if row["suite"] == "TC3"]
    tc4 = [row for row in rows if row["suite"] == "TC4"]
    sim_rows = unique_metric_rows(rows)
    r_storage = [row["R_storage"] for row in sim_rows if row["R_storage"] is not None]
    t_wait = [row["T_wait_seconds"] for row in sim_rows if row["T_wait_seconds"] is not None]
    t_ver = [row["T_verification_seconds"] for row in sim_rows if row["T_verification_seconds"] is not None]
    t_loop = [row["T_loop_seconds"] for row in sim_rows if row["T_loop_seconds"] is not None]
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
            {"label": "This study's T_wait mean", "value": (mean(t_wait) or 0) / 60, "series": "This study", "display": f"{(mean(t_wait) or 0):.1f}s", "note": "intent node to candidate-summary node"},
            {"label": "This study's T_verification mean", "value": (mean(t_ver) or 0) / 60, "series": "This study (different scope)", "display": f"{(mean(t_ver) or 0):.1f}s", "note": "wall interval minus measured Isaac startup"},
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
            {"label": "This study's TC2 pass", "value": tc2_pass / len(tc2) if tc2 else 0, "series": "This study (proxy)", "display": f"{tc2_pass}/{len(tc2)}", "note": "reviewed query rows"},
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
            {"label": "This study's TC4 pass", "value": tc4_pass / len(tc4) if tc4 else 0, "series": "This study (proxy)", "display": f"{tc4_pass}/{len(tc4)}", "note": "reviewed error rows"},
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
        source_note="Source: preserved n8n runData lifecycle events for unique live simulation rows.",
    )
    distribution_rows += svg_time_distribution(
        fig_dir / "fig_07_T_verification_distribution.svg",
        title="Verification Time Distribution",
        metric="T_verification_seconds",
        values=t_ver,
        source_note="Source: ScenarioSpec/RunArtifact lifecycle events minus measured initial Isaac startup.",
    )
    distribution_rows += svg_time_distribution(
        fig_dir / "fig_08_T_loop_distribution.svg",
        title="Closed-Loop Elapsed Time Distribution",
        metric="T_loop_seconds",
        values=t_loop,
        source_note="Source: intent event to recorded post-evidence operator review; incomplete loops excluded.",
    )
    cp_values = checkpoint_summary(rows)
    write_csv(
        output / "checkpoint_summary.csv",
        [
            {
                "checkpoint": cp,
                "test_object": CHECKPOINTS[cp]["test_object"],
                "pass_criteria": CHECKPOINTS[cp]["pass_criteria"],
                "entered": values["entered"],
                "passed": values["passed"],
                "pass_rate": values["rate"],
                "assessment_mode": CHECKPOINTS[cp]["mode"],
            }
            for cp, values in cp_values.items()
        ],
        [
            "checkpoint", "test_object", "pass_criteria", "entered", "passed",
            "pass_rate", "assessment_mode",
        ],
    )
    svg_grouped_horizontal(
        fig_dir / "fig_09_checkpoint_pass_rates.svg",
        title="Checkpoint Pass Rates",
        subtitle="Denominators include only cases that entered each checkpoint.",
        x_label="pass rate",
        max_value=1.0,
        rows=[
            {
                "label": cp,
                "value": values["rate"] or 0.0,
                "series": "Measured",
                "display": f"{values['passed']}/{values['entered']}" if values["entered"] else "DATA_INCOMPLETE",
                "note": CHECKPOINTS[cp]["test_object"],
            }
            for cp, values in cp_values.items()
        ],
    )
    judgment_values = auto_human_metrics(rows)
    svg_grouped_horizontal(
        fig_dir / "fig_10_automated_manual_review_rates.svg",
        title="Automated And Manual Review Rates",
        subtitle="Automated checks and manual review are reported separately.",
        x_label="rate",
        max_value=1.0,
        rows=[
            {"label": "Auto-check pass", "value": judgment_values["automated_pass_rate"] or 0.0, "series": "Automated", "display": fmt_num(judgment_values["automated_pass_rate"], 3), "note": "automated cases with binary result"},
            {"label": "Manual-review pass", "value": judgment_values["manual_verification_pass_rate"] or 0.0, "series": "Manual review", "display": fmt_num(judgment_values["manual_verification_pass_rate"], 3), "note": "manually reviewed cases"},
            {"label": "Auto-manual agreement", "value": judgment_values["auto_human_agreement_rate"] or 0.0, "series": "Agreement", "display": fmt_num(judgment_values["auto_human_agreement_rate"], 3), "note": "same binary determination"},
        ],
    )
    outcome_counts: dict[str, int] = {}
    for row in rows:
        outcome_counts[row["outcome_class"]] = outcome_counts.get(row["outcome_class"], 0) + 1
    svg_grouped_horizontal(
        fig_dir / "fig_11_outcome_classification.svg",
        title="Outcome Classification",
        subtitle="Autonomous and manually assisted completions remain separate.",
        x_label="case count",
        max_value=max(1, max(outcome_counts.values(), default=1)),
        rows=[
            {"label": name.replace("_", " ").title(), "value": count, "series": "Outcome", "display": str(count), "note": "recorded outcome class"}
            for name, count in sorted(outcome_counts.items())
        ],
    )
    write_csv(output / "time_distribution_source.csv", distribution_rows, ["metric", "bin_start", "bin_end", "count", "total", "probability", "mean", "max"])
    summary = {
        "rows": len(rows),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "pass_rate": pass_count / len(rows) if rows else None,
        "simulation_rows": len(sim_rows),
        "R_storage_mean": mean(r_storage),
        "T_wait_mean_seconds": mean(t_wait),
        "T_verification_mean_seconds": mean(t_ver),
        "T_loop_mean_seconds": mean(t_loop),
        "output": str(output),
    }
    (output / "reviewed_trial_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    write_report(output, rows, summary, source_run_dir)
    write_detailed_appendix(output, rows)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
