"""Host overlay reads and revision-checked commit."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from quail.datasets.catalog import source_entries, source_fields
from quail.datasets.db import CoreDb
from quail.datasets.db.db import _require_scope_id
from quail.datasets.hashing import (
    canonical_json,
    decode_json,
    value_hash_from_canonical_json,
)
from quail.session.errors import SessionConflictError, SessionSyntaxError
from quail.session.models import (
    AnalysisField,
    CatalogField,
    FieldCreate,
    Scope,
    ValueDelete,
    ValueWrite,
)
from quail.session.sessions import require_active_session

Mutation = FieldCreate | ValueWrite | ValueDelete


def resolve_scope(
    db: CoreDb,
    session_id: str,
    dataset_id: str,
    *,
    version_id: str | None = None,
) -> Scope:
    """Pin an active session to a ready dataset version."""

    session = require_active_session(db, session_id)
    dataset_id = _require_scope_id(dataset_id, label="Dataset id")
    if version_id is None:
        row = db.connection.execute(
            """
            SELECT active_version_id
            FROM quail_datasets
            WHERE workspace_id = ? AND id = ?
            """,
            (session.workspace_id, dataset_id),
        ).fetchone()
        if row is None:
            raise SessionSyntaxError("Dataset does not exist in this workspace")
        if row[0] is None:
            raise SessionSyntaxError("Dataset has no active version")
        version_id = str(row[0])
    else:
        version_id = _require_scope_id(version_id, label="Dataset version id")

    status_row = db.connection.execute(
        """
        SELECT status
        FROM quail_dataset_versions
        WHERE workspace_id = ? AND dataset_id = ? AND id = ?
        """,
        (session.workspace_id, dataset_id, version_id),
    ).fetchone()
    if status_row is None:
        raise SessionSyntaxError("Dataset version does not exist")
    if str(status_row[0]) != "ready":
        raise SessionSyntaxError("Dataset version is not ready")

    return Scope(
        session_id=session.id,
        workspace_id=session.workspace_id,
        dataset_id=dataset_id,
        dataset_version_id=version_id,
    )


def ensure_scope(db: CoreDb, scope: Scope) -> None:
    """Insert the analysis scope row if missing."""

    _validate_scope(scope)
    require_active_session(db, scope.session_id)
    db.connection.execute(
        """
        INSERT INTO quail_analysis_scopes(
          session_id, workspace_id, dataset_id, dataset_version_id,
          field_registry_revision, value_revision, created_at, updated_at
        ) VALUES (
          ?, ?, ?, ?, 0, 0,
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
        ON CONFLICT(session_id, workspace_id, dataset_id, dataset_version_id)
        DO NOTHING
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
        ),
    )
    db.connection.commit()


