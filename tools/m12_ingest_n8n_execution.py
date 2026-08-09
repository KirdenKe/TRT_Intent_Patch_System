from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from trt_core.m12 import (
    M12_ROOT,
    collect_run_metrics,
    connect_metrics_db,
    export_query_to_csv,
    now_utc,
    parse_ts,
    provenance,
)
from trt_core.repository import TRTRepository


RUN_ID_RE = re.compile(r"\bsim_[0-9a-fA-F-]{8,}\b")
SCENARIO_ID_RE = re.compile(r"\bscn_[0-9a-fA-F-]{8,}\b")
SQLITE_PATH_RE = re.compile(r"[\w:.$\\/\- ]*outputs[\\/]+run_artifacts[\\/]+sim_[0-9a-fA-F-]+\.sqlite3?")
STATUS_VALUES = {
    "PASS",
    "FAIL",
    "REJECTED",
    "SIMULATION_FAILED",
    "INCONCLUSIVE",
    "FAIL_SIMULATION_CONFIG_DRIFT",
    "FAIL_ERROR_NOT_INTERCEPTED",
    "WORKFLOW_LOOP",
    "EVIDENCE_SUMMARY_MISSING",
}


def resolve_path(repository: TRTRepository, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repository.root / path


def load_payload(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text}


def walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def strings(value: Any) -> list[str]:
    result = []
    for item in walk(value):
        if isinstance(item, str):
            result.append(item)
    return result


def json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def normalize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # n8n node runData often stores startTime in epoch milliseconds.
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000.0
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    parsed = parse_ts(value)
    if parsed is not None:
        return parsed.isoformat().replace("+00:00", "Z")
    return None


def n8n_node_runs(payload: Any) -> list[dict[str, Any]]:
    """Return n8n runData entries while retaining their parent node names."""

    runs: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for item in walk(payload):
        if not isinstance(item, dict):
            continue
        run_data = item.get("runData")
        if not isinstance(run_data, dict):
            continue
        for node_name, entries in run_data.items():
            if not isinstance(entries, list):
                entries = [entries]
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("startTime") is None:
                    continue
                start_ms = int(entry["startTime"])
                duration_ms = int(entry.get("executionTime") or 0)
                identity = (str(node_name), start_ms, duration_ms)
                if identity in seen:
                    continue
                seen.add(identity)
                runs.append(
                    {
                        "node_name": str(node_name),
                        "start_ms": start_ms,
                        "duration_ms": duration_ms,
                        "start_at_utc": normalize_timestamp(start_ms),
                        "end_at_utc": normalize_timestamp(start_ms + duration_ms),
                    }
                )
    return sorted(runs, key=lambda row: (row["start_ms"], row["node_name"]))


def infer_lifecycle_timestamps(payload: Any) -> dict[str, str | None]:
    runs = n8n_node_runs(payload)

    def first_start(*exact_names: str) -> str | None:
        names = {name.lower() for name in exact_names}
        return next((row["start_at_utc"] for row in runs if row["node_name"].lower() in names), None)

    def first_end(*exact_names: str) -> str | None:
        names = {name.lower() for name in exact_names}
        return next((row["end_at_utc"] for row in runs if row["node_name"].lower() in names), None)

    def last_end(*exact_names: str) -> str | None:
        names = {name.lower() for name in exact_names}
        matches = [row["end_at_utc"] for row in runs if row["node_name"].lower() in names]
        return matches[-1] if matches else None

    lifecycle = {
        "INTENT_CREATED": first_start("Receive Operator Intent"),
        "CANDIDATE_SUMMARY_CREATED": first_end("Chat Candidate Patch Summary"),
        "CANDIDATE_REVIEW_ENDED": first_end("Build Direct Approval Decision Turn"),
        "SCENARIO_CREATED": None,
        "SIMULATION_STARTED": None,
        "RUN_ARTIFACT_CREATED": None,
        "DEPLOYMENT_REVIEW_ENDED": last_end(
            "Restore Deployment Success After Clear",
            "Restore Non-Deploy Message After Clear",
            "Return Deployment Success",
            "Return Non-Deploy Decision Message",
        ),
    }

    strategy_selection = payload.get("strategy_selection") if isinstance(payload, dict) else None
    selected_run_id = payload.get("run_id") if isinstance(payload, dict) else None
    if isinstance(strategy_selection, dict):
        selected_run_id = strategy_selection.get("selected_run_id") or selected_run_id
        candidates = strategy_selection.get("ranked_candidates") or []
        candidate = next(
            (row for row in candidates if isinstance(row, dict) and row.get("run_id") == selected_run_id),
            None,
        )
        if isinstance(candidate, dict):
            timing = candidate.get("timing") if isinstance(candidate.get("timing"), dict) else {}
            lifecycle["SCENARIO_CREATED"] = normalize_timestamp(candidate.get("scenario_created_at_utc"))
            lifecycle["SIMULATION_STARTED"] = normalize_timestamp(timing.get("isaac_command_started_at_utc"))
            lifecycle["RUN_ARTIFACT_CREATED"] = normalize_timestamp(candidate.get("artifact_created_at_utc"))
    return lifecycle


def extract_messages(payload: Any) -> str:
    picked = []
    keys = {"operator_message", "message", "formatted_answer", "response", "text", "content", "raw_chat_input", "latest_user_message"}
    for item in walk(payload):
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if key in keys and isinstance(value, str) and value.strip():
                picked.append(f"{key}: {value.strip()}")
    if not picked:
        return json_text(payload)[:20000]
    seen = []
    for item in picked:
        if item not in seen:
            seen.append(item)
    return "\n\n".join(seen)


def insert_event(
    connection: Any,
    *,
    event_name: str,
    event_ts_utc: str,
    test_id: str,
    run_id: str | None,
    scenario_spec_id: str | None,
    workflow_execution_id: str | None,
    chat_session_id: str | None,
) -> None:
    prov = provenance(
        "LIVE_N8N_CHAT",
        detail=f"Ingested from n8n execution export for {test_id}.",
        generated_by="tools.m12_ingest_n8n_execution",
        test_case_id=test_id,
        run_id=run_id,
        scenario_spec_id=scenario_spec_id,
        workflow_execution_id=workflow_execution_id,
        chat_session_id=chat_session_id,
        semi_manual=True,
        deployment_suppressed=True,
    )
    connection.execute(
        """
        INSERT INTO m12_event_log (
            run_id, session_id, operator_id, scenario_spec_id, trt_id, trt_version,
            event_name, event_ts_utc, source_module, payload_json, created_at,
            data_source, data_source_detail, generated_by, created_at_utc,
            is_live_test, is_fixture, is_historical, test_case_id,
            workflow_execution_id, chat_session_id, semi_manual, deployment_suppressed,
            approval_status, approved_by_operator_id, approved_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            chat_session_id,
            None,
            scenario_spec_id,
            None,
            None,
            event_name,
            event_ts_utc,
            "tools.m12_ingest_n8n_execution",
            json.dumps({"test_id": test_id, "workflow_execution_id": workflow_execution_id}, sort_keys=True),
            now_utc(),
            prov["data_source"],
            prov["data_source_detail"],
            prov["generated_by"],
            prov["created_at_utc"],
            prov["is_live_test"],
            prov["is_fixture"],
            prov["is_historical"],
            prov["test_case_id"],
            prov["workflow_execution_id"],
            prov["chat_session_id"],
            prov["semi_manual"],
            prov["deployment_suppressed"],
            prov["approval_status"],
            prov["approved_by_operator_id"],
            prov["approved_at_utc"],
        ),
    )


def infer_status(payload: Any) -> str:
    text = json_text(payload).lower()
    if "workflow_loop" in text:
        return "WORKFLOW_LOOP"
    if "fail_error_not_intercepted" in text:
        return "FAIL_ERROR_NOT_INTERCEPTED"
    if "simulation failed" in text or '"status": "failed"' in text:
        return "SIMULATION_FAILED"
    if "rejected" in text or "needs_clarification" in text:
        return "REJECTED"
    if "evidence summary" in text or "deployment" in text or '"status": "completed"' in text:
        return "PASS"
    return "INCONCLUSIVE"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest one n8n execution export/chat JSON and record an M12 manual result.")
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--execution-json", required=True)
    parser.add_argument("--status", choices=sorted(STATUS_VALUES))
    parser.add_argument("--chat-session-id")
    parser.add_argument("--n8n-execution-id")
    parser.add_argument("--run-id")
    parser.add_argument("--scenario-spec-id")
    parser.add_argument("--transcript-output")
    parser.add_argument("--output", default=str(M12_ROOT / "manual_results.jsonl"))
    parser.add_argument("--metrics-output", default=str(M12_ROOT / "m12_metrics.sqlite3"))
    args = parser.parse_args()

    repository = TRTRepository()
    input_path = resolve_path(repository, args.execution_json)
    payload = load_payload(input_path)
    text = json_text(payload)
    strategy_selection = payload.get("strategy_selection") if isinstance(payload, dict) else {}
    run_id = (
        args.run_id
        or (strategy_selection.get("selected_run_id") if isinstance(strategy_selection, dict) else None)
        or (payload.get("run_id") if isinstance(payload, dict) else None)
        or first_match(RUN_ID_RE, text)
    )
    scenario_spec_id = (
        args.scenario_spec_id
        or (strategy_selection.get("selected_scenario_spec_id") if isinstance(strategy_selection, dict) else None)
        or (payload.get("scenario_spec_id") if isinstance(payload, dict) else None)
        or first_match(SCENARIO_ID_RE, text)
    )
    output_db_path = first_match(SQLITE_PATH_RE, text)
    workflow_execution_id = args.n8n_execution_id
    if not workflow_execution_id and isinstance(payload, dict):
        workflow_execution_id = str(payload.get("id") or payload.get("executionId") or "")
    chat_session_id = args.chat_session_id
    if not chat_session_id:
        for item in walk(payload):
            if isinstance(item, dict):
                candidate = item.get("session_id") or item.get("sessionId") or item.get("chat_session_id")
                if candidate:
                    chat_session_id = str(candidate)
                    break
    lifecycle = infer_lifecycle_timestamps(payload)
    transcript_text = extract_messages(payload)
    status = args.status or infer_status(payload)

    transcript_path = (
        resolve_path(repository, args.transcript_output)
        if args.transcript_output
        else repository.root / M12_ROOT / "manual_transcripts" / f"{args.test_id}.txt"
    )
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        "\n".join(
            [
                f"TEST_ID: {args.test_id}",
                f"STATUS: {status}",
                f"CHAT_SESSION_ID: {chat_session_id or ''}",
                f"N8N_EXECUTION_ID: {workflow_execution_id or ''}",
                f"SCENARIO_SPEC_ID: {scenario_spec_id or ''}",
                f"RUN_ID: {run_id or ''}",
                f"OUTPUT_DB_PATH: {output_db_path or ''}",
                "",
                "LIFECYCLE_TIMESTAMPS_UTC",
                *(f"{key}: {value or ''}" for key, value in lifecycle.items()),
                "",
                "EXTRACTED CHAT / OUTPUT TEXT",
                transcript_text,
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    metrics_result: dict[str, Any] | None = None
    metrics_error: str | None = None
    metrics_db = resolve_path(repository, args.metrics_output)
    with connect_metrics_db(path=metrics_db, repository=repository) as connection:
        if run_id:
            for event_name, timestamp in lifecycle.items():
                if timestamp:
                    insert_event(
                        connection,
                        event_name=event_name,
                        event_ts_utc=timestamp,
                        test_id=args.test_id,
                        run_id=run_id,
                        scenario_spec_id=scenario_spec_id,
                        workflow_execution_id=workflow_execution_id,
                        chat_session_id=chat_session_id,
                    )
            try:
                metrics_result = collect_run_metrics(
                    repository,
                    run_id,
                    connection=connection,
                    data_source="LIVE_N8N_CHAT",
                    data_source_detail=f"Ingested from n8n execution export {input_path.name}.",
                    generated_by="tools.m12_ingest_n8n_execution",
                    is_live_test_override=True,
                )
            except Exception as exc:
                metrics_error = f"{type(exc).__name__}: {exc}"
        if metrics_result is not None:
            export_query_to_csv(
                connection,
                "SELECT * FROM m12_run_metrics ORDER BY run_id",
                metrics_db.with_suffix(".csv"),
            )

    result = {
        "created_at_utc": now_utc(),
        "test_case_id": args.test_id,
        "status": status,
        "scenario_spec_id": scenario_spec_id,
        "run_id": run_id,
        "output_db_path": output_db_path,
        "n8n_execution_id": workflow_execution_id or None,
        "chat_session_id": chat_session_id,
        "chat_transcript_path": str(transcript_path),
        "source_execution_json": str(input_path),
        "data_source": "LIVE_N8N_CHAT",
        "data_source_detail": "Parsed from n8n execution export or chat output JSON.",
        "generated_by": "tools.m12_ingest_n8n_execution",
        "is_live_test": True,
        "is_fixture": False,
        "is_historical": False,
        "semi_manual": True,
        "deployment_suppressed": True,
        "deployment_suppressed_reason": "M12 manual comparison test mode",
        "metrics_fabricated": False,
        "metrics_collected": metrics_result is not None,
        "metrics_data_quality_status": metrics_result.get("data_quality_status") if metrics_result else None,
        "metrics_error": metrics_error,
        "lifecycle_timestamps": lifecycle,
    }
    output_path = resolve_path(repository, args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, sort_keys=True))
        handle.write("\n")

    print(json.dumps({"status": "N8N_EXECUTION_INGESTED", "result": result}, indent=2, sort_keys=True))
    return 0 if metrics_error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
