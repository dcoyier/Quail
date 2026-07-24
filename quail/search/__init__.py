"""Public exports for ``quail.search``."""

from quail.search.db import SearchDb, open_search_db
from quail.search.errors import SearchError
from quail.search.pin import get_embedding_pin, pin_embedding_profile
from quail.search.runtime import similarity_from_config
from quail.search.similarity import SimilarityService

__all__ = [
    "SearchDb",
    "SearchError",
    "SimilarityService",
    "get_embedding_pin",
    "open_search_db",
    "pin_embedding_profile",
    "similarity_from_config",
]
