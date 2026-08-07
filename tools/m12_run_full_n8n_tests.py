from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.sync.client import connect

from trt_core.experiment_evaluation import derive_checkpoint_record
from trt_core.repository import PROJECT_ROOT
from tools.m12_check_full_test_readiness import packet_audit, plan_audit, runner_audit
from tools.m12_packet_scorer import score_combined
from tools.m12_tc4_backend_injection import run_tc4_backend_injection


M12_ROOT = PROJECT_ROOT / "outputs" / "reports" / "m12"
RUN_ID_RE = re.compile(r"\bsim_[0-9a-fA-F-]{8,}\b")
SCENARIO_ID_RE = re.compile(r"\bscn_[0-9a-fA-F-]{8,}\b")
CHAT_INSTANCE_ID = "4c47430a442d0f9da40f29a50b9b8787215fa019001411bd6f426c91e04dc7a6"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_env() -> dict[str, str]:
    # PowerShell user-level variables are not automatically inherited by this
    # process in every Codex shell call, so use process env first and then
    # USERPROFILE-backed os.environ values set by the launching shell.
    env = {
        "N8N_API_KEY": os.environ.get("N8N_API_KEY", ""),
        "N8N_URL": os.environ.get("N8N_URL") or os.environ.get("N8N_BASE_URL") or "http://localhost:5678",
        "N8N_CHAT_URL": os.environ.get("N8N_CHAT_URL") or os.environ.get("N8N_WEBHOOK_URL") or "",
        "TRT_API_URL": os.environ.get("TRT_API_URL") or "http://localhost:8000",
    }
    missing = [key for key, value in env.items() if key != "N8N_URL" and not value]
    if missing and os.name == "nt":
        # Query user-level env without printing secrets.
        ps = (
            "$names=@('N8N_API_KEY','N8N_URL','N8N_BASE_URL','N8N_CHAT_URL');"
            "foreach($n in $names){$v=[Environment]::GetEnvironmentVariable($n,'User');"
            "if($v){Write-Output \"$n=$v\"}}"
        )
        try:
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=10)
            for line in proc.stdout.splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key == "N8N_BASE_URL" and not env.get("N8N_URL"):
                    env["N8N_URL"] = value
                elif key in env and value:
                    env[key] = value
        except Exception:
            pass
    return env


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def build_evaluation_fields(
    row: dict[str, str],
    packet_score: dict[str, Any],
    *,
    scenario_spec_id: str = "",
    run_id: str = "",
    turn_labels: list[str] | None = None,
    system_error: bool = False,
) -> dict[str, Any]:
    artifact_exists = bool(
        run_id
        and any(
            (PROJECT_ROOT / "outputs" / "run_artifacts" / f"{run_id}{suffix}").exists()
            for suffix in (".sqlite", ".sqlite3")
        )
    )
    should_launch = row.get("should_launch_isaac", "") != "false"
    return derive_checkpoint_record(
        suite=row.get("suite", ""),
        prompt=row.get("paste_into_n8n", ""),
        expected_status=row.get("expected_status", ""),
        should_launch_isaac=should_launch,
        scenario_spec_id=scenario_spec_id,
        run_artifact_exists=artifact_exists,
        packet_score=packet_score,
        turn_labels=turn_labels or [],
        system_error=system_error,
    )


def http_json(url: str, *, method: str = "GET", api_key: str | None = None, payload: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-N8N-API-KEY"] = api_key
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(text)
        except json.JSONDecodeError:
            body = {"raw_text": text}
        return {"status_code": response.status, "body": body, "headers": dict(response.headers)}


def post_chat_start(env: dict[str, str], *, session_id: str, message: str) -> dict[str, Any]:
    headers = {
        "Accept": "text/plain",
        "Content-Type": "application/json",
        "X-Instance-Id": CHAT_INSTANCE_ID,
    }
    payload = {"action": "sendMessage", "sessionId": session_id, "chatInput": message}
    request = urllib.request.Request(
        env["N8N_CHAT_URL"],
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text}


def active_isaac_process_count() -> int:
    if os.name != "nt":
        return 0
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.ProcessName -match 'isaac|kit|omni' -and $_.Path -like '*IsaacSim*' }; "
        "($p | Measure-Object).Count"
    )
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=10)
        return int((proc.stdout or "0").strip() or "0")
    except Exception:
        return 0


def wait_for_no_isaac(*, timeout_seconds: int = 300) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if active_isaac_process_count() == 0:
            return True
        time.sleep(5)
    return active_isaac_process_count() == 0


def websocket_url(env: dict[str, str], *, session_id: str, execution_id: str, resume_token: str) -> str:
    origin = urllib.parse.urlparse(env["N8N_CHAT_URL"])
    scheme = "wss" if origin.scheme == "https" else "ws"
    query = urllib.parse.urlencode(
        {
            "sessionId": session_id,
            "executionId": execution_id,
            "isPublic": "true",
            "token": resume_token,
        }
    )
    return f"{scheme}://{origin.netloc}/chat?{query}"


