"""Read SQLite simulation results into RunArtifact-compatible JSON."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


REQUIRED_TABLES = {"simulation_runs", "line_kpis", "tool_events"}


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _error(code: str, message: str, *, run_id: str, db_path: str | Path) -> dict[str, Any]:
    return {
        "status": "ERROR",
        "error_code": code,
        "message": message,
        "run_id": run_id,
        "db_path": str(db_path),
        "line_kpis": [],
        "tool_events": [],
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
            tool_events = _rows(
                connection.execute("SELECT * FROM tool_events WHERE run_id = ? ORDER BY line_id, event_time_seconds, tool_id", (run_id,))
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
            "run": run_rows[0],
            "line_kpis": line_kpis,
            "tool_events": tool_events,
            "summary": summary,
        }
    return {
        "status": run_status,
        "run_id": run_id,
        "db_path": str(path),
        "run": run_rows[0],
        "line_kpis": line_kpis,
        "tool_events": tool_events,
        "summary": summary,
    }
