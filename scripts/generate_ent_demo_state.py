"""Generate clean ENT surgical tooling TRT and runtime state demo files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trt_core.ent_demo import TRT_ID, TRT_VERSION, build_current_state, build_trt
from trt_core.repository import PROJECT_ROOT
from trt_core.repository import TRTRepository


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def generate(root: Path = PROJECT_ROOT) -> dict[str, str]:
    repository = TRTRepository(root)
    trt = build_trt(repository)
    state = build_current_state(repository)
    trt_versions_path = root / "data" / "trt_versions" / f"{TRT_ID}_{TRT_VERSION}.json"
    trt_review_path = root / "data" / "trt" / f"{TRT_ID}_{TRT_VERSION}.json"
    current_pointer_path = root / "data" / "trt" / "current_trt.json"
    state_path = root / "data" / "state_records" / "current_state.json"

    write_json(trt_versions_path, trt)
    write_json(trt_review_path, trt)
    write_json(current_pointer_path, {"trt_id": TRT_ID, "version": TRT_VERSION})
    write_json(state_path, state)
    return {
        "trt_versions_path": str(trt_versions_path),
        "trt_review_path": str(trt_review_path),
        "current_pointer_path": str(current_pointer_path),
        "state_path": str(state_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate ENT demo TRT and runtime state files.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Repository root. Defaults to this project.")
    args = parser.parse_args()
    print(json.dumps(generate(args.root), indent=2))


if __name__ == "__main__":
    main()
