"""Rebuildable vector cache keyed by text hash + profile."""

from __future__ import annotations

from collections.abc import Sequence

from quail.search.db import SearchDb
from quail.search.vectors import pack_unit_vector

_COPY_FORWARD_HASH_BATCH = 500


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


def copy_forward_cached_vectors(
    db: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    profile_hash: str,
    dimensions: int,
    text_hashes: Sequence[str],
) -> None:
    """Copy matching vectors from other versions of this dataset into version_id.

    Same workspace, dataset id, profile_hash, and dimensions. Any prior version
    can donate; the first inserted row for each text_hash wins. Does not embed.
    """

    needed = list(dict.fromkeys(text_hashes))
    if not needed:
        return
    connection = db.connection
    for start in range(0, len(needed), _COPY_FORWARD_HASH_BATCH):
        chunk = needed[start : start + _COPY_FORWARD_HASH_BATCH]
        placeholders = ",".join("?" for _ in chunk)
        connection.execute(
            f"""
            INSERT OR IGNORE INTO quail_embedding_vectors(
              workspace_id, dataset_id, version_id, profile_hash, text_hash,
              dimensions, vector, created_at
            )
            SELECT
              workspace_id,
              dataset_id,
              ?,
              profile_hash,
              text_hash,
              dimensions,
              vector,
              strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            FROM quail_embedding_vectors
            WHERE workspace_id = ?
              AND dataset_id = ?
              AND profile_hash = ?
              AND dimensions = ?
              AND version_id != ?
              AND text_hash IN ({placeholders})
            """,
            (
                version_id,
                workspace_id,
                dataset_id,
                profile_hash,
                dimensions,
                version_id,
                *chunk,
            ),
        )
    connection.commit()