def analysis_fields(db: CoreDb, scope: Scope) -> list[AnalysisField]:
    _validate_scope(scope)
    rows = db.connection.execute(
        """
        SELECT name, position, value_revision
        FROM quail_analysis_fields
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ?
        ORDER BY position, name
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
        ),
    ).fetchall()
    return [
        AnalysisField(name=str(row[0]), position=int(row[1]), value_revision=int(row[2]))
        for row in rows
    ]


def analysis_values(
    db: CoreDb,
    scope: Scope,
    field: str | AnalysisField,
    entry_ids: list[str] | None = None,
) -> list[Any | None]:
    _validate_scope(scope)
    field_name = field.name if isinstance(field, AnalysisField) else field
    if not isinstance(field_name, str) or not field_name.strip():
        raise SessionSyntaxError("Field name must be a non-empty string")
    field_name = field_name.strip()

    exists = db.connection.execute(
        """
        SELECT 1 FROM quail_analysis_fields
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ? AND name = ?
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
            field_name,
        ),
    ).fetchone()
    if exists is None:
        raise SessionSyntaxError(f"Unknown analysis field: {field_name}")

    if entry_ids is None:
        ordered_ids = [
            entry.id
            for entry in source_entries(
                db,
                scope.workspace_id,
                scope.dataset_id,
                scope.dataset_version_id,
            )
        ]
    else:
        ordered_ids = []
        for entry_id in entry_ids:
            if not isinstance(entry_id, str) or not entry_id:
                raise SessionSyntaxError("entry_ids must be non-empty strings")
            ordered_ids.append(entry_id)

    if not ordered_ids:
        return []

    placeholders = ",".join("?" for _ in ordered_ids)
    rows = db.connection.execute(
        f"""
        SELECT entry_id, value_json
        FROM quail_analysis_values
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ? AND field_name = ?
          AND entry_id IN ({placeholders})
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
            field_name,
            *ordered_ids,
        ),
    ).fetchall()
    by_entry = {str(row[0]): decode_json(str(row[1])) for row in rows}
    return [by_entry.get(entry_id) for entry_id in ordered_ids]


def catalog_fields(db: CoreDb, scope: Scope) -> list[CatalogField]:
    """Source fields then analysis fields for one scope."""

    _validate_scope(scope)
    result: list[CatalogField] = []
    for source_field in source_fields(
        db,
        scope.workspace_id,
        scope.dataset_id,
        scope.dataset_version_id,
    ):
        result.append(
            CatalogField(
                name=source_field.name,
                kind="source",
                position=source_field.position,
            )
        )
    source_count = len(result)
    for analysis_field in analysis_fields(db, scope):
        result.append(
            CatalogField(
                name=analysis_field.name,
                kind="analysis",
                position=source_count + analysis_field.position,
            )
        )
    return result


def commit_overlay(
    db: CoreDb,
    scope: Scope,
    *,
    expected_revision: int,
    mutations: Sequence[Mutation] = (),
    bindings: Mapping[str, Any] | None = None,
    binding_deletes: Sequence[str] | None = None,
) -> int:
    """Persist staged overlay mutations if session revision matches."""

    _validate_scope(scope)
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise SessionSyntaxError("expected_revision must be an int")
    if expected_revision < 0:
        raise SessionSyntaxError("expected_revision cannot be negative")

    connection = db.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        session = require_active_session(db, scope.session_id)
        if session.workspace_id != scope.workspace_id:
            raise SessionSyntaxError("Scope workspace does not match session")
        if session.state_revision != expected_revision:
            raise SessionConflictError("Session state_revision does not match expected_revision")

        _ensure_scope_in_transaction(connection, scope)

        source_names = {
            field.name
            for field in source_fields(
                db,
                scope.workspace_id,
                scope.dataset_id,
                scope.dataset_version_id,
            )
        }
        changed = False
        for mutation in mutations:
            if isinstance(mutation, FieldCreate):
                changed = _apply_field_create(connection, scope, mutation, source_names) or changed
            elif isinstance(mutation, ValueWrite):
                changed = _apply_value_write(connection, scope, mutation) or changed
            elif isinstance(mutation, ValueDelete):
                changed = _apply_value_delete(connection, scope, mutation) or changed
            else:
                raise SessionSyntaxError("Unsupported overlay mutation")

        if bindings:
            for name, value in bindings.items():
                _upsert_binding(connection, scope.session_id, name, value)
                changed = True
        if binding_deletes:
            for name in binding_deletes:
                if _delete_binding(connection, scope.session_id, name):
                    changed = True

        if changed:
            connection.execute(
                """
                UPDATE quail_sessions
                SET state_revision = state_revision + 1,
                    last_used_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (scope.session_id,),
            )
        else:
            connection.execute(
                """
                UPDATE quail_sessions
                SET last_used_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (scope.session_id,),
            )

        row = connection.execute(
            "SELECT state_revision FROM quail_sessions WHERE id = ?",
            (scope.session_id,),
        ).fetchone()
        assert row is not None
        new_revision = int(row[0])
        connection.commit()
        return new_revision
    except Exception:
        connection.rollback()
        raise


def _validate_scope(scope: Scope) -> None:
    if not isinstance(scope, Scope):
        raise SessionSyntaxError("scope must be a Scope")


def _ensure_scope_in_transaction(connection: Any, scope: Scope) -> None:
    connection.execute(
        """
        INSERT INTO quail_analysis_scopes(
          session_id, workspace_id, dataset_id, dataset_version_id,
          field_registry_revision, value_revision, created_at, updated_at
        ) VALUES (
          ?, ?, ?, ?, 0, 0,
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
        ON CONFLICT(session_id, workspace_id, dataset_id, dataset_version_id)
        DO NOTHING
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
        ),
    )


def _apply_field_create(
    connection: Any,
    scope: Scope,
    mutation: FieldCreate,
    source_names: set[str],
) -> bool:
    name = mutation.name.strip() if isinstance(mutation.name, str) else ""
    if not name:
        raise SessionSyntaxError("Analysis field name must be a non-empty string")
    if name in source_names:
        raise SessionConflictError(f"Analysis field name collides with source field: {name}")
    existing = connection.execute(
        """
        SELECT 1 FROM quail_analysis_fields
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ? AND name = ?
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
            name,
        ),
    ).fetchone()
    if existing is not None:
        return False

    position_row = connection.execute(
        """
        SELECT COALESCE(MAX(position) + 1, 0)
        FROM quail_analysis_fields
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ?
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
        ),
    ).fetchone()
    position = int(position_row[0])
    connection.execute(
        """
        INSERT INTO quail_analysis_fields(
          session_id, workspace_id, dataset_id, dataset_version_id,
          name, position, value_revision, created_at, updated_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, 0,
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
            name,
            position,
        ),
    )
    connection.execute(
        """
        UPDATE quail_analysis_scopes
        SET field_registry_revision = field_registry_revision + 1,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ?
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
        ),
    )
    return True


