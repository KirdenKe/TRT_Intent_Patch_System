"""File-backed repository for versioned TRTs and immutable Audit Bundles."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from trt_core.errors import RepositoryError
from trt_core.models import AuditBundle, TRT


PROJECT_ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)


def parse_trt_version(version: str) -> int:
    if not isinstance(version, str) or not version.startswith("v"):
        raise RepositoryError(f"Unsupported TRT version format: {version}")
    try:
        number = int(version[1:])
    except ValueError as exc:
        raise RepositoryError(f"Unsupported TRT version format: {version}") from exc
    if number < 1:
        raise RepositoryError(f"Unsupported TRT version format: {version}")
    return number


def format_trt_version(number: int) -> str:
    if not isinstance(number, int) or number < 1:
        raise RepositoryError(f"Unsupported TRT version number: {number}")
    return f"v{number}"


class TRTRepository:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else PROJECT_ROOT
        self.trt_dir = self.root / "data" / "trt_versions"
        self.trt_pointer_dir = self.root / "data" / "trt"
        self.audit_dir = self.root / "data" / "audit_bundles"
        self.release_dir = self.root / "data" / "releases"
        self.state_dir = self.root / "data" / "state_records"
        self.reconciliation_dir = self.root / "data" / "reconciliation_plans"
        self.trt_dir.mkdir(parents=True, exist_ok=True)
        self.trt_pointer_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.release_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.reconciliation_dir.mkdir(parents=True, exist_ok=True)

    def _trt_path(self, trt_id: str, version: str) -> Path:
        return self.trt_dir / f"{trt_id}_{version}.json"

    def _audit_path(self, audit_id: str) -> Path:
        return self.audit_dir / f"{audit_id}.json"

    def _release_path(self, release_id: str) -> Path:
        return self.release_dir / f"{release_id}.json"

    def _reconciliation_path(self, plan_id: str) -> Path:
        return self.reconciliation_dir / f"{plan_id}.json"

    @staticmethod
    def _version_number(version: str) -> int:
        try:
            return parse_trt_version(version)
        except RepositoryError:
            return -1

    def _atomic_write_json(self, path: Path, document: dict[str, Any] | list[Any], *, overwrite: bool = True) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise RepositoryError(f"Refusing to overwrite existing file: {path}")
        tmp_path = path.with_name(f".{path.name}.tmp")
        payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        return path

    def list_trt_versions(self, trt_id: str | None = None) -> list[Path]:
        paths = sorted(self.trt_dir.glob("*.json"))
        if trt_id is not None:
            prefix = f"{trt_id}_"
            paths = [path for path in paths if path.name.startswith(prefix)]
        return paths

    def list_trt_version_records(self, trt_id: str | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self.list_trt_versions(trt_id):
            trt = json.loads(path.read_text(encoding="utf-8"))
            records.append({"trt_id": trt["trt_id"], "version": trt["version"], "path": str(path)})
        return sorted(records, key=lambda item: (item["trt_id"], self._version_number(item["version"])))

    def list_release_records(self) -> list[dict[str, Any]]:
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.release_dir.glob("*.json"))]
        return sorted(records, key=lambda item: item.get("created_at_utc", ""))

    def list_reconciliation_plans(self) -> list[dict[str, Any]]:
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.reconciliation_dir.glob("*.json"))]
        return sorted(records, key=lambda item: item.get("created_at_utc", ""))

    def save_trt(self, trt: TRT | dict[str, Any]) -> Path:
        path = self._trt_path(str(trt["trt_id"]), str(trt["version"]))
        return self._atomic_write_json(path, dict(trt), overwrite=True)

    def current_trt_path(self) -> Path:
        return self.trt_pointer_dir / "current_trt.json"

    def save_current_trt_snapshot(self, trt: TRT | dict[str, Any]) -> Path:
        return self._atomic_write_json(self.current_trt_path(), dict(trt), overwrite=True)

    def load_trt(self, trt_id: str, version: str) -> TRT:
        path = self._trt_path(trt_id, version)
        if not path.exists():
            raise RepositoryError(f"TRT version not found: {trt_id}@{version}")
        return json.loads(path.read_text(encoding="utf-8"))

    def get_current_trt(self, trt_id: str | None = None) -> TRT:
        state = self.trt_version_state(trt_id)
        if not state["version_files"]:
            raise RepositoryError("No TRT versions found")
        if not state["is_consistent"]:
            latest = self.load_trt(state["trt_id"], format_trt_version(state["latest_version_number"]))
            logger.warning(
                "current_trt snapshot is stale; repairing to latest version: trt_id=%s current=%s latest=v%s",
                state["trt_id"],
                state["current_trt_version"],
                state["latest_version_number"],
            )
            self.save_current_trt_snapshot(latest)
            return latest
        pointer_path = self.current_trt_path()
        if pointer_path.exists():
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer_trt_id = pointer.get("trt_id")
            pointer_version = pointer.get("version")
            if pointer_trt_id and pointer_version and (trt_id is None or trt_id == pointer_trt_id):
                if "lines" not in pointer:
                    latest = self.load_trt(str(pointer_trt_id), str(pointer_version))
                    self.save_current_trt_snapshot(latest)
                    return latest
                return pointer
        return self.load_trt(state["trt_id"], format_trt_version(state["latest_version_number"]))

    def next_version(self, current_version: str) -> str:
        return format_trt_version(parse_trt_version(current_version) + 1)

    def get_latest_trt_version_number(self, trt_id: str) -> int:
        state = self.trt_version_state(trt_id)
        if not state["version_files"]:
            raise RepositoryError(f"No TRT versions found for {trt_id}")
        return int(state["latest_version_number"])

    def next_released_version(self, trt_id: str) -> str:
        return format_trt_version(self.get_latest_trt_version_number(trt_id) + 1)

    def save_released_trt_version(self, trt: TRT | dict[str, Any]) -> dict[str, Any]:
        version_path = self._trt_path(str(trt["trt_id"]), str(trt["version"]))
        self._atomic_write_json(version_path, dict(trt), overwrite=False)
        current_path = self.save_current_trt_snapshot(trt)
        return {
            "trt_id": trt["trt_id"],
            "trt_version": trt["version"],
            "version_path": str(version_path),
            "current_trt_path": str(current_path),
        }

    def trt_version_state(self, trt_id: str | None = None) -> dict[str, Any]:
        records = self.list_trt_version_records(trt_id)
        version_files = [Path(record["path"]).name for record in records]
        latest_record = max(records, key=lambda record: self._version_number(record["version"])) if records else None
        current_path = self.current_trt_path()
        current_version = None
        current_trt_id = trt_id
        if current_path.exists():
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current_trt_id = current.get("trt_id") or current_trt_id
            current_version = current.get("version")
        latest_number = self._version_number(latest_record["version"]) if latest_record else -1
        current_number = self._version_number(current_version) if current_version else -1
        if trt_id is not None and current_trt_id != trt_id:
            current_number = -1
            current_version = None
        return {
            "trt_id": trt_id or current_trt_id or (latest_record or {}).get("trt_id"),
            "current_trt_version": current_version,
            "current_trt_path": str(current_path),
            "latest_version_file": Path(latest_record["path"]).name if latest_record else None,
            "latest_version_path": latest_record["path"] if latest_record else None,
            "latest_version_number": latest_number,
            "version_files": version_files,
            "is_consistent": bool(latest_record and current_number == latest_number),
        }

    def repair_current_trt(self, trt_id: str) -> dict[str, Any]:
        state = self.trt_version_state(trt_id)
        if not state["version_files"]:
            raise RepositoryError(f"No TRT versions found for {trt_id}")
        latest = self.load_trt(trt_id, format_trt_version(state["latest_version_number"]))
        self.save_current_trt_snapshot(latest)
        return self.trt_version_state(trt_id)

    def save_audit_bundle(self, audit_bundle: AuditBundle | dict[str, Any]) -> Path:
        path = self._audit_path(str(audit_bundle["audit_id"]))
        if path.exists():
            raise RepositoryError(f"Audit bundle already exists: {audit_bundle['audit_id']}")
        path.write_text(json.dumps(audit_bundle, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return path

    def load_audit_bundle(self, audit_id: str) -> AuditBundle:
        path = self._audit_path(audit_id)
        if not path.exists():
            raise RepositoryError(f"Audit bundle not found: {audit_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_release_record(self, release_record: dict[str, Any]) -> Path:
        path = self._release_path(str(release_record["release_id"]))
        return self._atomic_write_json(path, release_record, overwrite=True)

    def load_release_record(self, release_id: str) -> dict[str, Any]:
        path = self._release_path(release_id)
        if not path.exists():
            raise RepositoryError(f"Release record not found: {release_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_state_records(self, state_records: list[dict[str, Any]]) -> Path:
        path = self.state_dir / "current_state.json"
        path.write_text(json.dumps({"state_records": state_records}, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return path

    def load_state_records(self) -> list[dict[str, Any]]:
        path = self.state_dir / "current_state.json"
        if not path.exists():
            raise RepositoryError("No current state records found")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "state_records" in payload:
            return payload["state_records"]
        if isinstance(payload, dict) and isinstance(payload.get("lines"), dict):
            records = []
            for line_id, line_state in sorted(payload["lines"].items()):
                record = dict(line_state)
                record["line_id"] = line_id
                records.append(record)
            return records
        raise RepositoryError("Unsupported current state record format")

    def save_reconciliation_plan(self, plan: dict[str, Any]) -> Path:
        path = self._reconciliation_path(str(plan["plan_id"]))
        path.write_text(json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return path

    def load_reconciliation_plan(self, plan_id: str) -> dict[str, Any]:
        path = self._reconciliation_path(plan_id)
        if not path.exists():
            raise RepositoryError(f"Reconciliation plan not found: {plan_id}")
        return json.loads(path.read_text(encoding="utf-8"))
