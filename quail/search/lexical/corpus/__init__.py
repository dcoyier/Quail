"""Public exports for ``quail.search.lexical.corpus``."""

from .corpus import (
    LexicalCorpus,
    ensure_entry_segments,
    expand_prefixes,
    resolve_corpus,
    validate_table_ident,
)

__all__ = [
    "LexicalCorpus",
    "ensure_entry_segments",
    "expand_prefixes",
    "resolve_corpus",
    "validate_table_ident",
]
