"""LLM candidate generation and deterministic evidence-based strategy selection."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator

from trt_core.repository import TRTRepository
from trt_core.time_arrival_state import load_time_arrival_state


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "candidate_strategy_batch.schema.json"
OBJECTIVE_PATH = PROJECT_ROOT / "data" / "strategy_selection" / "default_objective.json"
PROMPT_PROFILES_PATH = (
    PROJECT_ROOT / "data" / "strategy_selection" / "candidate_prompt_profiles.json"
)
DEPLOYED_SIMULATION_DEFAULTS_PATH = (
    PROJECT_ROOT / "data" / "digital_twin" / "default_simulation_config.json"
)
ALLOWED_LINE_OVERRIDE_FIELDS = {"manipulator_priority", "abnormal_strategy"}
ALLOWED_SIMULATION_OVERRIDE_FIELDS = {"chosen_intervention_mode"}
SAMPLING_PARAMETER_NAMES = {
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def strategy_prompt_version() -> str:
    return "candidate-strategy-v1.4"


def candidate_generation_schema() -> dict[str, Any]:
    return _load_json(CANDIDATE_SCHEMA_PATH)


def candidate_generation_grammar_schema(
    *,
    valid_line_ids: list[str] | None = None,
    candidate_count: int | None = None,
) -> dict[str, Any]:
    """Return the candidate schema subset supported by the vLLM grammar backend.

    The canonical schema remains unchanged and is applied after generation. In
    particular, array uniqueness is still enforced by deterministic validation.
    """

    unsupported_keys = {"$schema", "uniqueItems"}

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: scrub(item)
                for key, item in value.items()
                if key not in unsupported_keys
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    schema = scrub(candidate_generation_schema())
    candidates = schema["properties"]["candidates"]
    if candidate_count is not None:
        candidates["minItems"] = candidate_count
        candidates["maxItems"] = candidate_count
    candidates["items"]["properties"]["rationale"]["maxLength"] = 300
    if valid_line_ids:
        line_overrides = candidates["items"]["properties"]["line_policy_overrides"]
        override_schema = line_overrides.pop("additionalProperties")
        line_overrides["properties"] = {
            line_id: deepcopy(override_schema)
            for line_id in sorted(valid_line_ids)
        }
        line_overrides["additionalProperties"] = False
    return schema


def load_selection_objective() -> dict[str, Any]:
    objective = _load_json(OBJECTIVE_PATH)
    expected_components = {"throughput_attainment"}
    weights = objective.get("weights")
    if not isinstance(weights, dict) or set(weights) != expected_components:
        raise ValueError(
            f"Selection objective weights must contain exactly {sorted(expected_components)}."
        )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or float(value) < 0
        for value in weights.values()
    ):
        raise ValueError("Selection objective weights must be non-negative numbers.")
    if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-9:
        raise ValueError("Selection objective weights must sum to 1.0.")
    return objective


def load_candidate_prompt_profiles(exploratory_candidate_count: int) -> dict[str, Any]:
    profiles = _load_json(PROMPT_PROFILES_PATH)
    rigid = profiles.get("rigid_profile")
    exploratory = profiles.get("exploratory_profiles")
    if not isinstance(rigid, dict) or not str(rigid.get("prompt_instruction") or "").strip():
        raise ValueError("Candidate prompt profiles require a rigid_profile prompt_instruction.")
    if not isinstance(exploratory, list) or len(exploratory) < exploratory_candidate_count:
        raise ValueError(
            f"Candidate prompt profiles require at least {exploratory_candidate_count} "
            "exploratory profiles."
        )
    selected = deepcopy(exploratory[:exploratory_candidate_count])
    for profile in selected:
        if not isinstance(profile, dict) or not str(profile.get("prompt_instruction") or "").strip():
            raise ValueError("Every exploratory profile requires a prompt_instruction.")
    return {
        "profile_version": profiles.get("profile_version"),
        "rigid_profile": deepcopy(rigid),
        "exploratory_profiles": selected,
    }


def load_deployed_simulation_config(repository: TRTRepository) -> dict[str, Any]:
    path = repository.root / "data" / "digital_twin" / "default_simulation_config.json"
    if not path.exists():
        path = DEPLOYED_SIMULATION_DEFAULTS_PATH
    if not path.exists():
        return {}
    payload = _load_json(path)
    config = payload.get("simulation_config")
    return deepcopy(config) if isinstance(config, dict) else {}


def locked_line_policy_fields_from_release(
    release_record: dict[str, Any],
    released_trt: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Recover line-policy fields explicitly approved by the operator."""

    locked: dict[str, dict[str, Any]] = {}
    patch = release_record.get("candidate_patch") or {}
    for operation in patch.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        parts = [part for part in str(operation.get("path") or "").split("/") if part]
        if len(parts) < 3 or parts[0] != "lines":
            continue
        line_id, field = parts[1], parts[2]
        if field not in ALLOWED_LINE_OVERRIDE_FIELDS:
            continue
        line = (released_trt.get("lines") or {}).get(line_id)
        if not isinstance(line, dict) or field not in line:
            continue
        locked.setdefault(line_id, {})[field] = deepcopy(line[field])
    return locked


