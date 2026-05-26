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
        self.trt_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def _trt_path(self, trt_id: str, version: str) -> Path:
        return self.trt_dir / f"{trt_id}_{version}.json"

    def _audit_path(self, audit_id: str) -> Path:
        return self.audit_dir / f"{audit_id}.json"

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

