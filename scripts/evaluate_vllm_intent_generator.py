"""Evaluate vLLM extraction for TRT Intent Patch candidate generation."""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator

from trt_core.intent_precheck import deterministic_intent_precheck


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "tests" / "llm_eval" / "operator_intents.jsonl"
REPORTS_DIR = ROOT / "reports"
RESULTS_PATH = REPORTS_DIR / "llm_eval_results.jsonl"
SUMMARY_PATH = REPORTS_DIR / "llm_eval_summary.json"

VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://192.168.50.168:29987/v1").rstrip("/")
VLLM_MODEL = os.getenv("VLLM_MODEL", "cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit")
TRT_API_BASE_URL = os.getenv("TRT_API_BASE_URL", "http://localhost:8000").rstrip("/")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "EMPTY")

CLARIFICATION_ERROR_TYPES = {
    "ambiguous_request",
    "conflicting_intent",
    "multi_line_request",
    "missing_line_number",
    "missing_goal",
    "read_only_state_modification",
}
UNSUPPORTED_ERROR_TYPES = {
    "invalid_line_reference",
    "invalid_instrument_type",
    "read_only_state_modification",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def http_json(method: str, url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc


def build_vllm_structured_output_request(case: dict[str, Any], context: dict[str, Any], model: str) -> dict[str, Any]:
    schema = context["llm_candidate_generation_schema"]
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You extract only task intent fields. Return only JSON. Do not generate patch_id, trt_id, "
                    "base_version, operator_id, intent_text, reason, status, or operations. Use action=PROPOSE_PATCH "
                    "only when the request names exactly one valid line, one clear goal, and only supported instruments. "
                    "Use NEEDS_CLARIFICATION for ambiguous, missing, conflicting, or multi-line requests. "
                    "Use UNSUPPORTED_REQUEST for invalid lines, unsupported instruments, or read-only state changes."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Operator intent: {case['intent_text']}. Reason: {case['reason']}. "
                    "Extract action, line_id, goal, excluded_instruments, clarification_questions, unsupported_terms, "
                    "and detected_request_types. Valid lines are line_1, line_2, line_3, line_4. "
                    "Valid goals are ROUTINE_CLASSIFICATION, TRAUMA_SET_PRIORITY, BACKLOG_CLEARING. "
                    "Valid excluded instruments are SCISSORS, FORCEPS, CLAMPS, RETRACTOR."
                ),
            },
        ],
        "temperature": 0.0,
        "max_tokens": 160,
        "structured_outputs": {"json": schema},
    }


