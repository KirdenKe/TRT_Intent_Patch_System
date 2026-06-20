"""HTTP client for the Windows-hosted Isaac runner service."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HostRunnerClientError(RuntimeError):
    """Raised when the host runner cannot be reached or returns bad data."""


def post_isaac_run(base_url: str, payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/isaac/run"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = str(exc)
        raise HostRunnerClientError(f"Host runner returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HostRunnerClientError(f"Host runner request failed: {exc}") from exc


def post_isaac_runs(base_url: str, payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/isaac/runs"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = str(exc)
        raise HostRunnerClientError(f"Host runner async-start endpoint returned HTTP {exc.code}: {detail}") from exc
    except TimeoutError as exc:
        raise HostRunnerClientError(f"HOST_RUNNER_START_TIMEOUT: {exc}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise HostRunnerClientError(f"Host runner async-start request failed: {exc}") from exc


def post_isaac_dry_run(base_url: str, payload: dict[str, Any], *, timeout_seconds: int = 5) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/isaac/dry-run"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = str(exc)
        raise HostRunnerClientError(f"Host runner dry-run endpoint returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HostRunnerClientError(f"Host runner dry-run request failed: {exc}") from exc


def get_isaac_health(base_url: str, *, timeout_seconds: int = 5) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/health"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = str(exc)
        raise HostRunnerClientError(f"Host runner health endpoint returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HostRunnerClientError(f"Host runner health request failed: {exc}") from exc


def get_isaac_result(base_url: str, run_id: str, *, timeout_seconds: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + f"/isaac/results/{run_id}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = str(exc)
        raise HostRunnerClientError(f"Host runner result endpoint returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HostRunnerClientError(f"Host runner result request failed: {exc}") from exc


def get_isaac_run(base_url: str, run_id: str, *, timeout_seconds: int = 5) -> dict[str, Any]:
    url = base_url.rstrip("/") + f"/isaac/runs/{run_id}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = str(exc)
        raise HostRunnerClientError(f"Host runner run-status endpoint returned HTTP {exc.code}: {detail}") from exc
    except TimeoutError as exc:
        raise HostRunnerClientError(f"HOST_RUNNER_STATUS_TIMEOUT: {exc}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise HostRunnerClientError(f"Host runner run-status request failed: {exc}") from exc
