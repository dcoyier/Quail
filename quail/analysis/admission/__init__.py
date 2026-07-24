"""Public exports for ``quail.analysis.admission``."""

from .admission import (
    acquire_execution_slot,
    configure_execution_slots,
    configured_execution_slots,
    reset_execution_slots_for_tests,
)

__all__ = [
    "acquire_execution_slot",
    "configure_execution_slots",
    "configured_execution_slots",
    "reset_execution_slots_for_tests",
]
