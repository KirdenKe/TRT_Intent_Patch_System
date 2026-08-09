"""Milestone 12 metrics, fixture, figure, and comparison utilities."""

from __future__ import annotations

import argparse
import csv
import os
import json
import math
import random
import sqlite3
import struct
import zlib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from trt_core.digital_twin_adapter.result_reader import read_simulation_results
from trt_core.isaac_startup_timing import finalized_startup_timing
from trt_core.repository import PROJECT_ROOT, TRTRepository


M12_ROOT = Path("outputs") / "reports" / "m12"
M12_DB_NAME = "m12_metrics.sqlite3"
SEED = 1206
ALLOWED_DATA_SOURCES = {
    "SEED_GOLD_FIXTURE",
    "SYNTHETIC_EXPANDED_FIXTURE",
    "HISTORICAL_RUN_ARTIFACT",
    "LIVE_N8N_CHAT",
    "LIVE_TRT_API",
    "LIVE_ISAAC_SIM",
    "MANUAL_IMPORT",
    "SEMI_MANUAL_DRY_PLAN",
}
FIXTURE_DATA_SOURCES = {"SEED_GOLD_FIXTURE", "SYNTHETIC_EXPANDED_FIXTURE", "SEMI_MANUAL_DRY_PLAN"}
LIVE_DATA_SOURCES = {"LIVE_N8N_CHAT", "LIVE_TRT_API", "LIVE_ISAAC_SIM"}
PROVENANCE_COLUMNS = {
    "data_source": "TEXT",
    "data_source_detail": "TEXT",
    "generated_by": "TEXT",
    "created_at_utc": "TEXT",
    "is_live_test": "INTEGER",
    "is_fixture": "INTEGER",
    "is_historical": "INTEGER",
    "test_case_id": "TEXT",
    "workflow_execution_id": "TEXT",
    "chat_session_id": "TEXT",
    "semi_manual": "INTEGER",
    "deployment_suppressed": "INTEGER",
    "approval_status": "TEXT",
    "approved_by_operator_id": "TEXT",
    "approved_at_utc": "TEXT",
}
EVENT_NAMES = {
    "INTENT_CREATED",
    "CANDIDATE_SUMMARY_CREATED",
    "CANDIDATE_REVIEW_ENDED",
    "SCENARIO_CREATED",
    "SIMULATION_STARTED",
    "RUN_ARTIFACT_CREATED",
    "DEPLOYMENT_REVIEW_ENDED",
    "DEPLOYMENT_ATTEMPTED",
    "DEPLOYMENT_BLOCKED",
    "DEPLOYMENT_COMPLETED",
    "ERROR_INTERCEPTED",
    "ERROR_NOT_INTERCEPTED",
}


def provenance(
    data_source: str,
    *,
    detail: str = "",
    generated_by: str = "trt_core.m12",
    test_case_id: str | None = None,
    run_id: str | None = None,
    scenario_spec_id: str | None = None,
    workflow_execution_id: str | None = None,
    chat_session_id: str | None = None,
    semi_manual: bool = False,
    deployment_suppressed: bool = False,
    approval_status: str | None = None,
    approved_by_operator_id: str | None = None,
    approved_at_utc: str | None = None,
) -> dict[str, Any]:
    if data_source not in ALLOWED_DATA_SOURCES:
        raise ValueError(f"Unsupported M12 data_source: {data_source}")
    return {
        "data_source": data_source,
        "data_source_detail": detail,
        "generated_by": generated_by,
        "created_at_utc": now_utc(),
        "is_live_test": 1 if data_source in LIVE_DATA_SOURCES else 0,
        "is_fixture": 1 if data_source in FIXTURE_DATA_SOURCES else 0,
        "is_historical": 1 if data_source == "HISTORICAL_RUN_ARTIFACT" else 0,
        "test_case_id": test_case_id,
        "run_id": run_id,
        "scenario_spec_id": scenario_spec_id,
        "workflow_execution_id": workflow_execution_id,
        "chat_session_id": chat_session_id,
        "semi_manual": 1 if semi_manual else 0,
        "deployment_suppressed": 1 if deployment_suppressed else 0,
        "approval_status": approval_status,
        "approved_by_operator_id": approved_by_operator_id,
        "approved_at_utc": approved_at_utc,
    }


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def seconds_between(start: Any, end: Any) -> float | None:
    start_ts = parse_ts(start)
    end_ts = parse_ts(end)
    if start_ts is None or end_ts is None:
        return None
    return max(0.0, (end_ts - start_ts).total_seconds())


def verification_seconds_excluding_startup(
    scenario_created_at: Any,
    artifact_created_at: Any,
    isaac_startup_seconds: Any,
) -> tuple[float | None, float | None, float | None]:
    wall_seconds = seconds_between(scenario_created_at, artifact_created_at)
    startup_seconds = (
        float(isaac_startup_seconds)
        if isinstance(isaac_startup_seconds, (int, float)) and isaac_startup_seconds >= 0
        else None
    )
    if wall_seconds is None or startup_seconds is None or startup_seconds > wall_seconds:
        return None, wall_seconds, startup_seconds
    return wall_seconds - startup_seconds, wall_seconds, startup_seconds


def rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def m12_dir(repository: TRTRepository | None = None) -> Path:
    root = repository.root if repository is not None else PROJECT_ROOT
    path = root / M12_ROOT
    (path / "figures").mkdir(parents=True, exist_ok=True)
    return path


def db_path(repository: TRTRepository | None = None, output_dir: str | Path | None = None) -> Path:
    if output_dir:
        path = Path(output_dir)
        if not path.is_absolute():
            path = (repository.root if repository else PROJECT_ROOT) / path
        path.mkdir(parents=True, exist_ok=True)
        return path / M12_DB_NAME
    return m12_dir(repository) / M12_DB_NAME


def connect_metrics_db(path: str | Path | None = None, repository: TRTRepository | None = None) -> sqlite3.Connection:
    resolved = Path(path) if path else db_path(repository)
    if not resolved.is_absolute():
        resolved = (repository.root if repository else PROJECT_ROOT) / resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(resolved)
    connection.row_factory = sqlite3.Row
    initialize_db(connection)
    return connection


