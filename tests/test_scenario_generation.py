from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scenario_generation.errors import OperatorResolutionRequiredError, ScenarioGenerationError, TemplateRegistryError
from scenario_generation.generator import generate_scenario_spec
from scenario_generation.models import ScenarioGenerationRequest
from scenario_generation.template_registry import normalize_template_registry, validate_template_registry


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "scenario_spec.schema.json"


def make_request(
    fixture_loader,
    plan_name: str = "reconciliation_ready.json",
    *,
    include_waiting_scenarios: bool = False,
) -> ScenarioGenerationRequest:
    return ScenarioGenerationRequest(
        released_trt=fixture_loader("released_trt_v1.json"),
        state_records=fixture_loader("state_records_v1.json"),
        reconciliation_plan=fixture_loader(plan_name),
        template_registry=fixture_loader("scenario_templates.json"),
        release_id="rel_m6_001",
        candidate_strategy_id="strategy_m6_001",
        include_waiting_scenarios=include_waiting_scenarios,
    )


def validate_against_schema(spec: dict) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(spec))
    assert errors == []


def test_ready_reconciliation_generates_scenario_spec(fixture_loader):
    spec = generate_scenario_spec(make_request(fixture_loader))

    assert spec["scenario_spec_id"].startswith("scn_")
    assert spec["scenario_readiness"] == "READY"
    validate_against_schema(spec)


def test_scenario_spec_includes_governance_ids(fixture_loader):
    spec = generate_scenario_spec(make_request(fixture_loader))

    assert spec["release_id"] == "rel_m6_001"
    assert spec["trt_id"] == "trt-demo"
    assert spec["trt_version"] == "v1"
    assert spec["reconciliation_plan_id"] == "rec_ready_001"
    assert spec["candidate_strategy_id"] == "strategy_m6_001"


def test_scenario_spec_includes_workspace_contract(fixture_loader):
    spec = generate_scenario_spec(make_request(fixture_loader))
    contract = spec["workspace_contract"]

    assert contract["producer_workspace"] == "governance"
    assert contract["consumer_workspace"] == "isaac_sim"
    assert contract["exchange_mode"] == "file"
    assert contract["scenario_specs_dir"] == "outputs/scenario_specs"
    assert contract["run_artifacts_dir"] == "outputs/run_artifacts"
    assert contract["expected_scenario_spec_path"].startswith("outputs/scenario_specs/")
    assert contract["expected_run_artifact_path"].endswith(f"{spec['scenario_spec_id']}_run_artifact.json")
    assert contract["expected_run_artifact_path"].startswith("outputs/run_artifacts/")


def test_scenario_spec_uses_template_registry_defaults(fixture_loader):
    spec = generate_scenario_spec(make_request(fixture_loader))

    assert spec["scene_template"] == "pick_up_example.py"
    assert "global_seed" not in spec["simulation_config"]
    assert spec["simulation_config"]["num_envs"] == 2
    assert spec["simulation_config"]["allowed_overlap_ratio"] == 0.99
    assert spec["simulation_config"]["layout_source"] == "auto"
    assert spec["simulation_config"]["episode_success_requires_reset_cycles"] == 1
    assert spec["simulation_config"]["add_reference_number"] == 27
    assert spec["simulation_config"]["reuse_verified_seed"] is True
    assert "max_seed_trials" not in spec["simulation_config"]
    assert "reuse_precomputed_layouts" not in spec["simulation_config"]
    assert "seed_db_path" not in spec["simulation_config"]
    assert spec["simulation_config"]["chosen_intervention_mode"] == "continue-until-arrival"
    assert spec["simulation_config"]["travel_time"] == 5.0
    assert spec["simulation_config"]["fix_duration"] == 8.0
    assert spec["simulation_config"]["resume_delay"] == 0.5
    assert spec["operator_model"] == {"travel_time": 5.0, "fix_duration": 8.0, "resume_delay": 0.5}
    assert spec["assertions"] == {"use_existing_validation_module": True}


def test_template_registry_normalizes_null_optional_isaac_args(fixture_loader):
    registry = deepcopy(fixture_loader("scenario_templates.json"))
    registry["templates"][0]["simulation_config"]["global_seed"] = None
    registry["templates"][0]["simulation_config"]["seed_db_path"] = None

    normalized = normalize_template_registry(registry)

    assert "global_seed" not in normalized["templates"][0]["simulation_config"]
    assert "seed_db_path" not in normalized["templates"][0]["simulation_config"]
    validate_template_registry(registry)


