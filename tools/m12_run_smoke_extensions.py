"""Run the TC5-TC7 checks that extend the 27-case n8n smoke queue."""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.llm_generation_benchmark import load_jsonl, run_benchmark
from trt_core.repository import PROJECT_ROOT


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["extension_id", "status", "data_quality_status"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tc5_result(n8n_results: Path, metrics_db: Path) -> dict[str, Any]:
    source = next((row for row in read_csv(n8n_results) if row.get("test_id") == "SMOKE_001"), None)
    run_id = (source or {}).get("run_id")
    metric: dict[str, Any] = {}
    if run_id and metrics_db.exists():
        connection = sqlite3.connect(metrics_db)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT run_id, scenario_spec_id, intent_created_at, summary_created_at,
                   candidate_review_end_at, deployment_review_end_at,
                   scenario_created_at, artifact_created_at,
                   T_wait_seconds, T_verification_seconds,
                   T_verification_wall_seconds, T_isaac_startup_seconds,
                   verification_timing_source, T_loop_seconds,
                   data_quality_status, data_quality_reason
              FROM m12_run_metrics
             WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        metric = dict(row) if row else {}
        connection.close()
    required = [
        "intent_created_at",
        "summary_created_at",
        "scenario_created_at",
        "artifact_created_at",
        "T_wait_seconds",
        "T_verification_seconds",
        "T_isaac_startup_seconds",
        "T_loop_seconds",
    ]
    missing = [field for field in required if metric.get(field) is None]
    if metric.get("candidate_review_end_at") is None and metric.get("deployment_review_end_at") is None:
        missing.append("review_end_at")
    return {
        "extension_id": "SMOKE_028",
        "test_case_id": "TC5",
        "status": "PASS" if source and not missing else "INCOMPLETE",
        "source_smoke_sequence": "SMOKE_001",
        "run_id": run_id or "",
        "scenario_spec_id": metric.get("scenario_spec_id") or (source or {}).get("scenario_spec_id", ""),
        "measurements_json": json.dumps(metric, sort_keys=True),
        "missing_fields_json": json.dumps(missing),
        "data_source": "LIVE_N8N_CHAT" if source else "DATA_MISSING",
        "data_source_detail": "Lifecycle derived from the recorded SMOKE_001 n8n/trt-api/Isaac run.",
        "data_quality_status": "OK" if source and not missing else "DATA_INCOMPLETE",
        "created_at_utc": now_utc(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n8n-results", type=Path)
    parser.add_argument("--metrics-db", type=Path, default=Path("outputs/reports/m12/m12_metrics.sqlite3"))
    parser.add_argument("--seed-data", type=Path, default=Path("outputs/reports/m12/seed_data"))
    parser.add_argument("--output", type=Path, default=Path("outputs/reports/m12/smoke_extensions"))
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    resolve = lambda path: path if path.is_absolute() else PROJECT_ROOT / path
    output = resolve(args.output)
    output.mkdir(parents=True, exist_ok=True)
    packet = PROJECT_ROOT / "outputs" / "reports" / "m12" / "manual_test_packet" / "smoke_extension_tc5_tc7.csv"
    plan = read_csv(packet)
    if args.plan_only:
        manifest = {
            "status": "PLAN_ONLY",
            "created_at_utc": now_utc(),
            "core_smoke_denominator": 27,
            "extensions": plan,
            "tests_executed": 0,
            "final_charts_generated": False,
        }
        (output / "smoke_extension_plan.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps(manifest, indent=2))
        return 0

    if args.n8n_results is None:
        raise SystemExit("--n8n-results is required unless --plan-only is used.")
    n8n_results = resolve(args.n8n_results)
    metrics_db = resolve(args.metrics_db)
    seed_data = resolve(args.seed_data)
    results = [tc5_result(n8n_results, metrics_db)]

    benchmark_dir = output / "llm_generation"
    fixture = load_jsonl(seed_data / "operator_intent_gold.jsonl")[:1]
    fixture[0] = {
        **fixture[0],
        "operator_text": plan[1]["natural_language_trigger"],
        "expected_simulation_config_updates": {"num_envs": 4, "add_reference_number": 2},
    }
    benchmark_manifest = run_benchmark(
        rows=fixture,
        repetitions=args.repetitions,
        output=benchmark_dir,
        timeout_seconds=args.timeout_seconds,
        hardware_description=f"client={platform.platform()}; model_server_hardware=NOT_REPORTED",
    )
    for test_case_id, filename in (
        ("TC6", "tc6_generation_stability_results.csv"),
        ("TC7", "tc7_model_comparison_results.csv"),
    ):
        rows = read_csv(benchmark_dir / filename)
        failures = sum(int(float(row.get("failures") or 0)) for row in rows if row.get("failures") not in (None, ""))
        results.append(
            {
                "extension_id": "SMOKE_029" if test_case_id == "TC6" else "SMOKE_030",
                "test_case_id": test_case_id,
                "status": "PASS" if rows and failures == 0 else "FAIL",
                "source_smoke_sequence": "",
                "run_id": "",
                "scenario_spec_id": "",
                "measurements_json": json.dumps(rows, sort_keys=True),
                "missing_fields_json": "[]",
                "data_source": "LIVE_TRT_API",
                "data_source_detail": "Direct live model endpoint using the trt-api dialogue prompt and structured-output schema; n8n is not used for TC6/TC7.",
                "data_quality_status": "OK" if rows else "DATA_INCOMPLETE",
                "created_at_utc": now_utc(),
            }
        )
    write_csv(output / "smoke_extension_results.csv", results)
    manifest = {
        "status": "COMPLETE" if all(row["status"] == "PASS" for row in results) else "PARTIAL",
        "created_at_utc": now_utc(),
        "core_smoke_denominator": 27,
        "extension_results": results,
        "benchmark_manifest": benchmark_manifest,
        "final_charts_generated": False,
    }
    (output / "smoke_extension_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
