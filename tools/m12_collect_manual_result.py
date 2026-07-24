from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trt_core.m12 import M12_ROOT, collect_run_metrics, connect_metrics_db, export_metrics_csv, now_utc, parse_ts, provenance
from trt_core.repository import TRTRepository


ALLOWED_STATUS = {
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


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def normalize_ts(value: str | None) -> str | None:
    parsed = parse_ts(value)
    if parsed is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def insert_manual_event(
    connection: Any,
    *,
    event_name: str,
    event_ts_utc: str,
    test_id: str,
    run_id: str | None,
    scenario_spec_id: str | None,
    session_id: str | None,
    operator_id: str | None,
    n8n_execution_id: str | None,
) -> None:
    prov = provenance(
        "LIVE_N8N_CHAT",
        detail=f"Manual n8n transcript timestamp for {test_id}.",
        generated_by="tools.m12_collect_manual_result",
        test_case_id=test_id,
        run_id=run_id,
        scenario_spec_id=scenario_spec_id,
        workflow_execution_id=n8n_execution_id,
        chat_session_id=session_id,
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
            session_id,
            operator_id,
            scenario_spec_id,
            None,
            None,
            event_name,
            event_ts_utc,
            "tools.m12_collect_manual_result",
            json.dumps({"manual_test_id": test_id}, sort_keys=True),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Record one manual n8n M12 test result and collect metrics when a real run ID is available.")
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--status", required=True, choices=sorted(ALLOWED_STATUS))
    parser.add_argument("--chat-transcript", required=True)
    parser.add_argument("--scenario-spec-id")
    parser.add_argument("--run-id")
    parser.add_argument("--n8n-execution-id")
    parser.add_argument("--chat-session-id")
    parser.add_argument("--operator-id", default="op_001")
    parser.add_argument("--notes", default="")
    parser.add_argument("--intent-created-at", help="UTC ISO timestamp for INTENT_CREATED.")
    parser.add_argument("--summary-created-at", help="UTC ISO timestamp for CANDIDATE_SUMMARY_CREATED.")
    parser.add_argument("--candidate-review-end-at", help="UTC ISO timestamp for CANDIDATE_REVIEW_ENDED.")
    parser.add_argument("--scenario-created-at", help="UTC ISO timestamp for SCENARIO_CREATED.")
    parser.add_argument("--artifact-created-at", help="UTC ISO timestamp for RUN_ARTIFACT_CREATED.")
    parser.add_argument("--deployment-review-end-at", help="UTC ISO timestamp for DEPLOYMENT_REVIEW_ENDED.")
    parser.add_argument("--output", default=str(M12_ROOT / "manual_results.jsonl"))
    parser.add_argument("--metrics-output", default=str(M12_ROOT / "m12_metrics.sqlite3"))
    args = parser.parse_args()

    repository = TRTRepository()
    transcript_path = resolve_path(repository, args.chat_transcript)
    if not transcript_path.exists():
        raise FileNotFoundError(f"Chat transcript file not found: {transcript_path}")
    transcript_text = transcript_path.read_text(encoding="utf-8")

    metrics_result: dict[str, Any] | None = None
    metrics_error: str | None = None
    timestamp_args = {
        "INTENT_CREATED": normalize_ts(args.intent_created_at),
        "CANDIDATE_SUMMARY_CREATED": normalize_ts(args.summary_created_at),
        "CANDIDATE_REVIEW_ENDED": normalize_ts(args.candidate_review_end_at),
        "SCENARIO_CREATED": normalize_ts(args.scenario_created_at),
        "RUN_ARTIFACT_CREATED": normalize_ts(args.artifact_created_at),
        "DEPLOYMENT_REVIEW_ENDED": normalize_ts(args.deployment_review_end_at),
    }
    if args.run_id:
        try:
            metrics_db = resolve_path(repository, args.metrics_output)
            with connect_metrics_db(path=metrics_db, repository=repository) as connection:
                for event_name, timestamp in timestamp_args.items():
                    if timestamp:
                        insert_manual_event(
                            connection,
                            event_name=event_name,
                            event_ts_utc=timestamp,
                            test_id=args.test_id,
                            run_id=args.run_id,
                            scenario_spec_id=args.scenario_spec_id,
                            session_id=args.chat_session_id,
                            operator_id=args.operator_id,
                            n8n_execution_id=args.n8n_execution_id,
                        )
                metrics_result = collect_run_metrics(
                    repository,
                    args.run_id,
                    connection=connection,
                    data_source="LIVE_N8N_CHAT",
                    data_source_detail=f"Collected from manual n8n chat result {args.test_id}.",
                    is_live_test_override=True,
                )
            export_metrics_csv(repository=repository)
        except Exception as exc:
            metrics_error = f"{type(exc).__name__}: {exc}"

    payload = {
        "created_at_utc": now_utc(),
        "test_case_id": args.test_id,
        "status": args.status,
        "operator_id": args.operator_id,
        "scenario_spec_id": args.scenario_spec_id or None,
        "run_id": args.run_id or None,
        "n8n_execution_id": args.n8n_execution_id or None,
        "chat_session_id": args.chat_session_id or None,
        "chat_transcript_path": str(transcript_path),
        "chat_transcript_text": transcript_text,
        "notes": args.notes,
        "data_source": "LIVE_N8N_CHAT",
        "data_source_detail": "Manual operator-entered n8n chat transcript and supplied run identifiers.",
        "generated_by": "tools.m12_collect_manual_result",
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
        "manual_timestamps_recorded": {key: value for key, value in timestamp_args.items() if value},
    }
    output_path = resolve_path(repository, args.output)
    append_jsonl(output_path, payload)
    print(json.dumps({"status": "MANUAL_RESULT_RECORDED", "output": str(output_path), "metrics_collected": payload["metrics_collected"], "metrics_error": metrics_error}, indent=2, sort_keys=True))
    return 0 if metrics_error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
