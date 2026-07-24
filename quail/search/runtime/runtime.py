"""Build host search services from slim config."""

from __future__ import annotations

from dataclasses import dataclass

from quail.config.models import QuailConfig
from quail.search.db import SearchDb, open_search_db
from quail.search.lexical import LexicalService
from quail.search.similarity import SimilarityService


@dataclass(slots=True)
class SearchServices:
    """Shared search DB plus Semantic and Lexical host services."""

    search: SearchDb
    similarity: SimilarityService
    lexical: LexicalService


def search_services_from_config(config: QuailConfig) -> SearchServices | None:
    """Open search DB once and build both SimilarityService and LexicalService."""

    if config.search_database is None:
        return None
    search = open_search_db(config.search_database)
    return SearchServices(
        search=search,
        similarity=SimilarityService(search=search, providers=config.providers),
        lexical=LexicalService(search=search),
    )


def similarity_from_config(config: QuailConfig) -> SimilarityService | None:
    """Open search DB + providers when search_database is configured."""

    services = search_services_from_config(config)
    return None if services is None else services.similarity


def lexical_from_config(config: QuailConfig) -> LexicalService | None:
    """Open search DB for Lexical when search_database is configured."""

    services = search_services_from_config(config)
    return None if services is None else services.lexical