def receive_ws_message(ws: Any, *, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    chunks: list[str] = []
    while time.monotonic() < deadline:
        remaining = max(1, min(30, int(deadline - time.monotonic())))
        try:
            message = ws.recv(timeout=remaining)
        except TimeoutError:
            continue
        except Exception as exc:
            if chunks:
                return "\n".join(chunks)
            return f"WEBSOCKET_ERROR: {type(exc).__name__}: {exc}"
        if not isinstance(message, str):
            continue
        if message == "n8n|heartbeat":
            try:
                ws.send("n8n|heartbeat-ack")
            except Exception:
                pass
            continue
        if message == "n8n|continue":
            chunks.append("n8n workflow continued running...")
            continue
        chunks.append(message)
        return "\n".join(chunks)
    return "\n".join(chunks) if chunks else "WEBSOCKET_TIMEOUT"


def receive_ws_message_until_artifact(ws: Any, *, timeout_seconds: int, since_epoch: float) -> str:
    deadline = time.monotonic() + timeout_seconds
    chunks: list[str] = []
    while time.monotonic() < deadline:
        try:
            message = ws.recv(timeout=5)
        except TimeoutError:
            message = None
        except Exception as exc:
            chunks.append(f"WEBSOCKET_ERROR: {type(exc).__name__}: {exc}")
            message = None

        if isinstance(message, str):
            if message == "n8n|heartbeat":
                try:
                    ws.send("n8n|heartbeat-ack")
                except Exception:
                    pass
            elif message == "n8n|continue":
                chunks.append("n8n workflow continued running...")
            else:
                chunks.append(message)
                return "\n".join(chunks)

        artifact = newest_created_file(PROJECT_ROOT / "outputs" / "run_artifacts", "sim_*.sqlite*", since_epoch=since_epoch)
        scenario = newest_created_file(PROJECT_ROOT / "outputs" / "scenario_specs", "scn_*.json", since_epoch=since_epoch)
        if artifact is not None and scenario is not None and active_isaac_process_count() == 0:
            chunks.append(f"ARTIFACT_DETECTED: scenario_spec_id={scenario.stem} run_id={artifact.stem}")
            return "\n".join(chunks)

    return "\n".join(chunks) if chunks else "WEBSOCKET_TIMEOUT"


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def strings(value: Any) -> list[str]:
    return [item for item in walk(value) if isinstance(item, str)]


def compact_text(value: Any) -> str:
    texts = []
    keys = {"operator_message", "message", "response", "text", "content", "raw_chat_input", "latest_user_message", "formatted_answer"}
    for item in walk(value):
        if not isinstance(item, dict):
            continue
        for key, raw in item.items():
            if key in keys and isinstance(raw, str) and raw.strip():
                texts.append(raw.strip())
    if not texts:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:25000]
    seen = []
    for item in texts:
        if item not in seen:
            seen.append(item)
    return "\n\n".join(seen)


def first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(0) if match else ""


def extract_ids_from_payload(value: Any) -> tuple[str, str]:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return first_match(RUN_ID_RE, text), first_match(SCENARIO_ID_RE, text)


def extract_strategy_selection(value: Any) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for item in walk(value):
        if not isinstance(item, dict):
            continue
        selection = item.get("selection")
        if isinstance(selection, dict) and (
            selection.get("selected_candidate_strategy_id")
            or selection.get("status") == "NO_ELIGIBLE_STRATEGY"
            or "ranked_candidates" in selection
        ):
            candidate_runs = item.get("candidate_runs") or selection.get("ranked_candidates") or []
            best = {
                "strategy_batch_id": item.get("strategy_batch_id"),
                "candidate_count": item.get("candidate_count") or len(candidate_runs),
                "candidate_run_ids": [
                    row.get("run_id") for row in candidate_runs
                    if isinstance(row, dict) and row.get("run_id")
                ],
                "selected_candidate_strategy_id": selection.get("selected_candidate_strategy_id"),
                "selected_scenario_spec_id": selection.get("selected_scenario_spec_id"),
                "selected_run_id": selection.get("selected_run_id"),
                "objective_id": (selection.get("objective") or {}).get("objective_id"),
                "objective_score": selection.get("objective_score"),
                "operator_refinement_required": selection.get("operator_refinement_required"),
                "post_simulation_regeneration_performed": selection.get("post_simulation_regeneration_performed"),
                "ranked_candidates": selection.get("ranked_candidates") or [],
            }
    return best


def response_requests_operator_details(text: str) -> bool:
    lower = text.lower()
    required_phrases = [
        "need operator id",
        "need the operator id",
        "need operator_id",
        "need operator id and reason",
        "need operator_id and reason",
        "still need operator id",
        "still need the operator id",
        "still need operator_id",
        "still need operator id and reason",
        "still need operator_id and reason",
        "before i can proceed, i still need",
        "before i can submit this for review, i still need",
    ]
    return any(phrase in lower for phrase in required_phrases)


