from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trt_core.repository import PROJECT_ROOT


M12_ROOT = PROJECT_ROOT / "outputs" / "reports" / "m12"

ISAAC_DEFAULTS: dict[str, Any] = {
    "headless": False,
    "global_seed": 65,
    "max_seed_trials": 1,
    "reuse_precomputed_layouts": True,
    "layout_source": "auto",
    "episode_success_requires_reset_cycles": 1,
    "allowed_overlap_ratio": 0.99,
    "chosen_intervention_mode": "immediate-stop",
    "travel_time": 1.0,
    "fix_duration": 3.0,
    "resume_delay": 1.0,
}

TOOLING_TOTAL_SEQUENCE = [8, 10, 12]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def line_numbers(text: str) -> list[int]:
    numbers: set[int] = set()
    normalized = text.lower().replace("-", " ")
    for match in re.finditer(r"(?:line|lines|production line|production lines)\s+((?:\d+\s*(?:,|and)?\s*)+)", normalized):
        numbers.update(int(number) for number in re.findall(r"\d+", match.group(1)))
    return sorted(numbers)


def requires_four_line_scope(text: str, expected_lines: list[str] | None = None) -> bool:
    normalized = text.lower()
    expected_lines = expected_lines or []
    if "all production lines" in normalized or "all lines" in normalized or "every production line" in normalized:
        return True
    if any(line in {"line_3", "line_4"} for line in expected_lines):
        return True
    return any(number in {3, 4} for number in line_numbers(text))


def choose_num_envs(text: str, expected_lines: list[str] | None = None) -> int:
    normalized = text.lower()
    if "two production lines" in normalized or "limited two-line" in normalized or "two-line" in normalized:
        return 2
    return 4 if requires_four_line_scope(text, expected_lines) else 2


def choose_total_tooling(index: int, num_envs: int, counts: dict[int, int]) -> int:
    candidates = [total for total in TOOLING_TOTAL_SEQUENCE if total % num_envs == 0]
    if not candidates:
        candidates = TOOLING_TOTAL_SEQUENCE
    target = TOOLING_TOTAL_SEQUENCE[index % len(TOOLING_TOTAL_SEQUENCE)]
    if target in candidates and counts[target] <= min(counts[candidate] for candidate in candidates):
        chosen = target
    else:
        chosen = min(candidates, key=lambda total: (counts[total], abs(total - target), total))
    counts[chosen] += 1
    return chosen


def extract_time_adjustment_expected(text: str) -> tuple[dict[str, float], list[str]]:
    normalized = text.lower()
    expected: dict[str, float] = {}
    warnings: list[str] = []
    arrival = re.search(r"(?:reduce|reduced|can be reduced)[^\d]*arrival time[^\d]*(?:by|about)?\s*(\d+(?:\.\d+)?)", normalized)
    if not arrival:
        arrival = re.search(r"arrival time[^\d]*(?:reduced|reduce|can be reduced)[^\d]*(?:by|about)?\s*(\d+(?:\.\d+)?)", normalized)
    if arrival:
        expected["travel_time"] = ISAAC_DEFAULTS["travel_time"] - float(arrival.group(1))
    fix = re.search(r"(?:reduce|reduced|can be reduced)[^\d]*(?:resolve entanglements|entanglement fix time|time to resolve entanglements)[^\d]*(?:by|about)?\s*(\d+(?:\.\d+)?)", normalized)
    if not fix:
        fix = re.search(r"(?:resolve entanglements|entanglement fix time|time to resolve entanglements)[^\d]*(?:reduced|reduce|can be reduced)[^\d]*(?:by|about)?\s*(\d+(?:\.\d+)?)", normalized)
    if fix:
        expected["fix_duration"] = ISAAC_DEFAULTS["fix_duration"] - float(fix.group(1))
    resume = re.search(r"(?:recovery time|recovery delay)[^\d]*(?:to be|be|make)?[^\d]*(\d+(?:\.\d+)?)\s*second[s]?\s*slower", normalized)
    if resume:
        expected["resume_delay"] = ISAAC_DEFAULTS["resume_delay"] + float(resume.group(1))
    for key, value in expected.items():
        if value < 0:
            warnings.append(f"{key} would be negative under current Isaac defaults: {value}")
    return expected, warnings


