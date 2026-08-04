"""Host-side Semantic scoring with Turso exact cosine."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from quail.analysis.errors import QuailRuntimeError
from quail.config.models import EmbeddingProfile, ProvidersConfig
from quail.providers import EmbeddingClient, ProviderError, build_embedding_client
from quail.search.cache import get_cached_vector_blob, put_cached_vector
from quail.search.db import SearchDb
from quail.search.pin import get_embedding_pin, get_pinned_profile_hash
from quail.search.vectors import text_hash, unit_vector

_SCORE_TARGET_BATCH = 32
_STAGE_TABLE = "quail_semantic_score_stage"


@dataclass(slots=True)
class SimilarityService:
    """Embed + cache + Turso exact cosine for Semantic()."""

    search: SearchDb
    providers: ProvidersConfig
    embedder_factory: Callable[[EmbeddingProfile], EmbeddingClient] | None = None
    _clients: dict[str, EmbeddingClient] = field(default_factory=dict, init=False, repr=False)

    def semantic_score(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        corpus: Any,
        query_record: dict[str, Any],
        input_aggregation: str | None,
        target_aggregation: str | None,
    ) -> float | None:
        """Score one corpus field value against a Semantic query record."""

        scores = self.semantic_scores_for_entries(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            corpus_by_entry={"_": corpus},
            query_record=query_record,
            input_aggregation=input_aggregation,
            target_aggregation=target_aggregation,
        )
        return scores.get("_")

    def semantic_scores_for_entries(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        corpus_by_entry: Mapping[str, Any],
        query_record: dict[str, Any],
        input_aggregation: str | None,
        target_aggregation: str | None,
    ) -> dict[str, float | None]:
        """Score many entries with one embed pass and Turso batch cosine."""

        profile, profile_key = self._profile_for(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
        )

        target_texts = _target_texts(query_record)
        if not target_texts:
            raise QuailRuntimeError("Semantic query produced no text targets")

        segments: list[tuple[str, int, str]] = []
        empty_entries: set[str] = set()
        for entry_id, corpus in corpus_by_entry.items():
            corpus_texts = _corpus_texts(corpus)
            if corpus_texts is None:
                empty_entries.add(entry_id)
                continue
            for index, text in enumerate(corpus_texts):
                segments.append((entry_id, index, text))

        results: dict[str, float | None] = {entry_id: None for entry_id in empty_entries}
        if not segments:
            for entry_id in corpus_by_entry:
                results.setdefault(entry_id, None)
            return results

        unique_texts = list({text for *_rest, text in segments} | set(target_texts))
        self._ensure_texts(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            profile=profile,
            profile_hash=profile_key,
            texts=unique_texts,
        )

        target_hashes = [text_hash(text) for text in target_texts]
        target_blobs = [
            self._require_blob(
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                version_id=version_id,
                profile_hash=profile_key,
                digest=digest,
                dimensions=profile.dimensions,
            )
            for digest in target_hashes
        ]

        input_mode = input_aggregation or "total"
        target_mode = target_aggregation or "total"
        segment_scores = self._score_segments_turso(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            profile_hash=profile_key,
            segments=[(entry_id, index, text_hash(text)) for entry_id, index, text in segments],
            target_blobs=target_blobs,
            target_mode=target_mode,
        )

        by_entry: dict[str, list[float]] = {}
        for (entry_id, _index), score in segment_scores.items():
            by_entry.setdefault(entry_id, []).append(score)
        for entry_id, scores in by_entry.items():
            results[entry_id] = _aggregate(scores, input_mode)
        for entry_id in corpus_by_entry:
            results.setdefault(entry_id, None)
        return results

    def semantic_scores_for_source_entries(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        entry_ids: Sequence[str],
        source_field: str,
        all_entries: bool,
        query_record: dict[str, Any],
        input_aggregation: str | None,
        target_aggregation: str | None,
    ) -> dict[str, float | None] | None:
        """Score a warmed source field without loading source text into the exec."""

        profile, profile_key = self._profile_for(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
        )
        registration = self.search.connection.execute(
            """
            SELECT field_id
            FROM quail_embedding_fields
            WHERE workspace_id = ?
              AND dataset_id = ?
              AND version_id = ?
              AND profile_hash = ?
              AND field_name = ?
            LIMIT 1
            """,
            (workspace_id, dataset_id, version_id, profile_key, source_field),
        ).fetchone()
        if registration is None:
            return None
        field_id = int(registration[0])

        target_texts = _target_texts(query_record)
        if not target_texts:
            raise QuailRuntimeError("Semantic query produced no text targets")
        self._ensure_texts(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            profile=profile,
            profile_hash=profile_key,
            texts=target_texts,
        )
        target_blobs = [
            self._require_blob(
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                version_id=version_id,
                profile_hash=profile_key,
                digest=text_hash(text),
                dimensions=profile.dimensions,
            )
            for text in target_texts
        ]
        source_scores = self._score_source_entries_turso(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            profile_hash=profile_key,
            field_id=field_id,
            entry_ids=entry_ids,
            all_entries=all_entries,
            target_blobs=target_blobs,
            target_mode=target_aggregation or "total",
            input_mode=input_aggregation or "total",
        )
        results: dict[str, float | None] = {entry_id: None for entry_id in entry_ids}
        results.update(source_scores)
        return results

    def _profile_for(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
    ) -> tuple[EmbeddingProfile, str]:
        profile = get_embedding_pin(
            self.search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
        )
        if profile is None:
            raise QuailRuntimeError(
                "Semantic requires a pinned dataset embedding profile for this version",
                repair_hint=(
                    "Add [datasets.embedding] for this dataset, re-run quail apply/run, "
                    "then retry the whole exec."
                ),
            )
        profile_key = get_pinned_profile_hash(
            self.search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
        )
        if profile_key is None:
            raise QuailRuntimeError(
                "Semantic requires a stored pin profile_hash for this version",
                repair_hint=(
                    "Re-run quail apply/run to refresh the embedding pin, then retry."
                ),
            )
        return profile, profile_key

    def _ensure_texts(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        profile: EmbeddingProfile,
        profile_hash: str,
        texts: Sequence[str],
    ) -> None:
        missing: list[tuple[str, str]] = []
        seen: set[str] = set()
        for text in texts:
            digest = text_hash(text)
            if digest in seen:
                continue
            seen.add(digest)
            cached = get_cached_vector_blob(
                self.search,
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                version_id=version_id,
                profile_hash=profile_hash,
                text_hash=digest,
                dimensions=profile.dimensions,
            )
            if cached is None:
                missing.append((text, digest))

        if not missing:
            return

        client = self._client_for(profile)
        try:
            raw_vectors = client.embed_texts([text for text, _digest in missing])
        except ProviderError as error:
            hint = error.repair_hint or (
                "Fix the embedding provider configuration and retry the whole exec."
            )
            raise QuailRuntimeError(str(error), repair_hint=hint) from error
        if len(raw_vectors) != len(missing):
            raise QuailRuntimeError(
                "Embedding provider returned the wrong number of vectors",
                repair_hint="Retry the whole exec after confirming the embedding provider.",
            )
        for (text, digest), raw in zip(missing, raw_vectors, strict=True):
            del text
            if len(raw) != profile.dimensions:
                raise QuailRuntimeError(
                    f"Embedding provider returned {len(raw)} dimensions; "
                    f"expected {profile.dimensions}",
                    repair_hint=(
                        "Align datasets.embedding.dimensions with the provider, "
                        "re-apply, and retry the whole exec."
                    ),
                )
            put_cached_vector(
                self.search,
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                version_id=version_id,
                profile_hash=profile_hash,
                text_hash=digest,
                dimensions=profile.dimensions,
                vector=unit_vector(raw),
            )

    def _require_blob(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        profile_hash: str,
        digest: str,
        dimensions: int,
    ) -> bytes:
        blob = get_cached_vector_blob(
            self.search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            profile_hash=profile_hash,
            text_hash=digest,
            dimensions=dimensions,
        )
        if blob is None:
            raise QuailRuntimeError(
                "Semantic vector cache miss after embed",
                repair_hint="Retry the whole exec after confirming the embedding provider.",
            )
        return blob

    def _score_segments_turso(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        profile_hash: str,
        segments: Sequence[tuple[str, int, str]],
        target_blobs: Sequence[bytes],
        target_mode: str,
    ) -> dict[tuple[str, int], float]:
        if not segments or not target_blobs:
            return {}

        connection = self.search.connection
        connection.execute(
            f"""
            CREATE TEMP TABLE IF NOT EXISTS {_STAGE_TABLE}(
              entry_id TEXT NOT NULL,
              segment_index INTEGER NOT NULL,
              text_hash TEXT NOT NULL
            )
            """
        )
        connection.execute(f"DELETE FROM {_STAGE_TABLE}")
        connection.executemany(
            f"INSERT INTO {_STAGE_TABLE}(entry_id, segment_index, text_hash) VALUES (?, ?, ?)",
            list(segments),
        )
        connection.commit()

        pairwise: dict[tuple[str, int], list[float]] = {}
        try:
            for start in range(0, len(target_blobs), _SCORE_TARGET_BATCH):
                batch = target_blobs[start : start + _SCORE_TARGET_BATCH]
                score_columns = ",\n                       ".join(
                    "-vector_distance_dot(v.vector, ?)" for _ in batch
                )
                rows = connection.execute(
                    f"""
                    SELECT s.entry_id, s.segment_index,
                           {score_columns}
                    FROM {_STAGE_TABLE} AS s
                    JOIN quail_embedding_vectors AS v
                      ON v.workspace_id = ?
                     AND v.dataset_id = ?
                     AND v.version_id = ?
                     AND v.profile_hash = ?
                     AND v.text_hash = s.text_hash
                    """,
                    (*batch, workspace_id, dataset_id, version_id, profile_hash),
                ).fetchall()
                for row in rows:
                    key = (str(row[0]), int(row[1]))
                    scores = pairwise.setdefault(key, [])
                    for value in row[2:]:
                        scores.append(_finite_score(value))
        except QuailRuntimeError:
            raise
        except Exception as error:
            raise QuailRuntimeError(
                "Turso vector cosine scoring failed",
                repair_hint="Retry the whole exec; if it persists, rebuild the search database.",
            ) from error

        return {key: _aggregate(scores, target_mode) for key, scores in pairwise.items()}

    def _score_source_entries_turso(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        profile_hash: str,
        field_id: int,
        entry_ids: Sequence[str],
        all_entries: bool,
        target_blobs: Sequence[bytes],
        target_mode: str,
        input_mode: str,
    ) -> dict[str, float]:
        if not entry_ids or not target_blobs:
            return {}

        score_columns = ",\n                       ".join(
            "-vector_distance_dot(v.vector, ?)" for _ in target_blobs
        )
        candidate_clause = ""
        parameters: list[Any] = [
            *target_blobs,
            workspace_id,
            dataset_id,
            version_id,
            profile_hash,
            field_id,
        ]
        if not all_entries:
            candidate_clause = "AND s.entry_id IN (SELECT value FROM json_each(?))"
            parameters.append(
                json.dumps(list(entry_ids), separators=(",", ":"), allow_nan=False)
            )
        try:
            rows = self.search.connection.execute(
                f"""
                SELECT s.entry_id,
                       {score_columns}
                FROM quail_embedding_segments AS s
                JOIN quail_embedding_vectors AS v
                  ON v.workspace_id = ?
                 AND v.dataset_id = ?
                 AND v.version_id = ?
                 AND v.profile_hash = ?
                 AND v.text_hash = s.text_hash
                WHERE s.field_id = ?
                  {candidate_clause}
                """,
                tuple(parameters),
            )
            totals: dict[str, float] = {}
            counts: dict[str, int] = {}
            for row in rows:
                entry_id = str(row[0])
                segment_score = _aggregate(
                    tuple(_finite_score(value) for value in row[1:]), target_mode
                )
                totals[entry_id] = totals.get(entry_id, 0.0) + segment_score
                if input_mode == "avg":
                    counts[entry_id] = counts.get(entry_id, 0) + 1
        except Exception as error:
            raise QuailRuntimeError(
                "Turso vector cosine scoring failed",
                repair_hint="Retry the whole exec; if it persists, rebuild the search database.",
            ) from error

        if input_mode == "avg":
            return {
                entry_id: total / counts[entry_id]
                for entry_id, total in totals.items()
            }
        return totals

    def _client_for(self, profile: EmbeddingProfile) -> EmbeddingClient:
        key = profile.profile_hash()
        existing = self._clients.get(key)
        if existing is not None:
            return existing
        factory = self.embedder_factory or (
            lambda item: build_embedding_client(item, self.providers)
        )
        try:
            client = factory(profile)
        except ProviderError as error:
            hint = error.repair_hint or (
                "Fix the embedding provider configuration and retry the whole exec."
            )
            raise QuailRuntimeError(str(error), repair_hint=hint) from error
        self._clients[key] = client
        return client


def _corpus_texts(corpus: Any) -> list[str] | None:
    if corpus is None:
        return None
    if isinstance(corpus, str):
        return [corpus]
    if isinstance(corpus, list):
        texts: list[str] = []
        for item in corpus:
            if item is None:
                continue
            if not isinstance(item, str):
                raise QuailRuntimeError("Semantic corpus list values must be text")
            texts.append(item)
        if not texts:
            return None
        return texts
    return [str(corpus)]


def _target_texts(query_record: dict[str, Any]) -> list[str]:
    kind = query_record.get("kind")
    if kind == "LiteralText":
        text = query_record.get("text")
        if not isinstance(text, str) or not text:
            raise QuailRuntimeError("Semantic LiteralText query must be non-empty text")
        return [text]
    if kind == "LiteralTextList":
        texts = query_record.get("texts")
        if not isinstance(texts, list | tuple):
            raise QuailRuntimeError("Semantic LiteralTextList query must be a list of text")
        out = [item for item in texts if isinstance(item, str) and item]
        if not out:
            raise QuailRuntimeError("Semantic query must contain at least one non-empty text")
        return out
    raise QuailRuntimeError(
        "Semantic EntryGroup/EntryList queries must be resolved by QueryEngine; "
        "use text or list[str] query records here"
    )


def _aggregate(scores: Sequence[float], mode: str) -> float:
    if not scores:
        return 0.0
    total = float(sum(scores))
    if mode == "avg":
        return total / len(scores)
    return total


def _finite_score(value: object) -> float:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    number = float(value)
    if not math.isfinite(number):
        return 0.0
    return number