def _post_json(url: str, body: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    if SAMPLING_PARAMETER_NAMES.intersection(body):
        raise ValueError("Sampling parameters must not be sent by this client.")
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if len(detail) > 2000:
            detail = f"{detail[:2000]}..."
        raise OSError(
            f"Model server returned HTTP {exc.code}"
            + (f": {detail}" if detail else ".")
        ) from exc


def _parse_json_object_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError("LLM candidate output must be a JSON object or JSON string.")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM candidate JSON output must be an object.")
    return value


def _candidate_prompt(
    *,
    released_trt: dict[str, Any],
    reconciliation_plan: dict[str, Any],
    state_records: list[dict[str, Any]],
    time_arrival_state: dict[str, Any],
    candidate_count: int,
    locked_simulation_config: dict[str, Any],
    locked_line_policy_fields: dict[str, dict[str, Any]],
    base_simulation_config: dict[str, Any],
    candidate_line_ids: set[str] | None = None,
    operator_faithful_baseline: dict[str, Any] | None = None,
    prompt_profiles: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    locked_mode = locked_simulation_config.get("chosen_intervention_mode")
    required_abnormal_strategy = {
        "immediate-stop": "STOP_LINE",
        "continue-until-arrival": "CONTINUE_FEASIBLE_TASKS",
    }.get(locked_mode)
    affected_lines = reconciliation_plan.get("affected_lines") or []
    permitted_line_ids = sorted(
        candidate_line_ids
        or set(affected_lines)
        or set((released_trt.get("lines") or {}).keys())
    )
    context = {
        "trt_id": released_trt["trt_id"],
        "trt_version": released_trt["version"],
        "affected_lines": affected_lines,
        "line_decisions": reconciliation_plan.get("line_decisions") or [],
        "line_policies": {
            line_id: {
                "goal": line.get("goal"),
                "kpi": line.get("kpi"),
                "target_set_id": line.get("target_set_id"),
                "manipulator_priority": line.get("manipulator_priority"),
                "abnormal_strategy": line.get("abnormal_strategy"),
            }
            for line_id, line in sorted((released_trt.get("lines") or {}).items())
            if line_id in permitted_line_ids
        },
        "aligned_state_records": sorted(
            (
                deepcopy(record)
                for record in state_records
                if isinstance(record, dict)
                and record.get("line_id") in permitted_line_ids
            ),
            key=lambda record: str(record.get("line_id") or ""),
        ),
        "time_arrival_state": {
            field: time_arrival_state[field]
            for field in ("travel_time", "fix_duration", "resume_delay", "state_version", "updated_at_utc")
        },
        "locked_simulation_config": locked_simulation_config,
        "locked_line_policy_fields": locked_line_policy_fields,
        "deployed_simulation_config": {
            key: base_simulation_config.get(key)
            for key in ("chosen_intervention_mode",)
        },
        "exploratory_candidate_count": candidate_count,
        "operator_faithful_baseline": operator_faithful_baseline or {},
        "candidate_prompt_profiles": prompt_profiles or {},
        "candidate_generation_constraints": {
            "permitted_line_ids": permitted_line_ids,
            "chosen_intervention_mode_is_operator_locked": locked_mode is not None,
            "required_chosen_intervention_mode": locked_mode,
            "required_effective_abnormal_strategy": required_abnormal_strategy,
            "lines_requiring_that_abnormal_strategy": (
                permitted_line_ids if required_abnormal_strategy else []
            ),
            "permitted_sources_of_candidate_diversity": (
                ["line_policy_overrides.manipulator_priority"]
                if locked_mode is not None
                else [
                    "line_policy_overrides.manipulator_priority",
                    "line_policy_overrides.abnormal_strategy",
                    "simulation_config_overrides.chosen_intervention_mode",
                ]
            ),
        },
        "required_output_contract": {
            "candidates": [
                {
                    "candidate_strategy_id": "unique safe identifier beginning with a letter",
                    "name": "short strategy name",
                    "rationale": "one sentence",
                    "line_policy_overrides": {
                        "<valid_line_id_or_omit_entry>": {
                            "abnormal_strategy": "STOP_LINE or CONTINUE_FEASIBLE_TASKS",
                            "manipulator_priority": {
                                "policy": "FCFS, REQUIRED_FIRST, or EXPLICIT_TYPE_ORDER",
                                "ordered_tool_ids": [],
                                "ordered_normalized_types": [],
                                "tie_breaker": "FCFS",
                                "enabled": True,
                            },
                        }
                    },
                    "simulation_config_overrides": {
                        "chosen_intervention_mode": (
                            "immediate-stop or continue-until-arrival"
                        )
                    },
                }
            ]
        },
    }
    system = (
        "Generate distinct executable candidate strategies for an Isaac Sim comparison. "
        "Return only a JSON object matching required_output_contract; do not use Markdown. Every candidate "
        "must include candidate_strategy_id, name, rationale, line_policy_overrides, and "
        "simulation_config_overrides, even when an override object is empty. Preserve every operator "
        "constraint, KPI target, "
        "target line, tooling target, and locked Time-Arrival value. Never invent lines, tools, tasks, or KPIs. "
        "Candidates may differ only in line_policy_overrides.manipulator_priority, "
        "line_policy_overrides.abnormal_strategy, and simulation_config_overrides.chosen_intervention_mode. "
        "Do not change a locked field. The operator_faithful_baseline is an immutable candidate created from "
        "the released TRT and operator-locked settings. Return it exactly as supplied as the first candidate; "
        "do not rename, paraphrase, or modify it. Then generate meaningful exploratory alternatives. "
        "If candidate_generation_constraints says chosen_intervention_mode is operator-locked, every candidate "
        "must preserve that exact mode; omit chosen_intervention_mode from simulation_config_overrides or repeat "
        "the locked value, and create diversity only through manipulator-priority overrides. Every listed line "
        "must explicitly use required_effective_abnormal_strategy when its deployed strategy differs. "
        "Use rigid_profile.prompt_instruction for the required first candidate. For each remaining candidate, "
        "use the corresponding exploratory_profiles prompt_instruction to shape its behavior. Prompt profiles "
        "control strategy-generation style, but never override operator locks or deterministic constraints. "
        "After the required baseline, return exactly exploratory_candidate_count alternatives that are "
        "behaviorally distinct from each other and from operator_faithful_baseline. Keep each candidate compact: omit a line "
        "from line_policy_overrides when its deployed policy is unchanged. Keys inside line_policy_overrides must be "
        "IDs listed in candidate_generation_constraints.permitted_line_ids. TRT changes may affect more lines "
        "than the current simulation scope; never add overrides for those unsimulated lines. If "
        "chosen_intervention_mode is "
        "immediate-stop, every affected line's effective abnormal_strategy must be STOP_LINE. If it is "
        "continue-until-arrival, every affected line's effective abnormal_strategy must be "
        "CONTINUE_FEASIBLE_TASKS. Use a one-sentence rationale. "
        "Candidate IDs are opaque internal labels; use unique identifiers containing only letters, digits, "
        "underscores, or hyphens. "
        "Do not rank or claim that a strategy is feasible; Isaac evidence performs that decision."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(context, sort_keys=True)},
    ]


def _operator_faithful_baseline(
    *,
    valid_line_ids: set[str],
    locked_simulation_config: dict[str, Any],
    base_simulation_config: dict[str, Any],
    base_line_policies: dict[str, dict[str, Any]],
    locked_line_policy_fields: dict[str, dict[str, Any]],
    rigid_profile: dict[str, Any],
) -> dict[str, Any]:
    """Create the non-speculative candidate from approved state and locked settings."""

    effective_mode = (
        locked_simulation_config.get("chosen_intervention_mode")
        or base_simulation_config.get("chosen_intervention_mode")
    )
    required_strategy = {
        "immediate-stop": "STOP_LINE",
        "continue-until-arrival": "CONTINUE_FEASIBLE_TASKS",
    }.get(effective_mode)
    line_overrides: dict[str, dict[str, Any]] = {}
    if required_strategy:
        for line_id in sorted(valid_line_ids):
            deployed_strategy = (base_line_policies.get(line_id) or {}).get("abnormal_strategy")
            locked_strategy = (locked_line_policy_fields.get(line_id) or {}).get("abnormal_strategy")
            if locked_strategy is not None and locked_strategy != required_strategy:
                raise ValueError(
                    f"Operator-locked {line_id}.abnormal_strategy conflicts with "
                    f"chosen_intervention_mode={effective_mode}."
                )
            if deployed_strategy != required_strategy:
                line_overrides[line_id] = {"abnormal_strategy": required_strategy}
    return {
        "candidate_strategy_id": "operator_faithful_baseline",
        "name": str(rigid_profile.get("candidate_name") or "Operator-faithful baseline"),
        "rationale": str(
            rigid_profile.get("candidate_rationale")
            or "Applies the released TRT and operator-locked simulation settings without speculative changes."
        ),
        "line_policy_overrides": line_overrides,
        "simulation_config_overrides": {},
    }


def _validate_candidate_batch(
    batch: dict[str, Any],
    *,
    valid_line_ids: set[str],
    candidate_count: int,
    locked_simulation_config: dict[str, Any],
    base_simulation_config: dict[str, Any],
    base_line_policies: dict[str, dict[str, Any]],
    locked_line_policy_fields: dict[str, dict[str, Any]],
) -> None:
    errors = sorted(
        Draft202012Validator(candidate_generation_schema()).iter_errors(batch),
        key=lambda error: list(error.path),
    )
    if errors:
        first = errors[0]
        path = "/".join(str(part) for part in first.path) or "<root>"
        raise ValueError(f"Invalid candidate batch at {path}: {first.message}")
    candidates = batch["candidates"]
    if len(candidates) != candidate_count:
        raise ValueError(f"Expected exactly {candidate_count} candidates, received {len(candidates)}.")
    ids = [str(candidate["candidate_strategy_id"]) for candidate in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("Candidate strategy IDs must be unique.")
    fingerprints: set[str] = set()
    for candidate in candidates:
        line_overrides = candidate.get("line_policy_overrides") or {}
        unknown_lines = sorted(set(line_overrides) - valid_line_ids)
        if unknown_lines:
            raise ValueError(f"Candidate references unknown lines: {unknown_lines}.")
        for line_id, override in line_overrides.items():
            unknown_fields = sorted(set(override) - ALLOWED_LINE_OVERRIDE_FIELDS)
            if unknown_fields:
                raise ValueError(f"Unsupported line override fields for {line_id}: {unknown_fields}.")
            for field, locked_value in (locked_line_policy_fields.get(line_id) or {}).items():
                if field in override and override[field] != locked_value:
                    raise ValueError(
                        f"Candidate {candidate['candidate_strategy_id']} changes operator-locked "
                        f"{line_id}.{field}."
                    )
        sim_overrides = candidate.get("simulation_config_overrides") or {}
        unknown_sim = sorted(set(sim_overrides) - ALLOWED_SIMULATION_OVERRIDE_FIELDS)
        if unknown_sim:
            raise ValueError(f"Unsupported simulation override fields: {unknown_sim}.")
        for field, locked_value in locked_simulation_config.items():
            if field in sim_overrides and sim_overrides[field] != locked_value:
                raise ValueError(f"Candidate {candidate['candidate_strategy_id']} changes locked field {field}.")
        effective_mode = (
            sim_overrides.get("chosen_intervention_mode")
            or locked_simulation_config.get("chosen_intervention_mode")
            or base_simulation_config.get("chosen_intervention_mode")
        )
        mode_strategy = {
            "immediate-stop": "STOP_LINE",
            "continue-until-arrival": "CONTINUE_FEASIBLE_TASKS",
        }.get(effective_mode)
        if mode_strategy:
            mismatched_lines = []
            for line_id in valid_line_ids:
                effective_strategy = (
                    (line_overrides.get(line_id) or {}).get("abnormal_strategy")
                    or (base_line_policies.get(line_id) or {}).get("abnormal_strategy")
                )
                if effective_strategy != mode_strategy:
                    mismatched_lines.append(line_id)
            if mismatched_lines:
                raise ValueError(
                    f"Candidate {candidate['candidate_strategy_id']} uses {effective_mode} but "
                    f"does not use {mode_strategy} on lines {sorted(mismatched_lines)}."
                )
        effective_lines = {
            line_id: {
                field: deepcopy(
                    (line_overrides.get(line_id) or {}).get(
                        field,
                        (base_line_policies.get(line_id) or {}).get(field),
                    )
                )
                for field in sorted(ALLOWED_LINE_OVERRIDE_FIELDS)
            }
            for line_id in sorted(valid_line_ids)
        }
        effective_simulation = {
            "chosen_intervention_mode": (
                sim_overrides.get("chosen_intervention_mode")
                or locked_simulation_config.get("chosen_intervention_mode")
                or base_simulation_config.get("chosen_intervention_mode")
            )
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {"line": effective_lines, "simulation": effective_simulation},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if fingerprint in fingerprints:
            raise ValueError("Candidate strategies must be behaviorally distinct.")
        fingerprints.add(fingerprint)


def generate_candidate_batch(
    *,
    repository: TRTRepository,
    released_trt: dict[str, Any],
    reconciliation_plan: dict[str, Any],
    state_records: list[dict[str, Any]] | None = None,
    candidate_count: int = 3,
    locked_simulation_config: dict[str, Any] | None = None,
    locked_line_policy_fields: dict[str, dict[str, Any]] | None = None,
    simulation_line_ids: list[str] | None = None,
    post_json: Any | None = None,
) -> dict[str, Any]:
    if not 2 <= candidate_count <= 8:
        raise ValueError("candidate_count must be between 2 and 8.")
    time_state = load_time_arrival_state(repository)
    base_simulation_config = load_deployed_simulation_config(repository)
    locked = deepcopy(locked_simulation_config or {})
    locked_line_fields = deepcopy(locked_line_policy_fields or {})
    for field in ("travel_time", "fix_duration", "resume_delay"):
        locked.setdefault(field, time_state[field])
    affected_line_ids = set(
        reconciliation_plan.get("affected_lines")
        or (released_trt.get("lines") or {}).keys()
    )
    valid_line_ids = affected_line_ids
    if simulation_line_ids:
        valid_line_ids = affected_line_ids.intersection(simulation_line_ids)
        if not valid_line_ids:
            raise ValueError(
                "Candidate simulation scope does not contain any affected production line."
            )
    base_line_policies = {
        line_id: {
            "abnormal_strategy": line.get("abnormal_strategy"),
            "manipulator_priority": deepcopy(line.get("manipulator_priority")),
        }
        for line_id, line in (released_trt.get("lines") or {}).items()
    }
    exploratory_candidate_count = candidate_count - 1
    prompt_profiles = load_candidate_prompt_profiles(exploratory_candidate_count)
    baseline_candidate = _operator_faithful_baseline(
        valid_line_ids=valid_line_ids,
        locked_simulation_config=locked,
        base_simulation_config=base_simulation_config,
        base_line_policies=base_line_policies,
        locked_line_policy_fields=locked_line_fields,
        rigid_profile=prompt_profiles["rigid_profile"],
    )
    messages = _candidate_prompt(
        released_trt=released_trt,
        reconciliation_plan=reconciliation_plan,
        state_records=list(state_records or []),
        time_arrival_state=time_state,
        candidate_count=exploratory_candidate_count,
        operator_faithful_baseline=baseline_candidate,
        locked_simulation_config=locked,
        locked_line_policy_fields=locked_line_fields,
        base_simulation_config=base_simulation_config,
        candidate_line_ids=valid_line_ids,
        prompt_profiles=prompt_profiles,
    )
    model = os.getenv("VLLM_MODEL", "cyankiwi/gemma-4-26B-A4B-it-AWQ-8bit")
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": int(os.getenv("STRATEGY_CANDIDATE_MAX_TOKENS", "4000")),
        "chat_template_kwargs": {"enable_thinking": False},
    }
    started = now_utc()
    endpoint = os.getenv(
        "VLLM_CHAT_COMPLETIONS_URL",
        "http://192.168.50.168:29987/v1/chat/completions",
    )
    max_attempts = max(1, int(os.getenv("STRATEGY_CANDIDATE_GENERATION_ATTEMPTS", "3")))
    attempts: list[dict[str, Any]] = []
    raw: dict[str, Any] | None = None
    accepted_candidates: list[dict[str, Any]] = [baseline_candidate]
    model_confirmed_baseline = False
    retry_messages = list(messages)
    for attempt_number in range(1, max_attempts + 1):
        attempt_started = now_utc()
        try:
            attempt_body = {**body, "messages": retry_messages}
            raw = (post_json or _post_json)(
                endpoint,
                attempt_body,
                float(os.getenv("STRATEGY_CANDIDATE_TIMEOUT_SECONDS", "120")),
            )
            content = raw.get("choices", [{}])[0].get("message", {}).get("content", raw)
            parsed = _parse_json_object_content(content)
            proposed = parsed.get("candidates")
            if not isinstance(proposed, list):
                raise ValueError("LLM candidate output must contain a candidates array.")
            rejected: list[str] = []
            for candidate in proposed:
                if (
                    isinstance(candidate, dict)
                    and candidate.get("candidate_strategy_id")
                    == baseline_candidate["candidate_strategy_id"]
                ):
                    if candidate == baseline_candidate:
                        model_confirmed_baseline = True
                    else:
                        rejected.append("The model modified the immutable operator-faithful baseline.")
                    continue
                if len(accepted_candidates) >= candidate_count:
                    break
                try:
                    trial = {"candidates": [*accepted_candidates, candidate]}
                    _validate_candidate_batch(
                        trial,
                        valid_line_ids=valid_line_ids,
                        candidate_count=len(trial["candidates"]),
                        locked_simulation_config=locked,
                        base_simulation_config=base_simulation_config,
                        base_line_policies=base_line_policies,
                        locked_line_policy_fields=locked_line_fields,
                    )
                    accepted_candidates.append(candidate)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    rejected.append(str(exc))
            if len(accepted_candidates) < candidate_count:
                reason = "; ".join(rejected) or "too few valid exploratory candidates"
                raise ValueError(
                    f"Accepted {len(accepted_candidates) - 1} of {exploratory_candidate_count} "
                    f"exploratory candidates: {reason}"
                )
            attempts.append(
                {
                    "attempt": attempt_number,
                    "started_at_utc": attempt_started,
                    "completed_at_utc": now_utc(),
                    "status": "VALID",
                    "reasoning_feature_used": bool(
                        raw.get("choices", [{}])[0].get("message", {}).get("reasoning")
                    ),
                }
            )
            break
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            attempts.append(
                {
                    "attempt": attempt_number,
                    "started_at_utc": attempt_started,
                    "completed_at_utc": now_utc(),
                    "status": "INVALID_OUTPUT",
                    "validation_error": str(exc),
                }
            )
            retry_messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "The previous candidate batch failed deterministic validation: "
                        f"{exc}. Generate the remaining valid exploratory alternatives, preserve all "
                        "operator-locked fields, and return the supplied operator-faithful baseline exactly. "
                        "Return JSON only."
                    ),
                },
            ]
    completed = now_utc()
    complete = len(accepted_candidates) == candidate_count
    batch = {
        "strategy_batch_id": f"strategy_batch_{uuid4()}",
        "status": "GENERATED" if complete else "GENERATED_PARTIAL",
        "trt_id": released_trt["trt_id"],
        "trt_version": released_trt["version"],
        "reconciliation_plan_id": reconciliation_plan["plan_id"],
        "candidate_count": len(accepted_candidates),
        "requested_candidate_count": candidate_count,
        "candidates": accepted_candidates,
        "locked_simulation_config": locked,
        "locked_line_policy_fields": locked_line_fields,
        "base_simulation_config": base_simulation_config,
        "time_arrival_state": time_state,
        "aligned_state_records": deepcopy(list(state_records or [])),
        "simulation_line_ids": sorted(valid_line_ids),
        "selection_objective": load_selection_objective(),
        "generation_provenance": {
            "generated_by": "trt_core.strategy_selection.generate_candidate_batch",
            "model": model,
            "endpoint": endpoint,
            "prompt_version": strategy_prompt_version(),
            "attempt_count": len(attempts),
            "attempts": attempts,
            "sampling_parameters_sent": [],
            "server_sampling_configuration": "SERVER_PRESET_NOT_OVERRIDDEN",
            "started_at_utc": started,
            "completed_at_utc": completed,
            "usage": raw.get("usage") if raw else None,
            "baseline_candidate_source": "DETERMINISTIC_OPERATOR_FAITHFUL",
            "baseline_candidate_id": baseline_candidate["candidate_strategy_id"],
            "model_confirmed_baseline": model_confirmed_baseline,
            "exploratory_candidates_requested": exploratory_candidate_count,
            "exploratory_candidates_accepted": len(accepted_candidates) - 1,
            "exploratory_generation_status": "COMPLETE" if complete else "PARTIAL",
            "candidate_prompt_profiles": prompt_profiles,
        },
        "candidate_runs": [],
        "selection": None,
        "created_at_utc": started,
        "updated_at_utc": completed,
    }
    repository.save_strategy_batch(batch)
    return batch


