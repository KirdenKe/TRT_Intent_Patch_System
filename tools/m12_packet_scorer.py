from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def parse_jsonish(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def combined_response_text(combined: dict[str, Any]) -> str:
    parts: list[str] = []
    for turn in combined.get("turns") or []:
        for key in ("message", "text"):
            value = turn.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    return "\n\n".join(parts)


def load_scenario(project_root: Path, scenario_spec_id: str) -> tuple[dict[str, Any] | None, str]:
    if not scenario_spec_id:
        return None, "scenario_spec_id missing"
    path = project_root / "outputs" / "scenario_specs" / f"{scenario_spec_id}.json"
    if not path.exists():
        return None, f"ScenarioSpec file missing: {path}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except json.JSONDecodeError as exc:
        return None, f"ScenarioSpec JSON invalid: {exc}"


def load_release_for_spec(project_root: Path, spec: dict[str, Any]) -> dict[str, Any] | None:
    release_id = spec.get("release_id")
    if not release_id:
        return None
    path = project_root / "data" / "releases" / f"{release_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def scenario_line_policies(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(policy.get("line_id")): policy for policy in spec.get("line_policies") or [] if isinstance(policy, dict)}


def patch_affected_lines(spec: dict[str, Any]) -> list[str]:
    policies = scenario_line_policies(spec)
    affected = [line_id for line_id, policy in policies.items() if policy.get("patch_affected") is True]
    return sorted(affected)


def policy_changed_lines(project_root: Path, spec: dict[str, Any]) -> list[str]:
    """Return lines whose TRT policy/table content actually changed.

    ScenarioSpec `affected_lines` and line policy `patch_affected` can include
    simulation-scope lines. For M12 intent correctness, we need the release
    candidate summary/operation paths, because limited/full simulation is not
    itself a policy error.
    """

    release = load_release_for_spec(project_root, spec) or {}
    summary_lines = ((release.get("candidate_summary") or {}).get("affected_lines") or [])
    if summary_lines:
        return sorted(str(line_id) for line_id in summary_lines)
    operation_lines: set[str] = set()
    patch = release.get("candidate_patch") or {}
    for operation in patch.get("operations") or []:
        path = str(operation.get("path") or "")
        match = re.search(r"/lines/(line_\d+)/", path)
        if match:
            operation_lines.add(match.group(1))
    if operation_lines:
        return sorted(operation_lines)
    return patch_affected_lines(spec)


def compiled_simulation_config(spec: dict[str, Any]) -> dict[str, Any]:
    return (
        spec.get("governance_metadata", {})
        .get("simulation_config_compilation_trace", {})
        .get("compiled_simulation_config", {})
    )


def parse_expected_command_args(value: str) -> dict[str, Any]:
    if not value:
        return {}
    result: dict[str, Any] = {}
    tokens = value.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            index += 1
            continue
        key = token[2:].replace("-", "_")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            result[key] = True
            index += 1
            continue
        raw = tokens[index + 1]
        if raw.lower() in {"true", "false"}:
            parsed: Any = raw.lower() == "true"
        else:
            try:
                parsed = int(raw)
            except ValueError:
                try:
                    parsed = float(raw)
                except ValueError:
                    parsed = raw
        result[key] = parsed
        index += 2
    return result


def values_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            return abs(float(expected) - float(actual)) < 1e-6
        except (TypeError, ValueError):
            return False
    return expected == actual


def check_kpi(spec: dict[str, Any], expected: dict[str, Any], expected_lines: list[str], target_scope: str | None) -> tuple[bool, str]:
    if not expected:
        return True, ""
    policies = scenario_line_policies(spec)
    lines = expected_lines
    if target_scope == "ALL_LINES" or (not lines and expected):
        lines = sorted(policies)
    missing: list[str] = []
    for line_id in lines:
        policy = policies.get(line_id)
        if not policy:
            missing.append(f"{line_id}: missing line policy")
            continue
        kpi = policy.get("kpi") or {}
        for key, expected_value in expected.items():
            if not values_equal(expected_value, kpi.get(key)):
                missing.append(f"{line_id}.{key}: expected {expected_value}, actual {kpi.get(key)}")
    return not missing, "; ".join(missing)


def check_sim_config(spec: dict[str, Any], expected: dict[str, Any]) -> tuple[bool, str]:
    if not expected:
        return True, ""
    config = compiled_simulation_config(spec)
    mismatches = [
        f"{key}: expected {expected_value}, actual {config.get(key)}"
        for key, expected_value in expected.items()
        if not values_equal(expected_value, config.get(key))
    ]
    return not mismatches, "; ".join(mismatches)


def check_launch_args(spec: dict[str, Any], expected_args_text: str) -> tuple[bool, str]:
    expected_args = parse_expected_command_args(expected_args_text)
    if not expected_args:
        return True, ""
    config = compiled_simulation_config(spec)
    aliases = {
        "reuse_precomputed_layouts": "reuse_verified_seed",
    }
    checked_keys = {
        "num_envs",
        "add_reference_number",
        "headless",
        "layout_source",
        "episode_success_requires_reset_cycles",
        "allowed_overlap_ratio",
        "chosen_intervention_mode",
        "travel_time",
        "fix_duration",
        "resume_delay",
    }
    mismatches: list[str] = []
    for key, expected_value in expected_args.items():
        if key not in checked_keys and key not in aliases:
            continue
        actual_key = aliases.get(key, key)
        if not values_equal(expected_value, config.get(actual_key)):
            mismatches.append(f"{key}: expected {expected_value}, actual {config.get(actual_key)}")
    return not mismatches, "; ".join(mismatches)


def check_target_lines(project_root: Path, spec: dict[str, Any], expected_lines: list[str], target_scope: str | None) -> tuple[bool, str]:
    if target_scope == "ALL_LINES":
        return True, ""
    if not expected_lines:
        return True, ""
    actual = policy_changed_lines(project_root, spec)
    expected = sorted(expected_lines)
    if actual == expected:
        return True, ""
    simulated = sorted((spec.get("simulation_scope") or {}).get("lines") or [])
    return False, f"expected changed policy lines {expected}, actual {actual}; simulated lines {simulated}"


def check_tooling_policy(spec: dict[str, Any], expected_policy: dict[str, Any], expected_lines: list[str]) -> tuple[bool, str]:
    if not expected_policy:
        return True, ""
    selected = expected_policy.get("selected_normalized_types") or []
    policies = scenario_line_policies(spec)
    lines = expected_lines or patch_affected_lines(spec)
    mismatches: list[str] = []
    for line_id in lines:
        actual = (policies.get(line_id) or {}).get("selected_normalized_types") or []
        for value in selected:
            if value not in actual:
                mismatches.append(f"{line_id}: expected selected_normalized_types contains {value}, actual {actual}")
    return not mismatches, "; ".join(mismatches)


def check_manipulator_priority(spec: dict[str, Any], expected_priority: dict[str, Any], expected_lines: list[str]) -> tuple[bool, str]:
    if not expected_priority:
        return True, ""
    excluded = expected_priority.get("excluded_normalized_types") or []
    policies = scenario_line_policies(spec)
    lines = expected_lines or patch_affected_lines(spec)
    mismatches: list[str] = []
    for line_id in lines:
        priority = (policies.get(line_id) or {}).get("manipulator_priority") or {}
        haystack = []
        for key in ("excluded_normalized_types", "ordered_normalized_types", "excluded_tool_types"):
            value = priority.get(key)
            if isinstance(value, list):
                haystack.extend(value)
        for value in excluded:
            if value not in haystack:
                mismatches.append(f"{line_id}: expected priority references {value}, actual {priority}")
    return not mismatches, "; ".join(mismatches)


def node_names_from_snapshots(combined: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for snapshot in combined.get("n8n_execution_snapshots") or []:
        body = snapshot.get("body") if isinstance(snapshot, dict) else None
        run_data = (((body or {}).get("data") or {}).get("resultData") or {}).get("runData") or {}
        for name in run_data:
            if name not in names:
                names.append(name)
    return names


def actual_tools_from_combined(combined: dict[str, Any]) -> list[str]:
    tools: list[str] = []
    for item in walk(combined):
        if isinstance(item, dict):
            raw = item.get("m12_tool_trace") or item.get("tool_trace") or item.get("tools_called")
            if isinstance(raw, list):
                for tool in raw:
                    if isinstance(tool, str) and tool not in tools:
                        tools.append(tool)
    node_map = {
        "Execute Config Query": ["load_current_trt", "extract_kpi_targets"],
        "Generate ScenarioSpec": ["load_scenario_spec"],
        "ScenarioSpec to Isaac Simulation Run": ["load_run_artifact"],
    }
    for node in node_names_from_snapshots(combined):
        for mapped in node_map.get(node, []):
            if mapped not in tools:
                tools.append(mapped)
    return tools


def score_tc1_or_tc3(combined: dict[str, Any], project_root: Path) -> dict[str, Any]:
    row = combined.get("row") or {}
    expected = parse_jsonish(row.get("expected_fields_json"), {})
    expected_status = row.get("expected_status") or expected.get("expected_status") or "REVIEWED"
    text = combined_response_text(combined).lower()
    scenario_spec_id = combined.get("scenario_spec_id") or ""
    if row.get("should_launch_isaac") == "false" and row.get("expected_validation_issue"):
        return {
            "status": "SKIPPED_BY_TEST_PLAN",
            "failure_stage": "test_parameter_plan",
            "failure_cause": row.get("expected_validation_issue"),
            "data_quality_status": "NOT_RUN_TEST_PLAN_BLOCK",
            "checks": {"test_plan_blocked_invalid_or_slow_launch": True},
        }
    if expected_status == "ANSWER_READY":
        answer_tokens = ["current kpi", "task requirement", "line_1", "line_2", "throughput", "trt"]
        passed = any(token in text for token in answer_tokens) and "candidate patch passed validation" not in text
        return {
            "status": "PASS" if passed else "FAIL",
            "failure_stage": "config_query",
            "failure_cause": "Config/query answer returned." if passed else "Expected config/query answer, but response did not look like an answer.",
            "data_quality_status": "OK" if passed else "VALIDATION_FAILED",
            "checks": {"answer_ready_match": passed},
        }
    if expected_status == "HELP":
        passed = "i can help" in text or "help with" in text
        return {
            "status": "PASS" if passed else "FAIL",
            "failure_stage": "dialogue",
            "failure_cause": "Help response returned." if passed else "Expected help response, but transcript did not expose it.",
            "data_quality_status": "OK" if passed else "VALIDATION_FAILED",
            "checks": {"help_match": passed},
        }
    if expected_status == "CANCELLED":
        passed = "cancelled" in text and "no release" in text
        return {
            "status": "PASS" if passed else "FAIL",
            "failure_stage": "dialogue",
            "failure_cause": "Cancel response returned." if passed else "Expected cancel response, but transcript did not expose it.",
            "data_quality_status": "OK" if passed else "VALIDATION_FAILED",
            "checks": {"cancel_match": passed},
        }
    if expected_status != "REVIEWED":
        if "candidate patch passed validation" in text:
            return {
                "status": "FAIL_ERROR_NOT_INTERCEPTED",
                "failure_stage": "intent_validation",
                "failure_cause": f"Expected {expected_status}, but row reached candidate approval.",
                "data_quality_status": "OK",
                "checks": {"expected_status_match": False},
            }
        if any(token in text for token in ["requires revision", "please clarify", "cannot", "missing", "still need", "need the operator id", "need operator id"]):
            return {
                "status": "PASS",
                "failure_stage": "intent_validation",
                "failure_cause": f"Expected {expected_status}; workflow stopped before approval.",
                "data_quality_status": "OK",
                "checks": {"expected_status_match": True},
            }
        return {
            "status": "INCONCLUSIVE",
            "failure_stage": "intent_validation",
            "failure_cause": f"Expected {expected_status}, but transcript did not expose a clear rejection or clarification.",
            "data_quality_status": "DATA_INCOMPLETE",
            "checks": {"expected_status_match": None},
        }

    spec, spec_error = load_scenario(project_root, scenario_spec_id)
    if spec is None:
        return {
            "status": "INCONCLUSIVE",
            "failure_stage": "scenario_spec",
            "failure_cause": spec_error,
            "data_quality_status": "DATA_INCOMPLETE",
            "checks": {"scenario_spec_schema_pass": False},
        }

    expected_lines = expected.get("expected_target_lines") or []
    target_scope = expected.get("expected_target_scope")
    checks: dict[str, Any] = {"scenario_spec_schema_pass": True}
    reasons: list[str] = []
    for name, result in {
        "target_line_match": check_target_lines(project_root, spec, expected_lines, target_scope),
        "kpi_update_match": check_kpi(spec, expected.get("expected_kpi_updates") or {}, expected_lines, target_scope),
        "simulation_config_match": check_sim_config(spec, expected.get("expected_simulation_config_updates") or {}),
        "launch_parameter_match": check_launch_args(spec, row.get("expected_command_args", "")),
        "tooling_policy_match": check_tooling_policy(spec, expected.get("expected_tooling_policy") or {}, expected_lines),
        "manipulator_priority_match": check_manipulator_priority(spec, expected.get("expected_manipulator_priority") or {}, expected_lines),
    }.items():
        passed, reason = result
        checks[name] = passed
        if reason:
            reasons.append(f"{name}: {reason}")
    constraints = expected.get("expected_constraints") or []
    if "placement_verification_required" in constraints:
        checks["placement_verification_required"] = True
    if "R_reset_not_null" in constraints or "R_reset_equals_1_if_success" in constraints:
        checks["reset_constraint_requires_metric_collection"] = True

    passed = all(value is True for value in checks.values() if isinstance(value, bool))
    return {
        "status": "PASS" if passed else "FAIL",
        "failure_stage": "completed" if passed else "expected_field_validation",
        "failure_cause": "Packet expected fields matched." if passed else "; ".join(reasons),
        "data_quality_status": "OK" if passed else "VALIDATION_FAILED",
        "checks": checks,
    }


def precision_recall_f1(expected: list[str], actual: list[str]) -> tuple[float | None, float | None, float | None]:
    expected_set = set(expected)
    actual_set = set(actual)
    tp = len(expected_set & actual_set)
    fp = len(actual_set - expected_set)
    fn = len(expected_set - actual_set)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    return precision, recall, f1


def score_tc2(combined: dict[str, Any]) -> dict[str, Any]:
    row = combined.get("row") or {}
    expected_tools = parse_jsonish(row.get("required_tools"), [])
    expected_order = parse_jsonish(row.get("required_order"), [])
    required_arguments = parse_jsonish(row.get("required_arguments"), {})
    actual_tools = actual_tools_from_combined(combined)
    text = combined_response_text(combined).lower()
    if not actual_tools:
        if any(token in text for token in ["cannot perform calculations", "before i can submit this for review", "candidate patch passed validation"]):
            return {
                "status": "FAIL",
                "failure_stage": "tool_orchestration",
                "failure_cause": "Query was routed as a task-change dialogue or unsupported calculation instead of the required tool/evidence path.",
                "data_quality_status": "TRACE_MISSING",
                "checks": {
                    "expected_tools": expected_tools,
                    "actual_tools": actual_tools,
                    "tool_selection_correct": False,
                    "dependency_order_correct": False,
                    "required_arguments": required_arguments,
                    "actual_arguments": {},
                    "argument_match_score": 0.0,
                    "precision": 0.0,
                    "recall": 0.0,
                    "f1": 0.0,
                },
            }
        return {
            "status": "INCONCLUSIVE",
            "failure_stage": "tool_orchestration",
            "failure_cause": "No structured tool trace was captured, so required tools/order/arguments cannot be verified.",
            "data_quality_status": "DATA_MISSING",
            "checks": {
                "expected_tools": expected_tools,
                "actual_tools": actual_tools,
                "tool_selection_correct": None,
                "dependency_order_correct": None,
                "required_arguments": required_arguments,
                "actual_arguments": {},
                "argument_match_score": None,
                "precision": None,
                "recall": None,
                "f1": None,
            },
        }
    precision, recall, f1 = precision_recall_f1(expected_tools, actual_tools)
    tool_selection_correct = set(expected_tools) == set(actual_tools)
    order_positions = [actual_tools.index(tool) for tool in expected_order if tool in actual_tools]
    dependency_order_correct = len(order_positions) == len(expected_order) and order_positions == sorted(order_positions)
    passed = tool_selection_correct and dependency_order_correct
    return {
        "status": "PASS" if passed else "FAIL",
        "failure_stage": "query_response" if passed else "tool_orchestration",
        "failure_cause": "Required tools and dependency order matched." if passed else "Required tools/order did not match captured trace.",
        "data_quality_status": "OK" if passed else "VALIDATION_FAILED",
        "checks": {
            "expected_tools": expected_tools,
            "actual_tools": actual_tools,
            "tool_selection_correct": tool_selection_correct,
            "dependency_order_correct": dependency_order_correct,
            "required_arguments": required_arguments,
            "actual_arguments": {},
            "argument_match_score": None,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        },
    }


def score_tc4(combined: dict[str, Any]) -> dict[str, Any]:
    row = combined.get("row") or {}
    text = combined_response_text(combined).lower()
    expected_blocked = str(row.get("expected_deployment_blocked", "")).lower() == "true"
    feasibility = row.get("manual_feasibility", "")
    if feasibility == "REQUIRES_BACKEND_INJECTION":
        backend_result = combined.get("backend_injection_result")
        if not backend_result:
            return {
                "status": "INCONCLUSIVE",
                "failure_stage": "backend_injection",
                "failure_cause": "Packet row requires backend/state injection, but no backend injection result was recorded.",
                "data_quality_status": "DATA_MISSING",
                "checks": {
                    "expected_interceptor": row.get("expected_interceptor", ""),
                    "actual_interceptor": None,
                    "expected_deployment_blocked": expected_blocked,
                    "actual_deployment_blocked": None,
                    "was_intercepted": None,
                },
            }
        actual_blocked = bool(backend_result.get("actual_deployment_blocked"))
        actual_interceptor = backend_result.get("actual_interceptor")
        passed = actual_blocked == expected_blocked and actual_interceptor == row.get("expected_interceptor")
        return {
            "status": "PASS" if passed else "FAIL",
            "failure_stage": "backend_injection",
            "failure_cause": "Backend injection matched expected interceptor." if passed else "Backend injection result did not match expected interceptor/block state.",
            "data_quality_status": "OK" if passed else "VALIDATION_FAILED",
            "checks": {
                "expected_interceptor": row.get("expected_interceptor", ""),
                "actual_interceptor": actual_interceptor,
                "expected_deployment_blocked": expected_blocked,
                "actual_deployment_blocked": actual_blocked,
                "was_intercepted": bool(backend_result.get("was_intercepted")),
            },
        }

    reached_approval = "candidate patch passed validation" in text
    deployed = "deployment completed" in text or "deployment success" in text or "deployed" in text and "do_not_deploy" not in text
    rejected_or_clarified = any(token in text for token in ["requires revision", "please clarify", "cannot", "missing", "still need", "need the operator id", "need operator id", "not allowed", "blocked"])
    if expected_blocked and deployed:
        status = "FAIL_ERROR_NOT_INTERCEPTED"
        cause = "Deployment-like success text appeared for a row expected to be blocked."
        stage = "deployment"
        passed = False
    elif expected_blocked and reached_approval and not rejected_or_clarified:
        status = "FAIL_ERROR_NOT_INTERCEPTED"
        cause = "Row reached candidate approval without an observed rejection, clarification, or block."
        stage = "candidate_approval"
        passed = False
    elif expected_blocked and rejected_or_clarified:
        status = "PASS"
        cause = "Workflow stopped before deployment with rejection, clarification, missing-field prompt, or block."
        stage = "intent_validation"
        passed = True
    elif not expected_blocked and not deployed:
        status = "PASS"
        cause = "Non-deployment-blocking reporting error did not reach deployment."
        stage = "report_generation"
        passed = True
    else:
        status = "INCONCLUSIVE"
        cause = "Transcript did not expose a definitive interception or deployment-block result."
        stage = "unknown"
        passed = False
    return {
        "status": status,
        "failure_stage": stage,
        "failure_cause": cause,
        "data_quality_status": "OK" if passed else "VALIDATION_FAILED",
        "checks": {
            "expected_interceptor": row.get("expected_interceptor", ""),
            "actual_interceptor": "chat_or_workflow_guardrail" if rejected_or_clarified else None,
            "expected_deployment_blocked": expected_blocked,
            "actual_deployment_blocked": not deployed if expected_blocked else None,
            "was_intercepted": passed,
        },
    }


def score_combined(combined: dict[str, Any], project_root: Path) -> dict[str, Any]:
    suite = (combined.get("row") or {}).get("suite") or ""
    if suite == "TC1":
        score = score_tc1_or_tc3(combined, project_root)
    elif suite == "TC2":
        score = score_tc2(combined)
    elif suite == "TC3":
        score = score_tc1_or_tc3(combined, project_root)
    elif suite == "TC4":
        score = score_tc4(combined)
    else:
        score = {
            "status": "INCONCLUSIVE",
            "failure_stage": "unknown",
            "failure_cause": f"Unknown suite: {suite}",
            "data_quality_status": "DATA_INCOMPLETE",
            "checks": {},
        }
    score["scoring_method"] = "M12_PACKET_EXPECTATION_SCORER"
    return score