def parse_vllm_content(response: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    choice = response.get("choices", [{}])[0]
    finish_reason = choice.get("finish_reason")
    content = choice.get("message", {}).get("content")
    if finish_reason == "length":
        return None, finish_reason, "finish_reason_length"
    if not content:
        return None, finish_reason, "missing_content"
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        return None, finish_reason, "json_parse_error"
    return parsed, finish_reason, None


def complete_domain_candidate(case: dict[str, Any], context: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    current_trt = context["current_trt"]
    return {
        "patch_id": f"eval-{case['case_id']}",
        "trt_id": current_trt["trt_id"],
        "base_version": current_trt["version"],
        "operator_id": case["operator_id"],
        "intent_text": case["intent_text"],
        "reason": case["reason"],
        "line_id": extracted.get("line_id"),
        "goal": extracted.get("goal"),
        "excluded_instruments": extracted.get("excluded_instruments"),
        "status": "REVIEWED",
    }


def action_rejects_candidate(action: str | None) -> bool:
    return action in {"NEEDS_CLARIFICATION", "UNSUPPORTED_REQUEST"}


def finish_rejected_result(
    result: dict[str, Any],
    case: dict[str, Any],
    started: float,
    *,
    final_reason: str,
    extracted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result["errors"].append(final_reason)
    result["normalize_success"] = False
    result["patch_validate_success"] = False
    result["final_valid"] = False
    result["expected_valid_agrees"] = result["final_valid"] == case["expected_valid"]
    result["line_match"] = (
        extracted.get("line_id") == case.get("expected_line_id")
        if extracted is not None and case.get("expected_line_id") is not None
        else None
    )
    result["goal_match"] = (
        extracted.get("goal") == case.get("expected_goal")
        if extracted is not None and case.get("expected_goal") is not None
        else None
    )
    result["excluded_instrument_match"] = (
        exact_list_match(extracted.get("excluded_instruments"), case.get("expected_excluded_instruments"))
        if extracted is not None
        else None
    )
    result["latency_seconds"] = time.perf_counter() - started
    return result


def validate_against_schema(document: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    return [error.message for error in sorted(validator.iter_errors(document), key=lambda item: item.path)]


def exact_list_match(actual: Any, expected: Any) -> bool | None:
    if expected is None:
        return None
    return sorted(actual or []) == sorted(expected)


def percentile(values: list[float], percentile_rank: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile_rank
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def evaluate_case(case: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {
        "case_id": case["case_id"],
        "expected_valid": case["expected_valid"],
        "expected_error_type": case.get("expected_error_type"),
        "errors": [],
    }

    precheck = deterministic_intent_precheck(case["intent_text"], context["current_trt"])
    result["deterministic_precheck"] = precheck
    request_payload = build_vllm_structured_output_request(case, context, VLLM_MODEL)
    headers = {"Authorization": f"Bearer {VLLM_API_KEY}"} if VLLM_API_KEY and VLLM_API_KEY != "EMPTY" else {}
    response = http_json("POST", f"{VLLM_BASE_URL}/chat/completions", request_payload, headers)

    extracted, finish_reason, parse_error = parse_vllm_content(response)
    result["llm_finish_reason"] = finish_reason
    result["llm_usage"] = response.get("usage", {})
    result["llm_raw_content"] = response.get("choices", [{}])[0].get("message", {}).get("content")
    result["json_parse_success"] = parse_error is None
    result["completion_tokens"] = response.get("usage", {}).get("completion_tokens")
    if parse_error:
        result["errors"].append(parse_error)
        result["schema_valid"] = False
        result["normalize_success"] = False
        result["patch_validate_success"] = False
        result["final_valid"] = False
        result["expected_valid_agrees"] = result["final_valid"] == case["expected_valid"]
        result["line_match"] = None
        result["goal_match"] = None
        result["excluded_instrument_match"] = None
        result["latency_seconds"] = time.perf_counter() - started
        return result

    result["llm_extracted"] = extracted
    schema_errors = validate_against_schema(extracted or {}, context["llm_candidate_generation_schema"])
    result["schema_valid"] = not schema_errors
    result["schema_errors"] = schema_errors
    if schema_errors:
        result["errors"].append("schema_invalid")
        result["rejection_source"] = "schema"
        return finish_rejected_result(result, case, started, final_reason="schema_invalid_rejected", extracted=extracted)

    result["llm_action"] = extracted.get("action")
    result["llm_detected_request_types"] = extracted.get("detected_request_types", [])
    if action_rejects_candidate(extracted.get("action")):
        result["rejection_source"] = "llm_action"
        return finish_rejected_result(result, case, started, final_reason=f"llm_{extracted.get('action', '').lower()}", extracted=extracted)

    if precheck["action"] != "PROPOSE_PATCH":
        result["rejection_source"] = "deterministic_precheck"
        return finish_rejected_result(
            result,
            case,
            started,
            final_reason=f"precheck_{precheck['action'].lower()}",
            extracted=extracted,
        )

    candidate = complete_domain_candidate(case, context, extracted or {})
    result["candidate"] = candidate
    try:
        normalize_response = http_json("POST", f"{TRT_API_BASE_URL}/intent/normalize", candidate)
        result["normalize_success"] = True
        result["intent_patch"] = normalize_response["intent_patch"]
    except RuntimeError as exc:
        result["normalize_success"] = False
        result["patch_validate_success"] = False
        result["final_valid"] = False
        result["expected_valid_agrees"] = result["final_valid"] == case["expected_valid"]
        result["line_match"] = extracted.get("line_id") == case.get("expected_line_id") if case.get("expected_line_id") is not None else None
        result["goal_match"] = extracted.get("goal") == case.get("expected_goal") if case.get("expected_goal") is not None else None
        result["excluded_instrument_match"] = exact_list_match(
            extracted.get("excluded_instruments"), case.get("expected_excluded_instruments")
        )
        result["errors"].append("normalize_failed")
        result["normalize_error"] = str(exc)
        result["latency_seconds"] = time.perf_counter() - started
        return result

    validate_response = http_json("POST", f"{TRT_API_BASE_URL}/patch/validate", result["intent_patch"])
    result["patch_validate_response"] = validate_response
    result["patch_validate_success"] = validate_response.get("status") == "ACCEPTED"
    result["final_valid"] = result["patch_validate_success"]
    result["expected_valid_agrees"] = result["final_valid"] == case["expected_valid"]
    result["line_match"] = extracted.get("line_id") == case.get("expected_line_id") if case.get("expected_line_id") is not None else None
    result["goal_match"] = extracted.get("goal") == case.get("expected_goal") if case.get("expected_goal") is not None else None
    result["excluded_instrument_match"] = exact_list_match(extracted.get("excluded_instruments"), case.get("expected_excluded_instruments"))
    result["latency_seconds"] = time.perf_counter() - started
    return result


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    latencies = [item.get("latency_seconds", 0.0) for item in results]
    completion_tokens = [item["completion_tokens"] for item in results if isinstance(item.get("completion_tokens"), int)]
    finish_reasons = [item.get("llm_finish_reason") for item in results]
    clarification_cases = [item for item in results if item.get("expected_error_type") in CLARIFICATION_ERROR_TYPES]
    unsupported_cases = [item for item in results if item.get("expected_error_type") in UNSUPPORTED_ERROR_TYPES]
    invalid_cases = [item for item in results if item.get("expected_valid") is False]
    valid_cases = [item for item in results if item.get("expected_valid") is True]
    failure_breakdown: dict[str, int] = {}
    for item in results:
        for error in item.get("errors", []):
            failure_breakdown[error] = failure_breakdown.get(error, 0) + 1

    def present_rate(key: str) -> float:
        values = [item[key] for item in results if item.get(key) is not None]
        return rate(sum(1 for value in values if value), len(values))

    return {
        "total_cases": total,
        "json_parse_rate": present_rate("json_parse_success"),
        "finish_reason_stop_rate": rate(sum(1 for reason in finish_reasons if reason == "stop"), total),
        "finish_reason_length_rate": rate(sum(1 for reason in finish_reasons if reason == "length"), total),
        "schema_valid_rate": present_rate("schema_valid"),
        "normalize_success_rate": present_rate("normalize_success"),
        "patch_validate_success_rate": present_rate("patch_validate_success"),
        "expected_valid_agreement": present_rate("expected_valid_agrees"),
        "exact_line_match_rate": present_rate("line_match"),
        "exact_goal_match_rate": present_rate("goal_match"),
        "excluded_instrument_match_rate": present_rate("excluded_instrument_match"),
        "clarification_detection_rate": rate(
            sum(1 for item in clarification_cases if item.get("rejection_source") in {"llm_action", "deterministic_precheck"}),
            len(clarification_cases),
        ),
        "unsupported_detection_rate": rate(
            sum(1 for item in unsupported_cases if item.get("rejection_source") in {"llm_action", "deterministic_precheck"}),
            len(unsupported_cases),
        ),
        "false_accept_rate": rate(sum(1 for item in invalid_cases if item.get("final_valid", False)), len(invalid_cases)),
        "invalid_rejection_rate": rate(sum(1 for item in invalid_cases if not item.get("final_valid", False)), len(invalid_cases)),
        "valid_accept_rate": rate(sum(1 for item in valid_cases if item.get("final_valid", False)), len(valid_cases)),
        "average_latency_seconds": statistics.fmean(latencies) if latencies else 0.0,
        "p95_latency_seconds": percentile(latencies, 0.95),
        "average_completion_tokens": statistics.fmean(completion_tokens) if completion_tokens else 0.0,
        "failure_breakdown": failure_breakdown,
    }


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    cases = read_jsonl(DATASET_PATH)
    context = http_json("GET", f"{TRT_API_BASE_URL}/intent/context")
    results = [evaluate_case(case, context) for case in cases]
    RESULTS_PATH.write_text("\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in results) + "\n", encoding="utf-8")
    SUMMARY_PATH.write_text(json.dumps(summarize_results(results), indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {RESULTS_PATH}")
    print(f"Wrote {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
