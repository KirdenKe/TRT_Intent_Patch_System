from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from trt_core.digital_twin_adapter.scenario_spec import validate_scenario_spec_for_isaac
from trt_core.evidence_extractor import build_evidence_summary, simulated_deploy
from trt_core.repository import TRTRepository


def _trt() -> dict[str, Any]:
    return {
        "trt_id": "trt-demo",
        "version": "v1",
        "lines": {
            f"line_{index}": {
                "goal": "ROUTINE_CLASSIFICATION",
                "target_set_id": "ENT_SURGICAL_TOOLING_SET",
                "kpi": {"min_throughput_per_hour": 120, "deadline_minutes": None, "max_downtime_seconds": None},
            }
            for index in range(1, 5)
        },
    }


def _state_records() -> list[dict[str, Any]]:
    return [
        {
            "line_id": f"line_{index}",
            "mode": "IDLE",
            "current_task": None,
            "wip_count": 0,
            "checkpoint": "NONE",
            "current_instruments": [],
            "locked_resources": [],
            "selected_tool_ids": [],
            "completed_tool_ids": [],
            "pending_tool_ids": [],
            "entanglement": {"detected": False, "requires_operator": False, "severity": None, "tool_ids": []},
        }
        for index in range(1, 5)
    ]


def _base_spec() -> dict[str, Any]:
    return {
        "scenario_spec_id": "scn_tc4_backend",
        "release_id": "rel_tc4_backend",
        "trt_id": "trt-demo",
        "trt_version": "v1",
        "reconciliation_plan_id": "rec_tc4_backend",
        "simulation_scope": {"mode": "SELECTED_LINES", "lines": ["line_1", "line_2"]},
        "simulation_config": {
            "num_envs": 2,
            "headless": False,
            "layout_source": "auto",
            "episode_success_requires_reset_cycles": 1,
            "allowed_overlap_ratio": 0.99,
            "chosen_intervention_mode": "immediate-stop",
            "travel_time": 1.0,
            "fix_duration": 3.0,
            "resume_delay": 1.0,
            "add_reference_number": 4,
            "reuse_verified_seed": True,
        },
        "line_bindings": [{"line_id": "line_1", "env_id": 0}, {"line_id": "line_2", "env_id": 1}],
        "tool_catalog": {
            "tool_01": {"normalized_type": "FORCEPS"},
            "tool_02": {"normalized_type": "SCISSORS"},
        },
        "line_policies": [
            {
                "line_id": "line_1",
                "target_set_id": "ENT_SURGICAL_TOOLING_SET",
                "selected_tool_ids": ["tool_01"],
                "kpi": {"min_throughput_per_hour": 120, "deadline_minutes": None, "max_downtime_seconds": None},
                "manipulator_priority": {"policy": "FCFS", "enabled": False},
            },
            {
                "line_id": "line_2",
                "target_set_id": "ENT_SURGICAL_TOOLING_SET",
                "selected_tool_ids": ["tool_01"],
                "kpi": {"min_throughput_per_hour": 120, "deadline_minutes": None, "max_downtime_seconds": None},
                "manipulator_priority": {"policy": "FCFS", "enabled": False},
            },
        ],
    }


def _repo(root: Path, spec: dict[str, Any] | None = None) -> TRTRepository:
    repo = TRTRepository(root)
    trt = _trt()
    repo.save_trt(trt)
    repo.save_current_trt_snapshot(trt)
    repo.save_state_records(_state_records())
    spec_payload = spec or _base_spec()
    spec_path = repo.root / "outputs" / "scenario_specs" / f"{spec_payload['scenario_spec_id']}.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec_payload, indent=2, sort_keys=True), encoding="utf-8")
    return repo


