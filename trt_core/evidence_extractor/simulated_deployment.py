"""Simulated physical deployment backed by local state/config files."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from trt_core.evidence_extractor.evidence_summary_builder import build_evidence_summary
from trt_core.repository import TRTRepository
from trt_core.state_records import save_current_state


DEPLOYABLE_SIMULATION_CONFIG_KEYS = {
    "num_envs",
    "headless",
    "layout_source",
    "episode_success_requires_reset_cycles",
    "allowed_overlap_ratio",
    "chosen_intervention_mode",
    "travel_time",
    "fix_duration",
    "resume_delay",
    "add_reference_number",
    "reuse_verified_seed",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _scenario_spec_path(repository: TRTRepository, scenario_spec_id: str) -> Path:
    return repository.root / "outputs" / "scenario_specs" / f"{scenario_spec_id}.json"


def _load_scenario_spec(repository: TRTRepository, scenario_spec_id: str) -> dict[str, Any]:
    path = _scenario_spec_path(repository, scenario_spec_id)
    if not path.exists():
        raise ValueError(f"ScenarioSpec not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _line_target_set(scenario_spec: dict[str, Any], line_id: str) -> str | None:
    for policy in scenario_spec.get("line_policies") or []:
        if policy.get("line_id") == line_id:
            return policy.get("target_set_id")
    return None


def _update_state_records(
    records: list[dict[str, Any]],
    *,
    scenario_spec: dict[str, Any],
    trt_id: str,
    trt_version: str,
    run_id: str,
    scenario_spec_id: str,
    deployment_id: str,
) -> list[dict[str, Any]]:
    updated = []
    simulated_lines = set((scenario_spec.get("simulation_scope") or {}).get("lines") or [])
    for record in records:
        line_id = record.get("line_id")
        next_record = dict(record)
        next_record.update(
            {
                "active_trt_id": trt_id,
                "active_trt_version": trt_version,
                "last_deployment_id": deployment_id,
                "last_deployed_run_id": run_id,
                "last_deployed_scenario_spec_id": scenario_spec_id,
                "deployment_status": "DEPLOYED",
                "deployment_source": "DIGITAL_TWIN_EVIDENCE_APPROVED_BY_OPERATOR",
            }
        )
        if line_id in simulated_lines:
            next_record["mode"] = "RUNNING"
            next_record["active_set_id"] = _line_target_set(scenario_spec, str(line_id)) or next_record.get("active_set_id")
            next_record["selected_tool_ids"] = []
            next_record["pending_tool_ids"] = []
            next_record["completed_tool_ids"] = []
            next_record["current_task"] = None
            next_record["last_exception"] = None
            entanglement = dict(next_record.get("entanglement") or {})
            entanglement.update({"detected": False, "requires_operator": False, "severity": None, "tool_ids": []})
            next_record["entanglement"] = entanglement
        updated.append(next_record)
    return updated


def simulated_deploy(
    *,
    repository: TRTRepository,
    run_id: str,
    scenario_spec_id: str,
    trt_id: str,
    trt_version: str,
    operator_id: str | None = None,
    deployment_comment: str | None = None,
    decision: str | None = None,
    acknowledged_risks: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    evidence = build_evidence_summary(
        repository=repository,
        run_id=run_id,
        scenario_spec_id=scenario_spec_id,
        trt_id=trt_id,
        trt_version=trt_version,
    )
    recommendation = ((evidence.get("raw_evidence") or {}).get("run_artifact") or {}).get("deployment_recommendation") or {}
    deployment_decision = str(decision or "DEPLOY").upper()
    evidence_summary = evidence.get("evidence_summary") or {}
    allowed_with_ack = (
        deployment_decision == "DEPLOY_WITH_ACK"
        and recommendation.get("allowed") is True
        and recommendation.get("requires_operator_acknowledgement") is True
        and recommendation.get("risk_tier") == "OPERATOR_ACK_REQUIRED"
    )
    if not force and not recommendation.get("recommended") and not allowed_with_ack:
        return {
            "status": "REJECTED",
            "deployment_id": None,
            "trt_id": trt_id,
            "trt_version": trt_version,
            "message": "Deployment rejected because evidence does not allow deployment for this decision.",
            "evidence_summary": evidence_summary,
            "errors": evidence.get("errors") or ["Deployment evidence did not pass."],
        }
    if allowed_with_ack:
        required_risks = set(recommendation.get("acknowledged_risks") or [])
        supplied_risks = set(acknowledged_risks or required_risks)
        missing_risks = sorted(required_risks - supplied_risks)
        if missing_risks:
            return {
                "status": "REJECTED",
                "deployment_id": None,
                "trt_id": trt_id,
                "trt_version": trt_version,
                "message": "Deployment with acknowledgement rejected because not all required risks were acknowledged.",
                "evidence_summary": evidence_summary,
                "errors": [f"Missing acknowledged risks: {', '.join(missing_risks)}"],
            }

    scenario_spec = _load_scenario_spec(repository, scenario_spec_id)
    if scenario_spec.get("scenario_spec_id") != scenario_spec_id:
        return {
            "status": "FAILED",
            "deployment_id": None,
            "trt_id": trt_id,
            "trt_version": trt_version,
            "message": "ScenarioSpec ID mismatch.",
            "errors": [f"Expected {scenario_spec_id}, found {scenario_spec.get('scenario_spec_id')}"],
        }
    deployment_id = f"deploy_{uuid4()}"
    state_records = repository.load_state_records()
    updated_records = _update_state_records(
        state_records,
        scenario_spec=scenario_spec,
        trt_id=trt_id,
        trt_version=trt_version,
        run_id=run_id,
        scenario_spec_id=scenario_spec_id,
        deployment_id=deployment_id,
    )
    save_current_state(updated_records, repository)
    state_payload = {
        "active_trt_id": trt_id,
        "active_trt_version": trt_version,
        "last_deployment_id": deployment_id,
        "last_deployed_run_id": run_id,
        "last_deployed_scenario_spec_id": scenario_spec_id,
        "deployment_status": "DEPLOYED",
        "deployment_source": "DIGITAL_TWIN_EVIDENCE_APPROVED_BY_OPERATOR",
        "state_version": f"state-demo-{trt_version}",
        "lines": {
            str(record["line_id"]): {key: value for key, value in record.items() if key != "line_id"}
            for record in updated_records
        },
    }
    (repository.state_dir / "current_state.json").write_text(
        json.dumps(state_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    defaults_dir = repository.root / "data" / "digital_twin"
    defaults_dir.mkdir(parents=True, exist_ok=True)
    defaults_path = defaults_dir / "default_simulation_config.json"
    simulation_config = {
        key: value
        for key, value in (scenario_spec.get("simulation_config") or {}).items()
        if key in DEPLOYABLE_SIMULATION_CONFIG_KEYS
    }
    defaults = {
        "source": "simulated_physical_deployment",
        "deployment_id": deployment_id,
        "run_id": run_id,
        "scenario_spec_id": scenario_spec_id,
        "trt_id": trt_id,
        "trt_version": trt_version,
        "simulation_config": simulation_config,
        "updated_at": _now_utc(),
    }
    defaults_path.write_text(json.dumps(defaults, indent=2, sort_keys=True), encoding="utf-8")

    audit_dir = repository.root / "data" / "deployments"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / f"{deployment_id}.json"
    audit = {
        "deployment_id": deployment_id,
        "deployment_type": "SIMULATED_PHYSICAL_DEPLOYMENT",
        "decision": deployment_decision,
        "acknowledged_risks": acknowledged_risks or recommendation.get("acknowledged_risks") or [],
        "operator_acknowledgement_required": bool(recommendation.get("requires_operator_acknowledgement")),
        "operator_acknowledged": allowed_with_ack or deployment_decision == "DEPLOY",
        "operator_id": operator_id,
        "deployment_comment": deployment_comment,
        "run_id": run_id,
        "scenario_spec_id": scenario_spec_id,
        "trt_id": trt_id,
        "trt_version": trt_version,
        "status": "DEPLOYED",
        "created_at": _now_utc(),
        "evidence_summary": evidence_summary,
        "updated_state_record_path": "data/state_records/current_state.json",
        "updated_digital_twin_defaults_path": "data/digital_twin/default_simulation_config.json",
    }
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")

    return {
        "status": "DEPLOYED",
        "deployment_id": deployment_id,
        "trt_id": trt_id,
        "trt_version": trt_version,
        "state_record_version": f"state-demo-{trt_version}",
        "updated_state_record_path": "data/state_records/current_state.json",
        "updated_digital_twin_defaults_path": "data/digital_twin/default_simulation_config.json",
        "deployment_audit_path": str(audit_path.relative_to(repository.root)),
        "message": "Deployment simulated by updating current state records and digital twin defaults.",
        "errors": [],
    }
