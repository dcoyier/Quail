"""Public exports for ``quail.analysis.operations``."""

from .operations import (
    Operation,
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
    "Operation",
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
