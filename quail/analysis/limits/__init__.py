"""Public exports for ``quail.analysis.limits``."""

from .limits import (
    EXTENDED_LIMITS,
    MAX_MEMORY_BYTES,
    STANDARD_LIMITS,
    ExecLimits,
    cpu_timeout_error,
    limits_for_time_window,
    memory_limit_error,
    time_repair_hint,
    validate_time_window,
    wall_timeout_error,
)

__all__ = [
    "EXTENDED_LIMITS",
    "MAX_MEMORY_BYTES",
    "STANDARD_LIMITS",
    "ExecLimits",
    "cpu_timeout_error",
    "limits_for_time_window",
    "memory_limit_error",
    "time_repair_hint",
    "validate_time_window",
    "wall_timeout_error",
]
