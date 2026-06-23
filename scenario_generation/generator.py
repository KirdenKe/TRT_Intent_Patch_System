"""Generate Isaac-adapter-compatible ScenarioSpec JSON documents."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
import json
from pathlib import Path

from scenario_generation.errors import (
    OperatorResolutionRequiredError,
    ScenarioGenerationError,
    ScenarioTemplateLineBindingError,
)
from scenario_generation.models import (
    ScenarioGenerationRequest,
    ScenarioSpec,
    WaitingForCheckpointResult,
    new_scenario_spec_id,
    now_utc,
)
from scenario_generation.template_registry import get_template


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "scenario_spec.schema.json"
ISAAC_SUPPORTED_STRATEGIES = {"STOP_LINE", "CONTINUE_FEASIBLE_TASKS"}
DEFAULT_SIMULATION_CONFIG_PATH = PROJECT_ROOT / "data" / "digital_twin" / "default_simulation_config.json"


def omit_none_values(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _load_deployed_simulation_defaults() -> dict[str, Any]:
    if not DEFAULT_SIMULATION_CONFIG_PATH.exists():
        return {}
    try:
        payload = json.loads(DEFAULT_SIMULATION_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    config = payload.get("simulation_config")
    return deepcopy(config) if isinstance(config, dict) else {}


def generate_scenario_spec(
    request: ScenarioGenerationRequest | None = None,
    *,
    released_trt: dict[str, Any] | None = None,
    state_records: list[dict[str, Any]] | None = None,
    reconciliation_plan: dict[str, Any] | None = None,
    scenario_template_id: str | None = None,
    candidate_strategy_id: str | None = None,
    output_path: str | Path | None = None,
    template_registry: dict[str, Any] | None = None,
    operator_model_override: dict[str, Any] | None = None,
    assertions_override: dict[str, Any] | None = None,
    include_waiting_scenarios: bool = False,
    line_bindings: list[dict[str, Any]] | None = None,
    required_line_ids: list[str] | None = None,
    simulation_scope: str | None = None,
    simulation_config_override: dict[str, Any] | None = None,
) -> ScenarioSpec | WaitingForCheckpointResult:
    request = _coerce_request(
        request=request,
        released_trt=released_trt,
        state_records=state_records,
        reconciliation_plan=reconciliation_plan,
        scenario_template_id=scenario_template_id,
        candidate_strategy_id=candidate_strategy_id,
        template_registry=template_registry,
        include_waiting_scenarios=include_waiting_scenarios,
        line_bindings=line_bindings,
        required_line_ids=required_line_ids,
        simulation_scope=simulation_scope,
        simulation_config_override=simulation_config_override,
    )
    trt = request.released_trt
    state_records = request.state_records
    plan = request.reconciliation_plan
    template = get_template(request.template_registry, request.template_id)

    _validate_source_alignment(trt, plan)
    _validate_reconciliation_status(plan)
    line_bindings = deepcopy(request.line_bindings or template.get("line_bindings") or [])
    required_line_ids = request.required_line_ids or sorted(trt["lines"])
    simulation_scope_obj = _build_simulation_scope(request.simulation_scope, required_line_ids)
    if request.line_bindings is None:
        _validate_template_line_bindings(template, trt)
    else:
        _validate_resolved_line_bindings(
            template_id=template.get("template_id"),
            line_bindings=line_bindings,
            required_line_ids=required_line_ids,
        )

    release_id = _resolve_release_id(trt, plan, request)
    candidate_strategy_id = request.candidate_strategy_id or plan.get("candidate_strategy_id") or f"strategy_{plan['plan_id']}"
    if _scenario_readiness(plan) == "WAITING" and not request.include_waiting_scenarios:
        return _waiting_for_checkpoint_result(
            release_id=release_id,
            trt=trt,
            plan=plan,
            candidate_strategy_id=candidate_strategy_id,
        )
    scenario_spec_id = new_scenario_spec_id()
    spec: ScenarioSpec = {
        "scenario_spec_id": scenario_spec_id,
        "scenario_template_id": template["template_id"],
        "release_id": release_id,
        "trt_id": trt["trt_id"],
        "trt_version": trt["version"],
        "reconciliation_plan_id": plan["plan_id"],
        "candidate_strategy_id": candidate_strategy_id,
        "scenario_readiness": _scenario_readiness(plan),
        "affected_lines": list(plan.get("affected_lines", [])),
        "simulation_scope": simulation_scope_obj,
        "line_decisions": deepcopy(plan["line_decisions"]),
        "tool_catalog": deepcopy(trt.get("tool_catalog", {})),
        "tool_sets": deepcopy(trt.get("tool_sets", {})),
        "workspace_contract": _build_workspace_contract(template, scenario_spec_id, output_path),
        "scene_template": template["scene_template"],
        "simulation_config": _build_simulation_config(
            template,
            line_bindings,
            trt,
            simulation_scope_obj,
            operator_model_override or template["operator_model"],
            request.simulation_config_override,
        ),
        "line_bindings": line_bindings,
        "line_policies": _build_line_policies(trt, plan, required_line_ids),
        "operator_model": deepcopy(operator_model_override or template["operator_model"]),
        "abnormal_event_policy": _build_abnormal_event_policy(template),
        "assertions": _build_assertions(template, assertions_override),
        "governance_metadata": {
            "created_at_utc": now_utc(),
            "source_state_record_count": len(state_records),
            "reconciliation_overall_status": plan["overall_status"],
            "source_state_hash": plan.get("source_state_hash"),
            "source_trt_hash": plan.get("source_trt_hash"),
        },
    }
    validate_scenario_spec(spec)
    if output_path is not None:
        _export_generated_spec(spec, output_path)
    return spec


def _coerce_request(
    *,
    request: ScenarioGenerationRequest | None,
    released_trt: dict[str, Any] | None,
    state_records: list[dict[str, Any]] | None,
    reconciliation_plan: dict[str, Any] | None,
    scenario_template_id: str | None,
    candidate_strategy_id: str | None,
    template_registry: dict[str, Any] | None,
    include_waiting_scenarios: bool,
    line_bindings: list[dict[str, Any]] | None,
    required_line_ids: list[str] | None,
    simulation_scope: str | None,
    simulation_config_override: dict[str, Any] | None,
) -> ScenarioGenerationRequest:
    if request is not None:
        return request
    missing = [
        name
        for name, value in {
            "released_trt": released_trt,
            "state_records": state_records,
            "reconciliation_plan": reconciliation_plan,
            "template_registry": template_registry,
        }.items()
        if value is None
    ]
    if missing:
        raise ScenarioGenerationError(f"Missing required ScenarioSpec generation inputs: {missing}")
    return ScenarioGenerationRequest(
        released_trt=released_trt,
        state_records=state_records,
        reconciliation_plan=reconciliation_plan,
        template_registry=template_registry,
        template_id=scenario_template_id,
        candidate_strategy_id=candidate_strategy_id,
        include_waiting_scenarios=include_waiting_scenarios,
        line_bindings=line_bindings,
        required_line_ids=required_line_ids,
        simulation_scope=simulation_scope,
        simulation_config_override=simulation_config_override,
    )


def validate_scenario_spec(spec: dict[str, Any]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(spec), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        path = "/".join(str(part) for part in first.path) or "<root>"
        raise ScenarioGenerationError(f"Invalid ScenarioSpec at {path}: {first.message}")

    entanglement = spec["abnormal_event_policy"]["entanglement"]
    if entanglement.get("manual_event_injection") is True:
        raise ScenarioGenerationError("ScenarioSpec must not enable manual entanglement event injection.")
    if entanglement.get("event_injections") or entanglement.get("manual_events"):
        raise ScenarioGenerationError("ScenarioSpec must not include manual entanglement event lists.")
    if entanglement.get("predefined_entanglement_timestamps"):
        raise ScenarioGenerationError("ScenarioSpec must not include predefined entanglement timestamps.")


def _validate_source_alignment(trt: dict[str, Any], plan: dict[str, Any]) -> None:
    if plan["trt_id"] != trt["trt_id"] or plan["trt_version"] != trt["version"]:
        raise ScenarioGenerationError("Reconciliation Plan does not match the released TRT version.")


def _validate_reconciliation_status(plan: dict[str, Any]) -> None:
    if plan["overall_status"] == "REJECTED":
        raise ScenarioGenerationError("Cannot generate ScenarioSpec for a rejected Reconciliation Plan.")
    if plan["overall_status"] not in {"READY", "WAITING", "DEGRADED"}:
        raise ScenarioGenerationError(f"Unsupported reconciliation status: {plan['overall_status']}")


def _validate_template_line_bindings(template: dict[str, Any], trt: dict[str, Any]) -> None:
    trt_lines = set(trt["lines"])
    bound_lines = {binding["line_id"] for binding in template["line_bindings"]}
    missing = sorted(trt_lines - bound_lines)
    if missing:
        raise ScenarioTemplateLineBindingError(
            template_id=template.get("template_id"),
            required_trt_lines=sorted(trt_lines),
            template_bound_lines=sorted(bound_lines),
            missing_line_bindings=missing,
        )


def _validate_resolved_line_bindings(
    *,
    template_id: str | None,
    line_bindings: list[dict[str, Any]],
    required_line_ids: list[str],
) -> None:
    required = set(required_line_ids)
    bound = {binding["line_id"] for binding in line_bindings if isinstance(binding, dict) and binding.get("line_id")}
    missing = sorted(required - bound)
    if missing:
        raise ScenarioTemplateLineBindingError(
            template_id=template_id,
            required_trt_lines=sorted(required),
            template_bound_lines=sorted(bound),
            missing_line_bindings=missing,
    )


def _build_simulation_scope(value: dict[str, Any] | str | None, required_line_ids: list[str]) -> dict[str, Any]:
    default_reason = "Full-system simulation is required by default because the Time-Arrival Model is a system-level variable."
    if isinstance(value, dict):
        mode = value.get("mode") or "FULL_SYSTEM_DEFAULT"
        lines = list(value.get("lines") or required_line_ids)
        return {
            "mode": mode,
            "lines": lines,
            "reason": value.get("reason") or (
                "Operator explicitly requested a reduced simulation scope."
                if mode == "EXPLICIT_OPERATOR_LIMITED"
                else default_reason
            ),
        }
    if value == "EXPLICIT_OPERATOR_LIMITED":
        return {
            "mode": "EXPLICIT_OPERATOR_LIMITED",
            "lines": list(required_line_ids),
            "reason": "Operator explicitly requested a reduced simulation scope.",
        }
    return {
        "mode": "FULL_SYSTEM_DEFAULT",
        "lines": list(required_line_ids),
        "reason": default_reason,
    }


def _build_simulation_config(
    template: dict[str, Any],
    line_bindings: list[dict[str, Any]],
    trt: dict[str, Any],
    simulation_scope: dict[str, Any],
    operator_model: dict[str, Any],
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = deepcopy(template["simulation_config"])
    deployed_defaults = _load_deployed_simulation_defaults()
    if deployed_defaults:
        config.update({key: value for key, value in deployed_defaults.items() if value is not None})
    if override:
        config.update({key: value for key, value in override.items() if value is not None})
    simulation_lines = simulation_scope.get("lines") or []
    config["num_envs"] = len(simulation_lines) or len(line_bindings) or int(config.get("num_envs") or 1)
    config["headless"] = bool(config.get("headless", False))
    if config.get("global_seed") is not None:
        config["global_seed"] = int(config["global_seed"])
    else:
        config.pop("global_seed", None)
    config.pop("max_seed_trials", None)
    config["allowed_overlap_ratio"] = float(config.get("allowed_overlap_ratio", 0.99))
    config["layout_source"] = config.get("layout_source") or "auto"
    config["episode_success_requires_reset_cycles"] = int(config.get("episode_success_requires_reset_cycles", 1))
    if override and override.get("add_reference_number") is not None:
        config["add_reference_number"] = int(override["add_reference_number"])
    else:
        config["add_reference_number"] = int(config.get("add_reference_number") or len(trt.get("tool_catalog") or {}) or 27)
    config["reuse_verified_seed"] = config.get("global_seed") is None and bool(config.get("reuse_verified_seed", True))
    config.pop("reuse_precomputed_layouts", None)
    config.pop("seed_db_path", None)
    config["chosen_intervention_mode"] = config.get("chosen_intervention_mode") or "continue-until-arrival"
    config["travel_time"] = float(config.get("travel_time", operator_model.get("travel_time", 5.0)))
    config["fix_duration"] = float(config.get("fix_duration", operator_model.get("fix_duration", 8.0)))
    config["resume_delay"] = float(config.get("resume_delay", operator_model.get("resume_delay", 0.5)))
    return omit_none_values(config)


def _resolve_release_id(trt: dict[str, Any], plan: dict[str, Any], request: ScenarioGenerationRequest) -> str:
    release_id = request.release_id or plan.get("release_id") or trt.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise ScenarioGenerationError("release_id is required from the release, TRT, or Reconciliation Plan.")
    return release_id


def _scenario_readiness(plan: dict[str, Any]) -> str:
    if any(decision["decision"] == "WAIT_FOR_CHECKPOINT" for decision in plan["line_decisions"]):
        return "WAITING"
    if any(decision["decision"] == "DEGRADED_SWITCH" for decision in plan["line_decisions"]):
        return "DEGRADED"
    return "READY"


def _waiting_for_checkpoint_result(
    *,
    release_id: str,
    trt: dict[str, Any],
    plan: dict[str, Any],
    candidate_strategy_id: str,
) -> WaitingForCheckpointResult:
    return {
        "status": "WAITING_FOR_CHECKPOINT",
        "release_id": release_id,
        "trt_id": trt["trt_id"],
        "trt_version": trt["version"],
        "reconciliation_plan_id": plan["plan_id"],
        "candidate_strategy_id": candidate_strategy_id,
        "required_checkpoints": [
            {
                "line_id": decision["line_id"],
                "required_checkpoint": decision.get("required_checkpoint"),
                "reason": decision.get("reason"),
            }
            for decision in plan["line_decisions"]
            if decision["decision"] == "WAIT_FOR_CHECKPOINT"
        ],
    }


def _build_workspace_contract(
    template: dict[str, Any],
    scenario_spec_id: str,
    output_path: str | Path | None,
) -> dict[str, Any]:
    contract = deepcopy(template["workspace_contract"])
    contract["producer_workspace"] = "governance"
    contract["consumer_workspace"] = "isaac_sim"
    contract["exchange_mode"] = "file"
    scenario_specs_dir = Path(contract.get("scenario_specs_dir", "outputs/scenario_specs"))
    run_artifacts_dir = Path(contract.get("run_artifacts_dir", "outputs/run_artifacts"))
    if output_path is not None:
        scenario_spec_path = _scenario_spec_target_path(scenario_spec_id, output_path)
        scenario_specs_dir = scenario_spec_path.parent
        expected_scenario_spec_path = _portable_export_path(scenario_spec_path, output_path)
    else:
        expected_scenario_spec_path = _portable_path(scenario_specs_dir / f"{scenario_spec_id}.json")
    contract["scenario_specs_dir"] = _portable_path(scenario_specs_dir)
    contract["run_artifacts_dir"] = _portable_path(run_artifacts_dir)
    contract["expected_scenario_spec_path"] = expected_scenario_spec_path
    contract["expected_run_artifact_path"] = _portable_path(run_artifacts_dir / f"{scenario_spec_id}_run_artifact.json")
    return contract


def _scenario_spec_target_path(scenario_spec_id: str, output_path: str | Path) -> Path:
    path = Path(output_path)
    if path.suffix == ".json" and not (path.exists() and path.is_dir()):
        return path
    target_dir = path / "scenario_specs" if path.name == "outputs" else path
    return target_dir / f"{scenario_spec_id}.json"


def _portable_export_path(path: Path, output_path: str | Path) -> str:
    output = Path(output_path)
    try:
        if output.name == "outputs":
            return _portable_path(path.relative_to(output.parent))
        if output.name == "scenario_specs" and output.parent.name == "outputs":
            return _portable_path(path.relative_to(output.parent.parent))
    except ValueError:
        pass
    return _portable_path(path)


def _portable_path(path: Path) -> str:
    return path.as_posix()


def _build_line_policies(trt: dict[str, Any], plan: dict[str, Any], required_line_ids: list[str] | None = None) -> list[dict[str, Any]]:
    decisions = {decision["line_id"]: decision for decision in plan["line_decisions"]}
    affected_lines = set(plan.get("affected_lines") or [])
    policies: list[dict[str, Any]] = []
    selected_line_ids = required_line_ids or sorted(trt["lines"])
    for line_id in selected_line_ids:
        line = trt["lines"][line_id]
        abnormal_strategy = line["abnormal_strategy"]
        if abnormal_strategy == "ASK_OPERATOR":
            raise OperatorResolutionRequiredError(
                line_id=line_id,
                field=f"/lines/{line_id}/abnormal_strategy",
                current_value=abnormal_strategy,
                allowed_values=sorted(ISAAC_SUPPORTED_STRATEGIES),
            )
        if abnormal_strategy not in ISAAC_SUPPORTED_STRATEGIES:
            raise ScenarioGenerationError(
                f"Line {line_id} abnormal_strategy {abnormal_strategy!r} is not executable by the Isaac adapter."
            )
        decision = decisions.get(line_id, {"decision": "NO_CHANGE", "risk_flags": []})
        if decision["decision"] == "REJECT_INCOMPATIBLE":
            raise ScenarioGenerationError("Cannot generate ScenarioSpec for REJECT_INCOMPATIBLE line decision.")
        policy = {
            "line_id": line_id,
            "patch_affected": line_id in affected_lines,
            "goal": line["goal"],
            "allowed_instruments": list(line["allowed_instruments"]),
            "excluded_instruments": list(line["excluded_instruments"]),
            "selected_normalized_types": list(line.get("selected_normalized_types", [])),
            "selected_tool_ids": list(line.get("selected_tool_ids", [])),
            "excluded_tool_ids": list(line.get("excluded_tool_ids", [])),
            "required_tool_ids": list(line.get("required_tool_ids", [])),
            "target_set_id": line.get("target_set_id"),
            "tooling_policy": deepcopy(line.get("tooling_policy", {})),
            "digital_twin": deepcopy(line.get("digital_twin", {})),
            "priority": int(line["priority"]),
            "manipulator_priority": deepcopy(
                line.get(
                    "manipulator_priority",
                    {
                        "policy": "FCFS",
                        "ordered_tool_ids": [],
                        "ordered_normalized_types": [],
                        "tie_breaker": "FCFS",
                        "enabled": False,
                    },
                )
            ),
            "kpi": deepcopy(line["kpi"]),
            "abnormal_strategy": abnormal_strategy,
            "reconciliation_decision": decision["decision"],
            "required_checkpoint": decision.get("required_checkpoint"),
            "degraded_strategy": decision.get("degraded_strategy"),
            "risk_flags": list(decision.get("risk_flags", [])),
        }
        policies.append(policy)
    return policies


def _build_abnormal_event_policy(template: dict[str, Any]) -> dict[str, Any]:
    policy = deepcopy(template["abnormal_event_policy"])
    entanglement = policy.setdefault("entanglement", {})
    entanglement["generation_mode"] = "implicit_runtime_detection"
    entanglement["enabled"] = True
    entanglement["manual_event_injection"] = False
    entanglement.pop("event_injections", None)
    entanglement.pop("manual_events", None)
    entanglement["predefined_entanglement_timestamps"] = []
    return policy


def _build_assertions(template: dict[str, Any], assertions_override: dict[str, Any] | None) -> dict[str, Any]:
    assertions = deepcopy(assertions_override or template["assertions"])
    method = assertions.get("assert_method")
    if method is not None and method != "existing_validation_module":
        raise ScenarioGenerationError(f"Unsupported assert method: {method}")
    return assertions


def _export_generated_spec(spec: dict[str, Any], output_path: str | Path) -> Path:
    target_path = _scenario_spec_target_path(spec["scenario_spec_id"], output_path)
    target_dir = target_path.parent
    if target_dir.exists() and not target_dir.is_dir():
        raise RuntimeError(
            f"ScenarioSpec output parent exists but is not a directory: {target_dir}"
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and target_path.is_dir():
        raise RuntimeError(
            f"ScenarioSpec output path is a directory, expected file path: {target_path}"
        )
    target_path.write_text(json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return target_path