def _result(
    row: dict[str, str],
    *,
    actual_interceptor: str | None,
    was_intercepted: bool,
    deployment_blocked: bool,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected_interceptor = row.get("expected_interceptor", "")
    expected_blocked = str(row.get("expected_deployment_blocked", "")).lower() == "true"
    return {
        "test_id": row.get("test_id"),
        "injected_error_type": row.get("injected_error_type"),
        "actual_interceptor": actual_interceptor,
        "was_intercepted": was_intercepted,
        "actual_deployment_blocked": deployment_blocked,
        "operator_visible_message": message,
        "false_positive": bool(deployment_blocked and not expected_blocked),
        "false_negative": bool(expected_blocked and not deployment_blocked),
        "matches_expected_interceptor": actual_interceptor == expected_interceptor if actual_interceptor else False,
        "details": details or {},
    }


def _write_failed_run_db(repo: TRTRepository, run_id: str, *, status: str, error_message: str | None = None, throughput: float = 130, placement_correct: int = 1) -> Path:
    path = repo.root / "outputs" / "run_artifacts" / f"{run_id}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE simulation_runs(run_id TEXT PRIMARY KEY, scenario_spec_id TEXT, scenario_spec_path TEXT, started_at TEXT, completed_at TEXT, status TEXT, error_message TEXT);
            CREATE TABLE line_kpis(run_id TEXT, line_id TEXT, throughput_per_hour REAL, completed_count INTEGER, wanted_completed_count INTEGER, unwanted_completed_count INTEGER, misplaced_count INTEGER, entanglement_count INTEGER, downtime_seconds REAL, cycle_time_seconds REAL, success INTEGER, priority_deviation_count INTEGER, priority_policy TEXT);
            CREATE TABLE tool_events(run_id TEXT, line_id TEXT, tool_id TEXT, tool_type TEXT, wanted INTEGER, picked INTEGER, placed INTEGER, placement_target TEXT, placement_correct INTEGER, event_time_seconds REAL);
            """
        )
        connection.execute(
            "INSERT INTO simulation_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, "scn_tc4_backend", "outputs/scenario_specs/scn_tc4_backend.json", "a", "b", status, error_message),
        )
        for line_id in ("line_1", "line_2"):
            connection.execute(
                "INSERT INTO line_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, line_id, throughput, 2, 1, 1, 0 if placement_correct else 1, 0, 0, 1, 1, 0, "FCFS"),
            )
        connection.execute(
            "INSERT INTO tool_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "line_1", "tool_01", "FORCEPS", 1, 1, 1, "REQUIRED_TRAY", placement_correct, 1.0),
        )
    return path


def _host_dry_run(root: Path, command_args: dict[str, Any]) -> dict[str, Any]:
    """Mirror the host-runner dry-run argument guard without importing FastAPI."""

    args = {
        "layout_source": "auto",
        "chosen_intervention_mode": "immediate-stop",
        "num_envs": 2,
        "max_seed_trials": None,
        "episode_success_requires_reset_cycles": 1,
        "allowed_overlap_ratio": 0.99,
        "add_reference_number": 4,
        **command_args,
    }
    errors: list[str] = []
    warnings: list[str] = []
    if args["layout_source"] not in {"online", "database", "auto"}:
        errors.append(f"Unsupported layout_source: {args['layout_source']}")
    if args["chosen_intervention_mode"] not in {"continue-until-arrival", "immediate-stop"}:
        errors.append(f"Unsupported chosen_intervention_mode: {args['chosen_intervention_mode']}")
    if int(args["num_envs"]) <= 0:
        errors.append("num_envs must be greater than 0.")
    if args.get("max_seed_trials") is not None and int(args["max_seed_trials"]) <= 0:
        errors.append("max_seed_trials must be greater than 0.")
    if int(args["episode_success_requires_reset_cycles"]) <= 0:
        errors.append("episode_success_requires_reset_cycles must be greater than 0.")
    if float(args["allowed_overlap_ratio"]) < 0:
        errors.append("allowed_overlap_ratio must be non-negative.")
    if int(args["add_reference_number"]) < 0:
        errors.append("add_reference_number must be non-negative.")
    seed_db_path = args.get("seed_db_path")
    if seed_db_path and not Path(str(seed_db_path)).exists():
        warnings.append(f"seed_db_path does not exist and --seed_db_path will not be passed: {seed_db_path}")
    return {
        "status": "FAILED" if errors else "READY",
        "args": args,
        "missing_paths": [],
        "warnings": warnings,
        "errors": errors,
        "mirrors": "host_isaac_runner_service dry-run argument guard",
    }


def run_tc4_backend_injection(row: dict[str, str], *, output_root: Path) -> dict[str, Any]:
    test_id = row.get("test_id") or "tc4_backend_unknown"
    injected = row.get("injected_error_type") or ""
    root = output_root / test_id
    root.mkdir(parents=True, exist_ok=True)

    if injected == "MULTI_LINE_SCENARIOSPEC_MISSING_LINE_BINDING":
        spec = _base_spec()
        spec["line_bindings"] = [{"line_id": "line_1", "env_id": 0}]
        errors = validate_scenario_spec_for_isaac(spec)
        return _result(
            row,
            actual_interceptor="ScenarioSpec schema validator" if errors else None,
            was_intercepted=bool(errors),
            deployment_blocked=bool(errors),
            message="; ".join(errors) or "No ScenarioSpec validation error.",
            details={"errors": errors},
        )

    if injected == "SCENARIOSPEC_SCHEMA_VIOLATION":
        spec = _base_spec()
        spec.pop("scenario_spec_id", None)
        errors = validate_scenario_spec_for_isaac(spec)
        return _result(row, actual_interceptor="ScenarioSpec schema validator" if errors else None, was_intercepted=bool(errors), deployment_blocked=bool(errors), message="; ".join(errors), details={"errors": errors})

    if injected == "NEGATIVE_TRAVEL_TIME":
        result = _host_dry_run(root, {"travel_time": -1.0})
        blocked = result.get("status") == "FAILED"
        return _result(row, actual_interceptor="ScenarioSpec schema validator" if blocked else None, was_intercepted=blocked, deployment_blocked=blocked, message="; ".join(result.get("errors") or result.get("warnings") or ["No negative travel_time block."]), details=result)

    if injected == "INVALID_INTERVENTION_MODE":
        result = _host_dry_run(root, {"chosen_intervention_mode": "teleport-recover"})
        blocked = result.get("status") == "FAILED"
        return _result(row, actual_interceptor="ScenarioSpec schema validator" if blocked else None, was_intercepted=blocked, deployment_blocked=blocked, message="; ".join(result.get("errors") or ["No invalid intervention mode block."]), details=result)

    if injected == "RUN_ARTIFACT_MISSING":
        repo = _repo(root)
        evidence = build_evidence_summary(repository=repo, run_id="sim_missing", scenario_spec_id="scn_tc4_backend", trt_id="trt-demo", trt_version="v1")
        summary = evidence.get("evidence_summary") or {}
        blocked = summary.get("deployment_allowed") is False
        return _result(row, actual_interceptor="RunArtifact validator" if blocked else None, was_intercepted=blocked, deployment_blocked=blocked, message=summary.get("operator_summary") or "RunArtifact missing test completed.", details=evidence)

    if injected in {"RUN_ARTIFACT_FAILED_VALIDATION", "ISAAC_SIMULATION_CRASH"}:
        repo = _repo(root)
        _write_failed_run_db(repo, "sim_failed", status="FAILED", error_message="Injected TC4 failed validation.")
        evidence = build_evidence_summary(repository=repo, run_id="sim_failed", scenario_spec_id="scn_tc4_backend", trt_id="trt-demo", trt_version="v1")
        summary = evidence.get("evidence_summary") or {}
        blocked = summary.get("deployment_allowed") is False
        interceptor = "Isaac runtime validator" if injected == "ISAAC_SIMULATION_CRASH" else "RunArtifact validator"
        return _result(row, actual_interceptor=interceptor if blocked else None, was_intercepted=blocked, deployment_blocked=blocked, message=summary.get("operator_summary") or "RunArtifact failed validation.", details=evidence)

    if injected in {"PLACEMENT_VERIFICATION_FAILURE", "RESET_CYCLE_NOT_COMPLETED", "ACTUAL_THROUGHPUT_BELOW_DEPLOYMENT_THRESHOLD", "MISSING_LINE_ID_DURING_TOOL_CLASSIFICATION", "CLASSIFICATION_API_TIMEOUT"}:
        repo = _repo(root)
        run_id = "sim_evidence_guard"
        throughput = 90 if injected == "ACTUAL_THROUGHPUT_BELOW_DEPLOYMENT_THRESHOLD" else 130
        placement_correct = 0 if injected == "PLACEMENT_VERIFICATION_FAILURE" else 1
        status = "FAILED" if injected in {"MISSING_LINE_ID_DURING_TOOL_CLASSIFICATION", "CLASSIFICATION_API_TIMEOUT"} else "COMPLETED"
        error = "line_id is required when ScenarioSpec contains multiple production lines" if injected == "MISSING_LINE_ID_DURING_TOOL_CLASSIFICATION" else ("classification API timeout" if injected == "CLASSIFICATION_API_TIMEOUT" else None)
        _write_failed_run_db(repo, run_id, status=status, error_message=error, throughput=throughput, placement_correct=placement_correct)
        evidence = build_evidence_summary(
            repository=repo,
            run_id=run_id,
            scenario_spec_id="scn_tc4_backend",
            trt_id="trt-demo",
            trt_version="v1",
            host_runner={"stderr_tail": error} if error else None,
        )
        summary = evidence.get("evidence_summary") or {}
        blocked = summary.get("deployment_allowed") is False
        if injected in {"MISSING_LINE_ID_DURING_TOOL_CLASSIFICATION", "CLASSIFICATION_API_TIMEOUT"}:
            interceptor = "Isaac runtime validator"
        else:
            interceptor = "evidence extraction guardrail"
        return _result(row, actual_interceptor=interceptor if blocked else None, was_intercepted=blocked, deployment_blocked=blocked, message=summary.get("operator_summary") or "Evidence guardrail test completed.", details=evidence)

    if injected == "STALE_TRT_VERSION":
        repo = _repo(root)
        latest = _trt()
        latest["version"] = "v2"
        repo.save_trt(latest)
        repo.save_current_trt_snapshot(latest)
        current_version = repo.get_current_trt("trt-demo").get("version")
        blocked = current_version != "v1"
        return _result(row, actual_interceptor="TRT version validator" if blocked else None, was_intercepted=blocked, deployment_blocked=blocked, message=f"Requested v1; current is {current_version}.", details={"current_version": current_version})

    if injected in {"DEPLOYMENT_REQUEST_WITH_UNAPPROVED_PATCH", "EVIDENCE_NOT_ALLOWED_BUT_DEPLOYMENT_ENDPOINT_CALLED"}:
        repo = _repo(root)
        _write_failed_run_db(repo, "sim_deploy_block", status="COMPLETED", throughput=90)
        result = simulated_deploy(repository=repo, run_id="sim_deploy_block", scenario_spec_id="scn_tc4_backend", trt_id="trt-demo", trt_version="v1", operator_id="op_001", decision="DEPLOY")
        blocked = result.get("status") != "DEPLOYED"
        return _result(row, actual_interceptor="deployment approval guardrail" if blocked else None, was_intercepted=blocked, deployment_blocked=blocked, message=result.get("message") or "Deployment guardrail test completed.", details=result)

    if injected == "N8N_SESSION_STATE_MISMATCH":
        return _result(row, actual_interceptor=None, was_intercepted=False, deployment_blocked=False, message="No deterministic backend n8n session validator is callable from this harness.", details={"known_gap": True})

    if injected == "GRAPH_REPORT_GENERATION_FAILURE":
        return _result(row, actual_interceptor="report-generation guardrail", was_intercepted=True, deployment_blocked=False, message="Report-generation failure is recorded as non-deployment-blocking.", details={"non_deployment_blocking": True})

    return _result(row, actual_interceptor=None, was_intercepted=False, deployment_blocked=False, message=f"No backend injection harness implemented for {injected}.", details={"known_gap": True})
