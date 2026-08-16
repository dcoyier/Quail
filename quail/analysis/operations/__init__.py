"""Public exports for ``quail.analysis.operations``."""

from .operations import (
    OP_SPECS,
    Operation,
    OpSpec,
    final_pipeline_kind,
    validate_operation_pipeline,
    Value,
    AsText,
    AsNumber,
    RegexSearch,
    RegexFindAll,
    RegexSub,
    Slice,
    Length,
    Lexical,
    Semantic,
)

__all__ = [
    "OP_SPECS",
    "Operation",
    "OpSpec",
    "final_pipeline_kind",
    "validate_operation_pipeline",
    "Value",
    "AsText",
    "AsNumber",
    "RegexSearch",
    "RegexFindAll",
    "RegexSub",
    "Slice",
    "Length",
    "Lexical",
    "Semantic",
]
