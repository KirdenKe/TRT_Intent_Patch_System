"""Validate that all 30 M12 smoke checks were executed and passed."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def verdict(row: dict[str, str]) -> str:
    return str(
        row.get("manual_result")
        or row.get("reviewed_status")
        or row.get("human_binary_status")
        or row.get("status")
        or ""
    ).upper()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-results", type=Path, required=True)
    parser.add_argument("--extension-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    core = {row.get("test_id"): row for row in read_csv(args.core_results)}
    extensions = {row.get("extension_id"): row for row in read_csv(args.extension_results)}
    expected_core = [f"SMOKE_{index:03d}" for index in range(1, 28)]
    expected_extensions = [f"SMOKE_{index:03d}" for index in range(28, 31)]
    missing = [test_id for test_id in expected_core if test_id not in core]
    missing.extend(test_id for test_id in expected_extensions if test_id not in extensions)
    failed = [test_id for test_id in expected_core if verdict(core.get(test_id, {})) != "PASS"]
    failed.extend(
        test_id for test_id in expected_extensions if verdict(extensions.get(test_id, {})) != "PASS"
    )
    status = "PASS" if not missing and not failed else "INCOMPLETE_OR_FAILED"
    result = {
        "status": status,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "required_smoke_checks": 30,
        "core_checks_required": 27,
        "extension_checks_required": 3,
        "missing_checks": missing,
        "nonpassing_checks": failed,
        "full_suite_ready": status == "PASS",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
