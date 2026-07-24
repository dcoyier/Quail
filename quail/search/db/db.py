"""Embedded Turso search database open and migrate."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import turso

from quail.search.errors import SearchError


_MIGRATION_LEDGER_SQL = """
CREATE TABLE quail_schema_migrations(
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
)
"""


@dataclass(slots=True)
class SearchDb:
    """Process-local handle to one rebuildable search database file."""

    path: Path
    connection: Any
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self.connection.close()
        self._closed = True

    def __enter__(self) -> SearchDb:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_search_db(path: str | Path) -> SearchDb:
    """Create or open an embedded Turso search file and apply migrations."""

    db_path = Path(path).expanduser().resolve()
    if db_path.suffix == "":
        raise SearchError("Search database path must include a file name")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = turso.connect(str(db_path), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        _apply_migrations(connection)
    except Exception:
        connection.close()
        raise
    return SearchDb(path=db_path, connection=connection)


def _apply_migrations(connection: Any) -> None:
    schema_objects = connection.execute(
        """
        SELECT type, name FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    migration_object = next(
        (row for row in schema_objects if row[1] == "quail_schema_migrations"),
        None,
    )
    if migration_object is None:
        if schema_objects:
            raise SearchError(
                "Existing search database has schema objects but no Quail migration history"
            )
        connection.execute(_MIGRATION_LEDGER_SQL)
        connection.commit()
    elif migration_object[0] != "table":
        raise SearchError("Quail search migration history exists but is not a table")

    migrations = _load_migrations()
    applied = [
        (int(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """
            SELECT version, name, checksum FROM quail_schema_migrations
            ORDER BY version
            """
        ).fetchall()
    ]
    if len(applied) > len(migrations):
        raise SearchError("Search database has more applied migrations than this package provides")
    for index, (version, name, checksum) in enumerate(applied):
        expected = migrations[index]
        if (version, name, checksum) != (
            expected["version"],
            expected["name"],
            expected["checksum"],
        ):
            raise SearchError(
                f"Applied search migration {version} checksum or name does not match package"
            )

    for migration in migrations[len(applied) :]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_sql(migration["sql"]):
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO quail_schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (migration["version"], migration["name"], migration["checksum"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _load_migrations() -> list[dict[str, Any]]:
    package = resources.files("quail.search.migrations")
    migrations: list[dict[str, Any]] = []
    for entry in sorted(package.iterdir(), key=lambda item: item.name):
        name = entry.name
        if not name.endswith(".sql"):
            continue
        stem = name[: -len(".sql")]
        version_text, _, label = stem.partition("_")
        if not version_text.isdigit() or not label:
            raise SearchError(f"Invalid search migration file name: {name}")
        sql = entry.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        migrations.append(
            {
                "version": int(version_text),
                "name": stem,
                "checksum": checksum,
                "sql": sql,
            }
        )
    if not migrations:
        raise SearchError("No search migrations found")
    versions = [item["version"] for item in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise SearchError("Search migrations must be contiguous starting at 1")
    return migrations


def _split_sql(sql: str) -> list[str]:
    statements: list[str] = []
    for chunk in sql.split(";"):
        statement = chunk.strip()
        if statement:
            statements.append(statement)
    return statements
