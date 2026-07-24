"""Pin dataset embedding profiles to immutable versions."""

from __future__ import annotations

from quail.config.models import EmbeddingProfile
from quail.search.db import SearchDb


def pin_embedding_profile(
    db: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    profile: EmbeddingProfile,
) -> None:
    """Upsert the embedding profile pin for one dataset version."""

    profile_hash = profile.profile_hash()
    db.connection.execute(
        """
        INSERT INTO quail_embedding_pins(
          workspace_id, dataset_id, version_id,
          provider, model, dimensions, revision, profile_hash, pinned_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(workspace_id, dataset_id, version_id) DO UPDATE SET
          provider = excluded.provider,
          model = excluded.model,
          dimensions = excluded.dimensions,
          revision = excluded.revision,
          profile_hash = excluded.profile_hash,
          pinned_at = excluded.pinned_at
        """,
        (
            workspace_id,
            dataset_id,
            version_id,
            profile.provider,
            profile.model,
            profile.dimensions,
            profile.revision,
            profile_hash,
        ),
    )
    db.connection.commit()


def get_embedding_pin(
    db: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> EmbeddingProfile | None:
    """Load the pinned embedding profile for a dataset version, if any."""

    row = db.connection.execute(
        """
        SELECT provider, model, dimensions, revision
        FROM quail_embedding_pins
        WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchone()
    if row is None:
        return None
    provider = str(row[0])
    if provider not in {"ollama", "openrouter"}:
        return None
    return EmbeddingProfile(
        provider=provider,  # type: ignore[arg-type]
        model=str(row[1]),
        dimensions=int(row[2]),
        revision=str(row[3]),
    )
