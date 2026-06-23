"""Build host-runner requests for Isaac Sim ScenarioSpec runs."""

from __future__ import annotations

import os
import json
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any
from uuid import uuid4

from trt_core.digital_twin_adapter.scenario_spec import validate_scenario_spec_for_isaac


DEFAULT_ISAAC_WORKING_DIRECTORY = r"C:\Dev\IsaacSim"
DEFAULT_ISAAC_PYTHON_BAT = DEFAULT_ISAAC_WORKING_DIRECTORY + r"\_build\windows-x86_64\release\python.bat"
DEFAULT_UR5_ENTRY_SCRIPT = (
    DEFAULT_ISAAC_WORKING_DIRECTORY
    + r"\_build\windows-x86_64\release\standalone_examples\api"
    + r"\isaacsim.robot.manipulators\ur5\pick_up_example.py"
)
DEFAULT_CONTAINER_PROJECT_ROOT = "/app"
ISAAC_COMMAND_DEFAULT_SOURCE = "fallback: IsaacCommandArgs.default"
INTERNAL_DEV_ARG_FLAG = "ISAAC_INTERNAL_DEV_ARGS"
HOST_PROJECT_ROOT_MANGLED_HINT = (
    "HOST_PROJECT_ROOT appears to have lost a literal dollar-sign username segment. "
    "Use data/isaac_host_config.json or escape $ as $$ in Compose."
)