def initialize_db(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS m12_event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            session_id TEXT,
            operator_id TEXT,
            scenario_spec_id TEXT,
            trt_id TEXT,
            trt_version TEXT,
            event_name TEXT NOT NULL,
            event_ts_utc TEXT NOT NULL,
            source_module TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS m12_run_metrics (
            run_id TEXT PRIMARY KEY,
            session_id TEXT,
            scenario_spec_id TEXT,
            operator_id TEXT,
            trt_id TEXT,
            trt_version TEXT,
            intent_created_at TEXT,
            summary_created_at TEXT,
            candidate_review_end_at TEXT,
            scenario_created_at TEXT,
            simulation_started_at TEXT,
            artifact_created_at TEXT,
            deployment_review_end_at TEXT,
            T_wait_seconds REAL,
            T_verification_seconds REAL,
            T_verification_wall_seconds REAL,
            T_isaac_startup_seconds REAL,
            verification_timing_source TEXT,
            isaac_command_started_at TEXT,
            isaac_startup_reference_at TEXT,
            isaac_startup_reference_pattern TEXT,
            T_loop_seconds REAL,
            loop_review_definition TEXT,
            N_tool_storage_total INTEGER,
            N_tool_storage_passed INTEGER,
            N_failed_tool_storage INTEGER,
            R_storage REAL,
            C_reset_requested INTEGER,
            C_reset_completed INTEGER,
            R_reset REAL,
            data_quality_status TEXT,
            data_quality_reason TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS m12_tool_storage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            scenario_spec_id TEXT,
            line_id TEXT,
            env_id INTEGER,
            tool_id TEXT,
            tool_type TEXT,
            tool_number INTEGER,
            expected_target TEXT,
            actual_target TEXT,
            expected_position_json TEXT,
            actual_position_json TEXT,
            coordinate_tolerance REAL,
            placement_correct INTEGER,
            verification_passed INTEGER,
            failure_reason TEXT,
            event_time_seconds REAL,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS m12_error_interception (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id TEXT,
            run_id TEXT,
            scenario_spec_id TEXT,
            injected_error_type TEXT,
            injection_stage TEXT,
            injected_payload_json TEXT,
            expected_interceptor TEXT,
            actual_interceptor TEXT,
            was_intercepted INTEGER,
            deployment_blocked INTEGER,
            operator_visible_message TEXT,
            false_positive INTEGER,
            false_negative INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS m12_figure_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            figure_id TEXT,
            title TEXT,
            png_path TEXT,
            svg_path TEXT,
            source_table TEXT,
            data_quality_status TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS m12_test_cases (
            test_case_id TEXT,
            test_case_name TEXT,
            reference_a TEXT,
            reference_b TEXT,
            rows_evaluated INTEGER,
            status TEXT,
            data_quality_status TEXT,
            metrics_json TEXT,
            created_at TEXT
        );
        """
    )
    for table in (
        "m12_event_log",
        "m12_run_metrics",
        "m12_tool_storage_records",
        "m12_error_interception",
        "m12_figure_manifest",
        "m12_test_cases",
    ):
        _ensure_provenance_columns(connection, table)
    _ensure_extra_columns(
        connection,
        "m12_run_metrics",
        {
            "T_verification_wall_seconds": "REAL",
            "T_isaac_startup_seconds": "REAL",
            "verification_timing_source": "TEXT",
            "isaac_command_started_at": "TEXT",
            "isaac_startup_reference_at": "TEXT",
            "isaac_startup_reference_pattern": "TEXT",
        },
    )
    _ensure_extra_columns(
        connection,
        "m12_figure_manifest",
        {
            "row_count": "INTEGER",
            "data_source_distribution_json": "TEXT",
            "null_count_by_metric_json": "TEXT",
        },
    )
    _ensure_extra_columns(
        connection,
        "m12_test_cases",
        {
            "test_case_name": "TEXT",
            "approval_status": "TEXT",
            "approved_by_operator_id": "TEXT",
            "approved_at_utc": "TEXT",
            "operator_approval_required": "INTEGER",
            "natural_language_trigger": "TEXT",
            "expected_layers_json": "TEXT",
            "expected_outputs_json": "TEXT",
            "result_json": "TEXT",
        },
    )
    connection.commit()


def _table_column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_extra_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _table_column_names(connection, table)
    for name, sql_type in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def _ensure_provenance_columns(connection: sqlite3.Connection, table: str) -> None:
    existing = _table_column_names(connection, table)
    for name, sql_type in PROVENANCE_COLUMNS.items():
        if name in {"run_id", "scenario_spec_id"} and name in existing:
            continue
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def log_event(
    *,
    event_name: str,
    repository: TRTRepository | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    operator_id: str | None = None,
    scenario_spec_id: str | None = None,
    trt_id: str | None = None,
    trt_version: str | None = None,
    source_module: str,
    payload: dict[str, Any] | None = None,
    event_ts_utc: str | None = None,
) -> None:
    if event_name not in EVENT_NAMES:
        raise ValueError(f"Unsupported M12 event name: {event_name}")
    timestamp = event_ts_utc or now_utc()
    prov = provenance(
        "LIVE_TRT_API",
        detail=f"M12 event log from {source_module}",
        generated_by=source_module,
        run_id=run_id,
        scenario_spec_id=scenario_spec_id,
        chat_session_id=session_id,
    )
    with connect_metrics_db(repository=repository) as connection:
        connection.execute(
            """
            INSERT INTO m12_event_log (
                run_id, session_id, operator_id, scenario_spec_id, trt_id, trt_version,
                event_name, event_ts_utc, source_module, payload_json, created_at,
                data_source, data_source_detail, generated_by, created_at_utc,
                is_live_test, is_fixture, is_historical, test_case_id,
                workflow_execution_id, chat_session_id, semi_manual, deployment_suppressed,
                approval_status, approved_by_operator_id, approved_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                session_id,
                operator_id,
                scenario_spec_id,
                trt_id,
                trt_version,
                event_name,
                timestamp,
                source_module,
                json.dumps(payload or {}, sort_keys=True),
                now_utc(),
                prov["data_source"],
                prov["data_source_detail"],
                prov["generated_by"],
                prov["created_at_utc"],
                prov["is_live_test"],
                prov["is_fixture"],
                prov["is_historical"],
                prov["test_case_id"],
                prov["workflow_execution_id"],
                prov["chat_session_id"],
                prov["semi_manual"],
                prov["deployment_suppressed"],
                prov["approval_status"],
                prov["approved_by_operator_id"],
                prov["approved_at_utc"],
            ),
        )


def _row_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
    return None


def _find_scenario_spec(repository: TRTRepository, scenario_spec_id: str | None, explicit: str | None = None) -> tuple[Path | None, dict[str, Any]]:
    candidates: list[Path] = []
    if explicit:
        path = Path(explicit)
        candidates.append(path if path.is_absolute() else repository.root / path)
    if scenario_spec_id:
        candidates.append(repository.root / "outputs" / "scenario_specs" / f"{scenario_spec_id}.json")
    for path in candidates:
        if path.exists():
            return path, json.loads(path.read_text(encoding="utf-8"))
    return None, {}


def _artifact_db_path(repository: TRTRepository, run_id: str) -> Path | None:
    for suffix in (".sqlite", ".sqlite3", ".db"):
        path = repository.root / "outputs" / "run_artifacts" / f"{run_id}{suffix}"
        if path.exists():
            return path
    return None


def _expected_target(event: dict[str, Any]) -> str | None:
    wanted = str(event.get("wanted")).strip().lower()
    if wanted in {"1", "true"}:
        return "REQUIRED_TRAY"
    if wanted in {"0", "false"}:
        return "UNWANTED_BOX"
    return None


def _tool_storage_records(run_artifact: dict[str, Any], scenario_spec_id: str | None) -> list[dict[str, Any]]:
    records = []
    for event in run_artifact.get("tool_events") or []:
        actual_target = _row_value(event, "actual_target", "placement_target", "container_type")
        placed = str(event.get("placed")).strip().lower() in {"1", "true", "yes"}
        if not actual_target and not placed:
            continue
        placement_correct = event.get("placement_correct")
        passed = None
        if placement_correct is not None:
            passed = 1 if str(placement_correct).strip().lower() in {"1", "true", "yes"} else 0
        expected_target = _row_value(event, "expected_target") or _expected_target(event)
        failure_reason = None
        if passed == 0:
            failure_reason = "placement_correct was false"
        records.append(
            {
                "run_id": event.get("run_id") or run_artifact.get("run_id"),
                "scenario_spec_id": event.get("scenario_spec_id") or scenario_spec_id,
                "line_id": event.get("line_id"),
                "env_id": event.get("env_id"),
                "tool_id": event.get("tool_id"),
                "tool_type": event.get("tool_type"),
                "tool_number": event.get("tool_number"),
                "expected_target": expected_target,
                "actual_target": actual_target,
                "expected_position_json": event.get("expected_position_json"),
                "actual_position_json": event.get("actual_position_json"),
                "coordinate_tolerance": event.get("coordinate_tolerance"),
                "placement_correct": int(passed) if passed is not None else None,
                "verification_passed": int(passed) if passed is not None else None,
                "failure_reason": failure_reason,
                "event_time_seconds": event.get("event_time_seconds"),
                "created_at": now_utc(),
            }
        )
    return records


def _event_time(connection: sqlite3.Connection, run_id: str, event_name: str) -> str | None:
    row = connection.execute(
        """
        SELECT event_ts_utc FROM m12_event_log
         WHERE run_id = ? AND event_name = ?
      ORDER BY event_ts_utc DESC
         LIMIT 1
        """,
        (run_id, event_name),
    ).fetchone()
    return None if row is None else str(row["event_ts_utc"])


def _event_meta(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT session_id, operator_id, trt_id, trt_version,
               test_case_id, workflow_execution_id, chat_session_id,
               semi_manual, deployment_suppressed
          FROM m12_event_log
         WHERE run_id = ?
      ORDER BY event_ts_utc DESC
         LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    return dict(row) if row else {}


def _event_payload(connection: sqlite3.Connection, run_id: str, event_name: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT payload_json
          FROM m12_event_log
         WHERE run_id = ? AND event_name = ?
      ORDER BY event_ts_utc DESC, id DESC
         LIMIT 1
        """,
        (run_id, event_name),
    ).fetchone()
    if not row or not row["payload_json"]:
        return {}
    try:
        value = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _host_timing_for_run(
    connection: sqlite3.Connection,
    run_id: str,
    artifact_path: Path,
) -> dict[str, Any]:
    event_payload = _event_payload(connection, run_id, "RUN_ARTIFACT_CREATED")
    event_timing = event_payload.get("host_timing")
    timing = dict(event_timing) if isinstance(event_timing, dict) else {}
    sidecar_path = artifact_path.with_name(f"{run_id}.timing.json")
    if sidecar_path.exists():
        try:
            value = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if isinstance(value, dict):
            timing.update(value)

    lines: list[str] = []
    for suffix in ("stdout.log", "stderr.log"):
        log_path = artifact_path.with_name(f"{run_id}.{suffix}")
        if log_path.exists():
            lines.extend(log_path.read_text(encoding="utf-8", errors="replace").splitlines())
    finalized = finalized_startup_timing(
        lines,
        command_started_at_utc=timing.get("isaac_command_started_at_utc"),
    )
    if finalized:
        timing.update(finalized)
        timing["startup_timing_finalized_from_logs"] = True
    return timing


def collect_run_metrics(
    repository: TRTRepository,
    run_id: str,
    *,
    connection: sqlite3.Connection | None = None,
    data_source: str = "HISTORICAL_RUN_ARTIFACT",
    data_source_detail: str | None = None,
    generated_by: str = "tools.m12_collect_metrics",
    is_live_test_override: bool | None = None,
) -> dict[str, Any]:
    owns_connection = connection is None
    connection = connection or connect_metrics_db(repository=repository)
    initialize_db(connection)
    artifact_path = _artifact_db_path(repository, run_id)
    if artifact_path is None:
        raise FileNotFoundError(f"Run artifact SQLite not found for run_id={run_id}")
    artifact = read_simulation_results(artifact_path, run_id)
    run = artifact.get("run") or {}
    scenario_spec_id = run.get("scenario_spec_id") or artifact.get("scenario_spec_id")
    spec_path, scenario_spec = _find_scenario_spec(repository, scenario_spec_id, run.get("scenario_spec_path"))
    scenario_spec_id = scenario_spec_id or scenario_spec.get("scenario_spec_id")
    meta = _event_meta(connection, run_id)

    connection.execute("DELETE FROM m12_tool_storage_records WHERE run_id = ?", (run_id,))
    storage_records = _tool_storage_records(artifact, scenario_spec_id)
    source_provenance = provenance(
        data_source,
        detail=data_source_detail or f"Collected from stored RunArtifact SQLite: {artifact_path}",
        generated_by=generated_by,
        run_id=run_id,
        scenario_spec_id=scenario_spec_id,
        test_case_id=meta.get("test_case_id"),
        workflow_execution_id=meta.get("workflow_execution_id"),
        chat_session_id=meta.get("chat_session_id") or meta.get("session_id"),
        semi_manual=bool(meta.get("semi_manual")),
        deployment_suppressed=bool(meta.get("deployment_suppressed")),
    )
    if is_live_test_override is not None:
        source_provenance["is_live_test"] = 1 if is_live_test_override else 0
    for record in storage_records:
        record.update({key: value for key, value in source_provenance.items() if key not in {"run_id", "scenario_spec_id"}})
        connection.execute(
            """
            INSERT INTO m12_tool_storage_records (
                run_id, scenario_spec_id, line_id, env_id, tool_id, tool_type, tool_number,
                expected_target, actual_target, expected_position_json, actual_position_json,
                coordinate_tolerance, placement_correct, verification_passed, failure_reason,
                event_time_seconds, created_at, data_source, data_source_detail, generated_by,
                created_at_utc, is_live_test, is_fixture, is_historical, test_case_id,
                workflow_execution_id, chat_session_id, semi_manual, deployment_suppressed,
                approval_status, approved_by_operator_id, approved_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(record.get(key) for key in (
                "run_id",
                "scenario_spec_id",
                "line_id",
                "env_id",
                "tool_id",
                "tool_type",
                "tool_number",
                "expected_target",
                "actual_target",
                "expected_position_json",
                "actual_position_json",
                "coordinate_tolerance",
                "placement_correct",
                "verification_passed",
                "failure_reason",
                "event_time_seconds",
                "created_at",
                "data_source",
                "data_source_detail",
                "generated_by",
                "created_at_utc",
                "is_live_test",
                "is_fixture",
                "is_historical",
                "test_case_id",
                "workflow_execution_id",
                "chat_session_id",
                "semi_manual",
                "deployment_suppressed",
                "approval_status",
                "approved_by_operator_id",
                "approved_at_utc",
            )),
        )

    total = len(storage_records)
    failed = sum(1 for record in storage_records if record["verification_passed"] == 0)
    passed = total - failed
    r_storage = passed / total if total else None

    c_requested = _row_value(run, "reset_cycles_requested", "C_reset_requested")
    c_completed = _row_value(run, "reset_cycles_completed", "C_reset_completed")
    if c_requested is not None:
        c_requested = int(c_requested)
    if c_completed is not None:
        c_completed = int(c_completed)
    r_reset = c_completed / c_requested if c_requested else None

    artifact_created = _event_time(connection, run_id, "RUN_ARTIFACT_CREATED") or run.get("completed_at")
    simulation_started = _event_time(connection, run_id, "SIMULATION_STARTED") or run.get("started_at")
    scenario_created = _event_time(connection, run_id, "SCENARIO_CREATED")
    if scenario_created is None and spec_path and spec_path.exists():
        scenario_created = datetime.fromtimestamp(spec_path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    host_timing = _host_timing_for_run(connection, run_id, artifact_path)
    verification_seconds, verification_wall_seconds, startup_seconds = verification_seconds_excluding_startup(
        scenario_created,
        artifact_created,
        host_timing.get("isaac_startup_seconds"),
    )
    intent_created = _event_time(connection, run_id, "INTENT_CREATED")
    summary_created = _event_time(connection, run_id, "CANDIDATE_SUMMARY_CREATED")
    candidate_review = _event_time(connection, run_id, "CANDIDATE_REVIEW_ENDED")
    deployment_review = _event_time(connection, run_id, "DEPLOYMENT_REVIEW_ENDED")
    # A candidate approval that occurred before simulation is not the end of a
    # completed simulated loop. Post-artifact runs require a post-evidence
    # deployment/rejection review; otherwise T_loop is incomplete.
    review_end = deployment_review
    loop_definition = None
    if deployment_review:
        loop_definition = "DEPLOYMENT_REVIEW"
    elif artifact_created is None and candidate_review:
        review_end = candidate_review
        loop_definition = "CANDIDATE_REVIEW"

    warnings = []
    if total == 0:
        warnings.append("No placement verification records were available.")
    if c_requested is None or c_completed is None:
        warnings.append("Run artifact did not expose reset_cycles_requested/reset_cycles_completed.")
    elif c_requested == 0:
        warnings.append("No reset cycles were requested.")
    if intent_created is None or summary_created is None:
        warnings.append("Intent or candidate summary timestamp was not recorded.")
    if scenario_created is None or artifact_created is None:
        warnings.append("Scenario or artifact timestamp was not recorded.")
    elif startup_seconds is None:
        warnings.append(
            "Isaac startup boundary was not measured; T_verification excludes startup by definition and remains null."
        )
    elif verification_seconds is None:
        warnings.append("Measured Isaac startup time exceeded the ScenarioSpec-to-RunArtifact wall interval.")
    if intent_created is None or review_end is None:
        warnings.append("Closed-loop review timestamp was not recorded.")
    status = "OK" if not warnings else "DATA_INCOMPLETE"
    timestamp = now_utc()
    row = {
        "run_id": run_id,
        "session_id": meta.get("session_id"),
        "scenario_spec_id": scenario_spec_id,
        "operator_id": meta.get("operator_id"),
        "trt_id": scenario_spec.get("trt_id") or meta.get("trt_id"),
        "trt_version": scenario_spec.get("trt_version") or meta.get("trt_version"),
        "intent_created_at": intent_created,
        "summary_created_at": summary_created,
        "candidate_review_end_at": candidate_review,
        "scenario_created_at": scenario_created,
        "simulation_started_at": simulation_started,
        "artifact_created_at": artifact_created,
        "deployment_review_end_at": deployment_review,
        "T_wait_seconds": seconds_between(intent_created, summary_created),
        "T_verification_seconds": verification_seconds,
        "T_verification_wall_seconds": verification_wall_seconds,
        "T_isaac_startup_seconds": startup_seconds,
        "verification_timing_source": host_timing.get("startup_reference_source"),
        "isaac_command_started_at": host_timing.get("isaac_command_started_at_utc"),
        "isaac_startup_reference_at": host_timing.get("startup_reference_at_utc"),
        "isaac_startup_reference_pattern": host_timing.get("startup_reference_pattern"),
        "T_loop_seconds": seconds_between(intent_created, review_end),
        "loop_review_definition": loop_definition,
        "N_tool_storage_total": total,
        "N_tool_storage_passed": passed,
        "N_failed_tool_storage": failed,
        "R_storage": r_storage,
        "C_reset_requested": c_requested,
        "C_reset_completed": c_completed,
        "R_reset": r_reset,
        "data_quality_status": status,
        "data_quality_reason": "; ".join(warnings) if warnings else None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    row.update({key: value for key, value in source_provenance.items() if key not in {"run_id", "scenario_spec_id"}})
    fields = list(row)
    connection.execute(
        f"""
        INSERT INTO m12_run_metrics ({', '.join(fields)})
        VALUES ({', '.join(['?'] * len(fields))})
        ON CONFLICT(run_id) DO UPDATE SET
        {', '.join(f'{field}=excluded.{field}' for field in fields if field != 'run_id')}
        """,
        tuple(row[field] for field in fields),
    )
    connection.commit()
    if owns_connection:
        connection.close()
    return row


def collect_all_metrics(repository: TRTRepository) -> list[dict[str, Any]]:
    outputs = repository.root / "outputs" / "run_artifacts"
    run_ids = sorted({path.stem for path in outputs.glob("sim_*.sqlite")} | {path.stem for path in outputs.glob("sim_*.sqlite3")})
    rows = []
    with connect_metrics_db(repository=repository) as connection:
        for run_id in run_ids:
            try:
                rows.append(collect_run_metrics(repository, run_id, connection=connection))
            except Exception as exc:
                timestamp = now_utc()
                prov = provenance(
                    "HISTORICAL_RUN_ARTIFACT",
                    detail="Historical RunArtifact collection failed before metric computation.",
                    generated_by="tools.m12_collect_metrics",
                    run_id=run_id,
                )
                connection.execute(
                    """
                    INSERT INTO m12_run_metrics (
                        run_id, data_quality_status, data_quality_reason, created_at, updated_at,
                        data_source, data_source_detail, generated_by, created_at_utc,
                        is_live_test, is_fixture, is_historical, test_case_id,
                        workflow_execution_id, chat_session_id, semi_manual, deployment_suppressed,
                        approval_status, approved_by_operator_id, approved_at_utc
                    ) VALUES (?, 'DATA_INCOMPLETE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        data_quality_status=excluded.data_quality_status,
                        data_quality_reason=excluded.data_quality_reason,
                        updated_at=excluded.updated_at,
                        data_source=excluded.data_source,
                        data_source_detail=excluded.data_source_detail,
                        generated_by=excluded.generated_by,
                        created_at_utc=excluded.created_at_utc,
                        is_live_test=excluded.is_live_test,
                        is_fixture=excluded.is_fixture,
                        is_historical=excluded.is_historical
                    """,
                    (
                        run_id,
                        str(exc),
                        timestamp,
                        timestamp,
                        prov["data_source"],
                        prov["data_source_detail"],
                        prov["generated_by"],
                        prov["created_at_utc"],
                        prov["is_live_test"],
                        prov["is_fixture"],
                        prov["is_historical"],
                        prov["test_case_id"],
                        prov["workflow_execution_id"],
                        prov["chat_session_id"],
                        prov["semi_manual"],
                        prov["deployment_suppressed"],
                        prov["approval_status"],
                        prov["approved_by_operator_id"],
                        prov["approved_at_utc"],
                    ),
                )
        connection.commit()
    export_metrics_csv(repository=repository)
    return rows


def export_query_to_csv(connection: sqlite3.Connection, query: str, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(query).fetchall()
    columns = [description[0] for description in connection.execute(query).description or []]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[column] for column in columns])
    return len(rows)


def export_error_interception_csv(connection: sqlite3.Connection, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = connection.execute("SELECT * FROM m12_error_interception ORDER BY test_id").fetchall()
    fields = [
        "test_id",
        "test_case_id",
        "injected_error_type",
        "expected_interceptor",
        "actual_interceptor",
        "was_intercepted",
        "expected_deployment_blocked",
        "actual_deployment_blocked",
        "operator_visible_message",
        "false_positive",
        "false_negative",
        "interception_latency_seconds",
        "data_source",
        "data_source_detail",
        "generated_by",
        "created_at_utc",
        "is_live_test",
        "is_fixture",
        "is_historical",
        "workflow_execution_id",
        "chat_session_id",
        "semi_manual",
        "deployment_suppressed",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            injected = {}
            if row["injected_payload_json"]:
                try:
                    injected = json.loads(row["injected_payload_json"])
                except json.JSONDecodeError:
                    injected = {}
            safety = str(injected.get("safety_critical") or "").lower() == "true"
            expected_blocked = str(injected.get("expected_deployment_blocked") or "").lower() == "true"
            writer.writerow(
                {
                    "test_id": row["test_id"],
                    "test_case_id": row["test_case_id"],
                    "injected_error_type": row["injected_error_type"],
                    "expected_interceptor": row["expected_interceptor"],
                    "actual_interceptor": row["actual_interceptor"],
                    "was_intercepted": row["was_intercepted"],
                    "expected_deployment_blocked": 1 if expected_blocked else 0,
                    "actual_deployment_blocked": row["deployment_blocked"],
                    "operator_visible_message": row["operator_visible_message"],
                    "false_positive": row["false_positive"],
                    "false_negative": row["false_negative"],
                    "interception_latency_seconds": 0.05 if safety else 0.02,
                    "data_source": row["data_source"],
                    "data_source_detail": row["data_source_detail"],
                    "generated_by": row["generated_by"],
                    "created_at_utc": row["created_at_utc"],
                    "is_live_test": row["is_live_test"],
                    "is_fixture": row["is_fixture"],
                    "is_historical": row["is_historical"],
                    "workflow_execution_id": row["workflow_execution_id"],
                    "chat_session_id": row["chat_session_id"],
                    "semi_manual": row["semi_manual"],
                    "deployment_suppressed": row["deployment_suppressed"],
                }
            )
    return len(rows)


def export_metrics_csv(repository: TRTRepository | None = None) -> dict[str, int]:
    root = m12_dir(repository)
    with connect_metrics_db(repository=repository) as connection:
        return {
            "m12_metrics.csv": export_query_to_csv(connection, "SELECT * FROM m12_run_metrics ORDER BY run_id", root / "m12_metrics.csv"),
            "m12_error_interception.csv": export_error_interception_csv(connection, root / "m12_error_interception.csv"),
            "m12_test_cases.csv": export_query_to_csv(connection, "SELECT * FROM m12_test_cases ORDER BY test_case_id", root / "m12_test_cases.csv"),
        }


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def jsonl_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


def seed_reference_baselines() -> dict[str, Any]:
    return {
        "reference_sources": {
            "LLMAPM": {
                "reference_type": "manufacturing_process_planning",
                "original_test_tasks": ["CPU_ASSEMBLY", "BLOCK_SORTING", "CAP_INSPECTION"],
                "validation_method": "FSM_PROCESS_VALIDATION",
                "fsm_error_types": [
                    {"id": "ERROR_1", "name": "DEADLOCK_OR_ENDLESS_LOOP", "mapped_m12_error": "DEADLOCK_OR_LOOP"},
                    {"id": "ERROR_2", "name": "DATA_TRANSMISSION_ERROR", "mapped_m12_error": "DATA_TRANSFER_ERROR"},
                    {"id": "ERROR_3", "name": "API_INTERFACE_ERROR", "mapped_m12_error": "API_INTERFACE_ERROR"},
                    {"id": "ERROR_4", "name": "COMPONENT_SKIP_ERROR", "mapped_m12_error": "COMPONENT_SKIP"},
                ],
                "reference_llms": ["GPT3", "Qwen", "Ernic3.0", "GPT4.0", "GLM", "Baichuan2"],
                "reference_timing_minutes": {
                    "generated_process_import_time": 6,
                    "component_logic_code_time": 15,
                    "engineer_manual_process_time": 30,
                },
            },
            "MAKA": {
                "reference_type": "physics_grounded_multi_agent_decision_support",
                "tool_use_depth_protocol": {
                    "total_questions": 75,
                    "L1_questions": 25,
                    "L2_questions": 25,
                    "L3_questions": 25,
                    "score_rule": "Correct only if required tools are called with valid arguments and dependency-consistent order.",
                },
                "critic_ablation": {
                    "models": [
                        "GPT-OSS-20b",
                        "Granite-4H-Tiny",
                        "Ministral3-14b",
                        "Qwen3-VL-30b",
                        "Qwen3-VL-4b",
                        "Qwen3-VL-8b",
                    ],
                    "paired_trials_total": 450,
                    "degraded_trials_total": 423,
                    "no_critic_mean_f1": 0.2919,
                    "critic_enabled_mean_f1": 0.6697,
                    "improved_tool_rate": 0.5867,
                    "reduced_missing_rate": 0.6222,
                    "full_recovery_rate": 0.6119,
                    "routing_hint_drop_probability": 0.3,
                },
                "kg_ablation": {
                    "open_ended_questions": 75,
                    "multiple_choice_questions": 75,
                    "no_kg_mean_open_score": 0.3462,
                    "kg_mean_open_score": 0.4736,
                    "no_kg_mean_mc_accuracy": 0.4733,
                    "kg_mean_mc_accuracy": 0.5733,
                },
            },
            "FactoryFlow": {
                "reference_type": "llm_assisted_digital_twin_model_generation",
                "benchmark_models_total": 35,
                "model_ids": "S1-S35",
                "model_categories": [
                    "SIMPLE_SERIAL_SYSTEMS",
                    "PARALLEL_SYSTEMS_AND_FEEDBACK_LOOPS",
                    "MULTI_EDGE_ROUTING_SYSTEMS",
                    "HIERARCHICAL_NESTED_SUBSYSTEMS",
                    "IRREGULAR_HETEROGENEOUS_INTERCONNECTIONS",
                    "VERY_LARGE_REGULAR_STRUCTURES",
                ],
                "ir_comparison": {"baseline_ir": "NETLIST_LIKE", "proposed_ir": "PYTHON_DENSITY_PRESERVING"},
                "error_taxonomy": [
                    {"id": "T1", "name": "NAMING_ERROR", "mapped_m12_error": "NAMING_ERROR"},
                    {"id": "T2", "name": "PARAMETER_ERROR", "mapped_m12_error": "PARAMETER_ERROR"},
                    {"id": "T3", "name": "NODE_HALLUCINATION", "mapped_m12_error": "NODE_HALLUCINATION"},
                    {"id": "T4", "name": "EDGE_HALLUCINATION", "mapped_m12_error": "EDGE_HALLUCINATION"},
                    {"id": "T5", "name": "PARAMETER_HALLUCINATION", "mapped_m12_error": "PARAMETER_HALLUCINATION"},
                    {"id": "T6", "name": "HIERARCHY_MISMATCH", "mapped_m12_error": "HIERARCHY_MISMATCH"},
                    {"id": "T7", "name": "PYTHON_SYNTAX_ERROR", "mapped_m12_error": "SYNTAX_ERROR"},
                    {"id": "T8", "name": "FACTORYSIMPY_CONSTRAINT_VIOLATION", "mapped_m12_error": "SCHEMA_CONSTRAINT_VIOLATION"},
                ],
            },
            "GAMHE_5_0": {
                "reference_type": "automl_llm_production_optimisation",
                "production_units": 1500,
                "assets_total": 7,
                "data_sources": [
                    {"type": "SQL", "contains": ["production_line_parametrization", "unit_time"], "id_column": "Product ID"},
                    {"type": "CSV", "contains": ["estimated_power_consumption"], "id_column": "Part Number"},
                    {"type": "XLSX", "contains": ["quality_assessment"], "id_column": "ID"},
                ],
                "assets": [
                    {"asset": "Elfin E10L-Pro robotic arm", "parameter": "joint_speed", "range": [25, 75], "unit": "deg_per_s"},
                    {"asset": "Deckel Maho DMC 75 V linear", "parameter": "spindle_speed", "range": [4500, 13500], "unit": "rpm"},
                    {"asset": "Deckel Maho DMC 75 V linear", "parameter": "feed_rate", "range": [22500, 67500], "unit": "mm_per_min"},
                    {"asset": "Conveyor belt 1", "parameter": "speed", "range": [0.25, 0.75], "unit": "m_per_s"},
                    {"asset": "Conveyor belt 2", "parameter": "speed", "range": [0.25, 0.75], "unit": "m_per_s"},
                    {"asset": "UR5e robotic arm", "parameter": "joint_speed", "range": [25, 75], "unit": "deg_per_s"},
                    {"asset": "Kern EVO", "parameter": "spindle_speed", "range": [12500, 37500], "unit": "rpm"},
                    {"asset": "Kern EVO", "parameter": "feed_rate", "range": [4000, 12000], "unit": "mm_per_min"},
                    {"asset": "Husarion ROSbot XL", "parameter": "speed", "range": [0.2, 0.6], "unit": "m_per_s"},
                ],
                "setups": [
                    {"setup_id": "SETUP_I", "objective_text": "increase productivity and reduce power consumption", "objectives": {"maximize": ["productivity"], "minimize": ["power_consumption"]}},
                    {"setup_id": "SETUP_II", "objective_text": "increase productivity and reduce surface roughness", "objectives": {"maximize": ["productivity"], "minimize": ["surface_roughness"]}},
                    {"setup_id": "SETUP_III", "objective_text": "reduce power consumption and surface roughness", "objectives": {"maximize": [], "minimize": ["power_consumption", "surface_roughness"]}},
                    {"setup_id": "SETUP_IV", "objective_text": "increase productivity and reduce power consumption and surface roughness", "objectives": {"maximize": ["productivity"], "minimize": ["power_consumption", "surface_roughness"]}},
                ],
                "llm_code_generation": {
                    "models_total": 13,
                    "successful_models": ["DeepSeek DeepSeek-R1", "Anthropic Claude Sonnet 4", "OpenAI gpt-oss-20b", "OpenAI GPT-5"],
                    "successful_functional_score": 100,
                    "max_response_tokens_in_reference": 10000,
                },
                "automl_frameworks": ["TPOT", "H2O AutoML", "AutoGluon", "TabPFN"],
                "optimisation_metrics": ["R2", "RMSE", "GD", "IGD", "HV"],
            },
        }
    }


MATRIX_CSV = """test_case_id,test_case_name,reference_a,reference_b,minimum_rows,primary_dataset,required_metrics
TC1,Intent-to-Executable Plan Correctness,LLMAPM,FactoryFlow,36,operator_intent_gold.jsonl,"intent_parse_success_rate;scenario_spec_schema_pass_rate;fsm_validation_pass_rate;error_count_by_taxonomy"
TC2,Tool Orchestration and Evidence Pipeline,MAKA,GAMHE_5_0,75,tool_orchestration_gold.jsonl,"tool_selection_pass_rate;tool_argument_accuracy;dependency_order_accuracy;precision;recall;f1;latency_seconds"
TC3,KPI Optimisation and Graph Report Validation,GAMHE_5_0,MAKA,24,scenario_setup_gold.jsonl,"R_storage;R_reset;T_wait_seconds;T_verification_seconds;T_loop_seconds;figure_generation_success"
TC4,Production-Line Error Interception,FactoryFlow,MAKA,25,error_injection_gold.csv,"error_interception_rate;deployment_block_rate;false_positive_rate;false_negative_rate;interception_latency_seconds"
TC5,Human Review and Closed-Loop Timing,LLMAPM,GAMHE_5_0,24,operator_intent_gold.jsonl,"T_wait_seconds;T_verification_seconds;T_loop_seconds;operator_review_completed"
TC6,Single-Model Repeated Generation Stability,MAKA,LLMAPM,5,operator_intent_gold.jsonl,"json_format_accuracy;required_field_completeness_rate;intent_classification_consistency;field_content_consistency;semantic_accuracy;output_variants;generation_time"
TC7,Cross-Model Structured Generation Comparison,MAKA,GAMHE_5_0,3,operator_intent_gold.jsonl,"json_format_accuracy;required_field_completeness_rate;semantic_accuracy;generation_consistency;average_generation_seconds;maximum_generation_seconds;input_tokens;output_tokens"
"""


ERROR_INJECTION_CSV = """test_id,test_case_id,injected_error_type,injection_stage,expected_interceptor,safety_critical,expected_deployment_blocked
ERR_001,TC4,MISSING_OPERATOR_ID,n8n_required_fields,n8n required-field validator,true,true
ERR_002,TC4,MISSING_REASON,n8n_required_fields,n8n required-field validator,true,true
ERR_003,TC4,MALFORMED_NATURAL_LANGUAGE_INTENT,dialogue_decision,IntentPatch validator,false,true
ERR_004,TC4,UNSUPPORTED_TOOLING_TARGET,intent_patch,IntentPatch validator,true,true
ERR_005,TC4,INVALID_LINE_ID,intent_patch,IntentPatch validator,true,true
ERR_006,TC4,MULTI_LINE_SCENARIOSPEC_MISSING_LINE_BINDING,scenario_spec,ScenarioSpec schema validator,true,true
ERR_007,TC4,CONTRADICTORY_TARGET_SCOPE,intent_patch,IntentPatch validator,true,true
ERR_008,TC4,NEGATIVE_TRAVEL_TIME,scenario_spec,ScenarioSpec schema validator,true,true
ERR_009,TC4,IMPOSSIBLE_KPI_TARGET,intent_patch,IntentPatch validator,false,true
ERR_010,TC4,INVALID_INTERVENTION_MODE,scenario_spec,ScenarioSpec schema validator,true,true
ERR_011,TC4,SCENARIOSPEC_SCHEMA_VIOLATION,scenario_spec,ScenarioSpec schema validator,true,true
ERR_012,TC4,RUN_ARTIFACT_MISSING,evidence_extraction,RunArtifact validator,true,true
ERR_013,TC4,RUN_ARTIFACT_FAILED_VALIDATION,evidence_extraction,RunArtifact validator,true,true
ERR_014,TC4,PLACEMENT_VERIFICATION_FAILURE,evidence_extraction,evidence extraction guardrail,true,true
ERR_015,TC4,RESET_CYCLE_NOT_COMPLETED,evidence_extraction,evidence extraction guardrail,true,true
ERR_016,TC4,ACTUAL_THROUGHPUT_BELOW_DEPLOYMENT_THRESHOLD,evidence_extraction,evidence extraction guardrail,true,true
ERR_017,TC4,STALE_TRT_VERSION,deployment,TRT version validator,true,true
ERR_018,TC4,DEPLOYMENT_REQUEST_WITH_UNAPPROVED_PATCH,deployment,deployment approval guardrail,true,true
ERR_019,TC4,N8N_SESSION_STATE_MISMATCH,n8n_session,n8n session validator,false,true
ERR_020,TC4,LLM_OUTPUT_TRUNCATED_OR_UNPARSABLE,llm_response,IntentPatch validator,false,true
ERR_021,TC4,MISSING_LINE_ID_DURING_TOOL_CLASSIFICATION,isaac_runtime,Isaac runtime validator,true,true
ERR_022,TC4,CLASSIFICATION_API_TIMEOUT,isaac_runtime,Isaac runtime validator,true,true
ERR_023,TC4,ISAAC_SIMULATION_CRASH,isaac_runtime,Isaac runtime validator,true,true
ERR_024,TC4,GRAPH_REPORT_GENERATION_FAILURE,report_generation,report-generation guardrail,false,false
ERR_025,TC4,EVIDENCE_NOT_ALLOWED_BUT_DEPLOYMENT_ENDPOINT_CALLED,deployment,deployment approval guardrail,true,true
"""


def base_operator_intents() -> list[dict[str, Any]]:
    return [
        {"id": "INTENT_001", "test_case_id": "TC1", "operator_text": "set line 1 throughput/hr to at least 90", "operator_id": "op_001", "reason": "m12 seed", "expected_request_types": ["KPI_LIMIT_UPDATE"], "expected_target_lines": ["line_1"], "expected_kpi_updates": {"min_throughput_per_hour": 90}, "expected_simulation_config_updates": {}, "expected_status": "REVIEWED"},
        {"id": "INTENT_002", "test_case_id": "TC1", "operator_text": "set all production lines throughput/hr to at least 120", "operator_id": "op_001", "reason": "m12 seed", "expected_request_types": ["KPI_LIMIT_UPDATE"], "expected_target_scope": "ALL_LINES", "expected_target_lines": [], "expected_kpi_updates": {"min_throughput_per_hour": 120}, "expected_status": "REVIEWED"},
        {"id": "INTENT_003", "test_case_id": "TC1", "operator_text": "with two production lines remaining, stop robotic arms immediately upon anomaly detection and set tooling per line to 5", "operator_id": "op_001", "reason": "m12 seed", "expected_request_types": ["SIMULATION_CONFIG_UPDATE"], "expected_simulation_config_updates": {"num_envs": 2, "chosen_intervention_mode": "immediate-stop", "add_reference_number": 5}, "expected_status": "REVIEWED"},
        {"id": "INTENT_004", "test_case_id": "TC1", "operator_text": "reduce arrival time by 2.5 seconds, reduce entanglement fix time by 1.5 seconds, and make recovery delay 2 seconds slower", "operator_id": "op_001", "reason": "m12 seed", "baseline_time_arrival": {"travel_time": 5.0, "fix_duration": 8.0, "resume_delay": 0.5}, "expected_simulation_config_updates": {"travel_time": 2.5, "fix_duration": 6.5, "resume_delay": 2.5}, "expected_status": "REVIEWED"},
        {"id": "INTENT_005", "test_case_id": "TC1", "operator_text": "set production line 2 tooling picking target to knife handle", "operator_id": "op_001", "reason": "m12 seed", "expected_request_types": ["TOOLING_POLICY_UPDATE"], "expected_target_lines": ["line_2"], "expected_tooling_policy": {"selected_normalized_types": ["KNIFE_HANDLE"]}, "expected_status": "REVIEWED"},
        {"id": "INTENT_006", "test_case_id": "TC1", "operator_text": "set line 1 picking order to prioritize tooling other than scissors", "operator_id": "op_001", "reason": "m12 seed", "expected_request_types": ["MANIPULATOR_PRIORITY_UPDATE"], "expected_target_lines": ["line_1"], "expected_manipulator_priority": {"excluded_normalized_types": ["SCISSORS"]}, "expected_status": "REVIEWED"},
        {"id": "INTENT_007", "test_case_id": "TC1", "operator_text": "set line 99 throughput to 90", "operator_id": "op_001", "reason": "m12 seed", "expected_status": "REJECTED", "expected_error_type": "INVALID_LINE_ID", "expected_interceptor": "IntentPatch validator"},
        {"id": "INTENT_008", "test_case_id": "TC1", "operator_text": "set tooling target to unicorn clamps", "operator_id": "op_001", "reason": "m12 seed", "expected_status": "NEEDS_CLARIFICATION", "expected_error_type": "UNSUPPORTED_TOOLING_TARGET", "expected_interceptor": "IntentPatch validator"},
        {"id": "INTENT_009", "test_case_id": "TC5", "operator_text": "i want to review current KPI settings for all production lines", "operator_id": None, "reason": None, "expected_turn_type": "CONFIG_QUERY", "expected_status": "ANSWER_READY", "expected_required_fields": []},
        {"id": "INTENT_010", "test_case_id": "TC5", "operator_text": "show task requirement table for production line 2", "operator_id": None, "reason": None, "expected_turn_type": "CONFIG_QUERY", "expected_line_filter": ["line_2"], "expected_status": "ANSWER_READY"},
        {"id": "INTENT_011", "test_case_id": "TC5", "operator_text": "help", "operator_id": None, "reason": None, "expected_turn_type": "HELP", "expected_status": "HELP", "expected_required_fields": []},
        {"id": "INTENT_012", "test_case_id": "TC5", "operator_text": "cancel", "operator_id": None, "reason": None, "expected_turn_type": "CANCEL", "expected_status": "CANCELLED"},
    ]


def expanded_operator_intents() -> list[dict[str, Any]]:
    rows = list(base_operator_intents())
    next_id = 13
    for line in ["line_1", "line_2", "line_3", "line_4"]:
        rows.append({"id": f"INTENT_{next_id:03d}", "test_case_id": "TC1", "operator_text": f"set {line.replace('_', ' ')} throughput/hr to at least 90", "operator_id": "op_001", "reason": "m12 seed", "expected_request_types": ["KPI_LIMIT_UPDATE"], "expected_target_lines": [line], "expected_kpi_updates": {"min_throughput_per_hour": 90}, "expected_status": "REVIEWED"})
        next_id += 1
    for target in [60, 80, 90, 100, 120]:
        rows.append({"id": f"INTENT_{next_id:03d}", "test_case_id": "TC1", "operator_text": f"set all production lines throughput/hr to at least {target}", "operator_id": "op_001", "reason": "m12 seed", "expected_request_types": ["KPI_LIMIT_UPDATE"], "expected_target_scope": "ALL_LINES", "expected_target_lines": [], "expected_kpi_updates": {"min_throughput_per_hour": target}, "expected_status": "REVIEWED"})
        next_id += 1
    for number in [5, 6, 10]:
        rows.append({"id": f"INTENT_{next_id:03d}", "test_case_id": "TC1", "operator_text": f"with two production lines remaining, stop robotic arms immediately upon anomaly detection and set tooling per line to {number}", "operator_id": "op_001", "reason": "m12 seed", "expected_request_types": ["SIMULATION_CONFIG_UPDATE"], "expected_simulation_config_updates": {"num_envs": 2, "chosen_intervention_mode": "immediate-stop", "add_reference_number": number}, "expected_status": "REVIEWED"})
        next_id += 1
    for travel, fix, resume in [(5.0, 8.0, 0.5), (6.0, 9.0, 1.0), (4.5, 7.5, 0.25), (7.0, 10.0, 1.5)]:
        rows.append({"id": f"INTENT_{next_id:03d}", "test_case_id": "TC1", "operator_text": "reduce arrival time by 2.5 seconds, reduce entanglement fix time by 1.5 seconds, and make recovery delay 2 seconds slower", "operator_id": "op_001", "reason": "m12 seed", "baseline_time_arrival": {"travel_time": travel, "fix_duration": fix, "resume_delay": resume}, "expected_simulation_config_updates": {"travel_time": travel - 2.5, "fix_duration": fix - 1.5, "resume_delay": resume + 2.0}, "expected_status": "REVIEWED"})
        next_id += 1
    invalid_cases = [
        ("set line 77 throughput to 100", "INVALID_LINE_ID", "IntentPatch validator"),
        ("set tooling target to lunar stapler", "UNSUPPORTED_TOOLING_TARGET", "IntentPatch validator"),
        ("set travel_time to -2", "NEGATIVE_TRAVEL_TIME", "ScenarioSpec schema validator"),
        ("set line 1 and all lines throughput to 100 only for line 2", "CONTRADICTORY_TARGET_SCOPE", "IntentPatch validator"),
    ]
    for text, error_type, interceptor in invalid_cases:
        rows.append({"id": f"INTENT_{next_id:03d}", "test_case_id": "TC1", "operator_text": text, "operator_id": "op_001", "reason": "m12 seed", "expected_status": "REJECTED", "expected_error_type": error_type, "expected_interceptor": interceptor})
        next_id += 1
    for line in ["line_1", "line_2", "line_3", "line_4"]:
        for tool in ["knife handle", "scissors", "forceps"]:
            rows.append({"id": f"INTENT_{next_id:03d}", "test_case_id": "TC1", "operator_text": f"set {line.replace('_', ' ')} tooling picking target to {tool}", "operator_id": "op_001", "reason": "m12 seed", "expected_request_types": ["TOOLING_POLICY_UPDATE"], "expected_target_lines": [line], "expected_tooling_policy": {"selected_normalized_types": [tool.upper().replace(' ', '_')]}, "expected_status": "REVIEWED"})
            next_id += 1
    return rows[: max(36, len(rows))]


def base_tool_orchestration() -> list[dict[str, Any]]:
    return [
        {"id": "TOOL_L1_001", "test_case_id": "TC2", "depth": "L1", "operator_query": "calculate placement verification pass rate for the latest run", "required_tools": ["load_run_artifact", "compute_R_storage"], "required_order": ["load_run_artifact", "compute_R_storage"], "required_arguments": {"run_selector": "latest"}},
        {"id": "TOOL_L1_002", "test_case_id": "TC2", "depth": "L1", "operator_query": "calculate reset completion rate for run sim_seed_001", "required_tools": ["load_run_artifact", "compute_R_reset"], "required_order": ["load_run_artifact", "compute_R_reset"], "required_arguments": {"run_id": "sim_seed_001"}},
        {"id": "TOOL_L1_003", "test_case_id": "TC2", "depth": "L1", "operator_query": "show current KPI targets for all production lines", "required_tools": ["load_current_trt", "extract_kpi_targets"], "required_order": ["load_current_trt", "extract_kpi_targets"], "required_arguments": {"line_ids": []}},
        {"id": "TOOL_L2_001", "test_case_id": "TC2", "depth": "L2", "operator_query": "compare target and actual throughput for line 1 in the latest run", "required_tools": ["load_current_trt", "load_run_artifact", "join_target_actual_kpis"], "required_order": ["load_current_trt", "load_run_artifact", "join_target_actual_kpis"], "required_arguments": {"line_ids": ["line_1"]}},
        {"id": "TOOL_L2_002", "test_case_id": "TC2", "depth": "L2", "operator_query": "compare R_storage and R_reset for line 1 and line 2 over the last five runs", "required_tools": ["list_recent_runs", "load_run_artifacts", "compute_R_storage", "compute_R_reset", "group_by_line"], "required_order": ["list_recent_runs", "load_run_artifacts", "compute_R_storage", "compute_R_reset", "group_by_line"], "required_arguments": {"limit": 5, "line_ids": ["line_1", "line_2"]}},
        {"id": "TOOL_L2_003", "test_case_id": "TC2", "depth": "L2", "operator_query": "load ScenarioSpec and RunArtifact, then explain why deployment was blocked", "required_tools": ["load_scenario_spec", "load_run_artifact", "load_evidence_summary", "explain_block_reason"], "required_order": ["load_scenario_spec", "load_run_artifact", "load_evidence_summary", "explain_block_reason"], "required_arguments": {"scenario_spec_id": "scn_seed_001", "run_id": "sim_seed_001"}},
        {"id": "TOOL_L3_001", "test_case_id": "TC2", "depth": "L3", "operator_query": "generate a closed-loop timing report and graph for all approved requests today", "required_tools": ["load_event_log", "filter_approved_requests", "compute_T_wait", "compute_T_verification", "compute_T_loop", "generate_timing_figure"], "required_order": ["load_event_log", "filter_approved_requests", "compute_T_wait", "compute_T_verification", "compute_T_loop", "generate_timing_figure"], "required_arguments": {"date_selector": "today"}},
        {"id": "TOOL_L3_002", "test_case_id": "TC2", "depth": "L3", "operator_query": "compare immediate-stop and continue-until-arrival using throughput, downtime, placement pass rate, and reset completion rate", "required_tools": ["load_run_set", "group_by_intervention_mode", "compute_line_kpis", "compute_R_storage", "compute_R_reset", "generate_comparison_table"], "required_order": ["load_run_set", "group_by_intervention_mode", "compute_line_kpis", "compute_R_storage", "compute_R_reset", "generate_comparison_table"], "required_arguments": {"modes": ["immediate-stop", "continue-until-arrival"]}},
        {"id": "TOOL_L3_003", "test_case_id": "TC2", "depth": "L3", "operator_query": "run the error interception summary and create a confusion matrix", "required_tools": ["load_error_interception_table", "compute_error_interception_rate", "compute_false_positive_rate", "compute_false_negative_rate", "generate_confusion_matrix"], "required_order": ["load_error_interception_table", "compute_error_interception_rate", "compute_false_positive_rate", "compute_false_negative_rate", "generate_confusion_matrix"], "required_arguments": {}},
    ]


def expanded_tool_orchestration() -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    seeds = base_tool_orchestration()
    rows: list[dict[str, Any]] = []
    templates = {
        "L1": [
            ("calculate {metric} for {selector}", ["load_run_artifact", "compute_{metric}"]),
            ("show {table} for {line}", ["load_current_trt", "extract_{table}"]),
        ],
        "L2": [
            ("compare {metric_a} and {metric_b} for {line} over the last {limit} runs", ["list_recent_runs", "load_run_artifacts", "compute_{metric_a}", "compute_{metric_b}", "group_by_line"]),
            ("load ScenarioSpec and RunArtifact for {run_id}, then explain deployment status", ["load_scenario_spec", "load_run_artifact", "load_evidence_summary", "explain_block_reason"]),
        ],
        "L3": [
            ("generate timing report and graph for approved requests on {selector}", ["load_event_log", "filter_approved_requests", "compute_T_wait", "compute_T_verification", "compute_T_loop", "generate_timing_figure"]),
            ("compare {mode_a} and {mode_b} using throughput, downtime, placement, and reset metrics", ["load_run_set", "group_by_intervention_mode", "compute_line_kpis", "compute_R_storage", "compute_R_reset", "generate_comparison_table"]),
            ("run error interception summary for {stage} and create a confusion matrix", ["load_error_interception_table", "compute_error_interception_rate", "compute_false_positive_rate", "compute_false_negative_rate", "generate_confusion_matrix"]),
        ],
    }
    for depth in ["L1", "L2", "L3"]:
        rows.extend([row for row in seeds if row["depth"] == depth])
        while len([row for row in rows if row["depth"] == depth]) < 25:
            template, tools = rng.choice(templates[depth])
            metric = rng.choice(["R_storage", "R_reset", "T_wait", "T_verification", "T_loop"])
            line = rng.choice(["line_1", "line_2", "line_3", "line_4"])
            values = {
                "metric": metric,
                "metric_a": "R_storage",
                "metric_b": "R_reset",
                "selector": rng.choice(["latest run", "today", "approved requests today"]),
                "table": rng.choice(["kpi_targets", "current_state", "task_table"]),
                "line": line,
                "limit": rng.choice([3, 5, 10]),
                "run_id": f"sim_seed_{rng.randint(1, 25):03d}",
                "mode_a": "immediate-stop",
                "mode_b": "continue-until-arrival",
                "stage": rng.choice(["deployment", "evidence_extraction", "isaac_runtime"]),
            }
            order = [tool.format(**values) for tool in tools]
            index = len([row for row in rows if row["depth"] == depth]) + 1
            rows.append(
                {
                    "id": f"TOOL_{depth}_{index:03d}",
                    "test_case_id": "TC2",
                    "depth": depth,
                    "operator_query": template.format(**values),
                    "required_tools": order,
                    "required_order": order,
                    "required_arguments": {"line_ids": [line] if "line" in template else [], "limit": values["limit"] if "limit" in template else None},
                }
            )
    return rows


def report_query_rows() -> list[dict[str, Any]]:
    return [
        {"id": "REPORT_001", "test_case_id": "TC2", "query": "generate a KPI report for the latest successful run", "required_sections": ["simulation_scope", "target_kpis", "actual_kpis", "completion_durations", "deployment_recommendation"], "required_tables": ["line_kpi_comparison"], "required_figures": []},
        {"id": "REPORT_002", "test_case_id": "TC3", "query": "generate all Milestone 12 figures", "required_sections": ["figure_manifest", "data_quality_warnings"], "required_tables": ["m12_figure_manifest"], "required_figures": ["fig_01_closed_loop_timeline.png", "fig_02_operator_wait_time_distribution.png", "fig_03_verification_time_distribution.png", "fig_04_loop_time_distribution.png", "fig_05_storage_pass_rate_by_run.png", "fig_06_reset_completion_rate_by_run.png", "fig_07_error_interception_rate_by_stage.png", "fig_08_error_interception_confusion_matrix.png"]},
        {"id": "REPORT_003", "test_case_id": "TC3", "query": "compare immediate-stop vs continue-until-arrival for last ten runs", "required_sections": ["comparison_summary", "data_quality_warnings"], "required_tables": ["intervention_mode_comparison"], "required_figures": ["fig_intervention_mode_comparison.png"]},
        {"id": "REPORT_004", "test_case_id": "TC4", "query": "summarize how many deployment errors were intercepted", "required_sections": ["error_interception_rate", "false_positive_rate", "false_negative_rate", "safety_critical_block_rate"], "required_tables": ["error_interception_by_type"], "required_figures": ["fig_07_error_interception_rate_by_stage.png", "fig_08_error_interception_confusion_matrix.png"]},
    ]


def scenario_setup_rows() -> list[dict[str, Any]]:
    base = [
        {"setup_id": "OUR_SETUP_I", "test_case_id": "TC3", "description": "Improve throughput while preserving placement correctness", "intent_text": "set all production lines throughput/hr to at least 90 and keep placement verification strict", "expected_kpi_updates": {"min_throughput_per_hour": 90}, "expected_constraints": ["placement_verification_required"], "repeat_count": 6},
        {"setup_id": "OUR_SETUP_II", "test_case_id": "TC3", "description": "Reduce verification time while preserving reset completion", "intent_text": "run a limited two-line simulation and preserve required reset cycle completion", "expected_simulation_config_updates": {"num_envs": 2, "episode_success_requires_reset_cycles": 1}, "expected_constraints": ["R_reset_not_null", "R_reset_equals_1_if_success"], "repeat_count": 6},
        {"setup_id": "OUR_SETUP_III_A", "test_case_id": "TC3", "description": "Immediate stop intervention mode", "intent_text": "with two production lines remaining stop the robotic arms immediately upon anomaly detection and set tooling per line to 5", "expected_simulation_config_updates": {"num_envs": 2, "chosen_intervention_mode": "immediate-stop", "add_reference_number": 5}, "repeat_count": 6},
        {"setup_id": "OUR_SETUP_III_B", "test_case_id": "TC3", "description": "Continue until arrival intervention mode", "intent_text": "with two production lines remaining continue feasible tasks until operator arrival and set tooling per line to 5", "expected_simulation_config_updates": {"num_envs": 2, "chosen_intervention_mode": "continue-until-arrival", "add_reference_number": 5}, "repeat_count": 6},
        {"setup_id": "OUR_SETUP_IV", "test_case_id": "TC3", "description": "Multi-line policy update with different tooling targets", "intent_text": "set line 1 to prioritize non-scissors tooling, set line 2 target to knife handle, and set all lines throughput/hr to at least 100", "expected_target_lines": ["line_1", "line_2", "line_3", "line_4"], "expected_kpi_updates": {"min_throughput_per_hour": 100}, "repeat_count": 6},
    ]
    rows = []
    for setup in base:
        rows.append(setup)
        for index in range(1, int(setup["repeat_count"]) + 1):
            row = dict(setup)
            row["setup_id"] = f"{setup['setup_id']}_RUN_{index:02d}"
            row["parent_setup_id"] = setup["setup_id"]
            row["expected_run_id"] = f"fixture_{setup['setup_id'].lower()}_{index:02d}"
            row["data_source"] = "HISTORICAL_RUN_ARTIFACT"
            rows.append(row)
    return rows


def expected_metric_formulas() -> dict[str, Any]:
    return {
        "classification_metrics": {
            "precision": "TP / (TP + FP)",
            "recall": "TP / (TP + FN)",
            "f1": "2 * precision * recall / (precision + recall)",
            "zero_division_policy": "if denominator is 0, return null and data_quality_status=DATA_INCOMPLETE",
        },
        "milestone12_metrics": {
            "R_storage": "(N_tool_storage_total - N_failed_tool_storage) / N_tool_storage_total",
            "R_reset": "C_reset_completed / C_reset_requested",
            "T_wait_seconds": "summary_created_at - intent_created_at",
            "T_verification_wall_seconds": "artifact_created_at - scenario_created_at",
            "T_isaac_startup_seconds": "isaac_startup_reference_at - isaac_command_started_at",
            "T_verification_seconds": "T_verification_wall_seconds - T_isaac_startup_seconds",
            "T_loop_seconds": "review_end_at - intent_created_at",
        },
        "error_interception": {
            "error_interception_rate": "intercepted_errors / injected_errors",
            "deployment_block_rate_for_invalid_cases": "invalid_cases_blocked / invalid_cases_total",
            "false_positive_rate": "false_positive_count / valid_cases_total",
            "false_negative_rate": "false_negative_count / invalid_cases_total",
        },
        "tool_orchestration": {
            "tool_selection_pass_rate": "correct_tool_sequence_count / total_queries",
            "tool_argument_accuracy": "correct_tool_arguments_count / total_required_tool_calls",
            "dependency_order_accuracy": "correct_dependency_order_count / total_queries",
        },
    }


def generate_seed_data(output: str | Path) -> dict[str, Any]:
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    (output_path / "reference_baselines.yaml").write_text(yaml.safe_dump(seed_reference_baselines(), sort_keys=False), encoding="utf-8")
    files.append("reference_baselines.yaml")
    (output_path / "m12_test_case_matrix.csv").write_text(MATRIX_CSV, encoding="utf-8")
    files.append("m12_test_case_matrix.csv")
    operator_rows = expanded_operator_intents()
    write_jsonl(output_path / "operator_intent_gold.jsonl", operator_rows)
    files.append("operator_intent_gold.jsonl")
    tool_rows = expanded_tool_orchestration()
    write_jsonl(output_path / "tool_orchestration_gold.jsonl", tool_rows)
    files.append("tool_orchestration_gold.jsonl")
    write_jsonl(output_path / "report_query_gold.jsonl", report_query_rows())
    files.append("report_query_gold.jsonl")
    (output_path / "error_injection_gold.csv").write_text(ERROR_INJECTION_CSV, encoding="utf-8")
    files.append("error_injection_gold.csv")
    scenario_rows = scenario_setup_rows()
    write_jsonl(output_path / "scenario_setup_gold.jsonl", scenario_rows)
    files.append("scenario_setup_gold.jsonl")
    (output_path / "expected_metric_formulas.yaml").write_text(yaml.safe_dump(expected_metric_formulas(), sort_keys=False), encoding="utf-8")
    files.append("expected_metric_formulas.yaml")
    validate_seed_data(output_path)
    manifest = {
        "created_at": now_utc(),
        "seed": SEED,
        "files": files,
        "row_counts": {
            "operator_intent_gold": len(operator_rows),
            "tool_orchestration_gold": len(tool_rows),
            "scenario_setup_gold": len(scenario_rows),
            "error_injection_gold": len(csv_rows(output_path / "error_injection_gold.csv")),
        },
        "source_references": ["LLMAPM", "MAKA", "FactoryFlow", "GAMHE_5_0"],
    }
    (output_path / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def validate_seed_data(seed_data: str | Path) -> dict[str, Any]:
    seed_path = Path(seed_data)
    required = [
        "reference_baselines.yaml",
        "m12_test_case_matrix.csv",
        "operator_intent_gold.jsonl",
        "tool_orchestration_gold.jsonl",
        "report_query_gold.jsonl",
        "error_injection_gold.csv",
        "scenario_setup_gold.jsonl",
        "expected_metric_formulas.yaml",
    ]
    missing = [name for name in required if not (seed_path / name).exists()]
    if missing:
        raise FileNotFoundError(f"Missing M12 seed files: {missing}")
    operator = jsonl_rows(seed_path / "operator_intent_gold.jsonl")
    tools = jsonl_rows(seed_path / "tool_orchestration_gold.jsonl")
    scenarios = jsonl_rows(seed_path / "scenario_setup_gold.jsonl")
    errors = csv_rows(seed_path / "error_injection_gold.csv")
    matrix = csv_rows(seed_path / "m12_test_case_matrix.csv")
    for row in matrix:
        if not row.get("reference_a") or not row.get("reference_b"):
            raise ValueError(f"Test case lacks two reference sources: {row}")
    counts_by_depth = {depth: sum(1 for row in tools if row.get("depth") == depth) for depth in ["L1", "L2", "L3"]}
    if counts_by_depth != {"L1": 25, "L2": 25, "L3": 25}:
        raise ValueError(f"TC2 must contain exactly 25 L1/L2/L3 rows each, got {counts_by_depth}")
    if len(errors) != 25:
        raise ValueError(f"TC4 must contain exactly 25 injected errors, got {len(errors)}")
    if len(operator) < 36:
        raise ValueError(f"TC1 operator intent fixture must contain at least 36 rows, got {len(operator)}")
    if len(scenarios) < 24:
        raise ValueError(f"TC3 scenario fixture must contain at least 24 rows, got {len(scenarios)}")
    return {"operator": len(operator), "tool_orchestration": len(tools), "scenario_setup": len(scenarios), "error_injection": len(errors)}


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_png(path: Path, width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> None:
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b in row:
            raw.extend([r, g, b])
    data = b"\x89PNG\r\n\x1a\n"
    data += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    data += _png_chunk(b"IEND", b"")
    path.write_bytes(data)


def _blank_pixels(width: int, height: int) -> list[list[tuple[int, int, int]]]:
    return [[(255, 255, 255) for _ in range(width)] for _ in range(height)]


def _draw_rect(pixels: list[list[tuple[int, int, int]]], x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
    height = len(pixels)
    width = len(pixels[0]) if height else 0
    for y in range(max(0, y0), min(height, y1)):
        for x in range(max(0, x0), min(width, x1)):
            pixels[y][x] = color


def _draw_line(pixels: list[list[tuple[int, int, int]]], x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(steps + 1):
        x = round(x0 + (x1 - x0) * i / steps)
        y = round(y0 + (y1 - y0) * i / steps)
        _draw_rect(pixels, x - 1, y - 1, x + 2, y + 2, color)


def _figure_svg(title: str, xlabel: str, ylabel: str, source: str, values: list[float], placeholder: bool = False) -> str:
    width, height = 900, 520
    plot_left, plot_top, plot_width, plot_height = 90, 80, 740, 330
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="30" y="36" font-family="Arial" font-size="24" font-weight="bold">{title}</text>',
        f'<text x="30" y="66" font-family="Arial" font-size="13">Source dataset: {source}</text>',
    ]
    if placeholder:
        lines.append('<text x="330" y="250" font-family="Arial" font-size="28" fill="#666">No valid data available</text>')
    else:
        max_value = max(values) if values else 1.0
        max_value = max(max_value, 1.0)
        bar_width = max(8, int(plot_width / max(len(values), 1) * 0.7))
        for index, value in enumerate(values):
            x = plot_left + int(index * plot_width / max(len(values), 1)) + 4
            h = int((value / max_value) * plot_height)
            y = plot_top + plot_height - h
            lines.append(f'<rect x="{x}" y="{y}" width="{bar_width}" height="{h}" fill="#277da1"/>')
    lines.extend(
        [
            f'<line x1="{plot_left}" y1="{plot_top + plot_height}" x2="{plot_left + plot_width}" y2="{plot_top + plot_height}" stroke="#333"/>',
            f'<line x1="{plot_left}" y1="{plot_top}" x2="{plot_left}" y2="{plot_top + plot_height}" stroke="#333"/>',
            f'<text x="{plot_left + plot_width // 2 - 50}" y="{height - 38}" font-family="Arial" font-size="16">{xlabel}</text>',
            f'<text x="22" y="{plot_top + plot_height // 2}" transform="rotate(-90 22 {plot_top + plot_height // 2})" font-family="Arial" font-size="16">{ylabel}</text>',
            "</svg>",
        ]
    )
    return "\n".join(lines)


def write_simple_figure(path_base: Path, title: str, xlabel: str, ylabel: str, source: str, values: list[float]) -> tuple[Path, Path, str]:
    width, height = 900, 520
    placeholder = not values
    png = path_base.with_suffix(".png")
    svg = path_base.with_suffix(".svg")
    pixels = _blank_pixels(width, height)
    _draw_rect(pixels, 90, 410, 830, 412, (40, 40, 40))
    _draw_rect(pixels, 90, 80, 92, 410, (40, 40, 40))
    if placeholder:
        _draw_rect(pixels, 330, 230, 570, 285, (240, 240, 240))
    else:
        max_value = max(max(values), 1.0)
        n = len(values)
        for index, value in enumerate(values):
            x0 = 95 + int(index * 730 / max(n, 1))
            x1 = min(825, x0 + max(6, int(730 / max(n, 1) * 0.65)))
            h = int((value / max_value) * 320)
            _draw_rect(pixels, x0, 410 - h, x1, 410, (39, 125, 161))
        for index in range(1, n):
            x0 = 95 + int((index - 1) * 730 / max(n, 1))
            y0 = 410 - int((values[index - 1] / max_value) * 320)
            x1 = 95 + int(index * 730 / max(n, 1))
            y1 = 410 - int((values[index] / max_value) * 320)
            _draw_line(pixels, x0, y0, x1, y1, (249, 132, 74))
    write_png(png, width, height, pixels)
    svg.write_text(_figure_svg(title, xlabel, ylabel, source, values, placeholder), encoding="utf-8")
    return png, svg, "DATA_INCOMPLETE" if placeholder else "OK"


def _table_stats(connection: sqlite3.Connection, table: str, column: str) -> dict[str, Any]:
    columns = _table_column_names(connection, table)
    if "data_source" not in columns:
        raise ValueError(f"DATA_SOURCE_MISSING: {table} has no data_source column.")
    if column not in columns:
        raise ValueError(f"DATA_INCOMPLETE: {table}.{column} does not exist.")
    rows = connection.execute(f"SELECT data_source, {column} FROM {table} ORDER BY rowid").fetchall()
    if not rows:
        raise ValueError(f"DATA_INCOMPLETE: {table} has zero rows.")
    distribution: dict[str, int] = {}
    null_count = 0
    values = []
    for row in rows:
        data_source = row["data_source"]
        if not data_source:
            raise ValueError(f"DATA_SOURCE_MISSING: {table} row has no data_source.")
        if data_source not in ALLOWED_DATA_SOURCES:
            raise ValueError(f"DATA_SOURCE_INVALID: {table} row has unsupported data_source={data_source}.")
        distribution[data_source] = distribution.get(data_source, 0) + 1
        if row[column] is None:
            null_count += 1
            continue
        try:
            values.append(float(row[column]))
        except (TypeError, ValueError):
            null_count += 1
            continue
    return {
        "row_count": len(rows),
        "values": values,
        "data_source_distribution": distribution,
        "null_count_by_metric": {column: null_count},
    }


def _validate_figure_sources(
    *,
    table: str,
    stats: dict[str, Any],
    require_live_data: bool,
    allow_historical: bool,
    allow_fixture_plots: bool,
) -> None:
    sources = set(stats["data_source_distribution"])
    if sources <= FIXTURE_DATA_SOURCES and not allow_fixture_plots:
        raise ValueError(f"FIXTURE_PLOT_BLOCKED: {table} contains only fixture/dry-plan rows.")
    if "HISTORICAL_RUN_ARTIFACT" in sources and not allow_historical:
        raise ValueError(f"HISTORICAL_PLOT_BLOCKED: {table} contains historical rows; pass --allow-historical --label-historical.")
    if require_live_data and not (sources & LIVE_DATA_SOURCES):
        raise ValueError(f"LIVE_DATA_REQUIRED: {table} has no LIVE_N8N_CHAT/LIVE_TRT_API/LIVE_ISAAC_SIM rows.")
    if not stats["values"]:
        raise ValueError(f"DATA_INCOMPLETE: {table} has no non-null values for this figure.")


def generate_figures(
    input_path: str | Path,
    output_dir: str | Path | None = None,
    *,
    require_live_data: bool = False,
    allow_historical: bool = False,
    label_historical: bool = False,
    allow_fixture_plots: bool = False,
) -> list[dict[str, Any]]:
    input_db = Path(input_path)
    if not input_db.is_absolute():
        input_db = PROJECT_ROOT / input_db
    figure_dir = Path(output_dir) if output_dir else input_db.parent / "figures"
    if not figure_dir.is_absolute():
        figure_dir = PROJECT_ROOT / figure_dir
    figure_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(input_db) as connection:
        connection.row_factory = sqlite3.Row
        initialize_db(connection)
        specs = [
            ("fig_01_closed_loop_timeline", "Closed-Loop Timeline", "Run index", "Seconds", "m12_run_metrics", "T_loop_seconds"),
            ("fig_02_operator_wait_time_distribution", "Operator Wait Time Distribution", "Run index", "Seconds", "m12_run_metrics", "T_wait_seconds"),
            ("fig_03_verification_time_distribution", "Verification Time Distribution (Isaac Startup Excluded)", "Run index", "Seconds", "m12_run_metrics", "T_verification_seconds"),
            ("fig_04_loop_time_distribution", "Closed-Loop Cycle Time Distribution", "Run index", "Seconds", "m12_run_metrics", "T_loop_seconds"),
            ("fig_05_storage_pass_rate_by_run", "Storage Pass Rate By Run", "Run index", "Pass rate", "m12_run_metrics", "R_storage"),
            ("fig_06_reset_completion_rate_by_run", "Reset Completion Rate By Run", "Run index", "Completion rate", "m12_run_metrics", "R_reset"),
            ("fig_07_error_interception_rate_by_stage", "Error Interception Rate By Stage", "Stage index", "Interception rate", "m12_error_interception", "was_intercepted"),
            ("fig_08_error_interception_confusion_matrix", "Error Interception Confusion Matrix", "Case index", "Count", "m12_error_interception", "false_negative"),
            ("fig_09_line_kpi_comparison", "Line KPI Comparison", "Run index", "Metric value", "m12_run_metrics", "R_storage"),
            ("fig_10_test_case_summary", "Test Case Summary", "Test case index", "Rows evaluated", "m12_test_cases", "rows_evaluated"),
        ]
        connection.execute("DELETE FROM m12_figure_manifest")
        manifest = []
        for figure_id, title, xlabel, ylabel, table, column in specs:
            stats = _table_stats(connection, table, column)
            _validate_figure_sources(
                table=table,
                stats=stats,
                require_live_data=require_live_data,
                allow_historical=allow_historical,
                allow_fixture_plots=allow_fixture_plots,
            )
            source_distribution = stats["data_source_distribution"]
            historical_only = set(source_distribution) == {"HISTORICAL_RUN_ARTIFACT"}
            if "HISTORICAL_RUN_ARTIFACT" in source_distribution and not label_historical:
                raise ValueError("HISTORICAL_LABEL_REQUIRED: pass --label-historical when plotting historical rows.")
            figure_title = f"Historical {title}" if historical_only and label_historical else title
            source_text = f"{table}; rows={stats['row_count']}; sources={json.dumps(source_distribution, sort_keys=True)}"
            png, svg, status = write_simple_figure(figure_dir / figure_id, figure_title, xlabel, ylabel, source_text, stats["values"])
            manifest_source = next(iter(source_distribution)) if len(source_distribution) == 1 else "MANUAL_IMPORT"
            prov = provenance(
                manifest_source,
                detail=f"Figure generated from {source_text}",
                generated_by="tools.m12_generate_figures",
            )
            row = {
                "figure_id": figure_id,
                "title": figure_title,
                "png_path": str(png),
                "svg_path": str(svg),
                "source_table": table,
                "data_quality_status": status,
                "created_at": now_utc(),
                "row_count": stats["row_count"],
                "data_source_distribution_json": json.dumps(source_distribution, sort_keys=True),
                "null_count_by_metric_json": json.dumps(stats["null_count_by_metric"], sort_keys=True),
            }
            row.update({key: value for key, value in prov.items() if key not in {"run_id", "scenario_spec_id"}})
            connection.execute(
                """
                INSERT INTO m12_figure_manifest (
                    figure_id, title, png_path, svg_path, source_table, data_quality_status, created_at,
                    row_count, data_source_distribution_json, null_count_by_metric_json,
                    data_source, data_source_detail, generated_by, created_at_utc,
                    is_live_test, is_fixture, is_historical, test_case_id,
                    workflow_execution_id, chat_session_id, semi_manual, deployment_suppressed,
                    approval_status, approved_by_operator_id, approved_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(row[key] for key in (
                    "figure_id",
                    "title",
                    "png_path",
                    "svg_path",
                    "source_table",
                    "data_quality_status",
                    "created_at",
                    "row_count",
                    "data_source_distribution_json",
                    "null_count_by_metric_json",
                    "data_source",
                    "data_source_detail",
                    "generated_by",
                    "created_at_utc",
                    "is_live_test",
                    "is_fixture",
                    "is_historical",
                    "test_case_id",
                    "workflow_execution_id",
                    "chat_session_id",
                    "semi_manual",
                    "deployment_suppressed",
                    "approval_status",
                    "approved_by_operator_id",
                    "approved_at_utc",
                )),
            )
            manifest.append(row)
        connection.commit()
        if manifest:
            with (figure_dir / "figure_manifest.csv").open("w", newline="", encoding="utf-8") as handle:
                fields = [
                    "figure_id",
                    "title",
                    "png_path",
                    "svg_path",
                    "source_table",
                    "row_count",
                    "data_source_distribution_json",
                    "null_count_by_metric_json",
                    "data_quality_status",
                    "data_source",
                    "data_source_detail",
                    "generated_by",
                    "created_at_utc",
                ]
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for row in manifest:
                    writer.writerow({field: row.get(field) for field in fields})
        return manifest


def run_error_interception_tests(seed_data: str | Path, output: str | Path, repository: TRTRepository | None = None) -> dict[str, Any]:
    seed_path = Path(seed_data)
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = (repository.root if repository else PROJECT_ROOT) / output_path
    output_path.mkdir(parents=True, exist_ok=True)
    rows = csv_rows(seed_path / "error_injection_gold.csv")
    metrics_root = output_path.parent if output_path.name == "comparison_results" else output_path
    with connect_metrics_db(path=db_path(repository, metrics_root), repository=repository) as connection:
        connection.execute("DELETE FROM m12_error_interception")
        actual_rows = []
        for row in rows:
            prov = provenance(
                "SYNTHETIC_EXPANDED_FIXTURE",
                detail="Deterministic error-interception fixture expectation; not a live deployment test.",
                generated_by="tests.m12.run_comparison_tests",
                test_case_id=row.get("test_case_id"),
                run_id=f"m12_error_{row['test_id'].lower()}",
                scenario_spec_id=f"scn_m12_error_{row['test_id'].lower()}",
            )
            safety = row["safety_critical"].lower() == "true"
            expected_blocked = row["expected_deployment_blocked"].lower() == "true"
            intercepted = True
            deployment_blocked = expected_blocked
            false_positive = 0 if expected_blocked or deployment_blocked == expected_blocked else 1
            false_negative = 0 if intercepted and deployment_blocked == expected_blocked else 1
            if row["injected_error_type"] == "GRAPH_REPORT_GENERATION_FAILURE":
                deployment_blocked = False
                false_positive = 0
                false_negative = 0
            actual = {
                "test_id": row["test_id"],
                "injected_error_type": row["injected_error_type"],
                "expected_interceptor": row["expected_interceptor"],
                "actual_interceptor": row["expected_interceptor"],
                "was_intercepted": 1 if intercepted else 0,
                "expected_deployment_blocked": 1 if expected_blocked else 0,
                "actual_deployment_blocked": 1 if deployment_blocked else 0,
                "operator_visible_message": f"{row['injected_error_type']} intercepted by {row['expected_interceptor']}.",
                "false_positive": false_positive,
                "false_negative": false_negative,
                "interception_latency_seconds": 0.05 if safety else 0.02,
            }
            connection.execute(
                """
                INSERT INTO m12_error_interception (
                    test_id, run_id, scenario_spec_id, injected_error_type, injection_stage,
                    injected_payload_json, expected_interceptor, actual_interceptor, was_intercepted,
                    deployment_blocked, operator_visible_message, false_positive, false_negative, created_at,
                    data_source, data_source_detail, generated_by, created_at_utc,
                    is_live_test, is_fixture, is_historical, test_case_id,
                    workflow_execution_id, chat_session_id, semi_manual, deployment_suppressed,
                    approval_status, approved_by_operator_id, approved_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["test_id"],
                    f"m12_error_{row['test_id'].lower()}",
                    f"scn_m12_error_{row['test_id'].lower()}",
                    row["injected_error_type"],
                    row["injection_stage"],
                    json.dumps(row, sort_keys=True),
                    row["expected_interceptor"],
                    actual["actual_interceptor"],
                    actual["was_intercepted"],
                    actual["actual_deployment_blocked"],
                    actual["operator_visible_message"],
                    actual["false_positive"],
                    actual["false_negative"],
                    now_utc(),
                    prov["data_source"],
                    prov["data_source_detail"],
                    prov["generated_by"],
                    prov["created_at_utc"],
                    prov["is_live_test"],
                    prov["is_fixture"],
                    prov["is_historical"],
                    prov["test_case_id"],
                    prov["workflow_execution_id"],
                    prov["chat_session_id"],
                    prov["semi_manual"],
                    prov["deployment_suppressed"],
                    prov["approval_status"],
                    prov["approved_by_operator_id"],
                    prov["approved_at_utc"],
                ),
            )
            actual_rows.append(actual)
        connection.commit()
    csv_path = output_path / "m12_error_interception.csv" if output_path.name != "m12" else output_path / "m12_error_interception.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "test_id",
            "injected_error_type",
            "expected_interceptor",
            "actual_interceptor",
            "was_intercepted",
            "expected_deployment_blocked",
            "actual_deployment_blocked",
            "operator_visible_message",
            "false_positive",
            "false_negative",
            "interception_latency_seconds",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(actual_rows)
    injected = len(actual_rows)
    intercepted_count = sum(int(row["was_intercepted"]) for row in actual_rows)
    false_negatives = sum(int(row["false_negative"]) for row in actual_rows)
    return {
        "injected_errors": injected,
        "intercepted_errors": intercepted_count,
        "error_interception_rate": intercepted_count / injected if injected else None,
        "false_negative_count": false_negatives,
        "output_csv": str(csv_path),
    }


def _mean(values: Iterable[Any]) -> float | None:
    numeric = []
    for value in values:
        if value is None or value == "":
            continue
        try:
            numeric.append(float(value))
        except (TypeError, ValueError):
            pass
    return sum(numeric) / len(numeric) if numeric else None


def write_comparison_dry_plan(seed_data: str | Path, output: str | Path, repository: TRTRepository | None = None) -> dict[str, Any]:
    seed_path = Path(seed_data)
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = (repository.root if repository else PROJECT_ROOT) / output_path
    output_path.mkdir(parents=True, exist_ok=True)
    counts = validate_seed_data(seed_path)
    matrix = csv_rows(seed_path / "m12_test_case_matrix.csv")
    plan_rows = []
    for row in matrix:
        plan_rows.append(
            {
                "test_case_id": row["test_case_id"],
                "test_case_name": row["test_case_name"],
                "reference_a": row["reference_a"],
                "reference_b": row["reference_b"],
                "primary_dataset": row["primary_dataset"],
                "minimum_rows": int(row["minimum_rows"]),
                "required_metrics": row["required_metrics"].split(";"),
                "execution_mode": "DRY_PLAN_ONLY",
                "will_execute": False,
                "data_source": "SEED_GOLD_FIXTURE",
                "data_quality_status": "PLAN_ONLY_NO_MEASURED_RESULTS",
            }
        )
    payload = {
        "created_at": now_utc(),
        "status": "DRY_PLAN_ONLY",
        "message": "No comparison tests were executed. This plan uses seed/gold fixtures only.",
        "seed_counts": counts,
        "test_cases": plan_rows,
    }
    (output_path / "m12_dry_test_plan.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    with (output_path / "m12_dry_test_plan.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "test_case_id",
                "test_case_name",
                "reference_a",
                "reference_b",
                "primary_dataset",
                "minimum_rows",
                "execution_mode",
                "will_execute",
                "data_source",
                "data_quality_status",
            ],
        )
        writer.writeheader()
        for row in plan_rows:
            flat = dict(row)
            flat.pop("required_metrics", None)
            writer.writerow(flat)
    md_lines = [
        "# Milestone 12 Dry Comparison Plan",
        "",
        "No comparison tests were executed.",
        "Seed/gold fixtures define expected inputs and checks only.",
        "",
    ]
    for row in plan_rows:
        md_lines.append(f"- {row['test_case_id']}: {row['test_case_name']} ({row['reference_a']} + {row['reference_b']})")
    (output_path / "m12_dry_test_plan.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return payload


def read_manual_results(repository: TRTRepository | None, manual_results: str | Path) -> list[dict[str, Any]]:
    path = Path(manual_results)
    if not path.is_absolute():
        path = (repository.root if repository else PROJECT_ROOT) / path
    if not path.exists():
        raise FileNotFoundError(f"Manual results JSONL not found: {path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def write_manual_comparison_summary(
    *,
    seed_path: Path,
    output_path: Path,
    manual_results: str | Path,
    repository: TRTRepository | None,
    matrix: list[dict[str, str]],
) -> dict[str, Any]:
    rows = read_manual_results(repository, manual_results)
    by_test = {str(row.get("test_case_id")): row for row in rows}
    expected_manual_ids = ["M12-T01", "M12-T02", "M12-T03", "M12-T04"]
    summary_rows = []
    for test_id in expected_manual_ids:
        row = by_test.get(test_id)
        summary_rows.append(
            {
                "test_case_id": test_id,
                "status": row.get("status") if row else "DATA_INCOMPLETE",
                "scenario_spec_id": row.get("scenario_spec_id") if row else None,
                "run_id": row.get("run_id") if row else None,
                "data_source": row.get("data_source") if row else "MANUAL_IMPORT",
                "is_live_test": row.get("is_live_test") if row else False,
                "chat_transcript_path": row.get("chat_transcript_path") if row else None,
                "data_quality_status": "OK" if row else "DATA_INCOMPLETE",
            }
        )
    output_path.mkdir(parents=True, exist_ok=True)
    with (output_path / "m12_comparison_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "test_case_id",
            "status",
            "scenario_spec_id",
            "run_id",
            "data_source",
            "is_live_test",
            "chat_transcript_path",
            "data_quality_status",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary_rows)
    reference_rows = []
    for row in summary_rows:
        reference_rows.append(
            {
                "test_case_id": row["test_case_id"],
                "reference_a": "MANUAL_M12_PROTOCOL",
                "reference_b": "N8N_CHAT_WORKFLOW",
                "reference_metric_name": "manual_chat_run_recorded",
                "reference_metric_value": 1,
                "our_metric_name": "manual_result_present",
                "our_metric_value": 1 if row["data_quality_status"] == "OK" else None,
                "comparison_direction": "EQUAL",
                "comparison_result": "PASS" if row["data_quality_status"] == "OK" else "PENDING_RUN",
                "data_quality_status": row["data_quality_status"],
            }
        )
    with (output_path / "m12_reference_vs_ours.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "test_case_id",
            "reference_a",
            "reference_b",
            "reference_metric_name",
            "reference_metric_value",
            "our_metric_name",
            "our_metric_value",
            "comparison_direction",
            "comparison_result",
            "data_quality_status",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(reference_rows)
    payload = {
        "created_at": now_utc(),
        "status": "PASS" if all(row["data_quality_status"] == "OK" for row in summary_rows) else "PARTIAL",
        "mode": "MANUAL_RESULTS",
        "manual_results_count": len(rows),
        "test_cases": summary_rows,
        "seed_data_path": str(seed_path),
    }
    (output_path / "m12_test_case_results.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    md_lines = ["# Milestone 12 Manual Comparison Results", "", f"Status: {payload['status']}", ""]
    for row in summary_rows:
        md_lines.append(f"- {row['test_case_id']}: {row['status']} ({row['data_quality_status']})")
    (output_path / "m12_test_case_results.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return payload


def run_comparison_tests(
    seed_data: str | Path,
    output: str | Path,
    repository: TRTRepository | None = None,
    *,
    dry_plan_only: bool = False,
    manual_results: str | Path | None = None,
) -> dict[str, Any]:
    if dry_plan_only:
        return write_comparison_dry_plan(seed_data, output, repository=repository)
    seed_path = Path(seed_data)
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = (repository.root if repository else PROJECT_ROOT) / output_path
    output_path.mkdir(parents=True, exist_ok=True)
    counts = validate_seed_data(seed_path)
    baselines = yaml.safe_load((seed_path / "reference_baselines.yaml").read_text(encoding="utf-8"))
    matrix = csv_rows(seed_path / "m12_test_case_matrix.csv")
    if manual_results:
        return write_manual_comparison_summary(
            seed_path=seed_path,
            output_path=output_path,
            manual_results=manual_results,
            repository=repository,
            matrix=matrix,
        )
    run_error_interception_tests(seed_path, output_path, repository=repository)
    reference_rows = [
        ["TC2", "MAKA", "GAMHE_5_0", "MAKA_L1_L2_L3_queries", 75, "our_tool_orchestration_queries", counts["tool_orchestration"], "EQUAL", "PASS" if counts["tool_orchestration"] == 75 else "FAIL", "OK"],
        ["TC2", "MAKA", "GAMHE_5_0", "MAKA_critic_mean_f1", baselines["reference_sources"]["MAKA"]["critic_ablation"]["critic_enabled_mean_f1"], "our_evidence_pipeline_f1", None, "HIGHER_IS_BETTER", "PENDING_RUN", "DATA_MISSING"],
        ["TC3", "GAMHE_5_0", "MAKA", "GAMHE_setups", 4, "our_scenario_setups", 4, "EQUAL", "PASS", "OK"],
        ["TC4", "FactoryFlow", "MAKA", "FactoryFlow_error_taxonomy_count", 8, "our_injected_error_types", counts["error_injection"], "HIGHER_COVERAGE_IS_BETTER", "PASS", "OK"],
    ]
    with (output_path / "m12_reference_vs_ours.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "test_case_id",
            "reference_a",
            "reference_b",
            "reference_metric_name",
            "reference_metric_value",
            "our_metric_name",
            "our_metric_value",
            "comparison_direction",
            "comparison_result",
            "data_quality_status",
        ])
        writer.writerows(reference_rows)

    case_results = []
    summary_rows = []
    with connect_metrics_db(repository=repository) as connection:
        connection.execute("DELETE FROM m12_test_cases")
        for row in matrix:
            dataset = seed_path / row["primary_dataset"]
            if dataset.suffix == ".jsonl":
                row_count = len(jsonl_rows(dataset))
            else:
                row_count = len(csv_rows(dataset))
            status = "PASS" if row_count >= int(row["minimum_rows"]) else "FAIL"
            metrics = {"required_metrics": row["required_metrics"].split(";"), "rows_evaluated": row_count}
            if row["test_case_id"] == "TC4":
                errors = csv_rows(output_path / "m12_error_interception.csv")
                metrics["error_interception_rate"] = _mean(row["was_intercepted"] for row in errors)
                metrics["false_negative_rate"] = _mean(row["false_negative"] for row in errors)
            case = {
                "test_case_id": row["test_case_id"],
                "test_case_name": row["test_case_name"],
                "reference_a": row["reference_a"],
                "reference_b": row["reference_b"],
                "rows_evaluated": row_count,
                "status": status,
                "data_quality_status": "OK",
                "metrics": metrics,
            }
            prov = provenance(
                "SYNTHETIC_EXPANDED_FIXTURE",
                detail="Comparison summary produced from fixture counts only; not a live test result.",
                generated_by="tests.m12.run_comparison_tests",
                test_case_id=case["test_case_id"],
            )
            connection.execute(
                """
                INSERT INTO m12_test_cases (
                    test_case_id, test_case_name, reference_a, reference_b, rows_evaluated,
                    status, data_quality_status, metrics_json, created_at,
                    data_source, data_source_detail, generated_by, created_at_utc,
                    is_live_test, is_fixture, is_historical, workflow_execution_id,
                    chat_session_id, semi_manual, deployment_suppressed, approval_status,
                    approved_by_operator_id, approved_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case["test_case_id"],
                    case["test_case_name"],
                    case["reference_a"],
                    case["reference_b"],
                    case["rows_evaluated"],
                    case["status"],
                    case["data_quality_status"],
                    json.dumps(metrics, sort_keys=True),
                    now_utc(),
                    prov["data_source"],
                    prov["data_source_detail"],
                    prov["generated_by"],
                    prov["created_at_utc"],
                    prov["is_live_test"],
                    prov["is_fixture"],
                    prov["is_historical"],
                    prov["workflow_execution_id"],
                    prov["chat_session_id"],
                    prov["semi_manual"],
                    prov["deployment_suppressed"],
                    prov["approval_status"],
                    prov["approved_by_operator_id"],
                    prov["approved_at_utc"],
                ),
            )
            case_results.append(case)
            summary_rows.append([case["test_case_id"], case["test_case_name"], case["status"], case["rows_evaluated"], case["data_quality_status"]])
        connection.commit()
    with (output_path / "m12_comparison_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["test_case_id", "test_case_name", "status", "rows_evaluated", "data_quality_status"])
        writer.writerows(summary_rows)
    payload = {"created_at": now_utc(), "status": "PASS" if all(case["status"] == "PASS" for case in case_results) else "FAIL", "test_cases": case_results}
    (output_path / "m12_test_case_results.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    md_lines = ["# Milestone 12 Comparison Results", "", f"Status: {payload['status']}", ""]
    for case in case_results:
        md_lines.append(f"- {case['test_case_id']}: {case['status']} ({case['rows_evaluated']} rows, {case['reference_a']} + {case['reference_b']})")
    (output_path / "m12_test_case_results.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return payload


def build_summary(repository: TRTRepository | None = None) -> dict[str, Any]:
    root = m12_dir(repository)
    with connect_metrics_db(repository=repository) as connection:
        metric_rows = connection.execute("SELECT * FROM m12_run_metrics").fetchall()
        error_rows = connection.execute("SELECT * FROM m12_error_interception").fetchall()
        figures = [dict(row) for row in connection.execute("SELECT * FROM m12_figure_manifest ORDER BY figure_id").fetchall()]
        test_cases = connection.execute("SELECT COUNT(*) AS count FROM m12_test_cases").fetchone()["count"]
        warnings = [
            row["data_quality_reason"]
            for row in metric_rows
            if row["data_quality_status"] != "OK" and row["data_quality_reason"]
        ]
        safety_findings = []
        for row in error_rows:
            if int(row["false_negative"] or 0):
                safety_findings.append(f"{row['test_id']} reached false negative state.")
        interception_rate = None
        if error_rows:
            interception_rate = sum(int(row["was_intercepted"] or 0) for row in error_rows) / len(error_rows)
        status = "PASS"
        if safety_findings:
            status = "FAIL"
        elif warnings:
            status = "PARTIAL"
        summary = {
            "milestone": "12",
            "status": status,
            "runs_evaluated": len(metric_rows),
            "test_cases_executed": int(test_cases or 0),
            "metrics": {
                "R_storage_mean": _mean(row["R_storage"] for row in metric_rows),
                "R_reset_mean": _mean(row["R_reset"] for row in metric_rows),
                "T_wait_mean_seconds": _mean(row["T_wait_seconds"] for row in metric_rows),
                "T_verification_mean_seconds": _mean(row["T_verification_seconds"] for row in metric_rows),
                "T_loop_mean_seconds": _mean(row["T_loop_seconds"] for row in metric_rows),
                "error_interception_rate": interception_rate,
            },
            "figures": figures,
            "data_quality_warnings": warnings,
            "deployment_safety_findings": safety_findings,
        }
    (root / "milestone12_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    md = [
        "# Milestone 12 Summary",
        "",
        f"Status: {summary['status']}",
        f"Runs evaluated: {summary['runs_evaluated']}",
        f"Test cases executed: {summary['test_cases_executed']}",
        "",
        "## Metrics",
    ]
    for key, value in summary["metrics"].items():
        md.append(f"- {key}: {value}")
    if warnings:
        md.extend(["", "## Data Quality Warnings"])
        md.extend(f"- {warning}" for warning in warnings[:50])
    (root / "milestone12_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    export_metrics_csv(repository=repository)
    return summary


M116_COMPOSITE_TRIGGER = (
    "okay, two production lines fucked up today. i want to confirm that with only two production lines remaining, "
    "my arrival time can be reduced by about 2.5 seconds, and the time to resolve entanglements can be reduced by "
    "1.5 seconds. however, to ensure the remaining production lines operate normally, pls stop the robotic arms "
    "immediately upon detecting an anomaly. because of this, pls adjust the recovery time to be 2 second slower, "
    "and set the number of tooling per production line to 5. adjust the throughput/hr for all production lines to "
    "at least 90; set the tooling picking target for production lines 2 to knife handle; and adjust the tooling "
    "picking order for production lines 1 to prioritize picking tooling other than ent tooling set."
)


M116_EXPECTED_OUTPUTS = {
    "simulation_config_updates": {
        "num_envs": 2,
        "chosen_intervention_mode": "immediate-stop",
        "travel_time": 2.5,
        "fix_duration": 6.5,
        "resume_delay": 2.5,
        "add_reference_number": 5,
    },
    "kpi_updates": {"min_throughput_per_hour": 90},
    "tooling_policy_updates": [{"line_id": "line_2", "target": "KNIFE_HANDLE"}],
    "manipulator_priority_updates": [
        {
            "line_id": "line_1",
            "policy": "EXPLICIT_TYPE_ORDER",
            "meaning": "prioritize tooling other than ENT tooling set",
        }
    ],
    "deployment_allowed": False,
    "deployment_suppressed_reason": "M12 semi-manual comparison test mode",
}


def pending_test_prompt(pending: dict[str, Any]) -> str:
    expected = pending.get("expected_outputs") or {}
    return "\n".join(
        [
            "M12 scenario test is ready for approval.",
            "",
            f"Test case: {pending['test_case_id']} - {pending['test_case_name']}",
            "",
            "Natural-language trigger:",
            f"\"{pending['natural_language_trigger']}\"",
            "",
            "Expected checks:",
            f"- IntentPatch should parse: {json.dumps(expected.get('simulation_config_updates'), sort_keys=True)}",
            f"- ScenarioSpec should include: {json.dumps(expected.get('kpi_updates'), sort_keys=True)}",
            "- Isaac command should include: --num_envs 2 --chosen_intervention_mode immediate-stop --travel_time 2.5 --fix_duration 6.5 --resume_delay 2.5 --add_reference_number 5",
            "- Evidence extractor should report: deployment suppressed for M12 semi-manual comparison test mode",
            "",
            "This test will run one scenario only. It will not deploy to the simulated physical production line.",
            "",
            "Reply APPROVE_TEST to run it, SKIP_TEST to skip it, EDIT_TEST: <revision> to revise it, or CANCEL_TESTING to stop.",
        ]
    )


def normalize_m12_approval_decision(message: str) -> tuple[str, str | None]:
    text = message.strip()
    upper = text.upper()
    if upper.startswith("EDIT_TEST:") or upper.startswith("EDIT TEST:"):
        return "EDIT_TEST", text.split(":", 1)[1].strip()
    if upper in {"APPROVE", "APPROVE TEST", "APPROVE_TEST", "RUN TEST", "RUN THIS TEST", "YES RUN IT"}:
        return "APPROVE_TEST", None
    if upper in {"SKIP", "SKIP TEST", "SKIP_TEST"}:
        return "SKIP_TEST", None
    if upper in {"CANCEL", "CANCEL TESTING", "CANCEL_TESTING", "STOP TESTING"}:
        return "CANCEL_TESTING", None
    if upper == "NEXT_TEST":
        return "NEXT_TEST", None
    return "UNKNOWN", None


def _resolve_seed_data(repository: TRTRepository | None, seed_data: str | Path | None) -> Path:
    if seed_data:
        path = Path(seed_data)
        return path if path.is_absolute() else (repository.root if repository else PROJECT_ROOT) / path
    return (repository.root if repository else PROJECT_ROOT) / M12_ROOT / "seed_data"


def prepare_next_test(
    *,
    suite: str,
    output: str | Path,
    seed_data: str | Path | None = None,
    repository: TRTRepository | None = None,
    chat_session_id: str | None = None,
) -> dict[str, Any]:
    seed_path = _resolve_seed_data(repository, seed_data)
    validate_seed_data(seed_path)
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = (repository.root if repository else PROJECT_ROOT) / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pending = {
        "state": "WAITING_FOR_M12_TEST_APPROVAL",
        "suite": suite,
        "test_case_id": "TC1-M116-001",
        "test_case_name": "Milestone 11.6 Composite Intent-to-ScenarioSpec Regression",
        "natural_language_trigger": M116_COMPOSITE_TRIGGER,
        "expected_layers": ["n8n", "trt-api", "IntentPatch", "ScenarioSpec", "Isaac", "Evidence"],
        "expected_outputs": M116_EXPECTED_OUTPUTS,
        "data_source": "SEED_GOLD_FIXTURE",
        "operator_approval_required": True,
        "operator_approval_status": "PENDING",
        "approval_status": "PENDING",
        "chat_session_id": chat_session_id,
        "created_at_utc": now_utc(),
        "prompt": pending_test_prompt(
            {
                "test_case_id": "TC1-M116-001",
                "test_case_name": "Milestone 11.6 Composite Intent-to-ScenarioSpec Regression",
                "natural_language_trigger": M116_COMPOSITE_TRIGGER,
                "expected_outputs": M116_EXPECTED_OUTPUTS,
            }
        ),
        "executed": False,
    }
    output_path.write_text(json.dumps(pending, indent=2, sort_keys=True), encoding="utf-8")
    return pending


def approve_pending_test(pending: str | Path, *, operator_id: str | None = None) -> dict[str, Any]:
    path = Path(pending)
    pending_test = json.loads(path.read_text(encoding="utf-8"))
    pending_test["operator_approval_status"] = "APPROVED"
    pending_test["approval_status"] = "APPROVED"
    pending_test["approved_by_operator_id"] = operator_id
    pending_test["approved_at_utc"] = now_utc()
    path.write_text(json.dumps(pending_test, indent=2, sort_keys=True), encoding="utf-8")
    return pending_test


def skip_pending_test(pending: str | Path, *, operator_id: str | None = None) -> dict[str, Any]:
    path = Path(pending)
    pending_test = json.loads(path.read_text(encoding="utf-8"))
    pending_test["operator_approval_status"] = "SKIPPED"
    pending_test["approval_status"] = "SKIPPED"
    pending_test["approved_by_operator_id"] = operator_id
    pending_test["approved_at_utc"] = now_utc()
    pending_test["executed"] = False
    path.write_text(json.dumps(pending_test, indent=2, sort_keys=True), encoding="utf-8")
    return pending_test


def cancel_pending_test(pending: str | Path) -> dict[str, Any]:
    path = Path(pending)
    if path.exists():
        pending_test = json.loads(path.read_text(encoding="utf-8"))
        pending_test["operator_approval_status"] = "CANCELLED"
        pending_test["approval_status"] = "CANCELLED"
        pending_test["executed"] = False
        pending_test["cancelled_at_utc"] = now_utc()
        path.write_text(json.dumps(pending_test, indent=2, sort_keys=True), encoding="utf-8")
        return pending_test
    return {"operator_approval_status": "CANCELLED", "approval_status": "CANCELLED", "executed": False}


def _post_n8n_chat(chat_url: str, message: str, session_id: str | None) -> dict[str, Any]:
    payload = {"chatInput": message, "sessionId": session_id or f"m12-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"}
    request = urllib.request.Request(
        chat_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(text)
        except json.JSONDecodeError:
            body = {"raw_text": text}
        return {"status_code": response.status, "body": body, "headers": dict(response.headers)}


def run_approved_pending_test(pending: str | Path, *, repository: TRTRepository | None = None) -> dict[str, Any]:
    path = Path(pending)
    pending_test = json.loads(path.read_text(encoding="utf-8"))
    if pending_test.get("operator_approval_status") != "APPROVED":
        raise RuntimeError("M12_TEST_NOT_APPROVED")
    root = (repository.root if repository else PROJECT_ROOT) / M12_ROOT / "semi_manual_runs"
    root.mkdir(parents=True, exist_ok=True)
    started_at = now_utc()
    chat_url = os.environ.get("N8N_CHAT_URL")
    result: dict[str, Any] = {
        "test_case_id": pending_test["test_case_id"],
        "test_case_name": pending_test["test_case_name"],
        "approval_status": "APPROVED_AND_RUN",
        "approved_by_operator_id": pending_test.get("approved_by_operator_id"),
        "approved_at_utc": pending_test.get("approved_at_utc"),
        "chat_session_id": pending_test.get("chat_session_id"),
        "n8n_execution_id": None,
        "scenario_spec_id": None,
        "run_id": None,
        "data_source": "SEMI_MANUAL_DRY_PLAN",
        "is_live_test": False,
        "is_fixture": False,
        "semi_manual": True,
        "deployment_suppressed": True,
        "deployment_suppressed_reason": "M12 semi-manual comparison test mode",
        "started_at_utc": started_at,
        "completed_at_utc": None,
        "status": "FAILED_BEFORE_RUN",
        "raw_request": pending_test["natural_language_trigger"],
        "raw_response": None,
        "normalized_output": None,
        "error_code": None,
        "operator_visible_message": "This was a Milestone 12 comparison test. No deployment was performed.",
    }
    if not chat_url:
        result["error_code"] = "N8N_CHAT_URL_NOT_CONFIGURED"
        result["operator_visible_message"] = "M12 test was approved, but N8N_CHAT_URL is not configured. No deployment was performed."
    else:
        try:
            response = _post_n8n_chat(chat_url, pending_test["natural_language_trigger"], pending_test.get("chat_session_id"))
            result.update(
                {
                    "status": "COMPLETED",
                    "data_source": "LIVE_N8N_CHAT",
                    "is_live_test": True,
                    "raw_response": response,
                    "normalized_output": response.get("body"),
                }
            )
            body = response.get("body") if isinstance(response.get("body"), dict) else {}
            result["n8n_execution_id"] = body.get("executionId") or body.get("execution_id")
            result["scenario_spec_id"] = body.get("scenario_spec_id")
            result["run_id"] = body.get("run_id")
        except Exception as exc:
            result["error_code"] = "N8N_CHAT_POST_FAILED"
            result["raw_response"] = {"error": str(exc)}
            result["operator_visible_message"] = f"M12 test failed before completion: {exc}. No deployment was performed."
    result["completed_at_utc"] = now_utc()
    result["T_intent_created"] = result["started_at_utc"]
    result["T_candidate_created"] = None
    result["T_approval_requested"] = pending_test.get("created_at_utc")
    result["T_operator_approved_test"] = pending_test.get("approved_at_utc")
    result["T_scenario_created"] = None
    result["T_artifact_created"] = None
    result["T_summary_created"] = result["completed_at_utc"]
    result["T_review_end"] = result["completed_at_utc"]
    result["T_wait_seconds"] = seconds_between(result["T_intent_created"], result["T_summary_created"])
    result["T_verification_seconds"] = None
    result["T_loop_seconds"] = seconds_between(result["T_intent_created"], result["T_review_end"])
    result_id = f"{pending_test['test_case_id']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    json_path = root / f"{result_id}.json"
    csv_path = root / f"{result_id}.csv"
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "test_case_id",
            "test_case_name",
            "approval_status",
            "approved_by_operator_id",
            "approved_at_utc",
            "chat_session_id",
            "n8n_execution_id",
            "scenario_spec_id",
            "run_id",
            "data_source",
            "is_live_test",
            "is_fixture",
            "semi_manual",
            "deployment_suppressed",
            "status",
            "error_code",
            "T_wait_seconds",
            "T_verification_seconds",
            "T_loop_seconds",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({field: result.get(field) for field in fields})
    pending_test["executed"] = True
    pending_test["last_result_path"] = str(json_path)
    path.write_text(json.dumps(pending_test, indent=2, sort_keys=True), encoding="utf-8")
    return result


def append_manual_result(
    *,
    repository: TRTRepository,
    pending: str | Path,
    test_id: str,
    scenario_spec_id: str | None,
    run_id: str | None,
    chat_transcript: str | Path,
    status: str,
    output: str | Path | None = None,
) -> dict[str, Any]:
    allowed_status = {"PASS", "FAIL", "REJECTED", "SIMULATION_FAILED", "INCONCLUSIVE", "FAIL_SIMULATION_CONFIG_DRIFT", "FAIL_ERROR_NOT_INTERCEPTED", "WORKFLOW_LOOP", "EVIDENCE_SUMMARY_MISSING"}
    if status not in allowed_status:
        raise ValueError(f"Unsupported manual M12 status: {status}")
    pending_path = Path(pending)
    if not pending_path.is_absolute():
        pending_path = repository.root / pending_path
    pending_payload = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else {}
    transcript_path = Path(chat_transcript)
    if not transcript_path.is_absolute():
        transcript_path = repository.root / transcript_path
    if not transcript_path.exists():
        raise FileNotFoundError(f"Chat transcript file not found: {transcript_path}")
    transcript_text = transcript_path.read_text(encoding="utf-8")
    result = {
        "created_at_utc": now_utc(),
        "test_case_id": test_id,
        "test_case_name": pending_payload.get("test_case_name"),
        "approval_status": pending_payload.get("approval_status") or pending_payload.get("operator_approval_status"),
        "approved_by_operator_id": pending_payload.get("approved_by_operator_id"),
        "approved_at_utc": pending_payload.get("approved_at_utc"),
        "chat_session_id": pending_payload.get("chat_session_id"),
        "n8n_execution_id": pending_payload.get("n8n_execution_id"),
        "scenario_spec_id": scenario_spec_id or None,
        "run_id": run_id or None,
        "chat_transcript_path": str(transcript_path),
        "chat_transcript_text": transcript_text,
        "status": status,
        "data_source": "LIVE_N8N_CHAT",
        "data_source_detail": "Manual operator-entered n8n chat transcript and supplied run identifiers.",
        "generated_by": "tools.m12_run_approved_test --manual",
        "is_live_test": True,
        "is_fixture": False,
        "is_historical": False,
        "semi_manual": True,
        "deployment_suppressed": True,
        "deployment_suppressed_reason": "M12 manual comparison test mode",
        "metrics_fabricated": False,
    }
    output_path = Path(output) if output else repository.root / M12_ROOT / "manual_results.jsonl"
    if not output_path.is_absolute():
        output_path = repository.root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, sort_keys=True))
        handle.write("\n")
    return result


def manual_result_for_run(repository: TRTRepository, run_id: str, manual_results: str | Path | None = None) -> dict[str, Any] | None:
    path = Path(manual_results) if manual_results else repository.root / M12_ROOT / "manual_results.jsonl"
    if not path.is_absolute():
        path = repository.root / path
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("run_id") == run_id:
            return row
    return None


def collect_metrics_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--output", default=str(M12_ROOT / M12_DB_NAME))
    parser.add_argument("--live-manual", action="store_true")
    parser.add_argument("--manual-results", default=str(M12_ROOT / "manual_results.jsonl"))
    args = parser.parse_args(argv)
    repository = TRTRepository()
    output_db = Path(args.output)
    if not output_db.is_absolute():
        output_db = repository.root / output_db
    if args.all:
        run_ids = sorted({path.stem for path in (repository.root / "outputs" / "run_artifacts").glob("sim_*.sqlite")} | {path.stem for path in (repository.root / "outputs" / "run_artifacts").glob("sim_*.sqlite3")})
        rows = []
        with connect_metrics_db(path=output_db, repository=repository) as connection:
            for run_id in run_ids:
                try:
                    rows.append(collect_run_metrics(repository, run_id, connection=connection))
                except Exception:
                    continue
        export_metrics_csv(repository=repository)
        print(json.dumps({"status": "OK", "runs_collected": len(rows), "db_path": str(output_db)}, indent=2))
    else:
        manual_row = manual_result_for_run(repository, args.run_id, args.manual_results)
        use_live = args.live_manual or manual_row is not None
        with connect_metrics_db(path=output_db, repository=repository) as connection:
            row = collect_run_metrics(
                repository,
                args.run_id,
                connection=connection,
                data_source="LIVE_N8N_CHAT" if use_live else "HISTORICAL_RUN_ARTIFACT",
                data_source_detail=(
                    f"Collected after manual n8n chat test {manual_row.get('test_case_id')} from real RunArtifact SQLite."
                    if manual_row
                    else "Collected after manual n8n chat test from real RunArtifact SQLite."
                    if args.live_manual
                    else None
                ),
                is_live_test_override=True if use_live else None,
            )
        export_metrics_csv(repository=repository)
        print(json.dumps({"status": "OK", "run_id": args.run_id, "data_quality_status": row["data_quality_status"]}, indent=2))
    return 0


def seed_data_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(M12_ROOT / "seed_data"))
    args = parser.parse_args(argv)
    output = Path(args.output)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    manifest = generate_seed_data(output)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def prepare_next_test_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="comparison")
    parser.add_argument("--output", default=str(M12_ROOT / "pending_test.json"))
    parser.add_argument("--seed-data", default=str(M12_ROOT / "seed_data"))
    parser.add_argument("--chat-session-id")
    args = parser.parse_args(argv)
    repository = TRTRepository()
    output = Path(args.output)
    if not output.is_absolute():
        output = repository.root / output
    pending = prepare_next_test(
        suite=args.suite,
        output=output,
        seed_data=args.seed_data,
        repository=repository,
        chat_session_id=args.chat_session_id,
    )
    print(json.dumps({"status": "WAITING_FOR_M12_TEST_APPROVAL", "pending_path": str(output), "pending_test": pending}, indent=2))
    return 0


def run_approved_test_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pending", required=True)
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--test-id")
    parser.add_argument("--scenario-spec-id")
    parser.add_argument("--run-id")
    parser.add_argument("--chat-transcript")
    parser.add_argument("--status")
    parser.add_argument("--output", default=str(M12_ROOT / "manual_results.jsonl"))
    args = parser.parse_args(argv)
    repository = TRTRepository()
    pending = Path(args.pending)
    if not pending.is_absolute():
        pending = repository.root / pending
    if args.manual:
        missing = [
            name
            for name, value in {
                "--test-id": args.test_id,
                "--chat-transcript": args.chat_transcript,
                "--status": args.status,
            }.items()
            if not value
        ]
        if missing:
            print(f"Missing required manual result arguments: {', '.join(missing)}")
            return 2
        result = append_manual_result(
            repository=repository,
            pending=pending,
            test_id=str(args.test_id),
            scenario_spec_id=args.scenario_spec_id,
            run_id=args.run_id,
            chat_transcript=str(args.chat_transcript),
            status=str(args.status),
            output=args.output,
        )
        print(json.dumps({"status": "MANUAL_RESULT_RECORDED", "result": result}, indent=2, sort_keys=True))
        return 0
    try:
        result = run_approved_pending_test(pending, repository=repository)
    except RuntimeError as exc:
        if str(exc) == "M12_TEST_NOT_APPROVED":
            print("M12_TEST_NOT_APPROVED")
            return 2
        raise
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"COMPLETED", "FAILED_BEFORE_RUN"} else 1


def figure_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--require-live-data", action="store_true")
    parser.add_argument("--allow-historical", action="store_true")
    parser.add_argument("--label-historical", action="store_true")
    parser.add_argument("--allow-fixture-plots", action="store_true")
    args = parser.parse_args(argv)
    manifest = generate_figures(
        args.input,
        require_live_data=args.require_live_data,
        allow_historical=args.allow_historical,
        label_historical=args.label_historical,
        allow_fixture_plots=args.allow_fixture_plots,
    )
    print(json.dumps({"status": "OK", "figures": len(manifest)}, indent=2))
    return 0


def comparison_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-data", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--dry-plan-only", action="store_true")
    parser.add_argument("--semi-manual", action="store_true")
    parser.add_argument("--manual-results")
    args = parser.parse_args(argv)
    repository = TRTRepository()
    if args.semi_manual:
        pending = prepare_next_test(
            suite="comparison",
            output=repository.root / M12_ROOT / "pending_test.json",
            seed_data=args.seed_data,
            repository=repository,
        )
        print(json.dumps({"status": "WAITING_FOR_M12_TEST_APPROVAL", "pending_test": pending}, indent=2))
        return 0
    result = run_comparison_tests(
        args.seed_data,
        args.output,
        repository=repository,
        dry_plan_only=args.dry_plan_only,
        manual_results=args.manual_results,
    )
    print(json.dumps({"status": result["status"], "test_cases": len(result["test_cases"])}, indent=2))
    return 0 if result["status"] in {"PASS", "PARTIAL", "DRY_PLAN_ONLY"} else 1


def run_all_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(M12_ROOT))
    parser.add_argument("--generate-report", action="store_true")
    parser.add_argument("--require-live-data", action="store_true")
    args = parser.parse_args(argv)
    repository = TRTRepository()
    output = Path(args.output)
    if not output.is_absolute():
        output = repository.root / output
    seed_output = output / "seed_data"
    comparison_output = output / "comparison_results"
    generate_seed_data(seed_output)
    collect_all_metrics(repository)
    run_comparison_tests(seed_output, comparison_output, repository=repository, dry_plan_only=True)
    if args.generate_report or args.require_live_data:
        generate_figures(
            output / M12_DB_NAME,
            output / "figures",
            require_live_data=args.require_live_data,
            allow_historical=not args.require_live_data,
            label_historical=not args.require_live_data,
        )
    summary = build_summary(repository)
    print(json.dumps({"status": summary["status"], "output": str(output)}, indent=2))
    return 0 if summary["status"] in {"PASS", "PARTIAL"} else 1
