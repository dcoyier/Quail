"""Build host search runtime (pool + providers) from slim config."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quail.config.models import EmbeddingProfile, ProvidersConfig, QuailConfig
from quail.providers import EmbeddingClient
from quail.search.db import SearchDb, open_search_db
from quail.search.lexical import LexicalService
from quail.search.pool import SearchDbPool, open_search_pool
from quail.search.similarity import SimilarityService


@dataclass(slots=True)
class SearchRuntime:
    """Search file path, providers, and a connection pool for concurrent exec."""

    path: Path
    providers: ProvidersConfig
    pool: SearchDbPool
    embedder_factory: Callable[[EmbeddingProfile], EmbeddingClient] | None = None

    def bind_services(self, search: SearchDb) -> tuple[SimilarityService, LexicalService]:
        """Build per-exec Lexical/Similarity services on one SearchDb."""

        return (
            SimilarityService(
                search=search,
                providers=self.providers,
                embedder_factory=self.embedder_factory,
            ),
            LexicalService(search=search),
        )

    def close(self) -> None:
        """Close idle pooled SearchDb connections."""

        self.pool.close()


@dataclass(slots=True)
class SearchServices:
    """One-shot search handle for warm helpers and single-threaded tests."""

    search: SearchDb
    similarity: SimilarityService
    lexical: LexicalService


def search_runtime_from_config(config: QuailConfig) -> SearchRuntime | None:
    """Build a pooled search runtime when search_database is configured."""

    if config.search_database is None:
        return None
    path = Path(config.search_database).expanduser().resolve()
    return SearchRuntime(
        path=path,
        providers=config.providers,
        pool=open_search_pool(path, max_size=config.max_concurrent_executions),
    )


def search_services_from_config(config: QuailConfig) -> SearchServices | None:
    """Open one SearchDb and wrap both services (single-threaded helpers)."""

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
