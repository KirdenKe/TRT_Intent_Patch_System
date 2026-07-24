from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trt_core.repository import PROJECT_ROOT


OPENAPI_URLS = ["http://localhost:8000/openapi.json", "http://trt-api:8000/openapi.json"]
ROUTE_PATTERNS = {
    "dialogue_decision": ["/chat/dialogue-decision"],
    "intent_review_candidate_patch_generation": ["/intent/normalize", "/release/prepare"],
    "release_approval": ["/release/decision"],
    "scenario_spec_generation": ["/scenario/generate"],
    "simulation_run": ["/simulation/run", "/simulation/runs"],
    "host_runner_status": ["/debug/isaac-host-runner-status"],
    "evidence_summarization": ["/evidence/summarize"],
    "deployment_endpoint": ["/deployment/simulated-deploy"],
    "m12_metrics": ["/reports/m12/runs/{run_id}/metrics"],
    "m12_reports": ["/reports/m12/export.csv", "/reports/m12/figures"],
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_openapi(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def static_api_routes() -> list[str]:
    api_path = PROJECT_ROOT / "trt_core" / "api.py"
    if not api_path.exists():
        return []
    text = api_path.read_text(encoding="utf-8")
    routes = []
    for match in re.finditer(r'@app\.(?:get|post|put|delete)\("([^"]+)"', text):
        routes.append(match.group(1))
    return sorted(set(routes))


def detect_routes(paths: list[str]) -> dict[str, Any]:
    detected = {}
    for capability, expected_paths in ROUTE_PATTERNS.items():
        matched = [path for path in expected_paths if path in paths]
        detected[capability] = {"available": bool(matched), "matched_routes": matched, "expected_routes": expected_paths}
    return detected


def discover(output: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path
    output_path.mkdir(parents=True, exist_ok=True)

    openapi_source = None
    openapi_paths: list[str] = []
    for url in OPENAPI_URLS:
        payload = fetch_openapi(url)
        if payload:
            openapi_source = url
            openapi_paths = sorted((payload.get("paths") or {}).keys())
            break
    static_paths = static_api_routes()
    selected_paths = openapi_paths or static_paths
    discovery = {
        "created_at_utc": now_utc(),
        "openapi_source": openapi_source,
        "openapi_reachable": bool(openapi_source),
        "static_api_routes_used": not bool(openapi_source),
        "routes": detect_routes(selected_paths),
        "all_detected_paths": selected_paths,
    }
    n8n_base_url = os.environ.get("N8N_BASE_URL") or os.environ.get("N8N_URL")
    n8n_available = bool(os.environ.get("N8N_CHAT_URL") or os.environ.get("N8N_WEBHOOK_URL") or (n8n_base_url and os.environ.get("N8N_API_KEY")))
    if n8n_available:
        selected_tier = "N8N_CHAT_OR_API"
        reason = "n8n environment variables are configured."
        n8n_access = "CONFIGURED"
    else:
        selected_tier = "DIRECT_TRT_API"
        reason = "N8N_CHAT_URL and N8N_API_KEY were not configured."
        n8n_access = "UNAVAILABLE"
    strategy = {
        "created_at_utc": now_utc(),
        "n8n_access": n8n_access,
        "selected_execution_tier": selected_tier,
        "reason": reason,
        "openapi_reachable": bool(openapi_source),
        "direct_trt_api_routes_available": any(item["available"] for item in discovery["routes"].values()),
        "deployment_disabled": True,
        "deployment_suppressed_reason": "M12 automated comparison test mode",
    }
    (output_path / "trt_api_routes.json").write_text(json.dumps(discovery, indent=2, sort_keys=True), encoding="utf-8")
    (output_path / "execution_strategy.json").write_text(json.dumps(strategy, indent=2, sort_keys=True), encoding="utf-8")
    return discovery, strategy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="outputs/reports/m12/discovery")
    args = parser.parse_args()
    discovery, strategy = discover(args.output)
    print(json.dumps({"status": "OK", "strategy": strategy, "route_capabilities": discovery["routes"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
