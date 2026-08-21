"""Windows-host Isaac Sim runner service.

Run this outside Docker on the Windows host. The Dockerized TRT API calls this
service over HTTP; only this service expands the host request into a subprocess
command for Isaac Sim.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from trt_core.digital_twin_adapter.result_reader import read_simulation_results
from trt_core.isaac_startup_timing import (
    fallback_startup_timing,
    finalized_startup_timing,
    isaac_internal_seconds,
    startup_marker_name,
)


# HANDOVER CONFIGURATION: scripts/start_host_isaac_runner.ps1 loads the
# receiving machine's paths from data/isaac_host_config.json and exports them
# to this process. These constants are only direct-launch fallbacks.
DEFAULT_ISAAC_WORKING_DIRECTORY = r"C:\Dev\IsaacSim"
DEFAULT_ISAAC_PYTHON_BAT = DEFAULT_ISAAC_WORKING_DIRECTORY + r"\_build\windows-x86_64\release\python.bat"
DEFAULT_UR5_ENTRY_SCRIPT = (
    DEFAULT_ISAAC_WORKING_DIRECTORY
    + r"\_build\windows-x86_64\release\standalone_examples\api"
    + r"\isaacsim.robot.manipulators\ur5\pick_up_example.py"
)

RUNNER_VERSION = "0.3.0"
PROCESS_IO_MODE = "REGULAR_FILES_WITH_OBSERVER"
app = FastAPI(title="Isaac UR5 Host Runner", version=RUNNER_VERSION)
# Reuse Uvicorn's configured error logger so launch diagnostics are visible in
# the same PowerShell window as the access log on fresh installations.
logger = logging.getLogger("uvicorn.error")
RUNS: dict[str, dict[str, Any]] = {}
PROCESSES: dict[str, subprocess.Popen] = {}
STREAM_THREADS: dict[str, list[threading.Thread]] = {}
RUN_LOCK = threading.Lock()


class IsaacCommandArgs(BaseModel):
    """Validated command args from trt-api.

    Defaults are last-resort fallbacks for direct host-runner dry-runs. In the
    production path, trt-api resolves these values from ScenarioSpec and passes
    a complete command_args object.
    """

    num_envs: int = 4
    headless: bool = False
    global_seed: int | None = 65
    max_seed_trials: int | None = 1
    allowed_overlap_ratio: float = 0.99
    layout_source: str = "auto"
    episode_success_requires_reset_cycles: int = 1
    chosen_intervention_mode: str = "immediate-stop"
    travel_time: float = 1.0
    fix_duration: float = 3.0
    resume_delay: float = 1.0
    add_reference_number: int = 5
    reuse_verified_seed: bool = False
    reuse_precomputed_layouts: bool = True
    seed_db_path: str | None = None


class IsaacRunRequest(BaseModel):
    scenario_spec_id: str | None = None
    scenario_spec_path: str
    output_db_path: str
    run_id: str
    run_mode: str = "SYNC"
    working_directory: str = Field(default_factory=lambda: os.environ.get("ISAAC_WORKING_DIRECTORY", DEFAULT_ISAAC_WORKING_DIRECTORY))
    python_bat: str = Field(default_factory=lambda: os.environ.get("ISAAC_PYTHON_BAT", DEFAULT_ISAAC_PYTHON_BAT))
    entry_script: str = Field(default_factory=lambda: os.environ.get("ISAAC_UR5_ENTRY_SCRIPT", DEFAULT_UR5_ENTRY_SCRIPT))
    command_args: IsaacCommandArgs = Field(default_factory=IsaacCommandArgs)
    timeout_seconds: int = 600


def _tail(value: str | None, limit: int = 4000) -> str:
    return (value or "")[-limit:]


def _tail_file(path: str | None, limit: int = 4000) -> str:
    if not path:
        return ""
    try:
        file_path = Path(path)
        if not file_path.exists():
            return ""
        data = file_path.read_text(encoding="utf-8", errors="replace")
        return data[-limit:]
    except OSError:
        return ""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "scenario_spec_id": run.get("scenario_spec_id"),
        "status": run.get("status"),
        "accepted_at": run.get("accepted_at"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "launch_attempted": run.get("launch_attempted", False),
        "process_started": run.get("process_started", False),
        "runner_version": run.get("runner_version"),
        "process_io_mode": run.get("process_io_mode"),
        "launch_method": run.get("launch_method"),
        "pid": run.get("pid"),
        "return_code": run.get("return_code"),
        "scenario_spec_path": run.get("scenario_spec_path"),
        "output_db_path": run.get("output_db_path"),
        "stdout_path": run.get("stdout_path"),
        "stderr_path": run.get("stderr_path"),
        "actual_launch_command": run.get("actual_launch_command"),
        "errors": list(run.get("errors") or []),
        "missing_paths": list(run.get("missing_paths") or []),
        "warnings": list(run.get("warnings") or []),
    }


def _model_to_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return dict(value)


def _command_args_from_host_request(host_request: dict[str, Any]) -> dict[str, Any]:
    raw_args = host_request.get("command_args") or {}
    if isinstance(raw_args, IsaacCommandArgs):
        raw_args = _model_to_dict(raw_args)
    defaults = _model_to_dict(IsaacCommandArgs())
    args = {**defaults, **dict(raw_args)}
    # args["allowed_overlap_ratio"] = 0.9
    # args["chosen_intervention_mode"] = "continue-until-arrival"
    # args["travel_time"] = 4.0
    # args["fix_duration"] = 5.0
    # args["resume_delay"] = 0.5
    if not args.get("seed_db_path") and os.environ.get("ISAAC_SEED_DB_PATH"):
        args["seed_db_path"] = os.environ["ISAAC_SEED_DB_PATH"]
    return args


def _validate_host_request(host_request: dict[str, Any]) -> dict[str, Any]:
    args = _command_args_from_host_request(host_request)
    missing_paths: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    for key, label in [
        ("scenario_spec_path", "ScenarioSpec path"),
        ("working_directory", "Isaac working directory"),
        ("python_bat", "Isaac python.bat"),
        ("entry_script", "Isaac UR5 entry script"),
    ]:
        value = host_request.get(key)
        if not value or not Path(str(value)).exists():
            missing_paths.append(f"{label} does not exist: {value}")

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
    if seed_db_path:
        if not Path(str(seed_db_path)).exists():
            warnings.append(f"seed_db_path does not exist and --seed_db_path will not be passed: {seed_db_path}")
    else:
        warnings.append("seed_db_path is not configured; pick_up_example.py will use its internal default.")

    return {
        "args": args,
        "missing_paths": missing_paths,
        "warnings": warnings,
        "errors": errors,
    }


def build_pick_up_example_args(host_request: dict[str, Any]) -> list[str]:
    """Map a host request into supported pick_up_example.py CLI arguments."""

    args = _command_args_from_host_request(host_request)
    run_id = host_request.get("run_id")
    output_db_path = host_request.get("output_db_path")
    scenario_spec_path = host_request.get("scenario_spec_path")
    cli_args = [
        "--num_envs",
        str(int(args["num_envs"])),
        "--headless",
        "true" if bool(args["headless"]) else "false",
        "--layout_source",
        str(args["layout_source"]),
        "--episode_success_requires_reset_cycles",
        str(int(args["episode_success_requires_reset_cycles"])),
        "--allowed_overlap_ratio",
        str(float(args["allowed_overlap_ratio"])),
        "--chosen_intervention_mode",
        str(args["chosen_intervention_mode"]),
        "--travel_time",
        str(float(args["travel_time"])),
        "--fix_duration",
        str(float(args["fix_duration"])),
        "--resume_delay",
        str(float(args["resume_delay"])),
        "--add_reference_number",
        str(int(args["add_reference_number"])),
    ]
    if scenario_spec_path:
        cli_args.extend(["--scenario_spec_path", str(scenario_spec_path)])
    if args.get("global_seed") is not None:
        cli_args.extend(["--global_seed", str(int(args["global_seed"]))])
    if args.get("max_seed_trials") is not None:
        cli_args.extend(["--max_seed_trials", str(int(args["max_seed_trials"]))])
    if run_id:
        cli_args.extend(["--run_id", str(run_id)])
    if output_db_path:
        cli_args.extend(["--output_db_path", str(output_db_path)])
    seed_db_path = args.get("seed_db_path")
    if seed_db_path and Path(str(seed_db_path)).exists():
        cli_args.extend(["--seed_db_path", str(seed_db_path)])
    if args.get("global_seed") is None and bool(args.get("reuse_verified_seed")):
        cli_args.append("--reuse_verified_seed")
    if bool(args.get("reuse_precomputed_layouts")):
        cli_args.append("--reuse_precomputed_layouts")
    return cli_args


def _ensure_result_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS simulation_runs(
          run_id TEXT PRIMARY KEY,
          scenario_spec_id TEXT,
          scenario_spec_path TEXT,
          started_at TEXT,
          completed_at TEXT,
          status TEXT,
          error_message TEXT
        );
        CREATE TABLE IF NOT EXISTS line_kpis(
          run_id TEXT,
          line_id TEXT,
          throughput_per_hour REAL,
          completed_count INTEGER,
          wanted_completed_count INTEGER,
          unwanted_completed_count INTEGER,
          misplaced_count INTEGER,
          entanglement_count INTEGER,
          downtime_seconds REAL,
          cycle_time_seconds REAL,
          success INTEGER,
          required_tray_completion_seconds REAL,
          unwanted_box_completion_seconds REAL,
          all_sorting_completion_seconds REAL,
          priority_deviation_count INTEGER,
          priority_policy TEXT
        );
        CREATE TABLE IF NOT EXISTS tool_events(
          run_id TEXT,
          line_id TEXT,
          tool_id TEXT,
          tool_type TEXT,
          env_id INTEGER,
          tool_number INTEGER,
          wanted INTEGER,
          picked INTEGER,
          placed INTEGER,
          placement_target TEXT,
          placement_correct INTEGER,
          event_time_seconds REAL,
          actual_pick_index INTEGER,
          intended_priority_rank INTEGER,
          priority_policy TEXT
        );
        CREATE TABLE IF NOT EXISTS priority_events(
          run_id TEXT,
          line_id TEXT,
          env_id INTEGER,
          tool_id TEXT,
          tool_number INTEGER,
          intended_rank INTEGER,
          actual_pick_index INTEGER,
          priority_policy TEXT,
          deviation_reason TEXT,
          event_time_seconds REAL
        );
        CREATE TABLE IF NOT EXISTS container_completion_events(
          run_id TEXT,
          line_id TEXT,
          env_id INTEGER,
          container_type TEXT,
          completed_at_seconds REAL,
          required_count INTEGER,
          completed_count INTEGER,
          success INTEGER
        );
        CREATE TABLE IF NOT EXISTS line_completion_kpis(
          run_id TEXT,
          line_id TEXT,
          env_id INTEGER,
          priority_policy TEXT,
          required_tray_completion_seconds REAL,
          unwanted_box_completion_seconds REAL,
          all_sorting_completion_seconds REAL,
          priority_deviation_count INTEGER,
          success INTEGER
        );
        """
    )


