"""Read SQLite simulation results into RunArtifact-compatible JSON."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


REQUIRED_TABLES = {"simulation_runs", "line_kpis", "tool_events"}
OPTIONAL_PRIORITY_TABLES = {"priority_config", "priority_events", "container_completion_events", "line_completion_kpis"}


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _error(code: str, message: str, *, run_id: str, db_path: str | Path) -> dict[str, Any]:
    return {
        "status": "ERROR",
        "error_code": code,
        "message": message,
        "run_id": run_id,
        "db_path": str(db_path),
        "line_kpis": [],
        "tool_events": [],
        "priority_config": [],
        "priority_events": [],
        "container_completion_events": [],
        "line_completion_kpis": [],
        "priority_summary": {},
        "summary": {
            "total_completed": 0,
            "total_wanted_completed": 0,
            "total_unwanted_completed": 0,
            "total_entanglements": 0,
            "total_downtime_seconds": 0,
            "overall_success": False,
        },
    }


def read_simulation_results(db_path: str | Path, run_id: str) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return _error("SIMULATION_DB_NOT_FOUND", "Simulation SQLite database was not found.", run_id=run_id, db_path=path)

    try:
        with sqlite3.connect(path) as connection:
            cursor = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            missing_tables = sorted(REQUIRED_TABLES - tables)
            if missing_tables:
                return _error(
                    "SIMULATION_DB_SCHEMA_INVALID",
                    f"Simulation SQLite database is missing tables: {missing_tables}",
                    run_id=run_id,
                    db_path=path,
                )
            run_rows = _rows(
                connection.execute("SELECT * FROM simulation_runs WHERE run_id = ?", (run_id,))
            )
            if not run_rows:
                return _error("SIMULATION_RUN_NOT_FOUND", "Simulation run was not found in SQLite output.", run_id=run_id, db_path=path)
            line_kpis = _rows(connection.execute("SELECT * FROM line_kpis WHERE run_id = ? ORDER BY line_id", (run_id,)))
            tool_event_columns = _table_columns(connection, "tool_events")
            tool_event_order = "line_id, event_time_seconds, tool_id"
            if {"actual_pick_index", "tool_number"}.issubset(tool_event_columns):
                tool_event_order = "line_id, actual_pick_index, tool_number"
            tool_events = _rows(
                connection.execute(f"SELECT * FROM tool_events WHERE run_id = ? ORDER BY {tool_event_order}", (run_id,))
            )
            priority_config = (
                _rows(
                    connection.execute(
                        "SELECT * FROM priority_config WHERE run_id = ? ORDER BY env_id, line_id",
                        (run_id,),
                    )
                )
                if "priority_config" in tables
                else []
            )
            priority_events = (
                _rows(
                    connection.execute(
                        "SELECT * FROM priority_events WHERE run_id = ? ORDER BY line_id, actual_pick_index, tool_number",
                        (run_id,),
                    )
                )
                if "priority_events" in tables
                else []
            )
            container_completion_events = (
                _rows(
                    connection.execute(
                        "SELECT * FROM container_completion_events WHERE run_id = ? ORDER BY line_id, container_type",
                        (run_id,),
                    )
                )
                if "container_completion_events" in tables
                else []
            )
            line_completion_kpis = (
                _rows(
                    connection.execute(
                        "SELECT * FROM line_completion_kpis WHERE run_id = ? ORDER BY line_id",
                        (run_id,),
                    )
                )
                if "line_completion_kpis" in tables
                else []
            )
    except sqlite3.Error as exc:
        return _error("SIMULATION_DB_SCHEMA_INVALID", f"Could not read simulation SQLite database: {exc}", run_id=run_id, db_path=path)

    summary = {
        "total_completed": sum(int(row.get("completed_count") or 0) for row in line_kpis),
        "total_wanted_completed": sum(int(row.get("wanted_completed_count") or 0) for row in line_kpis),
        "total_unwanted_completed": sum(int(row.get("unwanted_completed_count") or 0) for row in line_kpis),
        "total_entanglements": sum(int(row.get("entanglement_count") or 0) for row in line_kpis),
        "total_downtime_seconds": sum(float(row.get("downtime_seconds") or 0) for row in line_kpis),
        "overall_success": bool(line_kpis) and all(int(row.get("success") or 0) == 1 for row in line_kpis),
    }
    priority_summary = _priority_summary(line_kpis, line_completion_kpis)
    run_status = run_rows[0].get("status", "UNKNOWN")
    if run_status == "RUNNING":
        return {
            "status": "ERROR",
            "error_code": "SIMULATION_RESULT_NOT_FINALIZED",
            "message": "Isaac exited successfully, but the result DB was left in RUNNING state.",
            "run_id": run_id,
            "db_path": str(path),
            "simulation_run_status": run_status,
            "completed_at": run_rows[0].get("completed_at"),
            "line_kpis_count": len(line_kpis),
            "tool_events_count": len(tool_events),
            "priority_config_count": len(priority_config),
            "priority_events_count": len(priority_events),
            "container_completion_events_count": len(container_completion_events),
            "run": run_rows[0],
            "line_kpis": line_kpis,
            "tool_events": tool_events,
            "priority_config": priority_config,
            "priority_events": priority_events,
            "container_completion_events": container_completion_events,
            "line_completion_kpis": line_completion_kpis,
            "priority_summary": priority_summary,
            "summary": summary,
        }
    if run_status == "FAILED":
        error_message = str(run_rows[0].get("error_message") or "")
        exception_markers = (
            "Traceback",
            "ValueError",
            "TypeError",
            "RuntimeError",
            "invalid literal for int()",
            "got multiple values for argument",
            "Could not resolve runtime tool reference",
        )
        if any(marker in error_message for marker in exception_markers):
            failed_function = None
            if "invalid literal for int()" in error_message:
                failed_function = "_current_table_tool_numbers"
            elif "got multiple values for argument" in error_message:
                failed_function = "log_runtime_event"
            elif "Could not resolve runtime tool reference" in error_message:
                failed_function = "normalize_runtime_tool_ref"
            return {
                "status": "ERROR",
                "error_code": "SIMULATION_SCRIPT_EXCEPTION",
                "message": "Isaac script failed with an exception before producing complete KPI rows.",
                "run_id": run_id,
                "db_path": str(path),
                "root_exception": error_message,
                "failed_function": failed_function,
                "script": "pick_up_example.py",
                "simulation_run_status": run_status,
                "completed_at": run_rows[0].get("completed_at"),
                "line_kpis_count": len(line_kpis),
                "tool_events_count": len(tool_events),
                "priority_config_count": len(priority_config),
                "priority_events_count": len(priority_events),
                "container_completion_events_count": len(container_completion_events),
                "run": run_rows[0],
                "line_kpis": line_kpis,
                "tool_events": tool_events,
                "priority_config": priority_config,
                "priority_events": priority_events,
                "container_completion_events": container_completion_events,
                "line_completion_kpis": line_completion_kpis,
                "priority_summary": priority_summary,
                "summary": summary,
            }
    return {
        "status": run_status,
        "run_id": run_id,
        "db_path": str(path),
        "run": run_rows[0],
        "line_kpis": line_kpis,
        "tool_events": tool_events,
        "priority_config": priority_config,
        "priority_events": priority_events,
        "container_completion_events": container_completion_events,
        "line_completion_kpis": line_completion_kpis,
        "priority_summary": priority_summary,
        "priority_events_count": len(priority_events),
        "priority_config_count": len(priority_config),
        "container_completion_events_count": len(container_completion_events),
        "line_completion_kpis_count": len(line_completion_kpis),
        "summary": summary,
    }


def _priority_summary(line_kpis: list[dict[str, Any]], line_completion_kpis: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for row in line_kpis:
        line_id = row.get("line_id")
        if not line_id:
            continue
        summary.setdefault(line_id, {})
        for key in (
            "priority_policy",
            "priority_deviation_count",
            "required_tray_completion_seconds",
            "unwanted_box_completion_seconds",
            "all_sorting_completion_seconds",
        ):
            if key in row:
                summary[line_id][key] = row.get(key)
    for row in line_completion_kpis:
        line_id = row.get("line_id")
        if not line_id:
            continue
        summary.setdefault(line_id, {})
        for key in (
            "priority_policy",
            "priority_deviation_count",
            "required_tray_completion_seconds",
            "unwanted_box_completion_seconds",
            "all_sorting_completion_seconds",
            "success",
        ):
            if key in row:
                summary[line_id][key] = row.get(key)
    return summary
