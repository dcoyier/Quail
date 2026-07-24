"""Rebuildable vector cache keyed by text hash + profile."""

from __future__ import annotations

from quail.search.db import SearchDb
from quail.search.vectors import pack_unit_vector


def get_cached_vector_blob(
    db: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    profile_hash: str,
    text_hash: str,
    dimensions: int,
) -> bytes | None:
    """Return a cached unit-vector blob or None on miss."""

    row = db.connection.execute(
        """
        SELECT dimensions, vector
        FROM quail_embedding_vectors
        WHERE workspace_id = ?
          AND dataset_id = ?
          AND version_id = ?
          AND profile_hash = ?
          AND text_hash = ?
        """,
        (workspace_id, dataset_id, version_id, profile_hash, text_hash),
    ).fetchone()
    if row is None:
        return None
    stored_dimensions = int(row[0])
    if stored_dimensions != dimensions:
        return None
    blob = row[1]
    if not isinstance(blob, bytes | memoryview):
        return None
    return bytes(blob)


def put_cached_vector(
    db: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    profile_hash: str,
    text_hash: str,
    dimensions: int,
    vector: list[float],
) -> None:
    """Store a unit vector in the rebuildable cache."""

    blob = pack_unit_vector(vector)
    db.connection.execute(
        """
        INSERT INTO quail_embedding_vectors(
          workspace_id, dataset_id, version_id, profile_hash, text_hash,
          dimensions, vector, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(workspace_id, dataset_id, version_id, profile_hash, text_hash)
        DO UPDATE SET
          dimensions = excluded.dimensions,
          vector = excluded.vector,
          created_at = excluded.created_at
        """,
        (
            workspace_id,
            dataset_id,
            version_id,
            profile_hash,
            text_hash,
            dimensions,
            blob,
        ),
    )
    db.connection.commit()
