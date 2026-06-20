"""Errors raised by ScenarioSpec generation."""

from __future__ import annotations


class ScenarioGenerationError(ValueError):
    """Base error for ScenarioSpec generation failures."""


class TemplateRegistryError(ScenarioGenerationError):
    """Raised when a scenario template registry is invalid."""


class ScenarioExportError(ScenarioGenerationError):
    """Raised when ScenarioSpec export fails."""


class ScenarioTemplateLineBindingError(ScenarioGenerationError):
    """Raised when a scenario template cannot bind every required TRT line."""

    def __init__(
        self,
        *,
        template_id: str | None,
        required_trt_lines: list[str],
        template_bound_lines: list[str],
        missing_line_bindings: list[str],
    ) -> None:
        self.template_id = template_id
        self.required_trt_lines = required_trt_lines
        self.template_bound_lines = template_bound_lines
        self.missing_line_bindings = missing_line_bindings
        super().__init__(
            f"Scenario template is missing line_bindings for TRT lines: {missing_line_bindings}"
        )


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
