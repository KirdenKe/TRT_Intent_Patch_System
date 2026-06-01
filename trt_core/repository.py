"""File-backed repository for versioned TRTs and immutable Audit Bundles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trt_core.errors import RepositoryError
from trt_core.models import AuditBundle, TRT


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TRTRepository:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else PROJECT_ROOT
        self.trt_dir = self.root / "data" / "trt_versions"
        self.audit_dir = self.root / "data" / "audit_bundles"
        self.release_dir = self.root / "data" / "releases"
        self.state_dir = self.root / "data" / "state_records"
        self.reconciliation_dir = self.root / "data" / "reconciliation_plans"
        self.trt_dir.mkdir(parents=True, exist_ok=True)
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
        if not version.startswith("v"):
            return -1
        try:
            return int(version[1:])
        except ValueError:
            return -1

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
        path.write_text(json.dumps(trt, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return path

    def load_trt(self, trt_id: str, version: str) -> TRT:
        path = self._trt_path(trt_id, version)
        if not path.exists():
            raise RepositoryError(f"TRT version not found: {trt_id}@{version}")
        return json.loads(path.read_text(encoding="utf-8"))

    def get_current_trt(self, trt_id: str | None = None) -> TRT:
        candidates: list[TRT] = []
        for path in self.list_trt_versions(trt_id):
            candidates.append(json.loads(path.read_text(encoding="utf-8")))
        if not candidates:
            raise RepositoryError("No TRT versions found")
        return max(candidates, key=lambda trt: self._version_number(trt["version"]))

    def next_version(self, current_version: str) -> str:
        number = self._version_number(current_version)
        if number < 0:
            raise RepositoryError(f"Unsupported version format: {current_version}")
        return f"v{number + 1}"

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
        path.write_text(json.dumps(release_record, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return path

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
        return json.loads(path.read_text(encoding="utf-8"))["state_records"]

    def save_reconciliation_plan(self, plan: dict[str, Any]) -> Path:
        path = self._reconciliation_path(str(plan["plan_id"]))
        path.write_text(json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        return path

    def load_reconciliation_plan(self, plan_id: str) -> dict[str, Any]:
        path = self._reconciliation_path(plan_id)
        if not path.exists():
            raise RepositoryError(f"Reconciliation plan not found: {plan_id}")
        return json.loads(path.read_text(encoding="utf-8"))
