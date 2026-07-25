"""Public exports for ``quail.search.lexical.corpus``."""

from .corpus import (
    LexicalCorpus,
    ensure_entry_segments,
    expand_prefixes,
    load_entry_segment_counts,
    resolve_corpus,
    validate_table_ident,
    warm_entry_segments,
)

__all__ = [
    "LexicalCorpus",
    "ensure_entry_segments",
    "expand_prefixes",
    "load_entry_segment_counts",
    "resolve_corpus",
    "validate_table_ident",
    "warm_entry_segments",
]