def test_line_bindings_come_from_registry_not_hardcoded_assumptions(fixture_loader):
    spec = generate_scenario_spec(make_request(fixture_loader))

    assert spec["line_bindings"] == [{"line_id": "line_1", "env_id": 2}, {"line_id": "line_2", "env_id": 0}]


def test_abnormal_event_policy_uses_implicit_runtime_detection(fixture_loader):
    spec = generate_scenario_spec(make_request(fixture_loader))
    entanglement = spec["abnormal_event_policy"]["entanglement"]

    assert entanglement["enabled"] is True
    assert entanglement["generation_mode"] == "implicit_runtime_detection"


def test_event_injections_is_never_generated(fixture_loader):
    spec = generate_scenario_spec(make_request(fixture_loader))
    entanglement = spec["abnormal_event_policy"]["entanglement"]

    assert "event_injections" not in entanglement
    assert "manual_events" not in entanglement
    assert entanglement["predefined_entanglement_timestamps"] == []


def test_rejected_reconciliation_does_not_generate_scenario_spec(fixture_loader):
    with pytest.raises(ScenarioGenerationError, match="rejected"):
        generate_scenario_spec(make_request(fixture_loader, "reconciliation_rejected.json"))


def test_waiting_reconciliation_returns_waiting_for_checkpoint_by_default(fixture_loader):
    result = generate_scenario_spec(make_request(fixture_loader, "reconciliation_waiting.json"))

    assert result["status"] == "WAITING_FOR_CHECKPOINT"
    assert result["required_checkpoints"] == [
        {"line_id": "line_1", "required_checkpoint": "TRAY_COMPLETE", "reason": "wip must clear"}
    ]


def test_waiting_reconciliation_generates_scenario_when_explicitly_included(fixture_loader):
    spec = generate_scenario_spec(
        make_request(fixture_loader, "reconciliation_waiting.json", include_waiting_scenarios=True)
    )
    line_1 = next(policy for policy in spec["line_policies"] if policy["line_id"] == "line_1")

    assert spec["scenario_readiness"] == "WAITING"
    assert line_1["reconciliation_decision"] == "WAIT_FOR_CHECKPOINT"
    assert line_1["required_checkpoint"] == "TRAY_COMPLETE"


def test_degraded_reconciliation_generates_degraded_scenario_spec(fixture_loader):
    spec = generate_scenario_spec(make_request(fixture_loader, "reconciliation_degraded.json"))
    line_1 = next(policy for policy in spec["line_policies"] if policy["line_id"] == "line_1")

    assert spec["scenario_readiness"] == "DEGRADED"
    assert line_1["reconciliation_decision"] == "DEGRADED_SWITCH"
    assert line_1["degraded_strategy"] == "APPLY_PRIORITY_ONLY_DELAY_INSTRUMENT_RESTRICTIONS"


def test_missing_template_id_fails(fixture_loader):
    with pytest.raises(TemplateRegistryError, match="not found"):
        generate_scenario_spec(
            ScenarioGenerationRequest(
                released_trt=fixture_loader("released_trt_v1.json"),
                state_records=fixture_loader("state_records_v1.json"),
                reconciliation_plan=fixture_loader("reconciliation_ready.json"),
                template_registry=fixture_loader("scenario_templates.json"),
                template_id="missing_template",
                release_id="rel_m6_001",
                candidate_strategy_id="strategy_m6_001",
            )
        )


def test_unsupported_assert_method_fails(fixture_loader):
    registry = deepcopy(fixture_loader("scenario_templates.json"))
    registry["templates"][0]["assertions"] = {"assert_method": "unsupported_custom_assertions"}

    with pytest.raises(ScenarioGenerationError, match="Unsupported assert method"):
        generate_scenario_spec(
            ScenarioGenerationRequest(
                released_trt=fixture_loader("released_trt_v1.json"),
                state_records=fixture_loader("state_records_v1.json"),
                reconciliation_plan=fixture_loader("reconciliation_ready.json"),
                template_registry=registry,
                release_id="rel_m6_001",
                candidate_strategy_id="strategy_m6_001",
            )
        )