def merge_candidate_simulation_config(batch: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(batch.get("base_simulation_config") or {})
    config.update(batch.get("locked_simulation_config") or {})
    config.update(candidate.get("simulation_config_overrides") or {})
    return config


def _explicit_bool(value: Any) -> bool | None:
    normalized = str(value).strip().lower()
    if value is True or value == 1 or normalized in {"true", "yes"}:
        return True
    if value is False or value == 0 or normalized in {"false", "no"}:
        return False
    return None


def _storage_rate(run_artifact: dict[str, Any]) -> tuple[float | None, int, int]:
    rows = (
        run_artifact.get("tool_storage_records")
        or run_artifact.get("placement_verification_records")
        or run_artifact.get("tool_events")
        or []
    )
    determinate: list[bool] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        has_placement = bool(
            row.get("actual_target")
            or row.get("placement_target")
            or row.get("container_type")
            or _explicit_bool(row.get("placed")) is True
        )
        if not has_placement:
            continue
        raw_value = (
            row.get("verification_passed")
            if row.get("verification_passed") is not None
            else row.get("placement_correct")
        )
        value = _explicit_bool(raw_value)
        if value is not None:
            determinate.append(value)
    if not determinate:
        return None, 0, 0
    passed = sum(determinate)
    return passed / len(determinate), len(determinate), passed


def _reset_rate(run_artifact: dict[str, Any]) -> tuple[float | None, int, int]:
    result = (
        run_artifact.get("run")
        or run_artifact.get("run_result")
        or run_artifact.get("summary")
        or {}
    )
    requested = result.get("reset_cycles_requested")
    completed = result.get("reset_cycles_completed")
    if not isinstance(requested, int) or requested <= 0 or not isinstance(completed, int):
        return None, int(requested or 0), int(completed or 0)
    return completed / requested, requested, completed


def _line_kpi_values(run_artifact: dict[str, Any], scenario_spec: dict[str, Any]) -> dict[str, Any]:
    target_by_line = {
        row["line_id"]: (row.get("kpi") or {}).get("min_throughput_per_hour")
        for row in scenario_spec.get("line_policies") or []
        if isinstance(row, dict) and row.get("line_id")
    }
    ratios: list[float] = []
    throughput_target_by_line: dict[str, float] = {}
    throughput_actual_by_line: dict[str, float] = {}
    throughput_attainment_by_line: dict[str, float] = {}
    throughput_below_target_lines: list[str] = []
    observed_throughput_lines: set[str] = set()
    priority_deviations = 0
    batch_violations = 0
    durations: list[float] = []
    for row in run_artifact.get("line_kpis") or []:
        if not isinstance(row, dict):
            continue
        line_id = row.get("line_id")
        target = target_by_line.get(line_id)
        actual = row.get("throughput_per_hour")
        if (
            isinstance(line_id, str)
            and isinstance(target, (int, float))
            and target > 0
            and isinstance(actual, (int, float))
        ):
            ratio = max(0.0, float(actual) / float(target))
            ratios.append(ratio)
            throughput_target_by_line[line_id] = float(target)
            throughput_actual_by_line[line_id] = float(actual)
            throughput_attainment_by_line[line_id] = ratio
            observed_throughput_lines.add(line_id)
            if float(actual) < float(target):
                throughput_below_target_lines.append(line_id)
        priority_deviations += int(row.get("priority_deviation_count") or 0)
        batch_violations += int(row.get("batch_gating_violation_count") or row.get("batch_gating_violation") or 0)
        duration = row.get("all_sorting_time_seconds") or row.get("all_sorting_duration_seconds")
        if isinstance(duration, (int, float)) and duration >= 0:
            durations.append(float(duration))
    expected_throughput_lines = {
        line_id
        for line_id, target in target_by_line.items()
        if isinstance(target, (int, float)) and target > 0
    }
    throughput_missing_lines = sorted(expected_throughput_lines - observed_throughput_lines)
    return {
        "throughput_attainment": sum(ratios) / len(ratios) if ratios else None,
        "throughput_target_by_line": throughput_target_by_line,
        "throughput_actual_by_line": throughput_actual_by_line,
        "throughput_attainment_by_line": throughput_attainment_by_line,
        "throughput_below_target_lines": sorted(throughput_below_target_lines),
        "throughput_missing_lines": throughput_missing_lines,
        "each_line_throughput_target_met": (
            bool(expected_throughput_lines)
            and not throughput_below_target_lines
            and not throughput_missing_lines
        ),
        "priority_deviation_count": priority_deviations,
        "batch_gating_violation_count": batch_violations,
        "strategy_simulation_seconds": max(durations) if durations else None,
    }


def candidate_measurements(
    *,
    run_artifact: dict[str, Any],
    scenario_spec: dict[str, Any],
    evidence_summary: dict[str, Any],
) -> dict[str, Any]:
    storage, storage_total, storage_passed = _storage_rate(run_artifact)
    reset, reset_requested, reset_completed = _reset_rate(run_artifact)
    values = _line_kpi_values(run_artifact, scenario_spec)
    evidence_rows = evidence_summary.get("kpi_table") or evidence_summary.get("line_results") or []
    priority_verdicts = [
        (str(row.get("line_id") or ""), row.get("priority_pass"))
        for row in evidence_rows
        if isinstance(row, dict) and isinstance(row.get("priority_pass"), bool)
    ]
    priority_failed_lines = sorted(
        line_id for line_id, passed in priority_verdicts if not passed and line_id
    )
    batch_verdicts = [
        (
            str(row.get("line_id") or ""),
            str((row.get("batch_gating") or {}).get("status") or "").upper(),
        )
        for row in evidence_rows
        if isinstance(row, dict)
        and isinstance(row.get("batch_gating"), dict)
        and (row.get("batch_gating") or {}).get("status") is not None
    ]
    batch_gating_failed_lines = sorted(
        line_id for line_id, status in batch_verdicts if status == "FAIL" and line_id
    )
    deployment_allowed = bool(
        evidence_summary.get("deployment_allowed")
        or (evidence_summary.get("deployment_recommendation") or {}).get("allowed")
    )
    missing = [
        name
        for name, value in {
            "R_storage": storage,
            "R_reset": reset,
            "throughput_attainment": values["throughput_attainment"],
        }.items()
        if value is None
    ]
    blocking = []
    if run_artifact.get("status") not in {"COMPLETED", "SUCCESS"}:
        blocking.append("RUN_ARTIFACT_NOT_COMPLETED")
    if not deployment_allowed:
        blocking.append("EVIDENCE_DISALLOWS_DEPLOYMENT")
    if storage is None:
        blocking.append("PLACEMENT_EVIDENCE_MISSING")
    elif storage < 1.0:
        blocking.append("PLACEMENT_VERIFICATION_FAILED")
    diagnostic_warnings = []
    if reset is None:
        diagnostic_warnings.append("RESET_EVIDENCE_MISSING")
    elif reset < 1.0:
        diagnostic_warnings.append("RESET_CYCLES_INCOMPLETE")
    if values["throughput_attainment"] is None:
        blocking.append("THROUGHPUT_EVIDENCE_MISSING")
    elif values["throughput_missing_lines"]:
        blocking.append("LINE_THROUGHPUT_EVIDENCE_MISSING")
        missing.append("throughput_by_line")
    elif values["throughput_below_target_lines"]:
        blocking.append("LINE_THROUGHPUT_TARGET_NOT_MET")
    if priority_failed_lines or (
        not priority_verdicts and values["priority_deviation_count"] > 0
    ):
        blocking.append("PRIORITY_COMPLIANCE_FAILED")
    if batch_gating_failed_lines or (
        not batch_verdicts and values["batch_gating_violation_count"] > 0
    ):
        blocking.append("BATCH_GATING_COMPLIANCE_FAILED")
    return {
        "R_storage": storage,
        "N_tool_storage_total": storage_total,
        "N_tool_storage_passed": storage_passed,
        "N_failed_tool_storage": storage_total - storage_passed,
        "R_reset": reset,
        "C_reset_requested": reset_requested,
        "C_reset_completed": reset_completed,
        **values,
        "priority_compliance_failed_lines": priority_failed_lines,
        "priority_compliance_source": (
            "EVIDENCE_SUMMARY" if priority_verdicts else "RAW_DEVIATION_COUNT"
        ),
        "batch_gating_failed_lines": batch_gating_failed_lines,
        "batch_gating_compliance_source": (
            "EVIDENCE_SUMMARY" if batch_verdicts else "RAW_VIOLATION_COUNT"
        ),
        "deployment_allowed": deployment_allowed,
        "eligible": not blocking,
        "blocking_reasons": blocking,
        "diagnostic_warnings": diagnostic_warnings,
        "R_reset_is_mandatory_constraint": False,
        "data_quality_status": "OK" if not missing else "DATA_INCOMPLETE",
        "missing_metrics": missing,
    }


def rank_candidate_runs(candidate_runs: list[dict[str, Any]]) -> dict[str, Any]:
    objective = load_selection_objective()
    ranked: list[dict[str, Any]] = []
    for row in candidate_runs:
        measurements = row.get("measurements") or {}
        components: dict[str, float | None] = {
            "throughput_attainment": measurements.get("throughput_attainment"),
        }
        score = None
        if measurements.get("eligible") and all(value is not None for value in components.values()):
            score = sum(
                float(components[name]) * float(objective["weights"][name])
                for name in objective["weights"]
            )
        ranked.append({
            **row,
            "selection_components": components,
            "objective_score": score,
        })
    ranked.sort(
        key=lambda row: (
            row.get("objective_score") is None,
            -float(row.get("objective_score") or 0.0),
            float((row.get("measurements") or {}).get("strategy_simulation_seconds") or 1e18),
            str(row.get("candidate_strategy_id") or ""),
        )
    )
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index if row.get("objective_score") is not None else None
    winner = next((row for row in ranked if row.get("objective_score") is not None), None)
    conclusive_evidence_rows = [
        row
        for row in ranked
        if row.get("status") == "EVALUATED" and isinstance(row.get("measurements"), dict)
    ]
    all_candidates_conclusively_evaluated = bool(ranked) and len(conclusive_evidence_rows) == len(ranked)
    operator_refinement_required = winner is None and all_candidates_conclusively_evaluated
    refinement_suggestions = (
        _refinement_suggestions(ranked) if operator_refinement_required else []
    )
    recovery_actions = []
    if winner is None and not operator_refinement_required:
        recovery_actions = [
            "Resolve candidate simulation, ScenarioSpec, RunArtifact, or evidence-pipeline errors and rerun the batch.",
            "Do not ask the operator to revise the intent until every candidate has conclusive simulation evidence.",
        ]
    return {
        "status": "SELECTED" if winner else "NO_ELIGIBLE_STRATEGY",
        "selected_candidate_strategy_id": winner.get("candidate_strategy_id") if winner else None,
        "selected_scenario_spec_id": winner.get("scenario_spec_id") if winner else None,
        "selected_run_id": winner.get("run_id") if winner else None,
        "objective_score": winner.get("objective_score") if winner else None,
        "objective": objective,
        "ranked_candidates": ranked,
        "selection_is_deterministic": True,
        "post_simulation_regeneration_performed": False,
        "operator_refinement_required": operator_refinement_required,
        "all_candidates_conclusively_evaluated": all_candidates_conclusively_evaluated,
        "failure_classification": (
            "POST_SIMULATION_CONSTRAINT_FAILURE"
            if operator_refinement_required
            else ("SYSTEM_OR_SIMULATION_INCOMPLETE" if winner is None else None)
        ),
        "refinement_suggestions": refinement_suggestions,
        "recovery_actions": recovery_actions,
        "selected_at_utc": now_utc(),
    }


def _refinement_suggestions(candidate_runs: list[dict[str, Any]]) -> list[str]:
    reasons = {
        str(reason)
        for row in candidate_runs
        for reason in ((row.get("measurements") or {}).get("blocking_reasons") or [])
    }
    suggestions: list[str] = []
    mappings = [
        (
            {"PLACEMENT_EVIDENCE_MISSING", "PLACEMENT_VERIFICATION_FAILED"},
            "Clarify the tooling targets and placement constraints, then submit a revised intent.",
        ),
        (
            {"PRIORITY_COMPLIANCE_FAILED"},
            "State the required tooling order explicitly for each affected production line.",
        ),
        (
            {"BATCH_GATING_COMPLIANCE_FAILED"},
            "Clarify which tooling belongs in the required tray and which belongs in the unwanted box.",
        ),
        (
            {"THROUGHPUT_EVIDENCE_MISSING", "EVIDENCE_DISALLOWS_DEPLOYMENT"},
            "Review the evidence blocking reasons and revise the KPI target or policy assumptions.",
        ),
        (
            {"SIMULATION_NOT_COMPLETED", "SYSTEM_ERROR", "RUN_ARTIFACT_NOT_COMPLETED"},
            "Resolve the reported simulator or scenario error before refining and resubmitting the intent.",
        ),
    ]
    for trigger_reasons, message in mappings:
        if reasons.intersection(trigger_reasons) and message not in suggestions:
            suggestions.append(message)
    if not suggestions:
        suggestions.append(
            "No candidate passed every mandatory constraint; review the candidate evidence and refine the operator intent."
        )
    return suggestions