def newest_created_file(directory: Path, pattern: str, *, since_epoch: float) -> Path | None:
    if not directory.exists():
        return None
    candidates = [path for path in directory.glob(pattern) if path.stat().st_mtime >= since_epoch]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_rows(packet_dir: Path) -> list[dict[str, str]]:
    files = [
        ("TC1", "tc1_intent_plan_manual.csv"),
        ("TC2", "tc2_tool_orchestration_manual.csv"),
        ("TC3", "tc3_kpi_report_manual.csv"),
        ("TC4", "tc4_error_interception_manual.csv"),
    ]
    rows: list[dict[str, str]] = []
    order = 0
    for suite, filename in files:
        for row in read_csv(packet_dir / filename):
            order += 1
            row = dict(row)
            row["suite"] = suite
            row["full_order"] = str(order)
            rows.append(row)
    return rows


def load_smoke_rows(packet_dir: Path) -> list[dict[str, str]]:
    base_rows = {row["test_id"]: row for row in load_rows(packet_dir)}
    smoke_path = packet_dir / "smoke_queue_manual.csv"
    rows: list[dict[str, str]] = []
    for order, smoke in enumerate(read_csv(smoke_path), start=1):
        packet_test_id = smoke["test_id"]
        base = dict(base_rows.get(packet_test_id, {}))
        if not base:
            base = {}
        base.update(
            {
                "packet_test_id": packet_test_id,
                "smoke_sequence": smoke["smoke_sequence"],
                "test_id": smoke["smoke_sequence"],
                "paste_into_n8n": smoke.get("paste_into_n8n", ""),
                "operator_details_reply": smoke.get("operator_details_reply", ""),
                "approval_reply": smoke.get("approval_reply", ""),
                "stop_point": smoke.get("stop_point", ""),
                "record_status_hint": smoke.get("record_status_hint", ""),
                "full_order": str(order),
                "is_smoke": "true",
            }
        )
        for field in ("expected_status", "expected_fields_json", "expected_interceptor", "expected_deployment_blocked"):
            if smoke.get(field):
                base[field] = smoke[field]
        if "suite" not in base:
            if packet_test_id.startswith("TC1-"):
                base["suite"] = "TC1"
            elif packet_test_id.startswith("TC2-"):
                base["suite"] = "TC2"
            elif packet_test_id.startswith("TC3-"):
                base["suite"] = "TC3"
            elif packet_test_id.startswith("TC4-"):
                base["suite"] = "TC4"
        rows.append(base)
    return rows


