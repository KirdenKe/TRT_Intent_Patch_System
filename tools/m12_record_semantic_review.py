"""Record an evidence-backed case review separately from automated scoring."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trt_core.experiment_evaluation import FAILURE_CAUSES
from trt_core.repository import PROJECT_ROOT


REVIEW_RESULTS = {"PASS", "FAIL"}
REVIEWER_TYPES = {
    "CODEX_SEMANTIC_REVIEW",
    "ENGINEER_REVIEW",
    "OPERATOR_REVIEW",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def record_review(
    combined_path: Path,
    *,
    result: str,
    reason: str,
    reviewer_type: str,
    output: Path,
    failure_cause: str | None = None,
    correction_method: str | None = None,
) -> dict[str, Any]:
    combined = json.loads(combined_path.read_text(encoding="utf-8"))
    normalized_result = result.upper()
    normalized_reviewer = reviewer_type.upper()
    if normalized_result not in REVIEW_RESULTS:
        raise ValueError(f"result must be one of {sorted(REVIEW_RESULTS)}")
    if normalized_reviewer not in REVIEWER_TYPES:
        raise ValueError(f"reviewer_type must be one of {sorted(REVIEWER_TYPES)}")
    if normalized_result == "FAIL" and not failure_cause:
        raise ValueError("failure_cause is required for a FAIL review")
    if failure_cause and failure_cause not in FAILURE_CAUSES:
        raise ValueError(f"failure_cause must be one of {sorted(FAILURE_CAUSES)}")
    if not reason.strip():
        raise ValueError("reason must not be empty")

    row = combined.get("row") or {}
    review = {
        "test_id": combined.get("test_id") or row.get("test_id"),
        "suite": row.get("suite"),
        "review_result": normalized_result,
        "review_reason": reason.strip(),
        "reviewer_type": normalized_reviewer,
        "reviewed_at_utc": now_utc(),
        "human_reviewed": normalized_reviewer in {"ENGINEER_REVIEW", "OPERATOR_REVIEW"},
        "operator_cp6_result": (
            normalized_result if normalized_reviewer == "OPERATOR_REVIEW" else None
        ),
        "failure_cause": failure_cause,
        "correction_method": correction_method,
        "scenario_spec_id": combined.get("scenario_spec_id"),
        "run_id": combined.get("run_id"),
        "chat_session_id": combined.get("session_id"),
        "combined_execution_json": str(combined_path.resolve()),
        "automated_result": (
            (combined.get("checkpoint_evaluation") or {}).get("automated_result")
        ),
        "packet_score": combined.get("packet_score"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(review, sort_keys=True))
        handle.write("\n")
    return review


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--combined-execution", type=Path, required=True)
    parser.add_argument("--result", choices=sorted(REVIEW_RESULTS), required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--reviewer-type",
        choices=sorted(REVIEWER_TYPES),
        default="CODEX_SEMANTIC_REVIEW",
    )
    parser.add_argument("--failure-cause", choices=sorted(FAILURE_CAUSES))
    parser.add_argument("--correction-method")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/reports/m12/semantic_reviews.jsonl"),
    )
    args = parser.parse_args()

    combined_path = args.combined_execution
    if not combined_path.is_absolute():
        combined_path = PROJECT_ROOT / combined_path
    output = args.output
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    review = record_review(
        combined_path,
        result=args.result,
        reason=args.reason,
        reviewer_type=args.reviewer_type,
        output=output,
        failure_cause=args.failure_cause,
        correction_method=args.correction_method,
    )
    print(json.dumps(review, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
