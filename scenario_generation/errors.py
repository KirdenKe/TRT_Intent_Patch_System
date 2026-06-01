"""Errors raised by ScenarioSpec generation."""

from __future__ import annotations


class ScenarioGenerationError(ValueError):
    """Base error for ScenarioSpec generation failures."""


class TemplateRegistryError(ScenarioGenerationError):
    """Raised when a scenario template registry is invalid."""


class ScenarioExportError(ScenarioGenerationError):
    """Raised when ScenarioSpec export fails."""


class OperatorResolutionRequiredError(ScenarioGenerationError):
    """Raised when a field must be resolved before simulation export."""

    def __init__(
        self,
        *,
        line_id: str,
        field: str,
        current_value: str,
        allowed_values: list[str],
    ) -> None:
        self.line_id = line_id
        self.field = field
        self.current_value = current_value
        self.allowed_values = allowed_values
        super().__init__(
            f"Line {line_id} abnormal_strategy is {current_value}. "
            "ScenarioSpec generation requires a concrete executable policy. "
            "Resolve this field to STOP_LINE or CONTINUE_FEASIBLE_TASKS before simulation."
        )
