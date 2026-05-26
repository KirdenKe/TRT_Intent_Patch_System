"""Domain errors for the TRT deterministic core."""


class TRTError(Exception):
    """Base class for deterministic TRT errors."""


class SchemaValidationError(TRTError):
    """Raised when a TRT, Intent Patch, or Audit Bundle fails schema checks."""


class PatchValidationError(TRTError):
    """Raised when an Intent Patch fails firewall checks."""


class SemanticValidationError(TRTError):
    """Raised when a patched TRT violates semantic rules."""


class RepositoryError(TRTError):
    """Raised when repository state cannot satisfy an operation."""

