"""Hardcoded quail_exec time windows and always-on resource ceiling."""

from __future__ import annotations

from dataclasses import dataclass

from quail.analysis.errors import QuailSyntaxError

# Always-on worker RSS ceiling (same for every time_window).
MAX_MEMORY_BYTES = 256 * 1024 * 1024

_TIME_WINDOWS = frozenset({"standard", "extended"})

_TIME_REPAIR_BODY = (
    "narrow the candidate group with "
    ".where before ranked or search-heavy retrieve/count (ranking scores the "
    "whole candidate set before limit), or split the work across successive "
    "successful quail_exec calls (session bindings persist). Failed exec does "
    "not commit tags or bindings."
)

_TIME_REPAIR_STANDARD = (
    'Potential routes for revision: Retry with time_window="extended", ' + _TIME_REPAIR_BODY
)

_TIME_REPAIR_EXTENDED = "Potential routes for revision: " + _TIME_REPAIR_BODY

_MEMORY_LIMIT_REPAIR = (
    "Reduce materialized results and large local values (use len or bytes), "
    "then retry. Failed exec does not commit tags or bindings."
)


@dataclass(frozen=True, slots=True)
class ExecLimits:
    """Per-exec wall, CPU, and memory ceilings."""

    wall_seconds: float
    cpu_seconds: int
    max_memory_bytes: int = MAX_MEMORY_BYTES

    @property
    def already_extended(self) -> bool:
        """True when these ceilings are at least the extended window."""

        return (
            self.wall_seconds >= EXTENDED_LIMITS.wall_seconds
            and self.cpu_seconds >= EXTENDED_LIMITS.cpu_seconds
        )


STANDARD_LIMITS = ExecLimits(wall_seconds=30.0, cpu_seconds=15)
EXTENDED_LIMITS = ExecLimits(wall_seconds=100.0, cpu_seconds=60)


def validate_time_window(time_window: str | None) -> str:
    """Normalize None to standard; require standard|extended."""

    if time_window is None:
        return "standard"
    if not isinstance(time_window, str):
        raise QuailSyntaxError("time_window must be a string or None")
    if time_window not in _TIME_WINDOWS:
        raise QuailSyntaxError('time_window must be "standard" or "extended"')
    return time_window


def limits_for_time_window(time_window: str | None) -> ExecLimits:
    """Resolve the hardcoded ceilings for a time_window."""

    window = validate_time_window(time_window)
    if window == "extended":
        return EXTENDED_LIMITS
    return STANDARD_LIMITS


def time_repair_hint(*, already_extended: bool) -> str:
    """Repair hint for wall/CPU timeouts; omit extended retry when already there."""

    if already_extended:
        return _TIME_REPAIR_EXTENDED
    return _TIME_REPAIR_STANDARD


def wall_timeout_error(
    wall_seconds: float,
    *,
    already_extended: bool = False,
) -> Exception:
    from quail.analysis.errors import QuailRuntimeError

    return QuailRuntimeError(
        f"quail_exec exceeded its {wall_seconds:g}s wall-clock deadline",
        repair_hint=time_repair_hint(already_extended=already_extended),
    )


def cpu_timeout_error(
    cpu_seconds: int,
    *,
    already_extended: bool = False,
) -> Exception:
    from quail.analysis.errors import QuailCpuTimeoutError

    return QuailCpuTimeoutError(
        f"quail_exec exceeded its {cpu_seconds:g}s CPU-time limit",
        repair_hint=time_repair_hint(already_extended=already_extended),
    )


def memory_limit_error(max_memory_bytes: int) -> Exception:
    from quail.analysis.errors import QuailRssLimitError

    mib = max_memory_bytes // (1024 * 1024)
    return QuailRssLimitError(
        f"quail_exec exceeded its {mib} MiB worker RSS limit",
        repair_hint=_MEMORY_LIMIT_REPAIR,
    )
