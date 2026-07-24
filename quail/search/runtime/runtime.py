"""Build host SimilarityService from slim config."""

from __future__ import annotations

from quail.config.models import QuailConfig
from quail.search.db import open_search_db
from quail.search.similarity import SimilarityService


def similarity_from_config(config: QuailConfig) -> SimilarityService | None:
    """Open search DB + providers when search_database is configured."""

    if config.search_database is None:
        return None
    search = open_search_db(config.search_database)
    return SimilarityService(search=search, providers=config.providers)
