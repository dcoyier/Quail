"""Opaque hybrid search surface (Lexical + Semantic + host RRF)."""

from .constants import SEARCH_FIELD
from .rrf import candidate_n, rrf_fuse
from .service import get_entry_payload, run_search
from .template import OUTPUT_MARKER, build_search_script
from .validate import normalize_query, normalize_top_k

__all__ = [
    "OUTPUT_MARKER",
    "SEARCH_FIELD",
    "build_search_script",
    "candidate_n",
    "get_entry_payload",
    "normalize_query",
    "normalize_top_k",
    "rrf_fuse",
    "run_search",
]
