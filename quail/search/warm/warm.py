"""Search warm: Lexical FTS + unbounded corpus embeddings + receipt."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from quail.analysis.errors import QuailRuntimeError
from quail.config.models import EmbeddingProfile, SearchWarmConfig
from quail.datasets.catalog import source_entries, source_fields, source_values
from quail.datasets.db import CoreDb
from quail.providers import EmbeddingClient, ProviderError
from quail.search.cache import get_cached_vector_blob, put_cached_vector
from quail.search.db import SearchDb
from quail.search.lexical.corpus import (
    ensure_entry_segments,
    resolve_corpus,
    validate_table_ident,
)
from quail.search.pin import get_pinned_profile_hash
from quail.search.vectors import text_hash, unit_vector


@dataclass(frozen=True, slots=True)
class WarmReceipt:
    """Recorded search warm state for one dataset version."""

    workspace_id: str
    dataset_id: str
    version_id: str
    profile_hash: str
    lexical_ready: bool
    embedding_ready: bool
    text_count: int
    warmed_at: str


@dataclass(frozen=True, slots=True)
class WarmDatasetResult:
    """Summary of one dataset warm pass."""

    workspace_id: str
    dataset_id: str
    version_id: str
    text_count: int
    unique_text_count: int
    embedded_batches: int
    lexical_ready: bool
    embedding_ready: bool
    profile_hash: str


def get_warm_receipt(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> WarmReceipt | None:
    row = search.connection.execute(
        """
        SELECT profile_hash, lexical_ready, embedding_ready, text_count, warmed_at
        FROM quail_search_warm
        WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchone()
    if row is None:
        return None
    return WarmReceipt(
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        version_id=version_id,
        profile_hash=str(row[0]),
        lexical_ready=bool(int(row[1])),
        embedding_ready=bool(int(row[2])),
        text_count=int(row[3]),
        warmed_at=str(row[4]),
    )


def put_warm_receipt(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    profile_hash: str,
    lexical_ready: bool,
    embedding_ready: bool,
    text_count: int,
) -> None:
    search.connection.execute(
        """
        INSERT INTO quail_search_warm(
          workspace_id, dataset_id, version_id, profile_hash,
          lexical_ready, embedding_ready, text_count, warmed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(workspace_id, dataset_id, version_id) DO UPDATE SET
          profile_hash = excluded.profile_hash,
          lexical_ready = excluded.lexical_ready,
          embedding_ready = excluded.embedding_ready,
          text_count = excluded.text_count,
          warmed_at = excluded.warmed_at
        """,
        (
            workspace_id,
            dataset_id,
            version_id,
            profile_hash,
            1 if lexical_ready else 0,
            1 if embedding_ready else 0,
            text_count,
        ),
    )
    search.connection.commit()