def _minimal_line_kpi_rows(connection: sqlite3.Connection, run_id: str, num_envs: int, success: bool) -> None:
    existing_count = connection.execute(
        "SELECT COUNT(*) FROM line_kpis WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    if int(existing_count) > 0:
        return
    line_kpi_columns = [row[1] for row in connection.execute("PRAGMA table_info(line_kpis)").fetchall()]
    for env_index in range(max(1, int(num_envs))):
        line_id = f"line_{env_index + 1}"
        row = {
            "run_id": run_id,
            "line_id": line_id,
            "throughput_per_hour": None,
            "completed_count": 0,
            "wanted_completed_count": 0,
            "unwanted_completed_count": 0,
            "misplaced_count": 0,
            "entanglement_count": 0,
            "downtime_seconds": 0.0,
            "cycle_time_seconds": None,
            "success": 1 if success else 0,
            "required_tray_completion_seconds": None,
            "unwanted_box_completion_seconds": None,
            "all_sorting_completion_seconds": None,
            "priority_deviation_count": 0,
            "priority_policy": "FCFS",
        }
        insert_columns = [column for column in line_kpi_columns if column in row]
        placeholders = ", ".join("?" for _ in insert_columns)
        connection.execute(
            f"INSERT INTO line_kpis ({', '.join(insert_columns)}) VALUES ({placeholders})",
            tuple(row[column] for column in insert_columns),
        )
        connection.execute(
            "INSERT INTO line_completion_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                line_id,
                env_index,
                "FCFS",
                None,
                None,
                None,
                0,
                1 if success else 0,
            ),
        )