def test_export_writes_json_to_outputs_scenario_specs_and_creates_directories(fixture_loader, tmp_path):
    output_root = tmp_path / "outputs"
    assert not (output_root / "scenario_specs").exists()

    spec = generate_scenario_spec(
        released_trt=fixture_loader("released_trt_v1.json"),
        state_records=fixture_loader("state_records_v1.json"),
        reconciliation_plan=fixture_loader("reconciliation_ready.json"),
        scenario_template_id="ur5_pick_place_minimal",
        candidate_strategy_id="strategy_explicit_001",
        output_path=output_root,
        template_registry=fixture_loader("scenario_templates.json"),
    )

    output_file = output_root / "scenario_specs" / f"scenario_spec_{spec['scenario_spec_id']}.json"
    assert output_file.exists()
    assert (output_root / "scenario_specs").is_dir()
    assert not (tmp_path / "exchange" / "scenario_specs").exists()
    assert "outputs/scenario_specs" in spec["workspace_contract"]["expected_scenario_spec_path"]
    assert "outputs/run_artifacts" in spec["workspace_contract"]["expected_run_artifact_path"]
    assert json.loads(output_file.read_text(encoding="utf-8")) == spec


def test_exported_json_validates_against_scenario_spec_schema(fixture_loader, tmp_path):
    output_root = tmp_path / "outputs"
    spec = generate_scenario_spec(
        released_trt=fixture_loader("released_trt_v1.json"),
        state_records=fixture_loader("state_records_v1.json"),
        reconciliation_plan=fixture_loader("reconciliation_ready.json"),
        scenario_template_id="ur5_pick_place_minimal",
        candidate_strategy_id="strategy_explicit_001",
        output_path=output_root,
        template_registry=fixture_loader("scenario_templates.json"),
    )
    exported = json.loads(
        (output_root / "scenario_specs" / f"scenario_spec_{spec['scenario_spec_id']}.json").read_text(encoding="utf-8")
    )

    validate_against_schema(exported)


def test_export_writes_to_scenario_specs_directory_path(fixture_loader, tmp_path):
    output_dir = tmp_path / "outputs" / "scenario_specs"
    spec = generate_scenario_spec(
        released_trt=fixture_loader("released_trt_v1.json"),
        state_records=fixture_loader("state_records_v1.json"),
        reconciliation_plan=fixture_loader("reconciliation_ready.json"),
        scenario_template_id="ur5_pick_place_minimal",
        candidate_strategy_id="strategy_explicit_001",
        output_path=output_dir,
        template_registry=fixture_loader("scenario_templates.json"),
    )

    output_file = output_dir / f"scenario_spec_{spec['scenario_spec_id']}.json"
    assert output_file.exists()
    assert json.loads(output_file.read_text(encoding="utf-8")) == spec


def test_export_raises_clear_error_if_parent_path_exists_as_file(fixture_loader, tmp_path):
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError, match="ScenarioSpec output parent exists but is not a directory"):
        generate_scenario_spec(
            released_trt=fixture_loader("released_trt_v1.json"),
            state_records=fixture_loader("state_records_v1.json"),
            reconciliation_plan=fixture_loader("reconciliation_ready.json"),
            scenario_template_id="ur5_pick_place_minimal",
            candidate_strategy_id="strategy_explicit_001",
            output_path=blocked_parent / "scenario.json",
            template_registry=fixture_loader("scenario_templates.json"),
        )


def test_ask_operator_strategy_is_rejected_before_isaac_export(fixture_loader):
    request = make_request(fixture_loader)
    trt = deepcopy(request.released_trt)
    trt["lines"]["line_1"]["abnormal_strategy"] = "ASK_OPERATOR"

    with pytest.raises(
        OperatorResolutionRequiredError,
        match=(
            "Line line_1 abnormal_strategy is ASK_OPERATOR. "
            "ScenarioSpec generation requires a concrete executable policy. "
            "Resolve this field to STOP_LINE or CONTINUE_FEASIBLE_TASKS before simulation."
        ),
    ) as exc_info:
        generate_scenario_spec(
            ScenarioGenerationRequest(
                released_trt=trt,
                state_records=request.state_records,
                reconciliation_plan=request.reconciliation_plan,
                template_registry=request.template_registry,
                release_id=request.release_id,
                candidate_strategy_id=request.candidate_strategy_id,
            )
        )
    assert exc_info.value.line_id == "line_1"
    assert exc_info.value.field == "/lines/line_1/abnormal_strategy"
    assert exc_info.value.current_value == "ASK_OPERATOR"
    assert exc_info.value.allowed_values == ["CONTINUE_FEASIBLE_TASKS", "STOP_LINE"]