def omit_none_values(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _config_path(repository: Any) -> Path:
    return Path(repository.root) / "data" / "isaac_host_config.json"


def _load_config_file(repository: Any) -> dict[str, Any]:
    path = _config_path(repository)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _mangled_host_project_root_warning(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("/", "\\")
    if "\\Users-" in normalized or normalized.startswith("C:\\Users-"):
        return HOST_PROJECT_ROOT_MANGLED_HINT
    return None


def isaac_host_runtime_config(repository: Any) -> dict[str, Any]:
    file_config = _load_config_file(repository)
    env_host_project_root = os.environ.get("HOST_PROJECT_ROOT")
    config_host_project_root = file_config.get("host_project_root")
    host_project_root = config_host_project_root or env_host_project_root
    host_project_root_source = "config_file" if config_host_project_root else ("env" if env_host_project_root else "default")
    env_warning = _mangled_host_project_root_warning(env_host_project_root)
    warnings = [env_warning] if env_warning else []
    if not host_project_root and os.name == "nt":
        host_project_root = str(Path(repository.root))
        host_project_root_source = "local_windows_default"

    container_project_root = (
        file_config.get("container_project_root")
        or os.environ.get("CONTAINER_PROJECT_ROOT")
        or DEFAULT_CONTAINER_PROJECT_ROOT
    )
    return {
        "config_path": str(_config_path(repository)),
        "config_file_loaded": bool(file_config),
        "host_project_root": host_project_root,
        "host_project_root_source": host_project_root_source,
        "container_project_root": str(container_project_root).rstrip("/"),
        "isaac_working_directory": file_config.get("isaac_working_directory")
        or os.environ.get("ISAAC_WORKING_DIRECTORY")
        or DEFAULT_ISAAC_WORKING_DIRECTORY,
        "python_bat": file_config.get("python_bat")
        or os.environ.get("ISAAC_PYTHON_BAT")
        or DEFAULT_ISAAC_PYTHON_BAT,
        "entry_script": file_config.get("entry_script")
        or os.environ.get("ISAAC_UR5_ENTRY_SCRIPT")
        or DEFAULT_UR5_ENTRY_SCRIPT,
        "seed_db_path": file_config.get("seed_db_path")
        or os.environ.get("ISAAC_SEED_DB_PATH"),
        "warnings": warnings,
    }


def _resolve_path(path: str | Path, repository: Any) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return Path(repository.root) / candidate


def _scenario_spec_path(scenario_spec: dict[str, Any], repository: Any, override: str | Path | None) -> Path:
    if override is not None:
        return _resolve_path(override, repository)
    expected = (scenario_spec.get("workspace_contract") or {}).get("expected_scenario_spec_path")
    if expected:
        return _resolve_path(expected, repository)
    return _resolve_path(Path("outputs") / "scenario_specs" / f"{scenario_spec['scenario_spec_id']}.json", repository)


def container_to_host_path(path: str | Path, repository: Any) -> str:
    text = str(path).replace("\\", "/")
    config = isaac_host_runtime_config(repository)
    container_root = config["container_project_root"]
    host_root = config["host_project_root"]
    if host_root and text.startswith(container_root + "/"):
        suffix = text[len(container_root) + 1 :].replace("/", "\\")
        if ":" in host_root or "\\" in host_root:
            return str(PureWindowsPath(host_root) / suffix)
        return str(Path(host_root) / suffix)
    if host_root and not Path(str(path)).is_absolute():
        if ":" in host_root or "\\" in host_root:
            return str(PureWindowsPath(host_root) / str(path))
        return str(Path(host_root) / str(path))
    return str(path)


def host_to_container_path(path: str | Path, repository: Any) -> str:
    config = isaac_host_runtime_config(repository)
    host_root = config["host_project_root"]
    container_root = config["container_project_root"]
    if not host_root:
        return str(path)
    text = str(path)
    host_text = str(host_root)
    if text.lower().startswith(host_text.lower()):
        suffix = text[len(host_text) :].lstrip("\\/").replace("\\", "/")
        return f"{container_root}/{suffix}"
    return str(path)


def _enabled_line_count(scenario_spec: dict[str, Any]) -> int:
    bindings = scenario_spec.get("line_bindings") or []
    enabled = [
        binding
        for binding in bindings
        if isinstance(binding, dict) and binding.get("enabled", True) is not False
    ]
    return len(enabled)


def _simulation_scope_lines(scenario_spec: dict[str, Any]) -> list[str]:
    scope = scenario_spec.get("simulation_scope") or {}
    if isinstance(scope, dict) and isinstance(scope.get("lines"), list):
        return [str(line_id) for line_id in scope["lines"] if line_id]
    return []


def _tool_count(scenario_spec: dict[str, Any]) -> int:
    catalog = scenario_spec.get("tool_catalog") or {}
    return len(catalog)


def _bool_config(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _chosen_intervention_mode(scenario_spec: dict[str, Any], config: dict[str, Any]) -> str:
    configured = config.get("chosen_intervention_mode")
    if configured:
        return str(configured)
    abnormal_policy = scenario_spec.get("abnormal_event_policy") or {}
    if abnormal_policy.get("chosen_intervention_mode"):
        return str(abnormal_policy["chosen_intervention_mode"])
    entanglement_policy = abnormal_policy.get("entanglement") or {}
    if entanglement_policy.get("chosen_intervention_mode"):
        return str(entanglement_policy["chosen_intervention_mode"])
    policies = scenario_spec.get("line_policies") or []
    strategies = {policy.get("abnormal_strategy") for policy in policies if isinstance(policy, dict)}
    if "STOP_LINE" in strategies and "CONTINUE_FEASIBLE_TASKS" not in strategies:
        return "immediate-stop"
    return "continue-until-arrival"


def _value_with_source(
    *,
    source: dict[str, Any],
    key: str,
    source_name: str,
    fallback: Any,
    fallback_source: str,
    transform=lambda value: value,
) -> tuple[Any, str]:
    if key in source and source.get(key) is not None:
        return transform(source[key]), f"{source_name}.{key}"
    return transform(fallback), fallback_source


def build_isaac_command_args_from_scenario_spec(scenario_spec: dict[str, Any]) -> dict[str, Any]:
    return build_isaac_command_args_with_sources(scenario_spec)["command_args"]


def build_isaac_command_args_with_sources(scenario_spec: dict[str, Any]) -> dict[str, Any]:
    config = scenario_spec.get("simulation_config") or {}
    operator_model = scenario_spec.get("operator_model") or {}
    args: dict[str, Any] = {}
    sources: dict[str, str] = {}
    internal_dev_args_enabled = os.environ.get(INTERNAL_DEV_ARG_FLAG, "").strip().lower() in {"1", "true", "yes", "y"}

    simulation_lines = _simulation_scope_lines(scenario_spec)
    if simulation_lines:
        args["num_envs"] = len(simulation_lines)
        sources["num_envs"] = "scenario_spec.simulation_scope.lines"
    elif config.get("num_envs") is not None:
        args["num_envs"] = int(config["num_envs"])
        sources["num_envs"] = "scenario_spec.simulation_config.num_envs"
    else:
        line_count = _enabled_line_count(scenario_spec)
        args["num_envs"] = int(line_count or 1)
        sources["num_envs"] = "scenario_spec.line_bindings_count" if line_count else ISAAC_COMMAND_DEFAULT_SOURCE

    args["headless"], sources["headless"] = _value_with_source(
        source=config,
        key="headless",
        source_name="scenario_spec.simulation_config",
        fallback=False,
        fallback_source="scenario_default",
        transform=lambda value: _bool_config({"value": value}, "value", False),
    )

    if config.get("global_seed") is not None:
        args["global_seed"] = int(config["global_seed"])
        sources["global_seed"] = "scenario_spec.simulation_config.global_seed"
    else:
        sources["global_seed"] = "omitted: no operator override"

    if internal_dev_args_enabled and config.get("max_seed_trials") is not None:
        args["max_seed_trials"] = int(config["max_seed_trials"])
        sources["max_seed_trials"] = "scenario_spec.simulation_config.max_seed_trials"
    else:
        sources["max_seed_trials"] = "omitted: restricted"

    args["allowed_overlap_ratio"], sources["allowed_overlap_ratio"] = _value_with_source(
        source=config,
        key="allowed_overlap_ratio",
        source_name="scenario_spec.simulation_config",
        fallback=0.99,
        fallback_source="scenario_default",
        transform=float,
    )
    args["layout_source"], sources["layout_source"] = _value_with_source(
        source=config,
        key="layout_source",
        source_name="scenario_spec.simulation_config",
        fallback="auto",
        fallback_source="scenario_default",
        transform=str,
    )
    args["episode_success_requires_reset_cycles"], sources["episode_success_requires_reset_cycles"] = _value_with_source(
        source=config,
        key="episode_success_requires_reset_cycles",
        source_name="scenario_spec.simulation_config",
        fallback=1,
        fallback_source="scenario_default",
        transform=int,
    )

    if args.get("global_seed") is not None:
        args["reuse_verified_seed"] = False
        sources["reuse_verified_seed"] = "default_false_global_seed_present"
    elif "reuse_verified_seed" in config and config.get("reuse_verified_seed") is False:
        args["reuse_verified_seed"] = _bool_config(config, "reuse_verified_seed", True)
        sources["reuse_verified_seed"] = "scenario_spec.simulation_config.reuse_verified_seed"
    else:
        args["reuse_verified_seed"] = True
        sources["reuse_verified_seed"] = "default_true_no_global_seed"

    if internal_dev_args_enabled and config.get("reuse_precomputed_layouts") is not None:
        args["reuse_precomputed_layouts"] = _bool_config(config, "reuse_precomputed_layouts", False)
        sources["reuse_precomputed_layouts"] = "scenario_spec.simulation_config.reuse_precomputed_layouts"
    else:
        sources["reuse_precomputed_layouts"] = "omitted: restricted"

    if config.get("chosen_intervention_mode"):
        args["chosen_intervention_mode"] = str(config["chosen_intervention_mode"])
        sources["chosen_intervention_mode"] = "scenario_spec.simulation_config.chosen_intervention_mode"
    else:
        abnormal_policy = scenario_spec.get("abnormal_event_policy") or {}
        entanglement_policy = abnormal_policy.get("entanglement") or {}
        args["chosen_intervention_mode"] = _chosen_intervention_mode(scenario_spec, config)
        if abnormal_policy.get("chosen_intervention_mode"):
            sources["chosen_intervention_mode"] = "scenario_spec.abnormal_event_policy.chosen_intervention_mode"
        elif entanglement_policy.get("chosen_intervention_mode"):
            sources["chosen_intervention_mode"] = "scenario_spec.abnormal_event_policy.entanglement.chosen_intervention_mode"
        elif scenario_spec.get("line_policies"):
            sources["chosen_intervention_mode"] = "scenario_spec.line_policies.abnormal_strategy"
        else:
            sources["chosen_intervention_mode"] = "scenario_default"

    for key, fallback in (("travel_time", 5.0), ("fix_duration", 8.0), ("resume_delay", 0.5)):
        if key in config and config.get(key) is not None:
            args[key] = float(config[key])
            sources[key] = f"scenario_spec.simulation_config.{key}"
        else:
            args[key], sources[key] = _value_with_source(
                source=operator_model,
                key=key,
                source_name="scenario_spec.operator_model",
                fallback=fallback,
                fallback_source="operator_model_default",
                transform=float,
            )

    if config.get("add_reference_number") is not None:
        args["add_reference_number"] = int(config["add_reference_number"])
        sources["add_reference_number"] = "scenario_spec.simulation_config.add_reference_number"
    else:
        tool_count = _tool_count(scenario_spec)
        args["add_reference_number"] = int(tool_count or 27)
        sources["add_reference_number"] = "len(scenario_spec.tool_catalog)" if tool_count else ISAAC_COMMAND_DEFAULT_SOURCE

    sources["seed_db_path"] = "omitted: restricted"
    return {"command_args": omit_none_values(args), "resolved_from": sources}


def build_isaac_command(
    scenario_spec: dict[str, Any],
    repository: Any,
    *,
    scenario_spec_path: str | Path | None = None,
    output_db_path: str | Path | None = None,
    run_id: str | None = None,
    headless: bool = True,
    line_id: str | None = None,
    max_steps: int | None = None,
    validate_script_path: bool = True,
) -> dict[str, Any]:
    """Return a host-runner request, not a Docker-executable command.

    The Dockerized API must not run Windows Isaac Sim paths directly. The
    Windows host runner expands ``host_request`` into the actual subprocess
    command using Isaac Sim's ``python.bat``.
    """

    _ = (headless, line_id, max_steps, validate_script_path)
    run_id = run_id or f"sim_{uuid4()}"
    spec_path = _scenario_spec_path(scenario_spec, repository, scenario_spec_path)
    if output_db_path is None:
        output_dir = _resolve_path(
            (scenario_spec.get("workspace_contract") or {}).get("run_artifacts_dir", "outputs/run_artifacts"),
            repository,
        )
        output_db_path = output_dir / f"{run_id}.sqlite"
    db_path = _resolve_path(output_db_path, repository)
    validation_errors = validate_scenario_spec_for_isaac(
        scenario_spec,
        isaac_script_path=None,
        output_db_path=db_path,
    )
    runtime_config = isaac_host_runtime_config(repository)
    command_arg_resolution = build_isaac_command_args_with_sources(scenario_spec)
    command_args = command_arg_resolution["command_args"]
    resolved_from = command_arg_resolution["resolved_from"]
    expected_args = (scenario_spec.get("governance_metadata") or {}).get("expected_command_args") or {}
    for key in ("num_envs", "chosen_intervention_mode", "travel_time", "fix_duration", "resume_delay", "add_reference_number"):
        if key not in expected_args or expected_args.get(key) is None:
            continue
        actual = command_args.get(key)
        expected = expected_args[key]
        if isinstance(expected, float) or isinstance(actual, float):
            mismatch = abs(float(actual) - float(expected)) > 1e-9
        else:
            mismatch = actual != expected
        if mismatch:
            validation_errors.append(
                "ScenarioSpec compilation failed: requested "
                f"{key}={expected!r} but Isaac command resolved {key}={actual!r}."
            )
    if not command_args.get("seed_db_path") and runtime_config.get("seed_db_path"):
        command_args["seed_db_path"] = runtime_config["seed_db_path"]
        resolved_from["seed_db_path"] = "host_config.seed_db_path"
    command_args = omit_none_values(command_args)
    host_request = {
        "scenario_spec_id": scenario_spec.get("scenario_spec_id"),
        "scenario_spec_path": container_to_host_path(spec_path, repository),
        "run_id": run_id,
        "output_db_path": container_to_host_path(db_path, repository),
        "working_directory": runtime_config["isaac_working_directory"],
        "python_bat": runtime_config["python_bat"],
        "entry_script": runtime_config["entry_script"],
        "command_args": command_args,
    }
    host_runner_url = os.environ.get("ISAAC_HOST_RUNNER_URL")
    return {
        "execution_mode": os.environ.get("ISAAC_EXECUTION_MODE", "host_runner"),
        "host_runner_url": host_runner_url,
        "run_id": run_id,
        "scenario_spec_id": scenario_spec.get("scenario_spec_id"),
        "scenario_spec_path": str(spec_path),
        "container_scenario_spec_path": str(spec_path),
        "host_scenario_spec_path": host_request["scenario_spec_path"],
        "output_db_path": str(db_path),
        "container_output_db_path": str(db_path),
        "host_output_db_path": host_request["output_db_path"],
        "host_request": host_request,
        "host_runtime_config": runtime_config,
        "command_args": command_args,
        "arg_provenance": resolved_from,
        "resolved_from": resolved_from,
        "validation_errors": validation_errors,
    }
