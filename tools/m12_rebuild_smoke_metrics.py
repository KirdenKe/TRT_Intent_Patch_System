from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any

from tools.m12_ingest_n8n_execution import infer_lifecycle_timestamps, insert_event, load_payload
from trt_core.m12 import collect_run_metrics, connect_metrics_db, export_query_to_csv, now_utc
from trt_core.repository import TRTRepository


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def rebuild(run_dir: Path, output_db: Path) -> dict[str, Any]:
    repository = TRTRepository()
    selected_rows = [
        row
        for row in _rows(run_dir / "full_n8n_results_latest.csv")
        if row.get("run_id") and row.get("scenario_spec_id") and row.get("strategy_batch_id")
    ]
    if output_db.exists():
        output_db.unlink()
    output_csv = output_db.with_suffix(".csv")
    if output_csv.exists():
        output_csv.unlink()

    results: list[dict[str, Any]] = []
    with connect_metrics_db(path=output_db, repository=repository) as connection:
        for row in selected_rows:
            execution_path = Path(row["combined_execution_json"])
            if not execution_path.is_absolute():
                execution_path = repository.root / execution_path
            payload = load_payload(execution_path)
            lifecycle = infer_lifecycle_timestamps(payload)
            execution_ids = [value for value in row.get("n8n_execution_ids", "").split(";") if value]
            execution_id = execution_ids[-1] if execution_ids else None
            for event_name, timestamp in lifecycle.items():
                if timestamp:
                    insert_event(
                        connection,
                        event_name=event_name,
                        event_ts_utc=timestamp,
                        test_id=row["test_id"],
                        run_id=row["run_id"],
                        scenario_spec_id=row["scenario_spec_id"],
                        workflow_execution_id=execution_id,
                        chat_session_id=row.get("chat_session_id") or None,
                    )
            metrics = collect_run_metrics(
                repository,
                row["run_id"],
                connection=connection,
                data_source="LIVE_N8N_CHAT",
                data_source_detail=f"Recalculated from preserved evidence for {row['test_id']}.",
                generated_by="tools.m12_rebuild_smoke_metrics",
                is_live_test_override=True,
            )
            results.append(
                {
                    "test_id": row["test_id"],
                    "run_id": row["run_id"],
                    "scenario_spec_id": row["scenario_spec_id"],
                    "lifecycle": lifecycle,
                    "metrics": metrics,
                }
            )
        export_query_to_csv(
            connection,
            "SELECT * FROM m12_run_metrics ORDER BY test_case_id, run_id",
            output_csv,
        )

    missing_lifecycle: dict[str, list[str]] = {}
    for result in results:
        lifecycle = result["lifecycle"]
        missing = [
            key
            for key in (
                "INTENT_CREATED",
                "CANDIDATE_SUMMARY_CREATED",
                "SCENARIO_CREATED",
                "SIMULATION_STARTED",
                "RUN_ARTIFACT_CREATED",
            )
            if not lifecycle.get(key)
        ]
        if not (lifecycle.get("DEPLOYMENT_REVIEW_ENDED") or lifecycle.get("CANDIDATE_REVIEW_ENDED")):
            missing.append("FINAL_REVIEW_EVENT")
        missing_lifecycle[result["test_id"]] = missing
    invalid_timing = [
        result["test_id"]
        for result in results
        if result["metrics"].get("T_verification_seconds") is None
        or result["metrics"].get("T_verification_seconds", -1) < 0
    ]
    manifest = {
        "created_at_utc": now_utc(),
        "generated_by": "tools.m12_rebuild_smoke_metrics",
        "source_results": str(run_dir / "full_n8n_results_latest.csv"),
        "output_db": str(output_db),
        "output_csv": str(output_csv),
        "selected_live_runs": len(results),
        "test_ids": [result["test_id"] for result in results],
        "missing_lifecycle_by_test": missing_lifecycle,
        "invalid_verification_timing": invalid_timing,
        "status": "PASS"
        if results and not invalid_timing and not any(missing_lifecycle.values())
        else "DATA_INCOMPLETE",
    }
    (run_dir / "metrics_recalculation_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild a completed M12 smoke metrics database from preserved evidence.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-db")
    parser.add_argument("--install", action="store_true", help="Install validated output as the run's canonical metrics DB/CSV.")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    output_db = Path(args.output_db).resolve() if args.output_db else run_dir / "m12_metrics_recalculated.sqlite3"
    manifest = rebuild(run_dir, output_db)
    if args.install and manifest["status"] == "PASS":
        shutil.copy2(output_db, run_dir / "m12_metrics.sqlite3")
        shutil.copy2(output_db.with_suffix(".csv"), run_dir / "m12_metrics.csv")
        manifest["installed_as_canonical"] = True
        (run_dir / "metrics_recalculation_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
