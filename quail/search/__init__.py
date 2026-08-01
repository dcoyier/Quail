"""Public exports for ``quail.search``."""

from quail.search.db import SearchDb, open_search_db
from quail.search.errors import SearchError
from quail.search.lexical import LexicalService
from quail.search.pin import (
    delete_embedding_pin,
    get_embedding_pin,
    pin_embedding_profile,
)
from quail.search.pool import SearchDbPool, open_search_pool
from quail.search.runtime import (
    SearchRuntime,
    SearchServices,
    lexical_from_config,
    search_runtime_from_config,
    search_services_from_config,
    similarity_from_config,
)
from quail.search.similarity import SimilarityService

__all__ = [
    "LexicalService",
    "SearchDb",
    "SearchDbPool",
    "SearchError",
    "SearchRuntime",
    "SearchServices",
    "SimilarityService",
    "delete_embedding_pin",
    "get_embedding_pin",
    "lexical_from_config",
    "open_search_db",
    "open_search_pool",
    "pin_embedding_profile",
    "search_runtime_from_config",
    "search_services_from_config",
    "similarity_from_config",
]