def command_args(params: dict[str, Any]) -> str:
    args = [
        f"--headless {'true' if params['headless'] else 'false'}",
        f"--global_seed {params['global_seed']}",
        f"--max_seed_trials {params['max_seed_trials']}",
    ]
    if params["reuse_precomputed_layouts"]:
        args.append("--reuse_precomputed_layouts")
    args.extend(
        [
            f"--layout_source {params['layout_source']}",
            f"--episode_success_requires_reset_cycles {params['episode_success_requires_reset_cycles']}",
            f"--allowed_overlap_ratio {params['allowed_overlap_ratio']}",
            f"--chosen_intervention_mode {params['chosen_intervention_mode']}",
            f"--travel_time {params['travel_time']}",
            f"--fix_duration {params['fix_duration']}",
            f"--resume_delay {params['resume_delay']}",
            f"--add_reference_number {params['add_reference_number']}",
        ]
    )
    return " ".join(args)


def capped_prompt(base_prompt: str, *, num_envs: int, add_reference_number: int, total_tooling: int) -> str:
    prompt = base_prompt.strip()
    prompt = re.sub(r"set tooling per line to \d+", f"set simulated tooling count per production line to {add_reference_number}", prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"simulated tooling count per production line to \d+", f"simulated tooling count per production line to {add_reference_number}", prompt, flags=re.IGNORECASE)
    if "simulated tooling count per production line" not in prompt.lower():
        prompt = f"{prompt} and set simulated tooling count per production line to {add_reference_number}"
    if num_envs == 2 and "two production lines" not in prompt.lower() and "two-line" not in prompt.lower():
        prompt = f"{prompt} and run this as a two-production-line simulation"
    if num_envs == 4 and "four production lines" not in prompt.lower() and "all production lines" not in prompt.lower() and "all lines" not in prompt.lower():
        prompt = f"{prompt} and run this as a four-production-line simulation"
    return f"{prompt} (M12 full test target total simulated tooling: {total_tooling})"


