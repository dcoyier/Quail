"""Opaque hybrid search surface (Lexical + Semantic + host RRF)."""

from .constants import SEARCH_FIELD
from .result_handles import ResultHandleRecord, ResultHandleRegistry
from .rrf import candidate_n, rrf_fuse
from .service import get_entry_payload, run_search
from .template import OUTPUT_MARKER, build_search_script, lexical_query_text
from .validate import normalize_query, normalize_top_k

__all__ = [
    "OUTPUT_MARKER",
    "SEARCH_FIELD",
    "ResultHandleRecord",
    "ResultHandleRegistry",
    "build_search_script",
    "candidate_n",
    "get_entry_payload",
    "lexical_query_text",
    "normalize_query",
    "normalize_top_k",
    "rrf_fuse",
    "run_search",
]
