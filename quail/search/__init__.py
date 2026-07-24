"""Public exports for ``quail.search``."""

from quail.search.db import SearchDb, open_search_db
from quail.search.errors import SearchError
from quail.search.lexical import LexicalService
from quail.search.pin import get_embedding_pin, pin_embedding_profile
from quail.search.runtime import (
    SearchServices,
    lexical_from_config,
    search_services_from_config,
    similarity_from_config,
)
from quail.search.similarity import SimilarityService

__all__ = [
    "LexicalService",
    "SearchDb",
    "SearchError",
    "SearchServices",
    "SimilarityService",
    "get_embedding_pin",
    "lexical_from_config",
    "open_search_db",
    "pin_embedding_profile",
    "search_services_from_config",
    "similarity_from_config",
]
