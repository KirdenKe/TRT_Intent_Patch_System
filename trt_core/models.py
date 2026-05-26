"""Typed model aliases for TRT, Intent Patch, and Audit Bundle documents."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


Instrument = Literal["SCISSORS", "FORCEPS", "CLAMPS", "RETRACTOR"]
Goal = Literal["ROUTINE_CLASSIFICATION", "TRAUMA_SET_PRIORITY", "BACKLOG_CLEARING"]
AbnormalStrategy = Literal["STOP_LINE", "CONTINUE_FEASIBLE_TASKS", "ASK_OPERATOR"]
LineMode = Literal["IDLE", "RUNNING", "INTERVENTION", "PAUSED", "ERROR"]
PatchStatus = Literal["DRAFT", "REVIEWED", "VALIDATED", "RELEASED", "REJECTED"]
AuditStatus = Literal["ACCEPTED", "REJECTED"]


class KPI(TypedDict):
    deadline_minutes: int | None
    max_downtime_seconds: int
    min_throughput_per_hour: int


class LineState(TypedDict):
    mode: LineMode
    current_task: str | None
    wip_count: int
    last_exception: str | None


class TRTLine(TypedDict):
    goal: Goal
    allowed_instruments: list[Instrument]
    excluded_instruments: list[Instrument]
    priority: int
    kpi: KPI
    abnormal_strategy: AbnormalStrategy
    state: LineState


class TRT(TypedDict):
    trt_id: str
    version: str
    lines: dict[str, TRTLine]


class PatchOperation(TypedDict, total=False):
    op: str
    path: str
    value: Any
    from_: str


class IntentPatch(TypedDict):
    patch_id: str
    trt_id: str
    base_version: str
    operator_id: str
    intent_text: str
    reason: str
    operations: list[PatchOperation]
    status: PatchStatus


class ValidationResults(TypedDict):
    schema: bool
    path_whitelist: bool
    readonly: bool
    base_version: bool
    semantic: bool


class AuditBundle(TypedDict, total=False):
    audit_id: str
    patch_id: str
    trt_id: str
    operator_id: str
    timestamp_utc: str
    status: AuditStatus
    trt_before_version: str
    trt_after_version: str | None
    trt_before_hash: str
    trt_after_hash: str | None
    operations: list[PatchOperation]
    validation_results: ValidationResults
    rejection_reasons: list[str]
    intent_text: str
    reason: str
    scenario_spec_id: None
    run_artifact_id: None