def finalize_successful_result_db(host_request: dict[str, Any]) -> dict[str, Any]:
    """Finalize a DB left RUNNING after a clean Isaac return code.

    pick_up_example.py owns normal result writing. This host-side guard keeps the
    API contract stable when the process exits 0 after only initializing the DB.
    """

    output_db_path = host_request.get("output_db_path")
    run_id = host_request.get("run_id")
    args = _command_args_from_host_request(host_request)
    diagnostics: dict[str, Any] = {
        "result_db_finalized_by_host_runner": False,
        "result_db_status_before": None,
        "result_db_status_after": None,
        "line_kpis_count": None,
        "tool_events_count": None,
        "completed_at": None,
        "errors": [],
    }
    if not output_db_path or not run_id:
        diagnostics["errors"].append("output_db_path and run_id are required for result DB finalization.")
        return diagnostics

    try:
        with sqlite3.connect(str(output_db_path)) as connection:
            _ensure_result_schema(connection)
            row = connection.execute(
                "SELECT status, completed_at FROM simulation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            now = _now_utc()
            if row is None:
                diagnostics["result_db_status_before"] = "MISSING_RUN_ROW"
                connection.execute(
                    "INSERT INTO simulation_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        host_request.get("scenario_spec_id"),
                        host_request.get("scenario_spec_path"),
                        now,
                        now,
                        "COMPLETED",
                        None,
                    ),
                )
                _minimal_line_kpi_rows(connection, run_id, int(args.get("num_envs") or 1), True)
                diagnostics["result_db_finalized_by_host_runner"] = True
            else:
                diagnostics["result_db_status_before"] = row[0]
                if row[0] == "RUNNING":
                    _minimal_line_kpi_rows(connection, run_id, int(args.get("num_envs") or 1), True)
                    connection.execute(
                        """
                        UPDATE simulation_runs
                           SET completed_at = ?, status = ?, error_message = ?
                         WHERE run_id = ?
                        """,
                        (now, "COMPLETED", None, run_id),
                    )
                    diagnostics["result_db_finalized_by_host_runner"] = True
                elif row[0] == "COMPLETED":
                    _minimal_line_kpi_rows(connection, run_id, int(args.get("num_envs") or 1), True)
            connection.commit()
            final_row = connection.execute(
                "SELECT status, completed_at FROM simulation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            diagnostics["result_db_status_after"] = final_row[0] if final_row else None
            diagnostics["completed_at"] = final_row[1] if final_row else None
            diagnostics["line_kpis_count"] = connection.execute(
                "SELECT COUNT(*) FROM line_kpis WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            diagnostics["tool_events_count"] = connection.execute(
                "SELECT COUNT(*) FROM tool_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
    except sqlite3.Error as exc:
        diagnostics["errors"].append(f"Could not finalize result DB after Isaac completed: {exc}")
    return diagnostics


def build_host_command(request: IsaacRunRequest) -> dict[str, Any]:
    request_dict = _model_to_dict(request)
    command = [request.python_bat, request.entry_script] + build_pick_up_example_args(request_dict)
    return {
        "command": command,
        "launch_command": _platform_launch_command(command),
        "fallback_launch_command": _platform_launch_command(command, use_comspec=True),
        "working_directory": request.working_directory,
        "python_bat": request.python_bat,
        "entry_script": request.entry_script,
    }


def _platform_launch_command(
    command: list[str],
    *,
    platform_name: str | None = None,
    use_comspec: bool = False,
) -> list[str]:
    """Return a direct command or an explicit Windows batch fallback.

    Direct execution most closely matches the known-good manual PowerShell
    command and is the default. COMSPEC is reserved for systems where direct
    batch execution raises an OSError during process creation.
    """

    effective_platform = platform_name or os.name
    if use_comspec and effective_platform == "nt" and Path(command[0]).suffix.lower() in {".bat", ".cmd"}:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/d", "/s", "/c", subprocess.list2cmdline(command)]
    return list(command)


def _log_paths_for_run(output_db_path: str, run_id: str) -> tuple[Path, Path]:
    output_dir = Path(output_db_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{run_id}.stdout.log", output_dir / f"{run_id}.stderr.log"


def _timing_path_for_run(output_db_path: str, run_id: str) -> Path:
    return Path(output_db_path).parent / f"{run_id}.timing.json"


def _write_timing_sidecar(run: dict[str, Any]) -> None:
    timing_path = run.get("timing_path")
    if not timing_path:
        return
    try:
        import json

        Path(timing_path).write_text(
            json.dumps(run.get("timing") or {}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return


def _record_process_output_line(
    run_id: str,
    stream_name: str,
    line: str,
) -> None:
    console = sys.stdout if stream_name == "stdout" else sys.stderr
    stream_to_console = os.environ.get("ISAAC_STREAM_TO_CONSOLE", "true").lower() in {"1", "true", "yes"}
    observed_at = _now_utc()
    observed_monotonic = time.monotonic()
    if stream_to_console:
        console.write(line)
        console.flush()
    marker = startup_marker_name(line)
    if marker is None:
        return
    with RUN_LOCK:
        run = RUNS.get(run_id)
        if run is None:
            return
        timing = dict(run.get("timing") or {})
        events = list(timing.get("startup_marker_events") or [])
        events.append(
            {
                "observed_at_utc": observed_at,
                "process_elapsed_seconds": max(
                    0.0,
                    observed_monotonic - float(run["started_monotonic"]),
                ),
                "stream": stream_name,
                "pattern": marker,
                "isaac_internal_seconds": isaac_internal_seconds(line),
            }
        )
        latest = max(events, key=lambda event: event["process_elapsed_seconds"])
        timing.update(
            {
                "startup_reference_at_utc": latest["observed_at_utc"],
                "startup_reference_source": "SYSTEM_LOG_FILE_MONITOR",
                "startup_reference_pattern": latest["pattern"],
                "isaac_startup_seconds": latest["process_elapsed_seconds"],
                "startup_marker_count": len(events),
                "startup_marker_events": events,
                "data_quality_status": "OK",
            }
        )
        run = {**run, "timing": timing}
        RUNS[run_id] = run
        _write_timing_sidecar(run)


def _monitor_process_log(run_id: str, stream_name: str, log_path: Path) -> None:
    """Tail a child-owned log without attaching a PIPE to the Isaac process.

    Isaac writes directly to a regular file, matching the original working
    host-runner behavior. This observer only reads appended lines, mirrors them
    to the service console, and timestamps startup markers.
    """

    offset = 0
    incomplete = ""
    while True:
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
        except OSError:
            chunk = ""

        if chunk:
            buffered = incomplete + chunk
            lines = buffered.splitlines(keepends=True)
            incomplete = ""
            if lines and not lines[-1].endswith(("\n", "\r")):
                incomplete = lines.pop()
            for line in lines:
                _record_process_output_line(run_id, stream_name, line)

        with RUN_LOCK:
            process = PROCESSES.get(run_id)
        if process is None or process.poll() is not None:
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    final_chunk = handle.read()
            except OSError:
                final_chunk = ""
            final_text = incomplete + final_chunk
            for line in final_text.splitlines(keepends=True):
                _record_process_output_line(run_id, stream_name, line)
            return
        time.sleep(0.05)


def _finalize_startup_timing(run: dict[str, Any]) -> dict[str, Any]:
    timing = dict(run.get("timing") or {})
    lines: list[str] = []
    for key in ("stdout_path", "stderr_path"):
        path = run.get(key)
        if path and Path(path).exists():
            lines.extend(Path(path).read_text(encoding="utf-8", errors="replace").splitlines())
    finalized = finalized_startup_timing(
        lines,
        command_started_at_utc=timing.get("isaac_command_started_at_utc"),
    )
    if finalized:
        timing.update(finalized)
        timing["startup_marker_events_live"] = timing.pop("startup_marker_events", [])
        timing["startup_timing_finalized_from_logs"] = True
    else:
        fallback = fallback_startup_timing(lines)
        if fallback:
            timing.update(fallback)
        else:
            timing.update(
                {
                    "startup_reference_source": None,
                    "isaac_startup_seconds": None,
                    "data_quality_status": "DATA_INCOMPLETE",
                    "data_quality_reason": "No configured Isaac startup marker was observed.",
                }
            )
    run = {**run, "timing": timing}
    _write_timing_sidecar(run)
    return run


def _start_isaac_async(request: IsaacRunRequest) -> dict[str, Any]:
    command_info = build_host_command(request)
    validation = _validate_host_request(_model_to_dict(request))
    command_args = validation["args"]
    errors: list[str] = list(validation["errors"]) + list(validation["missing_paths"])
    logger.info(
        "isaac_run.request run_id=%s scenario_spec_id=%s mode=%s scenario_spec_path=%s",
        request.run_id,
        request.scenario_spec_id,
        request.run_mode,
        request.scenario_spec_path,
    )
    if errors:
        result = {
            "run_id": request.run_id,
            "scenario_spec_id": request.scenario_spec_id,
            "scenario_spec_path": request.scenario_spec_path,
            "status": "FAILED",
            "accepted_at": _now_utc(),
            "started_at": None,
            "completed_at": _now_utc(),
            "pid": None,
            "output_db_path": request.output_db_path,
            "output_db_exists": Path(request.output_db_path).exists(),
            "seed_db_path": command_args.get("seed_db_path"),
            "command_args": command_args,
            "stdout_path": None,
            "stderr_path": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "return_code": None,
            "launch_attempted": False,
            "process_started": False,
            "runner_version": RUNNER_VERSION,
            "process_io_mode": PROCESS_IO_MODE,
            "errors": errors,
            "missing_paths": validation["missing_paths"],
            "warnings": validation["warnings"],
            "timeout_seconds": request.timeout_seconds,
            **command_info,
        }
        with RUN_LOCK:
            RUNS[request.run_id] = result
        logger.error(
            "isaac_run.rejected_before_launch run_id=%s errors=%s",
            request.run_id,
            errors,
        )
        return result

    stdout_path, stderr_path = _log_paths_for_run(request.output_db_path, request.run_id)
    timing_path = _timing_path_for_run(request.output_db_path, request.run_id)
    accepted_at = _now_utc()
    command_started_at = _now_utc()
    started_monotonic = time.monotonic()
    actual_launch_command = command_info["launch_command"]
    launch_method = "DIRECT"
    process: subprocess.Popen | None = None
    launch_error: OSError | None = None
    stdout_handle = stdout_path.open("w", encoding="utf-8", errors="replace")
    stderr_handle = stderr_path.open("w", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(
            actual_launch_command,
            cwd=command_info["working_directory"],
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=None,
            text=True,
        )
    except OSError as direct_exc:
        fallback_command = command_info["fallback_launch_command"]
        if fallback_command != actual_launch_command:
            logger.warning(
                "isaac_run.direct_process_creation_failed run_id=%s error=%s; retrying with COMSPEC",
                request.run_id,
                direct_exc,
            )
            actual_launch_command = fallback_command
            launch_method = "COMSPEC_FALLBACK"
            try:
                process = subprocess.Popen(
                    actual_launch_command,
                    cwd=command_info["working_directory"],
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    stdin=None,
                    text=True,
                )
            except OSError as fallback_exc:
                launch_error = fallback_exc
            else:
                launch_error = None
        else:
            launch_error = direct_exc
    finally:
        stdout_handle.close()
        stderr_handle.close()

    if process is None or launch_error is not None:
        exc = launch_error
        result = {
            "run_id": request.run_id,
            "scenario_spec_id": request.scenario_spec_id,
            "scenario_spec_path": request.scenario_spec_path,
            "status": "FAILED",
            "accepted_at": _now_utc(),
            "started_at": None,
            "completed_at": _now_utc(),
            "pid": None,
            "output_db_path": request.output_db_path,
            "output_db_exists": Path(request.output_db_path).exists(),
            "seed_db_path": command_args.get("seed_db_path"),
            "command_args": command_args,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_tail": _tail_file(str(stdout_path)),
            "stderr_tail": _tail_file(str(stderr_path)),
            "return_code": None,
            "launch_attempted": True,
            "process_started": False,
            "runner_version": RUNNER_VERSION,
            "process_io_mode": PROCESS_IO_MODE,
            "launch_method": launch_method,
            "actual_launch_command": actual_launch_command,
            "errors": [f"Could not launch Isaac process: {exc}"],
            "warnings": validation["warnings"],
            "timeout_seconds": request.timeout_seconds,
            **command_info,
        }
        with RUN_LOCK:
            RUNS[request.run_id] = result
        logger.exception(
            "isaac_run.process_creation_failed run_id=%s launch_command=%s",
            request.run_id,
            actual_launch_command,
        )
        return result

    result = {
        "run_id": request.run_id,
        "scenario_spec_id": request.scenario_spec_id,
        "scenario_spec_path": request.scenario_spec_path,
        "status": "RUNNING",
        "accepted_at": accepted_at,
        "started_at": command_started_at,
        "started_monotonic": started_monotonic,
        "completed_at": None,
        "pid": process.pid,
        "output_db_path": request.output_db_path,
        "output_db_exists": Path(request.output_db_path).exists(),
        "seed_db_path": command_args.get("seed_db_path"),
        "command_args": command_args,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "timing_path": str(timing_path),
        "stdout_tail": "",
        "stderr_tail": "",
        "return_code": None,
        "launch_attempted": True,
        "process_started": True,
        "runner_version": RUNNER_VERSION,
        "process_io_mode": PROCESS_IO_MODE,
        "launch_method": launch_method,
        "actual_launch_command": actual_launch_command,
        "errors": [],
        "warnings": validation["warnings"],
        "timeout_seconds": request.timeout_seconds,
        "timing": {
            "isaac_command_started_at_utc": command_started_at,
            "startup_reference_at_utc": None,
            "startup_reference_source": None,
            "startup_reference_pattern": None,
            "isaac_startup_seconds": None,
            "startup_marker_count": 0,
            "startup_marker_events": [],
            "data_quality_status": "DATA_INCOMPLETE",
        },
        **command_info,
    }
    with RUN_LOCK:
        RUNS[request.run_id] = result
        PROCESSES[request.run_id] = process
    logger.info(
        "isaac_run.process_started run_id=%s pid=%s launch_method=%s output_db_path=%s",
        request.run_id,
        process.pid,
        launch_method,
        request.output_db_path,
    )
    threads = [
        threading.Thread(
            target=_monitor_process_log,
            args=(request.run_id, "stdout", stdout_path),
            daemon=True,
        ),
        threading.Thread(
            target=_monitor_process_log,
            args=(request.run_id, "stderr", stderr_path),
            daemon=True,
        ),
    ]
    STREAM_THREADS[request.run_id] = threads
    for thread in threads:
        thread.start()
    return result


def _refresh_isaac_run(run_id: str) -> dict[str, Any]:
    with RUN_LOCK:
        run = RUNS.get(run_id)
        process = PROCESSES.get(run_id)
    if not run:
        return {"run_id": run_id, "status": "UNKNOWN", "errors": ["Run ID not found."]}

    if run.get("status") != "RUNNING":
        return {**run, "stdout_tail": _tail_file(run.get("stdout_path")), "stderr_tail": _tail_file(run.get("stderr_path"))}

    if process is None:
        run = {**run, "status": "FAILED", "errors": [*run.get("errors", []), "Process metadata is missing."]}
    else:
        elapsed = time.monotonic() - float(run.get("started_monotonic") or time.monotonic())
        timeout_seconds = int(run.get("timeout_seconds") or 600)
        if elapsed > timeout_seconds and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
            run = {
                **run,
                "status": "FAILED_TIMEOUT",
                "completed_at": _now_utc(),
                "return_code": process.returncode,
                "errors": [*run.get("errors", []), f"Isaac process timed out after {timeout_seconds} seconds."],
            }
            logger.error(
                "isaac_run.timeout run_id=%s pid=%s timeout_seconds=%s",
                run_id,
                run.get("pid"),
                timeout_seconds,
            )
        elif process.poll() is None:
            return {
                **run,
                "elapsed_seconds": elapsed,
                "output_db_exists": Path(str(run.get("output_db_path"))).exists(),
                "stdout_tail": _tail_file(run.get("stdout_path")),
                "stderr_tail": _tail_file(run.get("stderr_path")),
            }
        else:
            for thread in STREAM_THREADS.get(run_id, []):
                thread.join(timeout=5)
            with RUN_LOCK:
                run = RUNS.get(run_id, run)
            return_code = process.returncode
            output_db_exists = Path(str(run.get("output_db_path"))).exists()
            result_db_diagnostics = None
            if return_code == 0 and output_db_exists:
                result_db_diagnostics = finalize_successful_result_db(run)
            if return_code == 0 and output_db_exists:
                status = "COMPLETED"
                errors = list((result_db_diagnostics or {}).get("errors") or [])
            elif return_code == 0:
                status = "COMPLETED_NO_RESULT_DB"
                errors = ["Isaac completed successfully but did not produce the result DB."]
            else:
                status = "FAILED"
                errors = [f"Isaac process exited with code {return_code}."]
            run = {
                **run,
                "status": status,
                "completed_at": _now_utc(),
                "return_code": return_code,
                "output_db_exists": output_db_exists,
                "errors": errors,
                "result_db_diagnostics": result_db_diagnostics,
            }
            logger.info(
                "isaac_run.finished run_id=%s pid=%s status=%s return_code=%s output_db_exists=%s",
                run_id,
                run.get("pid"),
                status,
                return_code,
                output_db_exists,
            )

    if process is not None and process.poll() is not None:
        for thread in STREAM_THREADS.get(run_id, []):
            thread.join(timeout=5)
    run = _finalize_startup_timing(run)

    run = {
        **run,
        "stdout_tail": _tail_file(run.get("stdout_path")),
        "stderr_tail": _tail_file(run.get("stderr_path")),
    }
    with RUN_LOCK:
        RUNS[run_id] = run
        if run.get("status") != "RUNNING":
            PROCESSES.pop(run_id, None)
            STREAM_THREADS.pop(run_id, None)
    return run


@app.get("/health")
def get_health() -> dict[str, Any]:
    working_directory = os.environ.get("ISAAC_WORKING_DIRECTORY", DEFAULT_ISAAC_WORKING_DIRECTORY)
    python_bat = os.environ.get("ISAAC_PYTHON_BAT", DEFAULT_ISAAC_PYTHON_BAT)
    entry_script = os.environ.get("ISAAC_UR5_ENTRY_SCRIPT", DEFAULT_UR5_ENTRY_SCRIPT)
    python_bat_exists = Path(python_bat).is_file()
    entry_script_exists = Path(entry_script).is_file()
    working_directory_exists = Path(working_directory).is_dir()
    ready = python_bat_exists and entry_script_exists and working_directory_exists
    with RUN_LOCK:
        active_run_ids = [
            run_id for run_id, run in RUNS.items() if run.get("status") == "RUNNING"
        ]
        recent_runs = [_run_summary(run) for run in list(RUNS.values())[-10:]]
    return {
        "status": "OK" if ready else "MISCONFIGURED",
        "ready": ready,
        "service": "host_isaac_runner",
        "runner_version": RUNNER_VERSION,
        "process_io_mode": PROCESS_IO_MODE,
        "bind_address": os.environ.get("ISAAC_HOST_RUNNER_HOST", "127.0.0.1"),
        "port": int(os.environ.get("ISAAC_HOST_RUNNER_PORT", "8765")),
        "run_endpoint": "/isaac/run",
        "async_run_endpoint": "/isaac/runs",
        "active_run_ids": active_run_ids,
        "recent_runs": recent_runs,
        "python_bat_exists": python_bat_exists,
        "entry_script_exists": entry_script_exists,
        "working_directory_exists": working_directory_exists,
        "python_bat": python_bat,
        "entry_script": entry_script,
        "working_directory": working_directory,
    }


@app.post("/isaac/run")
def post_isaac_run(request: IsaacRunRequest) -> dict[str, Any]:
    if str(request.run_mode).upper() == "ASYNC":
        return _start_isaac_async(request)

    return _run_isaac_sync(request)


@app.post("/isaac/runs")
def post_isaac_runs(request: IsaacRunRequest) -> dict[str, Any]:
    return _start_isaac_async(request)


@app.get("/isaac/runs")
def list_isaac_runs() -> dict[str, Any]:
    with RUN_LOCK:
        run_ids = list(RUNS.keys())
    runs = [_run_summary(_refresh_isaac_run(run_id)) for run_id in run_ids]
    return {
        "status": "OK",
        "run_count": len(runs),
        "runs": runs,
    }


def _run_isaac_sync(request: IsaacRunRequest) -> dict[str, Any]:
    result = _start_isaac_async(request)
    while result.get("status") == "RUNNING":
        time.sleep(0.1)
        result = _refresh_isaac_run(request.run_id)
    return result


@app.post("/isaac/dry-run")
def post_isaac_dry_run(request: IsaacRunRequest) -> dict[str, Any]:
    command_info = build_host_command(request)
    validation = _validate_host_request(_model_to_dict(request))
    errors: list[str] = list(validation["errors"]) + list(validation["missing_paths"])
    return {
        "status": "READY" if not errors else "FAILED",
        "run_id": request.run_id,
        "scenario_spec_id": request.scenario_spec_id,
        "output_db_path": request.output_db_path,
        "missing_paths": validation["missing_paths"],
        "warnings": validation["warnings"],
        "errors": errors,
        **command_info,
    }


@app.get("/isaac/run/{run_id}")
def get_isaac_run(run_id: str) -> dict[str, Any]:
    return _refresh_isaac_run(run_id)


@app.get("/isaac/runs/{run_id}")
def get_isaac_runs(run_id: str) -> dict[str, Any]:
    return _refresh_isaac_run(run_id)


@app.get("/isaac/result/{run_id}")
def get_isaac_result(run_id: str) -> dict[str, Any]:
    _refresh_isaac_run(run_id)
    run = RUNS.get(run_id)
    if not run:
        return {"status": "ERROR", "error_code": "HOST_RUN_NOT_FOUND", "run_id": run_id}
    return read_simulation_results(run["output_db_path"], run_id)


@app.get("/isaac/results/{run_id}")
def get_isaac_results(run_id: str) -> dict[str, Any]:
    return get_isaac_result(run_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("ISAAC_HOST_RUNNER_HOST", "127.0.0.1"),
        port=int(os.environ.get("ISAAC_HOST_RUNNER_PORT", "8765")),
    )
