"""Immutable dataset catalog: CSV import and source reads."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from quail.datasets.csv_import import CsvDatasetScan, iter_csv_entries, scan_csv_dataset
from quail.datasets.db import CoreDb, ensure_workspace
from quail.datasets.db.db import _require_scope_id
from quail.datasets.errors import DatasetConflictError, DatasetSyntaxError
from quail.datasets.hashing import (
    canonical_json,
    decode_json,
    value_hash_from_canonical_json,
)
from quail.datasets.models import (
    ActiveVersion,
    DatasetRef,
    SourceEntry,
    SourceField,
)

_IMPORT_BATCH_SIZE = 10_000


def import_csv_dataset(
    db: CoreDb,
    workspace_id: str,
    dataset_id: str,
    csv_path: str | Path,
    *,
    name: str | None = None,
    activate: bool = True,
    progress: Callable[[str], None] | None = None,
) -> DatasetRef:
    """Import a CSV in bounded batches; optionally activate the ready version."""

    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    _emit_progress(progress, f"{dataset_id}: scanning CSV")
    csv_data = scan_csv_dataset(csv_path)
    version_id = csv_data.version_id
    content_hash = csv_data.content_hash
    field_names = list(csv_data.field_names)
    normalized_name = _normalize_dataset_name(name)
    _emit_progress(
        progress,
        f"{dataset_id}: scan complete rows={csv_data.row_count} values={csv_data.value_count}",
    )

    ensure_workspace(db, workspace_id)
    connection = db.connection
    existing = connection.execute(
        """
        SELECT content_hash, row_count, field_count, status
        FROM quail_dataset_versions
        WHERE workspace_id = ? AND dataset_id = ? AND id = ?
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchone()
    if existing is not None:
        expected_identity = (content_hash, csv_data.row_count, len(field_names))
        actual_identity = (str(existing[0]), int(existing[1]), int(existing[2]))
        status = str(existing[3])
        if actual_identity == expected_identity and status == "ready":
            _emit_progress(progress, f"{dataset_id}: verifying existing version")
            if not _stored_version_matches(
                connection, workspace_id, dataset_id, version_id, csv_data
            ):
                raise DatasetConflictError("Immutable dataset version storage is inconsistent")
            connection.execute("BEGIN IMMEDIATE")
            try:
                if normalized_name is not None:
                    _rename_dataset(connection, workspace_id, dataset_id, normalized_name)
                if activate:
                    _activate_version(connection, workspace_id, dataset_id, version_id)
                connection.commit()
                _checkpoint(connection)
            except Exception:
                connection.rollback()
                raise
            _emit_progress(progress, f"{dataset_id}: existing version verified")
            return _dataset_ref(connection, workspace_id, dataset_id, version_id, content_hash)
        if actual_identity != expected_identity or status not in {"importing", "failed"}:
            raise DatasetConflictError(
                "Dataset version identity conflicts with existing immutable data"
            )
        _emit_progress(progress, f"{dataset_id}: removing incomplete prior import")
        connection.execute("BEGIN IMMEDIATE")
        try:
            _remove_incomplete_version(connection, workspace_id, dataset_id, version_id)
            connection.commit()
            _checkpoint(connection)
        except Exception:
            connection.rollback()
            raise

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            INSERT INTO quail_datasets(
              workspace_id, id, name, active_version_id, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                      strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ON CONFLICT(workspace_id, id) DO UPDATE SET
              name = COALESCE(excluded.name, quail_datasets.name),
              updated_at = excluded.updated_at
            """,
            (workspace_id, dataset_id, normalized_name),
        )
        connection.execute(
            """
            INSERT INTO quail_dataset_versions(
              workspace_id, dataset_id, id, content_hash, row_count,
              field_count, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'importing',
                      strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (
                workspace_id,
                dataset_id,
                version_id,
                content_hash,
                csv_data.row_count,
                len(field_names),
            ),
        )
        connection.executemany(
            """
            INSERT INTO quail_source_fields(
              workspace_id, dataset_id, dataset_version_id, name, position
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (workspace_id, dataset_id, version_id, field, position)
                for position, field in enumerate(field_names)
            ],
        )
        connection.commit()
        _checkpoint(connection)
    except Exception:
        connection.rollback()
        raise

    entry_rows: list[tuple[Any, ...]] = []
    value_rows: list[tuple[Any, ...]] = []
    imported_rows = 0
    imported_values = 0
    ready_committed = False
    try:
        for position, entry in enumerate(iter_csv_entries(csv_data)):
            entry_id = entry["id"]
            entry_rows.append((workspace_id, dataset_id, version_id, entry_id, position))
            for field in field_names:
                if field not in entry:
                    continue
                encoded = canonical_json(entry[field])
                value_rows.append(
                    (
                        workspace_id,
                        dataset_id,
                        version_id,
                        entry_id,
                        field,
                        encoded,
                        value_hash_from_canonical_json(encoded),
                    )
                )
            if len(entry_rows) >= _IMPORT_BATCH_SIZE:
                _write_import_batch(connection, entry_rows, value_rows)
                imported_rows += len(entry_rows)
                imported_values += len(value_rows)
                entry_rows.clear()
                value_rows.clear()
                _emit_progress(
                    progress,
                    f"{dataset_id}: imported {imported_rows}/{csv_data.row_count} rows",
                )
        if entry_rows:
            _write_import_batch(connection, entry_rows, value_rows)
            imported_rows += len(entry_rows)
            imported_values += len(value_rows)
        if (
            imported_rows != csv_data.row_count
            or imported_values != csv_data.value_count
        ):
            raise DatasetSyntaxError("CSV row or value count changed during import")

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE quail_dataset_versions SET status = 'ready'
            WHERE workspace_id = ? AND dataset_id = ? AND id = ?
            """,
            (workspace_id, dataset_id, version_id),
        )
        if activate:
            _activate_version(connection, workspace_id, dataset_id, version_id)
        connection.commit()
        ready_committed = True
        _checkpoint(connection)
    except Exception:
        connection.rollback()
        if not ready_committed:
            _mark_import_failed(connection, workspace_id, dataset_id, version_id)
        raise

    _emit_progress(progress, f"{dataset_id}: import ready rows={imported_rows}")
    return _dataset_ref(connection, workspace_id, dataset_id, version_id, content_hash)


def _write_import_batch(
    connection: Any,
    entry_rows: list[tuple[Any, ...]],
    value_rows: list[tuple[Any, ...]],
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            """
            INSERT INTO quail_entries(
              workspace_id, dataset_id, dataset_version_id, id, position
            ) VALUES (?, ?, ?, ?, ?)
            """,
            entry_rows,
        )
        if value_rows:
            connection.executemany(
                """
                INSERT INTO quail_source_values(
                  workspace_id, dataset_id, dataset_version_id,
                  entry_id, field_name, value_json, value_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                value_rows,
            )
        connection.commit()
        _checkpoint(connection)
    except Exception:
        connection.rollback()
        raise


def _mark_import_failed(
    connection: Any,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            UPDATE quail_dataset_versions SET status = 'failed'
            WHERE workspace_id = ? AND dataset_id = ? AND id = ?
            """,
            (workspace_id, dataset_id, version_id),
        )
        connection.commit()
        _checkpoint(connection)
    except Exception:
        connection.rollback()


def _checkpoint(connection: Any) -> None:
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _emit_progress(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def active_version(db: CoreDb, workspace_id: str, dataset_id: str) -> ActiveVersion | None:
    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    row = db.connection.execute(
        """
        SELECT d.active_version_id, v.content_hash
        FROM quail_datasets AS d
        LEFT JOIN quail_dataset_versions AS v
          ON v.workspace_id = d.workspace_id
         AND v.dataset_id = d.id
         AND v.id = d.active_version_id
        WHERE d.workspace_id = ? AND d.id = ?
        """,
        (workspace_id, dataset_id),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return ActiveVersion(version_id=str(row[0]), content_hash=str(row[1]))


def list_datasets(db: CoreDb, workspace_id: str) -> list[DatasetRef]:
    """List datasets in a workspace (id order)."""

    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    rows = db.connection.execute(
        """
        SELECT id
        FROM quail_datasets
        WHERE workspace_id = ?
        ORDER BY id ASC
        """,
        (workspace_id,),
    ).fetchall()
    datasets: list[DatasetRef] = []
    for row in rows:
        ref = get_dataset(db, workspace_id, str(row[0]))
        if ref is not None:
            datasets.append(ref)
    return datasets


def get_dataset(db: CoreDb, workspace_id: str, dataset_id: str) -> DatasetRef | None:
    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    row = db.connection.execute(
        """
        SELECT name, active_version_id
        FROM quail_datasets
        WHERE workspace_id = ? AND id = ?
        """,
        (workspace_id, dataset_id),
    ).fetchone()
    if row is None:
        return None
    active_version_id = None if row[1] is None else str(row[1])
    content_hash = ""
    version_id = active_version_id or ""
    if active_version_id is not None:
        version_row = db.connection.execute(
            """
            SELECT content_hash
            FROM quail_dataset_versions
            WHERE workspace_id = ? AND dataset_id = ? AND id = ?
            """,
            (workspace_id, dataset_id, active_version_id),
        ).fetchone()
        if version_row is not None:
            content_hash = str(version_row[0])
    return DatasetRef(
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        name=None if row[0] is None else str(row[0]),
        active_version_id=active_version_id,
        version_id=version_id,
        content_hash=content_hash,
    )


def source_fields(
    db: CoreDb,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> list[SourceField]:
    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    version_id = _require_scope_id(version_id, label="Dataset version id")
    _require_ready_version(db.connection, workspace_id, dataset_id, version_id)
    rows = db.connection.execute(
        """
        SELECT name, position
        FROM quail_source_fields
        WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
        ORDER BY position, name
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchall()
    return [SourceField(name=str(row[0]), position=int(row[1])) for row in rows]


def source_entries(
    db: CoreDb,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> list[SourceEntry]:
    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    version_id = _require_scope_id(version_id, label="Dataset version id")
    _require_ready_version(db.connection, workspace_id, dataset_id, version_id)
    rows = db.connection.execute(
        """
        SELECT id, position
        FROM quail_entries
        WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
        ORDER BY position, id
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchall()
    return [SourceEntry(id=str(row[0]), position=int(row[1])) for row in rows]


def source_values(
    db: CoreDb,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    field: str | SourceField,
    entry_ids: list[str] | None = None,
) -> list[Any | None]:
    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    version_id = _require_scope_id(version_id, label="Dataset version id")
    field_name = field.name if isinstance(field, SourceField) else field
    if not isinstance(field_name, str) or not field_name.strip():
        raise DatasetSyntaxError("Field name must be a non-empty string")
    field_name = field_name.strip()
    _require_ready_version(db.connection, workspace_id, dataset_id, version_id)
    field_row = db.connection.execute(
        """
        SELECT 1 FROM quail_source_fields
        WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
          AND name = ?
        """,
        (workspace_id, dataset_id, version_id, field_name),
    ).fetchone()
    if field_row is None:
        raise DatasetSyntaxError(f"Unknown source field: {field_name}")

    if entry_ids is None:
        ordered_ids = [
            entry.id for entry in source_entries(db, workspace_id, dataset_id, version_id)
        ]
    else:
        ordered_ids = []
        for entry_id in entry_ids:
            if not isinstance(entry_id, str) or not entry_id:
                raise DatasetSyntaxError("entry_ids must be non-empty strings")
            ordered_ids.append(entry_id)

    if not ordered_ids:
        return []

    by_entry: dict[str, Any] = {}
    if entry_ids is None:
        rows = db.connection.execute(
            """
            SELECT entry_id, value_json
            FROM quail_source_values
            WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
              AND field_name = ?
            """,
            (workspace_id, dataset_id, version_id, field_name),
        ).fetchall()
        by_entry = {str(row[0]): decode_json(str(row[1])) for row in rows}
    else:
        chunk_size = 500
        for start in range(0, len(ordered_ids), chunk_size):
            chunk = ordered_ids[start : start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = db.connection.execute(
                f"""
                SELECT entry_id, value_json
                FROM quail_source_values
                WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
                  AND field_name = ?
                  AND entry_id IN ({placeholders})
                """,
                (workspace_id, dataset_id, version_id, field_name, *chunk),
            ).fetchall()
            for row in rows:
                by_entry[str(row[0])] = decode_json(str(row[1]))
    return [by_entry.get(entry_id) for entry_id in ordered_ids]


def iter_source_field_batches(
    db: CoreDb,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    field: str | SourceField,
    *,
    batch_size: int = 10_000,
) -> Iterator[list[tuple[str, Any | None]]]:
    """Stream one source field in entry order as bounded batches."""

    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    version_id = _require_scope_id(version_id, label="Dataset version id")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    field_name = field.name if isinstance(field, SourceField) else field
    if not isinstance(field_name, str) or not field_name.strip():
        raise DatasetSyntaxError("Field name must be a non-empty string")
    field_name = field_name.strip()
    available = {
        item.name for item in source_fields(db, workspace_id, dataset_id, version_id)
    }
    if field_name not in available:
        raise DatasetSyntaxError(f"Unknown source field: {field_name}")

    cursor = db.connection.execute(
        """
        SELECT e.id, v.value_json
        FROM quail_entries AS e
        LEFT JOIN quail_source_values AS v
          ON v.workspace_id = e.workspace_id
         AND v.dataset_id = e.dataset_id
         AND v.dataset_version_id = e.dataset_version_id
         AND v.entry_id = e.id
         AND v.field_name = ?
        WHERE e.workspace_id = ? AND e.dataset_id = ? AND e.dataset_version_id = ?
        ORDER BY e.position, e.id
        """,
        (field_name, workspace_id, dataset_id, version_id),
    )
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        yield [
            (
                str(row[0]),
                None if row[1] is None else decode_json(str(row[1])),
            )
            for row in rows
        ]


def iter_unique_source_texts(
    db: CoreDb,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    field_names: list[str] | tuple[str, ...] | None = None,
    *,
    batch_size: int = 10_000,
) -> Iterator[str]:
    """Stream distinct non-empty string values for selected source fields."""

    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    version_id = _require_scope_id(version_id, label="Dataset version id")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    fields = source_fields(db, workspace_id, dataset_id, version_id)
    available = {field.name for field in fields}
    if field_names is None:
        selected = [field.name for field in fields]
    else:
        selected = []
        for field_name in field_names:
            if field_name not in available:
                raise DatasetSyntaxError(f"Unknown source field: {field_name}")
            selected.append(field_name)
    if not selected:
        return

    placeholders = ",".join("?" for _ in selected)
    cursor = db.connection.execute(
        f"""
        SELECT DISTINCT value_json
        FROM quail_source_values
        WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
          AND field_name IN ({placeholders})
        ORDER BY value_json
        """,
        (workspace_id, dataset_id, version_id, *selected),
    )
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        for row in rows:
            value = decode_json(str(row[0]))
            if isinstance(value, str) and value:
                yield value


def _dataset_ref(
    connection: Any,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    content_hash: str,
) -> DatasetRef:
    row = connection.execute(
        """
        SELECT name, active_version_id
        FROM quail_datasets
        WHERE workspace_id = ? AND id = ?
        """,
        (workspace_id, dataset_id),
    ).fetchone()
    assert row is not None
    return DatasetRef(
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        name=None if row[0] is None else str(row[0]),
        active_version_id=None if row[1] is None else str(row[1]),
        version_id=version_id,
        content_hash=content_hash,
    )


def _normalize_dataset_name(name: str | None) -> str | None:
    if name is None:
        return None
    normalized = name.strip()
    if not normalized:
        raise DatasetSyntaxError("Dataset name cannot be empty")
    if len(normalized.encode("utf-8")) > 512:
        raise DatasetSyntaxError("Dataset name exceeds its byte limit")
    return normalized


def _rename_dataset(
    connection: Any,
    workspace_id: str,
    dataset_id: str,
    name: str,
) -> None:
    connection.execute(
        """
        UPDATE quail_datasets
        SET name = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE workspace_id = ? AND id = ?
        """,
        (name, workspace_id, dataset_id),
    )


def _activate_version(
    connection: Any,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> None:
    _require_ready_version(connection, workspace_id, dataset_id, version_id)
    connection.execute(
        """
        UPDATE quail_datasets
        SET active_version_id = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE workspace_id = ? AND id = ?
        """,
        (version_id, workspace_id, dataset_id),
    )


def activate_dataset_version(
    db: CoreDb,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> None:
    """Mark a ready dataset version as active in its own transaction."""

    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    version_id = _require_scope_id(version_id, label="Dataset version id")
    connection = db.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        _activate_version(connection, workspace_id, dataset_id, version_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _require_ready_version(
    connection: Any,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT status FROM quail_dataset_versions
        WHERE workspace_id = ? AND dataset_id = ? AND id = ?
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchone()
    if row is None:
        raise DatasetSyntaxError("Dataset version does not exist")
    if str(row[0]) != "ready":
        raise DatasetSyntaxError("Dataset version is not ready")


def _remove_incomplete_version(
    connection: Any,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> None:
    active = connection.execute(
        """
        SELECT 1 FROM quail_datasets
        WHERE workspace_id = ? AND id = ? AND active_version_id = ?
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchone()
    if active is not None:
        raise DatasetConflictError(
            "Incomplete dataset version is already referenced and cannot recover"
        )
    scope = (workspace_id, dataset_id, version_id)
    connection.execute(
        """
        DELETE FROM quail_source_values
        WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
        """,
        scope,
    )
    connection.execute(
        """
        DELETE FROM quail_entries
        WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
        """,
        scope,
    )
    connection.execute(
        """
        DELETE FROM quail_source_fields
        WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
        """,
        scope,
    )
    connection.execute(
        """
        DELETE FROM quail_dataset_versions
        WHERE workspace_id = ? AND dataset_id = ? AND id = ?
        """,
        scope,
    )


def _stored_version_matches(
    connection: Any,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    expected: CsvDatasetScan,
) -> bool:
    # The ready version row is the durable import receipt: it is written only
    # after the unchanged second CSV pass commits every expected row and value.
    # Rehashing physical source values here would make every idempotent process
    # O(cells), which is prohibitive for multi-million-value datasets.
    field_names = list(expected.field_names)
    stored_fields = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM quail_source_fields
            WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
            ORDER BY position, name
            """,
            (workspace_id, dataset_id, version_id),
        ).fetchall()
    ]
    if stored_fields != field_names:
        return False
    return True