def clear_search_version(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> None:
    """Drop warm receipt, vectors, and Lexical corpus for one version."""

    connection = search.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT doc_table, terms_table
            FROM quail_lexical_corpus
            WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
            """,
            (workspace_id, dataset_id, version_id),
        ).fetchone()
        if row is not None:
            doc_table = validate_table_ident(str(row[0]))
            terms_table = validate_table_ident(str(row[1]))
            connection.execute(f"DROP TABLE IF EXISTS {doc_table}")
            connection.execute(f"DROP TABLE IF EXISTS {terms_table}")
            connection.execute(
                """
                DELETE FROM quail_lexical_corpus
                WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
                """,
                (workspace_id, dataset_id, version_id),
            )
        connection.execute(
            """
            DELETE FROM quail_embedding_vectors
            WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
            """,
            (workspace_id, dataset_id, version_id),
        )
        connection.execute(
            """
            DELETE FROM quail_search_warm
            WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
            """,
            (workspace_id, dataset_id, version_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def delete_vectors_for_profile(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    profile_hash: str,
) -> None:
    search.connection.execute(
        """
        DELETE FROM quail_embedding_vectors
        WHERE workspace_id = ?
          AND dataset_id = ?
          AND version_id = ?
          AND profile_hash = ?
        """,
        (workspace_id, dataset_id, version_id, profile_hash),
    )
    search.connection.commit()


def collect_corpus_texts(
    db: CoreDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    field_names: Sequence[str] | None = None,
) -> tuple[list[str], dict[str, list[str]]]:
    """Return (non-empty texts, entry_id -> segments).

    When ``field_names`` is set, only those source fields are included.
    """

    fields = source_fields(db, workspace_id, dataset_id, version_id)
    if field_names is not None:
        available = {field.name for field in fields}
        missing = [name for name in field_names if name not in available]
        if missing:
            missing_list = ", ".join(repr(name) for name in missing)
            raise QuailRuntimeError(
                f"Source fields not present on dataset {dataset_id!r}: {missing_list}",
                repair_hint=(
                    "Fix datasets.lexical.fields or datasets.embedding.fields to match "
                    "CSV column names, then re-run quail process."
                ),
            )
        wanted = set(field_names)
        fields = [field for field in fields if field.name in wanted]
    entries = source_entries(db, workspace_id, dataset_id, version_id)
    entry_ids = [entry.id for entry in entries]
    all_texts: list[str] = []
    entry_segments: dict[str, list[str]] = {entry_id: [] for entry_id in entry_ids}
    for field in fields:
        values = source_values(
            db,
            workspace_id,
            dataset_id,
            version_id,
            field.name,
            entry_ids=entry_ids,
        )
        for entry_id, value in zip(entry_ids, values, strict=True):
            if not isinstance(value, str) or not value:
                continue
            all_texts.append(value)
            entry_segments[entry_id].append(value)
    return all_texts, entry_segments


def warm_dataset(
    db: CoreDb,
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    profile: EmbeddingProfile | None,
    warm: SearchWarmConfig,
    embedder_factory: Callable[[EmbeddingProfile], EmbeddingClient],
    clear: bool = False,
    lexical_fields: Sequence[str] | None = None,
) -> WarmDatasetResult:
    """Warm Lexical (+ embeddings when profile set) and write the receipt."""

    if clear:
        clear_search_version(
            search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
        )

    desired_hash = profile.profile_hash() if profile is not None else ""
    existing = get_warm_receipt(
        search,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        version_id=version_id,
    )
    if (
        not clear
        and existing is not None
        and existing.profile_hash
        and existing.profile_hash != desired_hash
    ):
        delete_vectors_for_profile(
            search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            profile_hash=existing.profile_hash,
        )

    texts, entry_segments = collect_corpus_texts(
        db,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        version_id=version_id,
        field_names=lexical_fields,
    )
    corpus = resolve_corpus(
        search,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        version_id=version_id,
    )
    # Batched Lexical commits can leave a partial index; clear readiness first so
    # serve cannot treat a failed re-warm as authoritative.
    put_warm_receipt(
        search,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        version_id=version_id,
        profile_hash=desired_hash,
        lexical_ready=False,
        embedding_ready=False,
        text_count=0,
    )
    ensure_entry_segments(search, corpus, entry_segments=entry_segments)

    embedded_batches = 0
    embedding_ready = False
    if profile is not None:
        # Always collect for the embedding field set (None = all source fields).
        # Do not reuse Lexical `texts` — lexical_fields may be a narrower subset.
        embed_texts, _ = collect_corpus_texts(
            db,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            field_names=profile.fields,
        )
        unique_texts = _unique_texts(embed_texts)
        embedded_batches = _warm_embeddings(
            search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            profile=profile,
            texts=unique_texts,
            warm=warm,
            client=embedder_factory(profile),
        )
        embedding_ready = True
    else:
        unique_texts = _unique_texts(texts)

    put_warm_receipt(
        search,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        version_id=version_id,
        profile_hash=desired_hash,
        lexical_ready=True,
        embedding_ready=embedding_ready,
        text_count=len(texts),
    )
    return WarmDatasetResult(
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        version_id=version_id,
        text_count=len(texts),
        unique_text_count=len(unique_texts),
        embedded_batches=embedded_batches,
        lexical_ready=True,
        embedding_ready=embedding_ready,
        profile_hash=desired_hash,
    )


def require_warm_ready(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    profile: EmbeddingProfile | None,
) -> None:
    """Raise QuailRuntimeError when warm receipt does not match TOML expectations."""

    receipt = get_warm_receipt(
        search,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        version_id=version_id,
    )
    desired_hash = profile.profile_hash() if profile is not None else ""
    hint = "Run `quail process --config <path>` then retry quail run."
    if receipt is None:
        raise QuailRuntimeError(
            f"Dataset {dataset_id!r} version {version_id!r} has not been processed for search",
            repair_hint=hint,
        )
    if not receipt.lexical_ready:
        raise QuailRuntimeError(
            f"Dataset {dataset_id!r} Lexical warm is incomplete",
            repair_hint=hint,
        )
    if receipt.profile_hash != desired_hash:
        raise QuailRuntimeError(
            f"Dataset {dataset_id!r} warm profile does not match quail.toml embedding",
            repair_hint=hint,
        )
    if profile is not None:
        if not receipt.embedding_ready:
            raise QuailRuntimeError(
                f"Dataset {dataset_id!r} embedding warm is incomplete",
                repair_hint=hint,
            )
        pin_hash = get_pinned_profile_hash(
            search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
        )
        if pin_hash is None or pin_hash != desired_hash:
            raise QuailRuntimeError(
                f"Dataset {dataset_id!r} embedding pin does not match quail.toml",
                repair_hint=hint,
            )
    elif receipt.embedding_ready:
        raise QuailRuntimeError(
            f"Dataset {dataset_id!r} was warmed with embeddings but quail.toml has none",
            repair_hint=hint,
        )


def _unique_texts(texts: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for text in texts:
        digest = text_hash(text)
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(text)
    return unique


def _warm_embeddings(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    profile: EmbeddingProfile,
    texts: Sequence[str],
    warm: SearchWarmConfig,
    client: EmbeddingClient,
) -> int:
    profile_key = profile.profile_hash()
    missing: list[tuple[str, str]] = []
    for text in texts:
        digest = text_hash(text)
        cached = get_cached_vector_blob(
            search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            profile_hash=profile_key,
            text_hash=digest,
            dimensions=profile.dimensions,
        )
        if cached is None:
            missing.append((text, digest))
    if not missing:
        return 0

    batches = [
        missing[offset : offset + warm.embed_batch_size]
        for offset in range(0, len(missing), warm.embed_batch_size)
    ]

    def _embed_batch(
        batch: list[tuple[str, str]],
    ) -> list[tuple[str, list[float]]]:
        try:
            raw_vectors = client.embed_texts([text for text, _digest in batch])
        except ProviderError as error:
            raise QuailRuntimeError(
                str(error),
                repair_hint=error.repair_hint
                or "Fix the embedding provider configuration and re-run quail process.",
            ) from error
        if len(raw_vectors) != len(batch):
            raise QuailRuntimeError(
                "Embedding provider returned the wrong number of vectors",
                repair_hint="Re-run quail process after confirming the embedding provider.",
            )
        paired: list[tuple[str, list[float]]] = []
        for (text, digest), raw in zip(batch, raw_vectors, strict=True):
            del text
            if len(raw) != profile.dimensions:
                raise QuailRuntimeError(
                    f"Embedding provider returned {len(raw)} dimensions; "
                    f"expected {profile.dimensions}",
                    repair_hint=(
                        "Align datasets.embedding.dimensions with the provider, "
                        "then re-run quail process."
                    ),
                )
            paired.append((digest, unit_vector(raw)))
        return paired

    workers = max(1, min(warm.max_concurrent_embed_requests, len(batches)))
    embedded: list[tuple[str, list[float]]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_embed_batch, batch) for batch in batches]
        for future in as_completed(futures):
            embedded.extend(future.result())
    for digest, vector in embedded:
        put_cached_vector(
            search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            profile_hash=profile_key,
            text_hash=digest,
            dimensions=profile.dimensions,
            vector=vector,
        )
    search.connection.commit()
    return len(batches)
