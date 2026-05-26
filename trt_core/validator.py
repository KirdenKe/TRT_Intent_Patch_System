"""Schema and firewall validation for TRT Intent Patches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from trt_core.models import IntentPatch, TRT, ValidationResults


SUPPORTED_OPERATIONS = {"test", "add", "replace", "remove"}
WRITABLE_LINE_FIELDS = {"goal", "allowed_instruments", "excluded_instruments", "priority", "kpi", "abnormal_strategy"}
WRITABLE_KPI_FIELDS = {"deadline_minutes", "max_downtime_seconds", "min_throughput_per_hour"}


def default_validation_results() -> ValidationResults:
    return {
        "schema": True,
        "path_whitelist": True,
        "readonly": True,
        "base_version": True,
        "semantic": True,
    }


def load_schema(name: str) -> dict[str, Any]:
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / name
    return json.loads(schema_path.read_text(encoding="utf-8"))


def schema_errors(document: dict[str, Any], schema_name: str) -> list[str]:
    validator = Draft202012Validator(load_schema(schema_name))
    return [error.message for error in sorted(validator.iter_errors(document), key=lambda item: item.path)]


def validate_trt_schema(trt: TRT | dict[str, Any]) -> list[str]:
    return schema_errors(dict(trt), "trt.schema.json")


def validate_intent_patch_schema(intent_patch: IntentPatch | dict[str, Any]) -> list[str]:
    return schema_errors(dict(intent_patch), "intent_patch.schema.json")


def validate_audit_bundle_schema(audit_bundle: dict[str, Any]) -> list[str]:
    return schema_errors(audit_bundle, "audit_bundle.schema.json")


def _decode_pointer(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        return []
    return [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]


def is_readonly_path(path: str) -> bool:
    parts = _decode_pointer(path)
    return len(parts) >= 3 and parts[0] == "lines" and parts[2] == "state"


def is_whitelisted_path(path: str) -> bool:
    parts = _decode_pointer(path)
    if len(parts) < 3 or parts[0] != "lines":
        return False
    field = parts[2]
    if field == "state":
        return False
    if field in {"goal", "priority", "abnormal_strategy"}:
        return len(parts) == 3
    if field in {"allowed_instruments", "excluded_instruments"}:
        return len(parts) in {3, 4}
    if field == "kpi":
        return len(parts) == 4 and parts[3] in WRITABLE_KPI_FIELDS
    return False


def validate_firewall(intent_patch: IntentPatch | dict[str, Any], current_trt: TRT | dict[str, Any]) -> tuple[ValidationResults, list[str]]:
    results = default_validation_results()
    reasons: list[str] = []

    patch_schema_errors = validate_intent_patch_schema(intent_patch)
    current_schema_errors = validate_trt_schema(current_trt)
    if patch_schema_errors or current_schema_errors:
        results["schema"] = False
        reasons.extend(f"schema: {message}" for message in patch_schema_errors + current_schema_errors)

    if intent_patch.get("trt_id") != current_trt.get("trt_id") or intent_patch.get("base_version") != current_trt.get("version"):
        results["base_version"] = False
        reasons.append(
            f"base_version mismatch: patch targets {intent_patch.get('trt_id')}@{intent_patch.get('base_version')}, "
            f"current is {current_trt.get('trt_id')}@{current_trt.get('version')}"
        )

    for index, operation in enumerate(intent_patch.get("operations", [])):
        op = operation.get("op")
        path = operation.get("path", "")
        if op not in SUPPORTED_OPERATIONS:
            results["path_whitelist"] = False
            reasons.append(f"operation {index} uses unsupported op {op!r}")
        if not is_whitelisted_path(path):
            results["path_whitelist"] = False
            reasons.append(f"operation {index} path is not whitelisted: {path}")
        if is_readonly_path(path):
            results["readonly"] = False
            reasons.append(f"operation {index} attempts to patch read-only state path: {path}")

    return results, reasons