def load_full_plan(plan_dir: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    path = plan_dir / "full_isaac_parameter_plan.csv"
    if not path.exists():
        return {}, {}
    by_test_id: dict[str, dict[str, str]] = {}
    by_setup_id: dict[str, dict[str, str]] = {}
    for row in read_csv(path):
        by_test_id[row["test_id"]] = row
        by_setup_id[row["seed_id"]] = row
    return by_test_id, by_setup_id


def apply_full_plan(row: dict[str, str], by_test_id: dict[str, dict[str, str]], by_setup_id: dict[str, dict[str, str]]) -> dict[str, str]:
    row = dict(row)
    plan = by_test_id.get(row.get("test_id", "")) or by_setup_id.get(row.get("setup_id", ""))
    if plan:
        row["full_sequence"] = plan["full_sequence"]
        row["paste_into_n8n_original"] = row.get("paste_into_n8n", "")
        row["paste_into_n8n"] = plan["full_test_prompt"]
        row["expected_command_args"] = plan["expected_command_args"]
        row["should_launch_isaac"] = plan["should_launch_isaac"]
        row["expected_validation_issue"] = plan["expected_validation_issue"]
        row["total_tooling"] = plan["total_tooling"]
        row["num_envs"] = plan["num_envs"]
        row["add_reference_number"] = plan["add_reference_number"]
    else:
        row.setdefault("full_sequence", "")
        row.setdefault("should_launch_isaac", "")
        row.setdefault("expected_validation_issue", "")
    return row


def wait_execution(base_url: str, api_key: str, execution_id: str, path: Path, *, max_wait_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + max_wait_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = http_json(f"{base_url.rstrip('/')}/api/v1/executions/{execution_id}?includeData=true", api_key=api_key, timeout=60)
        body = response["body"]
        if isinstance(body, dict):
            last = body
            path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
            text = compact_text(body).lower()
            if body.get("stoppedAt") or any(token in text for token in ["candidate patch passed validation", "requires revision", "deployment is not allowed", "simulation completed", "simulation failed", "i can help"]):
                return body
        time.sleep(5)
    if last:
        path.write_text(json.dumps(last, indent=2, sort_keys=True), encoding="utf-8")
    return last


def start_chat_websocket(env: dict[str, str], *, session_id: str, message: str) -> tuple[str, Any, str]:
    body = post_chat_start(env, session_id=session_id, message=message)
    execution_id = str(body.get("executionId") or body.get("execution_id") or "")
    resume_token = str(body.get("resumeToken") or "")
    if not execution_id or not resume_token:
        raise RuntimeError(f"n8n chat did not return executionId/resumeToken: {body}")
    ws = connect(websocket_url(env, session_id=session_id, execution_id=execution_id, resume_token=resume_token), open_timeout=20)
    return execution_id, ws, json.dumps(body, sort_keys=True)


def record_result(row: dict[str, Any], combined_path: Path, status: str, session_id: str, execution_ids: list[str]) -> None:
    cmd = [
        "python",
        "-m",
        "tools.m12_ingest_n8n_execution",
        "--test-id",
        row["test_id"],
        "--execution-json",
        str(combined_path),
        "--status",
        status,
        "--chat-session-id",
        session_id,
    ]
    if execution_ids:
        cmd.extend(["--n8n-execution-id", execution_ids[-1]])
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=False, capture_output=True, text=True)


def fetch_execution_snapshots(env: dict[str, str], execution_ids: list[str], execution_dir: Path) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for execution_id in execution_ids:
        try:
            response = http_json(
                f"{env['N8N_URL'].rstrip('/')}/api/v1/executions/{execution_id}?includeData=true",
                api_key=env.get("N8N_API_KEY") or None,
                timeout=60,
            )
        except Exception as exc:
            try:
                local = fetch_local_execution_snapshot(execution_id)
                path = execution_dir / f"execution_{execution_id}_local_db.json"
                path.write_text(json.dumps(local, indent=2, sort_keys=True), encoding="utf-8")
                snapshots.append(
                    {
                        "execution_id": execution_id,
                        "fetch_status": "OK_LOCAL_N8N_DATABASE",
                        "path": str(path),
                        "api_error": f"{type(exc).__name__}: {exc}",
                        "body": local,
                    }
                )
                continue
            except Exception as local_exc:
                snapshots.append(
                    {
                        "execution_id": execution_id,
                        "fetch_status": "ERROR",
                        "error": f"API={type(exc).__name__}: {exc}; LOCAL_DB={type(local_exc).__name__}: {local_exc}",
                    }
                )
                continue
        path = execution_dir / f"execution_{execution_id}_include_data.json"
        path.write_text(json.dumps(response["body"], indent=2, sort_keys=True), encoding="utf-8")
        snapshots.append(
            {
                "execution_id": execution_id,
                "fetch_status": "OK",
                "path": str(path),
                "body": response["body"],
            }
        )
    return snapshots


def fetch_local_execution_snapshot(execution_id: str) -> dict[str, Any]:
    database_path = Path(os.environ.get("N8N_DATABASE_PATH") or (PROJECT_ROOT.parent / "database.sqlite"))
    if not database_path.exists():
        raise FileNotFoundError(f"n8n database not found: {database_path}")
    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT e.*, d.workflowData, d.data, d.workflowVersionId
            FROM execution_entity e
            JOIN execution_data d ON d.executionId = e.id
            WHERE e.id = ?
            """,
            (int(execution_id),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise KeyError(f"n8n execution {execution_id} was not found in the local database")
    flatted_module = os.environ.get(
        "N8N_FLATTED_MODULE_PATH",
        "/usr/local/lib/node_modules/n8n/node_modules/.pnpm/flatted@3.4.2/node_modules/flatted",
    )
    decoder = (
        f"const {{parse}}=require('{flatted_module}');let s='';"
        "process.stdin.on('data',d=>s+=d);"
        "process.stdin.on('end',()=>process.stdout.write(JSON.stringify(parse(s))));"
    )
    decoded = subprocess.run(
        ["docker", "exec", "-i", "n8n", "node", "-e", decoder],
        input=row["data"],
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    entity_fields = {
        key: row[key]
        for key in row.keys()
        if key not in {"data", "workflowData"}
    }
    return {
        **entity_fields,
        "workflowData": json.loads(row["workflowData"]),
        "data": json.loads(decoded.stdout),
        "execution_data_source": "LOCAL_N8N_SQLITE",
    }


def run_one(env: dict[str, str], row: dict[str, str], out_dir: Path, *, max_wait_seconds: int) -> dict[str, Any]:
    test_id = row["test_id"]
    test_started_epoch = time.time()
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", test_id)
    session_id = f"m12-full-{safe_id}-{int(time.time())}"
    execution_dir = out_dir / "executions" / safe_id
    execution_dir.mkdir(parents=True, exist_ok=True)
    if row.get("suite") == "TC4" and row.get("manual_feasibility") == "REQUIRES_BACKEND_INJECTION":
        backend_result = run_tc4_backend_injection(row, output_root=out_dir / "tc4_backend_injection")
        combined = {
            "test_id": test_id,
            "session_id": session_id,
            "row": row,
            "turns": [],
            "backend_injection_result": backend_result,
            "created_at_utc": now_utc(),
            "run_id": "",
            "scenario_spec_id": "",
            "n8n_execution_snapshots": [],
        }
        packet_score = score_combined(combined, PROJECT_ROOT)
        evaluation_fields = build_evaluation_fields(row, packet_score)
        combined["status"] = packet_score["status"]
        combined["failure_stage"] = packet_score["failure_stage"]
        combined["failure_cause"] = packet_score["failure_cause"]
        combined["packet_score"] = packet_score
        combined["checkpoint_evaluation"] = evaluation_fields
        combined_path = out_dir / "combined_executions" / f"{safe_id}.json"
        combined_path.parent.mkdir(parents=True, exist_ok=True)
        combined_path.write_text(json.dumps(combined, indent=2, sort_keys=True), encoding="utf-8")
        transcript_path = M12_ROOT / "manual_transcripts" / f"{safe_id}.txt"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(
            "\n".join(
                [
                    f"TEST_ID: {test_id}",
                    f"STATUS: {packet_score['status']}",
                    f"BACKEND_INJECTION: true",
                    f"FAILURE_STAGE: {packet_score['failure_stage']}",
                    f"FAILURE_CAUSE: {packet_score['failure_cause']}",
                    "",
                    json.dumps(backend_result, indent=2, sort_keys=True),
                ]
            ),
            encoding="utf-8",
        )
        record_result(row, combined_path, packet_score["status"], session_id, [])
        return {
            "created_at_utc": now_utc(),
            "test_id": test_id,
            "packet_test_id": row.get("packet_test_id", ""),
            "smoke_sequence": row.get("smoke_sequence", ""),
            "suite": row.get("suite", ""),
            "status": packet_score["status"],
            "failure_stage": packet_score["failure_stage"],
            "failure_cause": packet_score["failure_cause"],
            "data_quality_status": packet_score.get("data_quality_status", ""),
            "scoring_method": packet_score.get("scoring_method", ""),
            "packet_score_json": json.dumps(packet_score, sort_keys=True),
            "expected_status": row.get("expected_status", ""),
            "expected_interceptor": row.get("expected_interceptor", ""),
            "expected_deployment_blocked": row.get("expected_deployment_blocked", ""),
            "manual_feasibility": row.get("manual_feasibility", ""),
            "required_tools": row.get("required_tools", ""),
            "required_order": row.get("required_order", ""),
            "required_arguments": row.get("required_arguments", ""),
            "expected_fields_json": row.get("expected_fields_json", ""),
            "scenario_spec_id": "",
            "run_id": "",
            "chat_session_id": session_id,
            "n8n_execution_ids": "",
            "combined_execution_json": str(combined_path),
            "transcript": str(transcript_path),
            "full_sequence": row.get("full_sequence", ""),
            "total_tooling": row.get("total_tooling", ""),
            "num_envs": row.get("num_envs", ""),
            "add_reference_number": row.get("add_reference_number", ""),
            "should_launch_isaac": "false",
            "expected_validation_issue": row.get("expected_validation_issue", ""),
            "deployment_performed": False,
            **evaluation_fields,
        }
    turns: list[dict[str, Any]] = []
    execution_ids: list[str] = []
    approved = False
    should_launch_isaac = row.get("should_launch_isaac", "") != "false"
    ws: Any | None = None
    if should_launch_isaac and not wait_for_no_isaac(timeout_seconds=300):
        raise RuntimeError("ISAAC_BUSY_BEFORE_TEST: refusing to start another M12 test while Isaac Sim is still running")

    def write_turn_payload(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def turn(label: str, message: str, wait_seconds: int, *, artifact_since_epoch: float | None = None) -> dict[str, Any]:
        nonlocal ws
        path = execution_dir / f"{len(turns)+1:02d}_{label}.json"
        started = now_utc()
        execution_id = ""
        if ws is None:
            execution_id, ws, raw_start = start_chat_websocket(env, session_id=session_id, message=message)
            if execution_id:
                execution_ids.append(execution_id)
            text = receive_ws_message(ws, timeout_seconds=wait_seconds)
            execution = {"executionId": execution_id, "startResponse": raw_start, "websocketText": text}
        else:
            ws.send(json.dumps({"sessionId": session_id, "action": "sendMessage", "chatInput": message, "files": []}))
            if artifact_since_epoch is not None:
                text = receive_ws_message_until_artifact(ws, timeout_seconds=wait_seconds, since_epoch=artifact_since_epoch)
            else:
                text = receive_ws_message(ws, timeout_seconds=wait_seconds)
            execution = {"websocketText": text}
        completed = now_utc()
        write_turn_payload(path, execution)
        entry = {
            "label": label,
            "message": message,
            "execution_id": execution_id,
            "execution_path": str(path),
            "started_at_utc": started,
            "completed_at_utc": completed,
            "text": compact_text(execution),
        }
        turns.append(entry)
        return entry

    prompt = row.get("paste_into_n8n", "")
    first = turn("prompt", prompt, 120)
    text = "\n\n".join(item["text"] for item in turns)
    lower = text.lower()
    if response_requests_operator_details(text) and row.get("operator_details_reply"):
        turn("operator_details", row["operator_details_reply"], 180)
        text = "\n\n".join(item["text"] for item in turns)
        lower = text.lower()

    if "candidate patch passed validation" in lower:
        if row.get("approval_reply", "").startswith("APPROVE:") and should_launch_isaac:
            approved = True
            wait_seconds = max_wait_seconds if row.get("suite") in {"TC1", "TC3"} else 180
            approval_started_epoch = time.time()
            turn("approval", row["approval_reply"], wait_seconds, artifact_since_epoch=approval_started_epoch - 1)
            text = "\n\n".join(item["text"] for item in turns)
            lower = text.lower()
            if "deploy" in lower and "deployment is not allowed" not in lower:
                turn("do_not_deploy", "DO_NOT_DEPLOY", 120)
        else:
            turn("cancel", "cancel", 120)
    elif row.get("suite") == "TC4" and "deploy" in lower and "blocked" not in lower and "not allowed" not in lower:
        turn("cancel", "cancel", 120)

    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass

    combined = {
        "test_id": test_id,
        "session_id": session_id,
        "row": row,
        "turns": turns,
        "created_at_utc": now_utc(),
    }
    combined_text = json.dumps(combined, ensure_ascii=False, sort_keys=True)
    run_id = first_match(RUN_ID_RE, combined_text)
    scenario_spec_id = first_match(SCENARIO_ID_RE, combined_text)
    if not run_id and approved:
        artifact = newest_created_file(PROJECT_ROOT / "outputs" / "run_artifacts", "sim_*.sqlite*", since_epoch=test_started_epoch - 2)
        if artifact is not None:
            run_id = artifact.stem
    if not scenario_spec_id and approved:
        scenario = newest_created_file(PROJECT_ROOT / "outputs" / "scenario_specs", "scn_*.json", since_epoch=test_started_epoch - 2)
        if scenario is not None:
            scenario_spec_id = scenario.stem
    combined["run_id"] = run_id
    combined["scenario_spec_id"] = scenario_spec_id
    combined["n8n_execution_snapshots"] = fetch_execution_snapshots(env, execution_ids, execution_dir)
    strategy_selection = extract_strategy_selection(combined["n8n_execution_snapshots"])
    strategy_batch_id = strategy_selection.get("strategy_batch_id")
    if strategy_batch_id and not strategy_selection.get("selected_run_id"):
        try:
            batch_response = http_json(
                f"{env['TRT_API_URL'].rstrip('/')}/strategy/batches/{strategy_batch_id}",
                timeout=30,
            )
            strategy_selection = extract_strategy_selection(batch_response.get("body")) or strategy_selection
        except Exception as exc:
            combined["strategy_batch_fetch_error"] = f"{type(exc).__name__}: {exc}"
    selected_run_id = strategy_selection.get("selected_run_id")
    selected_scenario_spec_id = strategy_selection.get("selected_scenario_spec_id")
    if selected_run_id:
        run_id = str(selected_run_id)
    if selected_scenario_spec_id:
        scenario_spec_id = str(selected_scenario_spec_id)
    if not selected_run_id or not selected_scenario_spec_id:
        snapshot_run_id, snapshot_scenario_spec_id = extract_ids_from_payload(combined["n8n_execution_snapshots"])
        if snapshot_run_id and not selected_run_id:
            run_id = snapshot_run_id
        if snapshot_scenario_spec_id and not selected_scenario_spec_id:
            scenario_spec_id = snapshot_scenario_spec_id
    combined["strategy_selection"] = strategy_selection
    combined["run_id"] = run_id
    combined["scenario_spec_id"] = scenario_spec_id
    packet_score = score_combined(combined, PROJECT_ROOT)
    evaluation_fields = build_evaluation_fields(
        row,
        packet_score,
        scenario_spec_id=scenario_spec_id,
        run_id=run_id,
        turn_labels=[item["label"] for item in turns],
    )
    status = packet_score["status"]
    failure_stage = packet_score["failure_stage"]
    failure_cause = packet_score["failure_cause"]
    combined["status"] = status
    combined["run_id"] = run_id
    combined["scenario_spec_id"] = scenario_spec_id
    combined["failure_stage"] = failure_stage
    combined["failure_cause"] = failure_cause
    combined["packet_score"] = packet_score
    combined["checkpoint_evaluation"] = evaluation_fields
    combined_path = out_dir / "combined_executions" / f"{safe_id}.json"
    combined_path.parent.mkdir(parents=True, exist_ok=True)
    combined_path.write_text(json.dumps(combined, indent=2, sort_keys=True), encoding="utf-8")

    transcript_path = M12_ROOT / "manual_transcripts" / f"{safe_id}.txt"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        "\n".join(
            [
                f"TEST_ID: {test_id}",
                f"STATUS: {status}",
                f"CHAT_SESSION_ID: {session_id}",
                f"N8N_EXECUTION_IDS: {', '.join(execution_ids)}",
                f"SCENARIO_SPEC_ID: {scenario_spec_id}",
                f"RUN_ID: {run_id}",
                f"FAILURE_STAGE: {failure_stage}",
                f"FAILURE_CAUSE: {failure_cause}",
                "",
                "TURNS",
                *(
                    f"\n[{item['label']}]\nSENT: {item['message']}\nEXECUTION_ID: {item['execution_id']}\nTEXT:\n{item['text']}\n"
                    for item in turns
                ),
            ]
        ),
        encoding="utf-8",
    )

    record_result(row, combined_path, status, session_id, execution_ids)
    result = {
        "created_at_utc": now_utc(),
        "test_id": test_id,
        "packet_test_id": row.get("packet_test_id", ""),
        "smoke_sequence": row.get("smoke_sequence", ""),
        "suite": row.get("suite", ""),
        "status": status,
        "failure_stage": failure_stage,
        "failure_cause": failure_cause,
        "data_quality_status": packet_score.get("data_quality_status", ""),
        "scoring_method": packet_score.get("scoring_method", ""),
        "packet_score_json": json.dumps(packet_score, sort_keys=True),
        "expected_status": row.get("expected_status", ""),
        "expected_interceptor": row.get("expected_interceptor", ""),
        "expected_deployment_blocked": row.get("expected_deployment_blocked", ""),
        "manual_feasibility": row.get("manual_feasibility", ""),
        "required_tools": row.get("required_tools", ""),
        "required_order": row.get("required_order", ""),
        "required_arguments": row.get("required_arguments", ""),
        "expected_fields_json": row.get("expected_fields_json", ""),
        "scenario_spec_id": scenario_spec_id,
        "run_id": run_id,
        "strategy_batch_id": strategy_selection.get("strategy_batch_id", ""),
        "candidate_count": strategy_selection.get("candidate_count", ""),
        "candidate_run_ids": ";".join(strategy_selection.get("candidate_run_ids") or []),
        "selected_candidate_strategy_id": strategy_selection.get("selected_candidate_strategy_id", ""),
        "selection_objective_id": strategy_selection.get("objective_id", ""),
        "selection_objective_score": strategy_selection.get("objective_score", ""),
        "post_simulation_regeneration_performed": strategy_selection.get("post_simulation_regeneration_performed", ""),
        "chat_session_id": session_id,
        "n8n_execution_ids": ";".join(execution_ids),
        "combined_execution_json": str(combined_path),
        "transcript": str(transcript_path),
        "full_sequence": row.get("full_sequence", ""),
        "total_tooling": row.get("total_tooling", ""),
        "num_envs": row.get("num_envs", ""),
        "add_reference_number": row.get("add_reference_number", ""),
        "should_launch_isaac": row.get("should_launch_isaac", ""),
        "expected_validation_issue": row.get("expected_validation_issue", ""),
        "deployment_performed": False,
        **evaluation_fields,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full 174-row M12 n8n comparison packet.")
    parser.add_argument("--packet", default="outputs/reports/m12/manual_test_packet")
    parser.add_argument("--plan", default="outputs/reports/m12/full_test_plan")
    parser.add_argument("--output", default="outputs/reports/m12/automated_full_n8n")
    parser.add_argument("--start-at", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-wait-seconds", type=int, default=1800)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="Run outputs/reports/m12/manual_test_packet/smoke_queue_manual.csv only.")
    parser.add_argument(
        "--allow-unvalidated-scoring",
        action="store_true",
        help="Allow execution even though the current runner uses heuristic scoring. Use only for diagnostics, not final M12 results.",
    )
    args = parser.parse_args()

    packet_dir = PROJECT_ROOT / args.packet if not Path(args.packet).is_absolute() else Path(args.packet)
    plan_dir = PROJECT_ROOT / args.plan if not Path(args.plan).is_absolute() else Path(args.plan)
    runner_path = Path(__file__).resolve()
    readiness_findings = (
        packet_audit(packet_dir)["findings"]
        + plan_audit(plan_dir)["findings"]
        + runner_audit(runner_path)["findings"]
    )
    fatal_readiness = [finding for finding in readiness_findings if finding["severity"] == "FATAL"]
    if fatal_readiness and not args.allow_unvalidated_scoring:
        message = {
            "status": "BLOCKED",
            "reason": "Full M12 automation is not ready for trusted scoring. Run tools.m12_check_full_test_readiness for details, or pass --allow-unvalidated-scoring for diagnostic execution only.",
            "fatal_findings": fatal_readiness,
        }
        raise SystemExit(json.dumps(message, indent=2, sort_keys=True))

    env = load_env()
    local_execution_db = Path(os.environ.get("N8N_DATABASE_PATH") or (PROJECT_ROOT.parent / "database.sqlite"))
    missing = [key for key in ["N8N_URL", "N8N_CHAT_URL"] if not env.get(key)]
    if not env.get("N8N_API_KEY") and not local_execution_db.exists():
        missing.append("N8N_API_KEY or N8N_DATABASE_PATH")
    if missing:
        raise SystemExit(f"Missing n8n environment values: {', '.join(missing)}")

    out_dir = PROJECT_ROOT / args.output if not Path(args.output).is_absolute() else Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_test_id, by_setup_id = load_full_plan(plan_dir)
    if args.smoke:
        rows = load_smoke_rows(packet_dir)
    else:
        rows = [apply_full_plan(row, by_test_id, by_setup_id) for row in load_rows(packet_dir)]
    selected = [row for row in rows if int(row["full_order"]) >= args.start_at]
    if args.limit is not None:
        selected = selected[: args.limit]

    progress_path = out_dir / "progress.jsonl"
    completed = set()
    if args.resume and progress_path.exists():
        for line in progress_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                completed.add(json.loads(line)["test_id"])

    results: list[dict[str, Any]] = []
    for row in selected:
        if row["test_id"] in completed:
            continue
        append_jsonl(out_dir / "events.jsonl", {"event": "TEST_STARTED", "test_id": row["test_id"], "created_at_utc": now_utc()})
        try:
            result = run_one(env, row, out_dir, max_wait_seconds=args.max_wait_seconds)
        except Exception as exc:
            packet_score = {
                "status": "FAIL",
                "failure_stage": "runner_exception",
                "failure_cause": f"{type(exc).__name__}: {exc}",
                "data_quality_status": "SYSTEM_ERROR",
                "checks": {},
                "scoring_method": "M12_RUNNER_EXCEPTION",
            }
            evaluation_fields = build_evaluation_fields(
                row,
                packet_score,
                system_error=True,
            )
            result = {
                "created_at_utc": now_utc(),
                "test_id": row["test_id"],
                "packet_test_id": row.get("packet_test_id", ""),
                "smoke_sequence": row.get("smoke_sequence", ""),
                "suite": row.get("suite", ""),
                "status": "FAIL",
                "failure_stage": "runner_exception",
                "failure_cause": f"{type(exc).__name__}: {exc}",
                "scenario_spec_id": "",
                "run_id": "",
                "chat_session_id": "",
                "n8n_execution_ids": "",
                "combined_execution_json": "",
                "transcript": "",
                "full_sequence": row.get("full_sequence", ""),
                "total_tooling": row.get("total_tooling", ""),
                "num_envs": row.get("num_envs", ""),
                "add_reference_number": row.get("add_reference_number", ""),
                "should_launch_isaac": row.get("should_launch_isaac", ""),
                "expected_validation_issue": row.get("expected_validation_issue", ""),
                "deployment_performed": False,
                **evaluation_fields,
            }
        append_jsonl(progress_path, result)
        append_jsonl(out_dir / "events.jsonl", {"event": "TEST_COMPLETED", **result})
        results.append(result)
        write_csv(
            out_dir / "full_n8n_results_latest.csv",
            results,
            [
                "created_at_utc",
                "test_id",
                "packet_test_id",
                "smoke_sequence",
                "suite",
                "status",
                "failure_stage",
                "failure_cause",
                "data_quality_status",
                "scoring_method",
                "packet_score_json",
                "expected_status",
                "expected_interceptor",
                "expected_deployment_blocked",
                "manual_feasibility",
                "required_tools",
                "required_order",
                "required_arguments",
                "expected_fields_json",
                "scenario_spec_id",
                "run_id",
                "strategy_batch_id",
                "candidate_count",
                "candidate_run_ids",
                "selected_candidate_strategy_id",
                "selection_objective_id",
                "selection_objective_score",
                "post_simulation_regeneration_performed",
                "chat_session_id",
                "n8n_execution_ids",
                "full_sequence",
                "total_tooling",
                "num_envs",
                "add_reference_number",
                "should_launch_isaac",
                "expected_validation_issue",
                "deployment_performed",
                "CP0",
                "CP1",
                "CP2",
                "CP3",
                "CP4",
                "CP5",
                "CP6",
                "automated_result",
                "manual_result",
                "human_reviewed",
                "manual_correction_used",
                "manual_intervention_required",
                "outcome_class",
                "failure_cause_code",
                "rejection_reason",
                "correction_method",
                "combined_execution_json",
                "transcript",
            ],
        )
    print(json.dumps({"status": "OK", "attempted": len(results), "output": str(out_dir)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
