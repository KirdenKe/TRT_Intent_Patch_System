from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trt_core.repository import PROJECT_ROOT


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def request_json(url: str, *, api_key: str | None = None, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-N8N-API-KEY"] = api_key
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(text)
        except json.JSONDecodeError:
            body = {"raw_text": text}
        return {"status_code": response.status, "body": body, "headers": dict(response.headers)}


def workflow_payload(body: Any) -> dict[str, Any]:
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


def workflow_list_payload(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return [item for item in body["data"] if isinstance(item, dict)]
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    return []


def node_names(workflow: dict[str, Any]) -> list[str]:
    return [str(node.get("name") or "") for node in workflow.get("nodes") or [] if isinstance(node, dict)]


def node_types(workflow: dict[str, Any]) -> list[str]:
    return [str(node.get("type") or "") for node in workflow.get("nodes") or [] if isinstance(node, dict)]


def verify_workflow_structure(workflow: dict[str, Any]) -> dict[str, Any]:
    names = node_names(workflow)
    types = node_types(workflow)
    lower_names = [name.lower() for name in names]
    lower_types = [node_type.lower() for node_type in types]

    def has_name(*tokens: str) -> bool:
        return any(all(token in name for token in tokens) for name in lower_names)

    return {
        "chat_trigger_exists": any("chattrigger" in node_type or "chattrigger" in name for node_type, name in zip(lower_types, lower_names)),
        "response_mode_uses_response_nodes": any("respond" in node_type or "response" in name for node_type, name in zip(lower_types, lower_names)),
        "approval_chat_node_exists": has_name("approval") or has_name("approve"),
        "deployment_decision_chat_node_exists": has_name("deployment") or has_name("deploy"),
        "session_save_load_nodes_exist": (has_name("save", "session") or has_name("session", "save")) and (has_name("load", "session") or has_name("session", "load")),
        "config_query_path_exists": has_name("config") or has_name("query"),
        "error_evidence_paths_save_session_state": (has_name("evidence") or has_name("error")) and (has_name("save", "session") or has_name("session", "save")),
        "deployment_path_does_not_rerun_isaac_after_deploy": not (has_name("deploy") and has_name("isaac", "run")),
        "cancel_routed_before_required_fields": has_name("cancel"),
        "help_routed_to_help": has_name("help"),
        "node_names": names,
    }


def resolve_workflow_reference(base_url: str, api_key: str, workflow_ref: str, report: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    try:
        workflow_response = request_json(f"{base_url}/api/v1/workflows/{workflow_ref}", api_key=api_key)
        workflow = workflow_payload(workflow_response["body"])
        if workflow:
            report["workflow_reference_used"] = workflow_ref
            report["workflow_resolved_by"] = "id"
            return str(workflow.get("id") or workflow_ref), workflow
    except Exception as exc:
        report["errors"].append(f"workflow direct lookup failed for {workflow_ref}: {exc}")

    try:
        workflows_response = request_json(f"{base_url}/api/v1/workflows?limit=100", api_key=api_key)
        workflows = workflow_list_payload(workflows_response["body"])
        exact = [workflow for workflow in workflows if str(workflow.get("name") or "") == workflow_ref]
        insensitive = [workflow for workflow in workflows if str(workflow.get("name") or "").lower() == workflow_ref.lower()]
        matches = exact or insensitive
        report["workflow_name_matches"] = [
            {"id": str(workflow.get("id")), "name": workflow.get("name"), "active": workflow.get("active")}
            for workflow in matches
        ]
        if len(matches) == 1:
            resolved_id = str(matches[0].get("id"))
            workflow_response = request_json(f"{base_url}/api/v1/workflows/{resolved_id}", api_key=api_key)
            workflow = workflow_payload(workflow_response["body"])
            report["workflow_reference_used"] = workflow_ref
            report["workflow_resolved_by"] = "name"
            report["resolved_workflow_id"] = resolved_id
            return resolved_id, workflow
        if len(matches) > 1:
            report["errors"].append(f"workflow name is ambiguous: {workflow_ref}")
        else:
            report["errors"].append(f"workflow not found by id or name: {workflow_ref}")
    except Exception as exc:
        report["errors"].append(f"workflow list lookup failed: {exc}")
    return None, {}


def probe() -> dict[str, Any]:
    base_url = (os.environ.get("N8N_BASE_URL") or os.environ.get("N8N_URL") or "").rstrip("/")
    api_key = os.environ.get("N8N_API_KEY")
    workflow_id = os.environ.get("N8N_WORKFLOW_ID")
    chat_url = os.environ.get("N8N_CHAT_URL") or os.environ.get("N8N_WEBHOOK_URL")
    report: dict[str, Any] = {
        "created_at_utc": now_utc(),
        "n8n_api_accessible": False,
        "workflow_found": False,
        "workflow_active": False,
        "latest_execution_ids": [],
        "chat_endpoint_accessible": False,
        "chat_response_received": False,
        "chat_session_id": None,
        "workflow_execution_id": None,
        "workflow_structure": {},
        "errors": [],
    }

    workflow: dict[str, Any] = {}
    if not api_key:
        report["reason"] = "N8N_API_KEY not configured"
        report["errors"].append("N8N_API_KEY not configured")
    elif not base_url or not workflow_id:
        report["errors"].append("N8N_BASE_URL/N8N_URL or N8N_WORKFLOW_ID not configured")
    else:
        try:
            report["n8n_api_accessible"] = True
            resolved_workflow_id, workflow = resolve_workflow_reference(base_url, api_key, workflow_id, report)
            workflow_id = resolved_workflow_id or workflow_id
            report["workflow_found"] = bool(workflow)
            report["workflow_active"] = bool(workflow.get("active"))
            report["workflow_structure"] = verify_workflow_structure(workflow)
        except Exception as exc:
            report["errors"].append(f"workflow API probe failed: {exc}")
        try:
            query = urllib.parse.urlencode({"workflowId": workflow_id, "limit": 5})
            executions_response = request_json(f"{base_url}/api/v1/executions?{query}", api_key=api_key)
            body = executions_response["body"]
            executions = body.get("data") if isinstance(body, dict) else []
            report["latest_execution_ids"] = [
                str(item.get("id"))
                for item in (executions or [])
                if isinstance(item, dict) and item.get("id") is not None
            ]
        except Exception as exc:
            report["errors"].append(f"executions API probe failed: {exc}")

    if not chat_url:
        report["chat_reason"] = "N8N_CHAT_URL not configured"
        report["errors"].append("N8N_CHAT_URL not configured")
    else:
        try:
            session_id = f"m12-access-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            response = request_json(chat_url, method="POST", payload={"chatInput": "help", "sessionId": session_id})
            report["chat_endpoint_accessible"] = True
            report["chat_session_id"] = session_id
            report["chat_raw_response"] = response["body"]
            body = response["body"]
            text = json.dumps(body) if not isinstance(body, str) else body
            report["chat_response_received"] = bool(text.strip())
            if isinstance(body, dict):
                report["workflow_execution_id"] = body.get("executionId") or body.get("execution_id")
        except Exception as exc:
            report["errors"].append(f"chat endpoint probe failed: {exc}")

    if not report["chat_endpoint_accessible"]:
        report["chat_access_next_steps"] = [
            "Provide the public Chat Trigger URL.",
            "Provide the active workflow export JSON and a test execution endpoint.",
            "Provide an n8n API key that can trigger executions.",
        ]
    return report


def write_reports(report: dict[str, Any]) -> None:
    root = PROJECT_ROOT / "outputs" / "reports" / "m12"
    root.mkdir(parents=True, exist_ok=True)
    (root / "n8n_access_report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# M12 n8n Access Report",
        "",
        f"Created: {report['created_at_utc']}",
        f"n8n API accessible: {report.get('n8n_api_accessible')}",
        f"Workflow found: {report.get('workflow_found')}",
        f"Workflow active: {report.get('workflow_active')}",
        f"Chat endpoint accessible: {report.get('chat_endpoint_accessible')}",
        f"Chat response received: {report.get('chat_response_received')}",
        "",
        "## Errors",
    ]
    errors = report.get("errors") or []
    lines.extend([f"- {error}" for error in errors] or ["- None"])
    lines.extend(["", "## Workflow Structure"])
    structure = report.get("workflow_structure") or {}
    for key, value in structure.items():
        if key != "node_names":
            lines.append(f"- {key}: {value}")
    (root / "n8n_access_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    report = probe()
    write_reports(report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
