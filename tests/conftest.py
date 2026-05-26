from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from trt_core.repository import TRTRepository


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def fixture_loader():
    return load_fixture


@pytest.fixture
def repo(tmp_path: Path) -> TRTRepository:
    repository = TRTRepository(tmp_path)
    repository.save_trt(load_fixture("trt_v1.json"))
    return repository


@pytest.fixture
def valid_patch(fixture_loader):
    return deepcopy(fixture_loader("valid_patch.json"))