def tc1_launch_rows(seed_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for row in read_jsonl(seed_dir / "operator_intent_gold.jsonl"):
        if row.get("test_case_id") != "TC1":
            continue
        if row.get("expected_status") != "REVIEWED":
            continue
        rows.append(
            {
                "source_test_case": "TC1",
                "test_id": f"TC1-{row['id']}",
                "seed_id": row["id"],
                "natural_language_input": row["operator_text"],
                "expected_target_lines": row.get("expected_target_lines") or [],
            }
        )
    return rows


def tc3_launch_rows(seed_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for row in read_jsonl(seed_dir / "scenario_setup_gold.jsonl"):
        if not row.get("expected_run_id"):
            continue
        rows.append(
            {
                "source_test_case": "TC3",
                "test_id": f"TC3-{row['setup_id']}",
                "seed_id": row["setup_id"],
                "natural_language_input": row["intent_text"],
                "expected_target_lines": row.get("expected_target_lines") or [],
            }
        )
    return rows


def build_parameter_plan(seed_dir: Path) -> list[dict[str, Any]]:
    launch_rows = tc1_launch_rows(seed_dir) + tc3_launch_rows(seed_dir)
    counts = {8: 0, 10: 0, 12: 0}
    planned: list[dict[str, Any]] = []
    for index, row in enumerate(launch_rows):
        num_envs = choose_num_envs(row["natural_language_input"], row.get("expected_target_lines"))
        total_tooling = choose_total_tooling(index, num_envs, counts)
        add_reference_number = total_tooling // num_envs
        time_expected, warnings = extract_time_adjustment_expected(row["natural_language_input"])
        params = {
            **ISAAC_DEFAULTS,
            **time_expected,
            "num_envs": num_envs,
            "add_reference_number": add_reference_number,
        }
        if "continue feasible tasks until operator arrival" in row["natural_language_input"].lower():
            params["chosen_intervention_mode"] = "continue-until-arrival"
        total_from_params = int(params["num_envs"]) * int(params["add_reference_number"])
        if total_from_params > 12:
            warnings.append(f"total tooling exceeds cap: {total_from_params}")
        if int(params["num_envs"]) > 4:
            warnings.append(f"num_envs exceeds cap: {params['num_envs']}")
        if total_tooling == 10 and num_envs == 4:
            warnings.append("10 total tooling is not divisible across 4 lines; planner should avoid this combination.")
        should_launch_isaac = not any("would be negative" in warning for warning in warnings)
        planned.append(
            {
                "full_sequence": f"FULL_SIM_{len(planned) + 1:03d}",
                "source_test_case": row["source_test_case"],
                "test_id": row["test_id"],
                "seed_id": row["seed_id"],
                "natural_language_input": row["natural_language_input"],
                "full_test_prompt": capped_prompt(
                    row["natural_language_input"],
                    num_envs=int(params["num_envs"]),
                    add_reference_number=int(params["add_reference_number"]),
                    total_tooling=total_from_params,
                ),
                "num_envs": int(params["num_envs"]),
                "total_tooling": total_from_params,
                "add_reference_number": int(params["add_reference_number"]),
                "headless": str(params["headless"]).lower(),
                "global_seed": params["global_seed"],
                "max_seed_trials": params["max_seed_trials"],
                "reuse_precomputed_layouts": str(params["reuse_precomputed_layouts"]).lower(),
                "layout_source": params["layout_source"],
                "episode_success_requires_reset_cycles": params["episode_success_requires_reset_cycles"],
                "allowed_overlap_ratio": params["allowed_overlap_ratio"],
                "chosen_intervention_mode": params["chosen_intervention_mode"],
                "travel_time": params["travel_time"],
                "fix_duration": params["fix_duration"],
                "resume_delay": params["resume_delay"],
                "should_launch_isaac": str(should_launch_isaac).lower(),
                "expected_command_args": command_args(params) if should_launch_isaac else "",
                "expected_validation_issue": "; ".join(warnings),
                "failure_documentation_required": "true",
                "deployment_allowed": "false",
                "analysis_group": f"total_tooling_{total_from_params}",
            }
        )
    return planned


def write_readme(output: Path, rows: list[dict[str, Any]]) -> None:
    launch_rows = [row for row in rows if row.get("should_launch_isaac") == "true"]
    blocked_rows = [row for row in rows if row.get("should_launch_isaac") != "true"]
    counts: dict[int, int] = {}
    for row in launch_rows:
        counts[int(row["total_tooling"])] = counts.get(int(row["total_tooling"]), 0) + 1
    lines = [
        "# M12 Full-Test Isaac Parameter Plan",
        "",
        f"Created at: {now_utc()}",
        "",
        "This folder contains a launch-only Isaac command plan and a broader expectation plan that also lists rows expected to be blocked before Isaac.",
        "",
        "- `full_isaac_launch_parameter_plan.csv/json`: only rows where `should_launch_isaac=true`.",
        "- `full_isaac_parameter_plan.csv/json`: all simulation-candidate rows, including rows that should be blocked before Isaac because their computed parameters are invalid.",
        "",
        "## Confirmed Defaults",
        "",
        "```text",
        "--headless false --global_seed 65 --max_seed_trials 1 --reuse_precomputed_layouts --layout_source auto --episode_success_requires_reset_cycles 1 --allowed_overlap_ratio 0.99 --chosen_intervention_mode immediate-stop --travel_time 1.0 --fix_duration 3.0 --resume_delay 1.0 --add_reference_number <varies>",
        "```",
        "",
        "## Full-Test Caps",
        "",
        "- Production lines: `num_envs <= 4`.",
        "- Total simulated tooling: `num_envs * add_reference_number <= 12`.",
        "- Tooling groups are distributed across 8, 10, and 12 total tools where feasible.",
        "- `10` total tools is only assigned to two-line simulations because it is not evenly divisible across four lines.",
        "",
        "## Distribution",
        "",
    ]
    for total in TOOLING_TOTAL_SEQUENCE:
        lines.append(f"- total_tooling={total}: {counts.get(total, 0)} actual Isaac launch rows")
    lines.extend(
        [
            f"- blocked_before_isaac: {len(blocked_rows)} rows",
            "",
            "",
            "## Analysis Requirement",
            "",
            "Final analysis must include simulation time versus `total_tooling` using these groups. The expected trend is that higher total tooling tends to increase simulation time, but the report should show the measured relationship rather than assume it.",
            "",
            "## Failure Documentation",
            "",
            "Every failed row must record `failure_cause`, `failure_stage`, `operator_visible_message`, and whether Isaac launched. Do not mark a row as failed solely because it mentions many production lines; for example, a request about 99 production lines is valid as a task-requirement-table generation concept if the generated table is structurally correct.",
        ]
    )
    (output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_templates(output: Path) -> None:
    write_csv(
        output / "full_failure_log_template.csv",
        [],
        [
            "test_id",
            "full_sequence",
            "status",
            "failure_stage",
            "failure_cause",
            "operator_visible_message",
            "isaac_launched",
            "scenario_spec_id",
            "run_id",
            "data_quality_status",
            "created_at_utc",
        ],
    )
    write_csv(
        output / "simulation_time_vs_tooling_schema.csv",
        [],
        [
            "test_id",
            "full_sequence",
            "scenario_spec_id",
            "run_id",
            "num_envs",
            "add_reference_number",
            "total_tooling",
            "T_verification_seconds",
            "isaac_runtime_seconds",
            "artifact_created_at",
            "data_source",
            "data_quality_status",
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare M12 full-test campaign parameter expectations.")
    parser.add_argument("--seed-data", default="outputs/reports/m12/seed_data")
    parser.add_argument("--output", default="outputs/reports/m12/full_test_plan")
    args = parser.parse_args()
    seed_dir = Path(args.seed_data)
    output = Path(args.output)
    if not seed_dir.is_absolute():
        seed_dir = PROJECT_ROOT / seed_dir
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.mkdir(parents=True, exist_ok=True)

    rows = build_parameter_plan(seed_dir)
    fields = [
        "full_sequence",
        "source_test_case",
        "test_id",
        "seed_id",
        "natural_language_input",
        "full_test_prompt",
        "num_envs",
        "total_tooling",
        "add_reference_number",
        "headless",
        "global_seed",
        "max_seed_trials",
        "reuse_precomputed_layouts",
        "layout_source",
        "episode_success_requires_reset_cycles",
        "allowed_overlap_ratio",
        "chosen_intervention_mode",
        "travel_time",
        "fix_duration",
        "resume_delay",
        "should_launch_isaac",
        "expected_command_args",
        "expected_validation_issue",
        "failure_documentation_required",
        "deployment_allowed",
        "analysis_group",
    ]
    write_csv(output / "full_isaac_parameter_plan.csv", rows, fields)
    (output / "full_isaac_parameter_plan.json").write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    launch_rows = [row for row in rows if row.get("should_launch_isaac") == "true"]
    write_csv(output / "full_isaac_launch_parameter_plan.csv", launch_rows, fields)
    (output / "full_isaac_launch_parameter_plan.json").write_text(json.dumps(launch_rows, indent=2, sort_keys=True), encoding="utf-8")
    write_readme(output, rows)
    write_templates(output)
    counts: dict[str, int] = {}
    for row in launch_rows:
        key = str(row["total_tooling"])
        counts[key] = counts.get(key, 0) + 1
    print(
        json.dumps(
            {
                "status": "OK",
                "output": str(output),
                "simulation_candidate_rows": len(rows),
                "isaac_launch_rows": len(launch_rows),
                "blocked_before_isaac": len(rows) - len(launch_rows),
                "tooling_distribution": counts,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