def _apply_value_write(connection: Any, scope: Scope, mutation: ValueWrite) -> bool:
    field = _require_analysis_field_name(mutation.field)
    entry_id = _require_entry_id(mutation.entry_id)
    if mutation.value is None:
        raise SessionSyntaxError("Tagged values cannot be None")
    _require_analysis_field_exists(connection, scope, field)
    _require_entry_exists(connection, scope, entry_id)
    encoded = canonical_json(mutation.value)
    digest = value_hash_from_canonical_json(encoded)
    connection.execute(
        """
        INSERT INTO quail_analysis_values(
          session_id, workspace_id, dataset_id, dataset_version_id,
          entry_id, field_name, value_json, value_hash, updated_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?,
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
        ON CONFLICT(session_id, workspace_id, dataset_id, dataset_version_id,
                    entry_id, field_name)
        DO UPDATE SET
          value_json = excluded.value_json,
          value_hash = excluded.value_hash,
          updated_at = excluded.updated_at
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
            entry_id,
            field,
            encoded,
            digest,
        ),
    )
    _bump_value_revisions(connection, scope, field)
    return True


def _apply_value_delete(connection: Any, scope: Scope, mutation: ValueDelete) -> bool:
    field = _require_analysis_field_name(mutation.field)
    entry_id = _require_entry_id(mutation.entry_id)
    _require_analysis_field_exists(connection, scope, field)
    if mutation.value is None:
        cursor = connection.execute(
            """
            DELETE FROM quail_analysis_values
            WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
              AND dataset_version_id = ? AND entry_id = ? AND field_name = ?
            """,
            (
                scope.session_id,
                scope.workspace_id,
                scope.dataset_id,
                scope.dataset_version_id,
                entry_id,
                field,
            ),
        )
    else:
        encoded = canonical_json(mutation.value)
        cursor = connection.execute(
            """
            DELETE FROM quail_analysis_values
            WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
              AND dataset_version_id = ? AND entry_id = ? AND field_name = ?
              AND value_json = ?
            """,
            (
                scope.session_id,
                scope.workspace_id,
                scope.dataset_id,
                scope.dataset_version_id,
                entry_id,
                field,
                encoded,
            ),
        )
    deleted = int(getattr(cursor, "rowcount", 0) or 0)
    if deleted > 0:
        _bump_value_revisions(connection, scope, field)
        return True
    return False


def _bump_value_revisions(connection: Any, scope: Scope, field: str) -> None:
    connection.execute(
        """
        UPDATE quail_analysis_fields
        SET value_revision = value_revision + 1,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ? AND name = ?
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
            field,
        ),
    )
    connection.execute(
        """
        UPDATE quail_analysis_scopes
        SET value_revision = value_revision + 1,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ?
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
        ),
    )


def _upsert_binding(connection: Any, session_id: str, name: str, value: Any) -> None:
    if not isinstance(name, str) or not name.strip():
        raise SessionSyntaxError("Binding name must be a non-empty string")
    if value is None:
        raise SessionSyntaxError("Binding values cannot be None")
    encoded = canonical_json(value)
    connection.execute(
        """
        INSERT INTO quail_session_bindings(session_id, name, value_json, updated_at)
        VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(session_id, name) DO UPDATE SET
          value_json = excluded.value_json,
          updated_at = excluded.updated_at
        """,
        (session_id, name.strip(), encoded),
    )


def _delete_binding(connection: Any, session_id: str, name: str) -> bool:
    if not isinstance(name, str) or not name.strip():
        raise SessionSyntaxError("Binding name must be a non-empty string")
    cursor = connection.execute(
        """
        DELETE FROM quail_session_bindings
        WHERE session_id = ? AND name = ?
        """,
        (session_id, name.strip()),
    )
    return int(getattr(cursor, "rowcount", 0) or 0) > 0


def _require_analysis_field_name(field: object) -> str:
    if not isinstance(field, str) or not field.strip():
        raise SessionSyntaxError("Analysis field name must be a non-empty string")
    return field.strip()


def _require_entry_id(entry_id: object) -> str:
    if not isinstance(entry_id, str) or not entry_id:
        raise SessionSyntaxError("entry_id must be a non-empty string")
    return entry_id


def _require_analysis_field_exists(connection: Any, scope: Scope, field: str) -> None:
    row = connection.execute(
        """
        SELECT 1 FROM quail_analysis_fields
        WHERE session_id = ? AND workspace_id = ? AND dataset_id = ?
          AND dataset_version_id = ? AND name = ?
        """,
        (
            scope.session_id,
            scope.workspace_id,
            scope.dataset_id,
            scope.dataset_version_id,
            field,
        ),
    ).fetchone()
    if row is None:
        raise SessionSyntaxError(f"Unknown analysis field: {field}")


def _require_entry_exists(connection: Any, scope: Scope, entry_id: str) -> None:
    row = connection.execute(
        """
        SELECT 1 FROM quail_entries
        WHERE workspace_id = ? AND dataset_id = ? AND dataset_version_id = ?
          AND id = ?
        """,
        (scope.workspace_id, scope.dataset_id, scope.dataset_version_id, entry_id),
    ).fetchone()
    if row is None:
        raise SessionSyntaxError(f"Unknown entry id: {entry_id}")
