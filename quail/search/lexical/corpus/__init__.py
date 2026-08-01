"""Public exports for ``quail.search.lexical.corpus``."""

from .corpus import (
    LexicalCorpus,
    drop_field_corpora_except,
    drop_version_corpora,
    ensure_entry_segments,
    expand_prefixes,
    load_entry_segment_counts,
    lookup_field_corpus,
    resolve_corpus,
    scratch_corpus,
    sweep_scratch_corpora,
    validate_table_ident,
    warm_entry_segments,
)

__all__ = [
    "LexicalCorpus",
    "drop_field_corpora_except",
    "drop_version_corpora",
    "ensure_entry_segments",
    "expand_prefixes",
    "load_entry_segment_counts",
    "lookup_field_corpus",
    "resolve_corpus",
    "scratch_corpus",
    "sweep_scratch_corpora",
    "validate_table_ident",
    "warm_entry_segments",
]
