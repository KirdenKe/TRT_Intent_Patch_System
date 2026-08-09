"""Repeated structured-generation benchmark for the supported local LLM endpoints."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import statistics
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from trt_core.api import _build_dialogue_decision_prompt, _dialogue_decision_schema


MODELS = [
    {
        "model": "cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit",
        "endpoint": "http://192.168.50.168:29987/v1/chat/completions",
    },
    {
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "endpoint": "http://192.168.50.168:29022/v1/chat/completions",
    },
    {
        "model": "meta-llama/Llama-3.1-8B-Instruct",
        "endpoint": "http://192.168.50.168:26337/v1/chat/completions",
    },
]
PROMPT_VERSION = "tc7-cross-model-dialogue-decision-v1"
PROHIBITED_SAMPLING_FIELDS = {
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object.")
        rows.append(value)
    return rows


def post_json(url: str, body: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    prohibited = sorted(PROHIBITED_SAMPLING_FIELDS.intersection(body))
    if prohibited:
        raise ValueError(f"Client request contains prohibited sampling fields: {prohibited}")
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def expected_turn_type(row: dict[str, Any]) -> str:
    if row.get("expected_turn_type"):
        return str(row["expected_turn_type"])
    return "TASK_REQUEST"


def expected_semantic_fields(row: dict[str, Any]) -> dict[str, Any]:
    expected: dict[str, Any] = {}
    mappings = {
        "expected_request_types": "request_types",
        "expected_target_lines": "target_lines",
        "expected_target_scope": "target_scope",
        "expected_kpi_updates": "kpi_updates",
        "expected_simulation_config_updates": "simulation_config_updates",
        "expected_tooling_policy": "tooling_policy",
        "expected_manipulator_priority": "manipulator_priority",
    }
    for source, target in mappings.items():
        if source in row:
            expected[target] = row[source]
    return expected


def _subset_score(expected: Any, actual: Any) -> tuple[int, int]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return 0, max(1, len(expected))
        matched = total = 0
        for key, value in expected.items():
            item_matched, item_total = _subset_score(value, actual.get(key))
            matched += item_matched
            total += item_total
        return matched, total
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return 0, max(1, len(expected))
        expected_normalized = {json.dumps(value, sort_keys=True) for value in expected}
        actual_normalized = {json.dumps(value, sort_keys=True) for value in actual}
        return len(expected_normalized & actual_normalized), max(1, len(expected_normalized))
    return (1 if expected == actual else 0), 1


def semantic_accuracy(row: dict[str, Any], decision: dict[str, Any]) -> float | None:
    expected = expected_semantic_fields(row)
    if not expected:
        return None
    actual = decision.get("normalized_request") or {}
    matched, total = _subset_score(expected, actual)
    return matched / total if total else None


def required_field_completeness(decision: dict[str, Any]) -> float:
    required = _dialogue_decision_schema()["required"]
    return sum(field in decision and decision[field] is not None for field in required) / len(required)


def run_benchmark(
    *,
    rows: list[dict[str, Any]],
    repetitions: int,
    output: Path,
    timeout_seconds: float,
    hardware_description: str,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / "llm_generation_repetitions.jsonl"
    request_path = output / "llm_generation_requests.jsonl"
    records: list[dict[str, Any]] = []
    request_hashes: set[str] = set()
    prompt_hashes: set[str] = set()
    schema_hashes: set[str] = set()
    with (
        raw_path.open("w", encoding="utf-8") as raw_handle,
        request_path.open("w", encoding="utf-8") as request_handle,
    ):
        for model in MODELS:
            for fixture in rows:
                text = str(fixture.get("operator_text") or "")
                details = []
                if fixture.get("operator_id"):
                    details.append(f"operator_id: {fixture['operator_id']}")
                if fixture.get("reason"):
                    details.append(f"reason: {fixture['reason']}")
                latest = " ".join([text, *details]).strip()
                messages, _ = _build_dialogue_decision_prompt(
                    {"latest_user_message": latest, "session_id": f"benchmark-{fixture['id']}"},
                    {},
                )
                body = {
                    "model": model["model"],
                    "messages": messages,
                    "max_tokens": 4000,
                    "structured_outputs": {"json": _dialogue_decision_schema()},
                }
                canonical_prompt = json.dumps(messages, sort_keys=True, separators=(",", ":"))
                canonical_schema = json.dumps(
                    body["structured_outputs"]["json"],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                canonical_request = json.dumps(body, sort_keys=True, separators=(",", ":"))
                prompt_sha256 = hashlib.sha256(canonical_prompt.encode("utf-8")).hexdigest()
                schema_sha256 = hashlib.sha256(canonical_schema.encode("utf-8")).hexdigest()
                request_sha256 = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
                prompt_hashes.add(prompt_sha256)
                schema_hashes.add(schema_sha256)
                request_hashes.add(request_sha256)
                request_handle.write(
                    json.dumps(
                        {
                            "test_case_id": "TC7",
                            "fixture_id": fixture["id"],
                            "model": model["model"],
                            "endpoint": model["endpoint"],
                            "prompt_version": PROMPT_VERSION,
                            "prompt_sha256": prompt_sha256,
                            "schema_sha256": schema_sha256,
                            "request_sha256": request_sha256,
                            "request_body": body,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                for repetition in range(1, repetitions + 1):
                    started_at = now_utc()
                    started = time.perf_counter()
                    decision: dict[str, Any] | None = None
                    response: dict[str, Any] | None = None
                    error: str | None = None
                    try:
                        response = post_json(model["endpoint"], body, timeout_seconds)
                        content = response.get("choices", [{}])[0].get("message", {}).get("content", response)
                        decision = json.loads(content) if isinstance(content, str) else content
                        if not isinstance(decision, dict):
                            raise ValueError("Structured output is not an object.")
                        validate(instance=decision, schema=body["structured_outputs"]["json"])
                    except (
                        OSError,
                        TimeoutError,
                        urllib.error.URLError,
                        ValidationError,
                        ValueError,
                        KeyError,
                        TypeError,
                    ) as exc:
                        error = str(exc)
                    latency = time.perf_counter() - started
                    canonical = json.dumps(decision, sort_keys=True, separators=(",", ":")) if decision else None
                    record = {
                        "test_case_ids": ["TC6", "TC7"] if model == MODELS[0] else ["TC7"],
                        "fixture_id": fixture["id"],
                        "model": model["model"],
                        "model_version": model["model"],
                        "endpoint": model["endpoint"],
                        "repetition": repetition,
                        "prompt_version": PROMPT_VERSION,
                        "prompt_sha256": prompt_sha256,
                        "schema_sha256": schema_sha256,
                        "request_sha256": request_sha256,
                        "temperature": None,
                        "top_p": None,
                        "seed": None,
                        "sampling_configuration": "SERVER_PRESET_NOT_OVERRIDDEN",
                        "sampling_parameters_sent": [],
                        "reasoning_feature_requested": False,
                        "reasoning_configuration": "SERVER_DEFAULT_OR_NOT_EXPOSED",
                        "hardware_environment": hardware_description,
                        "gpu_memory_requirement_gb": None,
                        "gpu_memory_data_quality": "DATA_MISSING",
                        "started_at_utc": started_at,
                        "completed_at_utc": now_utc(),
                        "latency_seconds": latency,
                        "json_format_accurate": decision is not None,
                        "required_field_completeness": required_field_completeness(decision) if decision else 0.0,
                        "expected_turn_type": expected_turn_type(fixture),
                        "actual_turn_type": decision.get("turn_type") if decision else None,
                        "intent_classification_correct": (
                            decision.get("turn_type") == expected_turn_type(fixture)
                            if decision else False
                        ),
                        "semantic_accuracy": semantic_accuracy(fixture, decision) if decision else 0.0,
                        "normalized_output_hash": (
                            hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                            if canonical else None
                        ),
                        "input_tokens": (response or {}).get("usage", {}).get("prompt_tokens"),
                        "output_tokens": (response or {}).get("usage", {}).get("completion_tokens"),
                        "total_tokens": (response or {}).get("usage", {}).get("total_tokens"),
                        "failure_type": "NONE" if not error else "MODEL_REQUEST_OR_SCHEMA_ERROR",
                        "error": error,
                        "decision": decision,
                    }
                    records.append(record)
                    raw_handle.write(json.dumps(record, sort_keys=True) + "\n")

    summaries: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["model"], record["fixture_id"])].append(record)
    for (model, fixture_id), group in grouped.items():
        classifications = [row["actual_turn_type"] for row in group if row["actual_turn_type"]]
        hashes = [row["normalized_output_hash"] for row in group if row["normalized_output_hash"]]
        summary = {
            "model": model,
            "fixture_id": fixture_id,
            "repetitions": len(group),
            "json_format_accuracy": sum(row["json_format_accurate"] for row in group) / len(group),
            "required_field_completeness_rate": statistics.fmean(row["required_field_completeness"] for row in group),
            "intent_classification_accuracy": sum(row["intent_classification_correct"] for row in group) / len(group),
            "intent_classification_consistency": (
                Counter(classifications).most_common(1)[0][1] / len(classifications)
                if classifications else None
            ),
            "field_content_consistency": (
                Counter(hashes).most_common(1)[0][1] / len(hashes)
                if hashes else None
            ),
            "semantic_accuracy": _mean_present(
                row["semantic_accuracy"] for row in group
            ),
            "unique_output_variants": len(set(hashes)),
            "average_generation_seconds": statistics.fmean(row["latency_seconds"] for row in group),
            "maximum_generation_seconds": max(row["latency_seconds"] for row in group),
            "average_input_tokens": _mean_present(row["input_tokens"] for row in group),
            "average_output_tokens": _mean_present(row["output_tokens"] for row in group),
            "failures": sum(row["failure_type"] != "NONE" for row in group),
        }
        summaries.append(summary)

    csv_path = output / "llm_generation_stability.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]) if summaries else ["model"])
        writer.writeheader()
        writer.writerows(summaries)
    tc6_rows = [row for row in summaries if row["model"] == MODELS[0]["model"]]
    tc7_by_model: list[dict[str, Any]] = []
    for model in MODELS:
        model_rows = [row for row in summaries if row["model"] == model["model"]]
        tc7_by_model.append(
            {
                "test_case_id": "TC7",
                "model": model["model"],
                "fixtures": len(model_rows),
                "repetitions_per_fixture": repetitions,
                "json_format_accuracy": _mean_present(row["json_format_accuracy"] for row in model_rows),
                "required_field_completeness_rate": _mean_present(
                    row["required_field_completeness_rate"] for row in model_rows
                ),
                "intent_classification_consistency": _mean_present(
                    row["intent_classification_consistency"] for row in model_rows
                ),
                "field_content_consistency": _mean_present(
                    row["field_content_consistency"] for row in model_rows
                ),
                "semantic_accuracy": _mean_present(row["semantic_accuracy"] for row in model_rows),
                "average_generation_seconds": _mean_present(
                    row["average_generation_seconds"] for row in model_rows
                ),
                "maximum_generation_seconds": (
                    max(row["maximum_generation_seconds"] for row in model_rows)
                    if model_rows else None
                ),
                "average_input_tokens": _mean_present(row["average_input_tokens"] for row in model_rows),
                "average_output_tokens": _mean_present(row["average_output_tokens"] for row in model_rows),
                "manual_semantic_review_status": "PENDING_MANUAL_REVIEW",
                "data_quality_status": "OK" if model_rows else "DATA_MISSING",
            }
        )
    _write_csv(output / "tc6_generation_stability_results.csv", [
        {"test_case_id": "TC6", **row} for row in tc6_rows
    ])
    _write_csv(output / "tc7_model_comparison_results.csv", tc7_by_model)
    manifest = {
        "created_at_utc": now_utc(),
        "prompt_version": PROMPT_VERSION,
        "benchmark_scope": "DIRECT_CROSS_MODEL_STRUCTURED_GENERATION",
        "n8n_used": False,
        "trt_api_http_used": False,
        "isaac_sim_used": False,
        "deployment_attempted": False,
        "models": MODELS,
        "fixture_count": len(rows),
        "repetitions_per_fixture": repetitions,
        "request_sampling_policy": "SERVER_PRESET_NOT_OVERRIDDEN",
        "hardware_environment": hardware_description,
        "raw_results": str(raw_path),
        "request_snapshots": str(request_path),
        "prompt_sha256_values": sorted(prompt_hashes),
        "schema_sha256_values": sorted(schema_hashes),
        "request_sha256_values": sorted(request_hashes),
        "summary_csv": str(csv_path),
        "tc6_results": str(output / "tc6_generation_stability_results.csv"),
        "tc7_results": str(output / "tc7_model_comparison_results.csv"),
        "data_quality_warnings": [
            "Temperature, top-p, and seed are null because the client does not override model-server presets.",
            "GPU memory requirement remains null unless measured independently on the model servers.",
            "Automated semantic scores require a separate manual semantic review before final interpretation.",
        ],
    }
    (output / "llm_generation_benchmark_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def _mean_present(values: Any) -> float | None:
    present = [float(value) for value in values if isinstance(value, (int, float))]
    return statistics.fmean(present) if present else None


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["test_case_id", "data_quality_status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("outputs/reports/m12/seed_data/operator_intent_gold.jsonl"),
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--output", type=Path, default=Path("outputs/reports/llm_comparison"))
    parser.add_argument(
        "--hardware-description",
        default=f"client={platform.platform()}; model_server_hardware=NOT_REPORTED",
    )
    args = parser.parse_args()
    if args.repetitions < 2:
        raise SystemExit("--repetitions must be at least 2.")
    rows = load_jsonl(args.input)
    if args.limit:
        rows = rows[: args.limit]
    manifest = run_benchmark(
        rows=rows,
        repetitions=args.repetitions,
        output=args.output,
        timeout_seconds=args.timeout_seconds,
        hardware_description=args.hardware_description,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
