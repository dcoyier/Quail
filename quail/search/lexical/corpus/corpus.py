"""Per-field Turso document tables; FTS indexes built after plain inserts."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from quail.analysis.errors import QuailRuntimeError
from quail.search.db import SearchDb
from quail.search.lexical.query import prepare_prefix_text, prepare_text

_TABLE_NAME_RE = re.compile(
    r"^(?:quail_lex_[dt]_[0-9a-f]{32}|quail_lex_scratch_[dt]_[0-9a-f]{32})$"
)
_SCRATCH_TABLE_RE = re.compile(r"^quail_lex_scratch_[dt]_[0-9a-f]{32}$")
_MAX_PREFIX_TERMS = 4_096
# Bound WAL growth while writing plain segment rows during warm.
_ENTRY_COMMIT_BATCH_SIZE = 1_000


@dataclass(frozen=True, slots=True)
class LexicalCorpus:
    """Resolved document and terms table names for one field or scratch corpus."""

    doc_table: str
    terms_table: str


def resolve_corpus(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    field_name: str,
) -> LexicalCorpus:
    """Return existing or newly created per-field document tables (no FTS yet)."""

    if not field_name:
        raise QuailRuntimeError("Lexical corpus field_name must be non-empty")

    connection = search.connection
    row = connection.execute(
        """
        SELECT doc_table, terms_table
        FROM quail_lexical_corpus
        WHERE workspace_id = ? AND dataset_id = ? AND version_id = ? AND field_name = ?
        """,
        (workspace_id, dataset_id, version_id, field_name),
    ).fetchone()
    if row is not None:
        doc_table = str(row[0])
        terms_table = str(row[1])
        _require_safe_table(doc_table)
        _require_safe_table(terms_table)
        return LexicalCorpus(doc_table=doc_table, terms_table=terms_table)

    key = hashlib.sha256(
        f"{workspace_id}\0{dataset_id}\0{version_id}\0{field_name}".encode()
    ).hexdigest()[:32]
    doc_table = f"quail_lex_d_{key}"
    terms_table = f"quail_lex_t_{key}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"""
            CREATE TABLE {doc_table}(
              document_id INTEGER PRIMARY KEY,
              entry_id TEXT NOT NULL,
              segment_position INTEGER NOT NULL,
              text TEXT NOT NULL,
              prefix_text TEXT NOT NULL,
              UNIQUE(entry_id, segment_position)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE {terms_table}(
              term TEXT PRIMARY KEY,
              indexed_term TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE INDEX {doc_table}_entry
            ON {doc_table}(entry_id, segment_position)
            """
        )
        connection.execute(
            """
            INSERT INTO quail_lexical_corpus(
              workspace_id, dataset_id, version_id, field_name,
              doc_table, terms_table, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (workspace_id, dataset_id, version_id, field_name, doc_table, terms_table),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise QuailRuntimeError(
            "Failed to create lexical corpus tables",
            repair_hint="Retry the whole exec; if it persists, rebuild the search database.",
        ) from None
    return LexicalCorpus(doc_table=doc_table, terms_table=terms_table)


def lookup_field_corpus(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    field_name: str,
) -> LexicalCorpus | None:
    """Give back a registered per-field corpus, or None when missing."""

    row = search.connection.execute(
        """
        SELECT doc_table, terms_table
        FROM quail_lexical_corpus
        WHERE workspace_id = ? AND dataset_id = ? AND version_id = ? AND field_name = ?
        """,
        (workspace_id, dataset_id, version_id, field_name),
    ).fetchone()
    if row is None:
        return None
    doc_table = str(row[0])
    terms_table = str(row[1])
    _require_safe_table(doc_table)
    _require_safe_table(terms_table)
    return LexicalCorpus(doc_table=doc_table, terms_table=terms_table)


@contextmanager
def scratch_corpus(search: SearchDb) -> Iterator[LexicalCorpus]:
    """Create a request-local corpus; drop it when the block exits."""

    key = uuid.uuid4().hex
    doc_table = f"quail_lex_scratch_d_{key}"
    terms_table = f"quail_lex_scratch_t_{key}"
    connection = search.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"""
            CREATE TABLE {doc_table}(
              document_id INTEGER PRIMARY KEY,
              entry_id TEXT NOT NULL,
              segment_position INTEGER NOT NULL,
              text TEXT NOT NULL,
              prefix_text TEXT NOT NULL,
              UNIQUE(entry_id, segment_position)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE {terms_table}(
              term TEXT PRIMARY KEY,
              indexed_term TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE INDEX {doc_table}_entry
            ON {doc_table}(entry_id, segment_position)
            """
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise QuailRuntimeError(
            "Failed to create lexical scratch corpus",
            repair_hint="Retry the whole exec; if it persists, rebuild the search database.",
        ) from None
    corpus = LexicalCorpus(doc_table=doc_table, terms_table=terms_table)
    try:
        yield corpus
    finally:
        _drop_corpus_tables(search, corpus)


def sweep_scratch_corpora(search: SearchDb) -> int:
    """Drop orphaned scratch document/terms tables left by crashed requests."""

    connection = search.connection
    rows = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name LIKE 'quail_lex_scratch_%'
        """
    ).fetchall()
    dropped = 0
    for row in rows:
        name = str(row[0])
        if not _SCRATCH_TABLE_RE.fullmatch(name):
            continue
        connection.execute(f"DROP TABLE IF EXISTS {name}")
        dropped += 1
    if dropped:
        connection.commit()
    return dropped


def drop_version_corpora(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> None:
    """Drop every registered per-field corpus for one dataset version."""

    connection = search.connection
    rows = connection.execute(
        """
        SELECT doc_table, terms_table
        FROM quail_lexical_corpus
        WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchall()
    for row in rows:
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


def drop_field_corpora_except(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    keep_fields: Sequence[str],
) -> None:
    """Drop registered field corpora not listed in keep_fields."""

    keep = set(keep_fields)
    connection = search.connection
    rows = connection.execute(
        """
        SELECT field_name, doc_table, terms_table
        FROM quail_lexical_corpus
        WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchall()
    for row in rows:
        field_name = str(row[0])
        if field_name in keep:
            continue
        doc_table = validate_table_ident(str(row[1]))
        terms_table = validate_table_ident(str(row[2]))
        connection.execute(f"DROP TABLE IF EXISTS {doc_table}")
        connection.execute(f"DROP TABLE IF EXISTS {terms_table}")
        connection.execute(
            """
            DELETE FROM quail_lexical_corpus
            WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
              AND field_name = ?
            """,
            (workspace_id, dataset_id, version_id, field_name),
        )


def load_entry_segment_counts(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    entry_ids: Sequence[str] | None,
) -> dict[str, int]:
    """Return entry_id → segment count for entries that already have document rows."""

    if entry_ids is not None and not entry_ids:
        return {}
    doc_table = validate_table_ident(corpus.doc_table)
    if entry_ids is None:
        rows = search.connection.execute(
            f"""
            SELECT entry_id, COUNT(*)
            FROM {doc_table}
            GROUP BY entry_id
            """
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}
    candidates_json = json.dumps(list(entry_ids), separators=(",", ":"), allow_nan=False)
    rows = search.connection.execute(
        f"""
        SELECT entry_id, COUNT(*)
        FROM {doc_table}
        WHERE entry_id IN (SELECT value FROM json_each(?))
        GROUP BY entry_id
        """,
        (candidates_json,),
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def warm_entry_segments(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    entry_segments: dict[str, list[str]],
) -> dict[str, int]:
    """Full Lexical rebuild: plain segment writes, then one dual FTS index build.

    Used by ``quail process``. Drops any existing FTS indexes first so inserts
    stay cheap (v0.10 build order). Score paths should use scratch corpora
    when warm reuse is not available.
    """

    _drop_fts_indexes(search, corpus)
    counts = _write_entry_segments(
        search,
        corpus,
        entry_segments=entry_segments,
        replace_all=True,
    )
    _create_fts_indexes(search, corpus)
    return counts


def warm_entry_segment_batches(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    entry_segment_batches: Iterable[dict[str, list[str]]],
) -> int:
    """Full Lexical rebuild from bounded entry batches."""

    _drop_fts_indexes(search, corpus)
    _write_entry_segments(search, corpus, entry_segments={}, replace_all=True)
    text_count = 0
    for entry_segments in entry_segment_batches:
        text_count += sum(len(segments) for segments in entry_segments.values())
        _write_entry_segments(
            search,
            corpus,
            entry_segments=entry_segments,
            replace_all=False,
        )
    _create_fts_indexes(search, corpus)
    return text_count


def ensure_entry_segments(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    entry_segments: dict[str, list[str]],
) -> dict[str, int]:
    """Replace segments for the given entries; ensure FTS indexes exist afterward.

    Used for request-local scratch corpora (and tests). Prefer
    ``load_entry_segment_counts`` when a warmed field corpus is ready.
    """

    counts = _write_entry_segments(
        search,
        corpus,
        entry_segments=entry_segments,
        replace_all=False,
    )
    _create_fts_indexes(search, corpus)
    return counts


def expand_prefixes(
    search: SearchDb,
    corpus: LexicalCorpus,
    prefixes: tuple[str, ...],
    *,
    max_prefix_terms: int = _MAX_PREFIX_TERMS,
) -> dict[str, tuple[str, ...]]:
    """Expand prefix leaves against the corpus term dictionary."""

    connection = search.connection
    expanded_count = 0
    output: dict[str, tuple[str, ...]] = {}
    for prefix in prefixes:
        remaining = max_prefix_terms - expanded_count
        rows = connection.execute(
            f"""
            SELECT indexed_term FROM {corpus.terms_table}
            WHERE term >= ? AND term < ?
            ORDER BY term
            LIMIT ?
            """,
            (prefix, prefix + "\U0010ffff", remaining + 1),
        ).fetchall()
        if len(rows) > remaining:
            raise QuailRuntimeError(
                "Lexical prefixes expand beyond the request-wide "
                f"{max_prefix_terms}-term limit at {prefix!r}"
            )
        terms = tuple(str(row[0]) for row in rows)
        expanded_count += len(terms)
        output[prefix] = terms
    return output


def _drop_corpus_tables(search: SearchDb, corpus: LexicalCorpus) -> None:
    doc_table = validate_table_ident(corpus.doc_table)
    terms_table = validate_table_ident(corpus.terms_table)
    connection = search.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        fts_name, prefix_name = _fts_index_names(doc_table)
        connection.execute(f"DROP INDEX IF EXISTS {fts_name}")
        connection.execute(f"DROP INDEX IF EXISTS {prefix_name}")
        connection.execute(f"DROP TABLE IF EXISTS {doc_table}")
        connection.execute(f"DROP TABLE IF EXISTS {terms_table}")
        connection.commit()
    except Exception:
        connection.rollback()


def _write_entry_segments(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    entry_segments: dict[str, list[str]],
    replace_all: bool,
) -> dict[str, int]:
    if not entry_segments and not replace_all:
        return {}

    connection = search.connection
    doc_table = validate_table_ident(corpus.doc_table)
    terms_table = validate_table_ident(corpus.terms_table)
    counts: dict[str, int] = {}
    batch: list[tuple[str, list[str]]] = []

    def _commit_batch() -> None:
        if not batch:
            return
        try:
            connection.execute("BEGIN IMMEDIATE")
            for entry_id, segments in batch:
                connection.execute(
                    f"DELETE FROM {doc_table} WHERE entry_id = ?",
                    (entry_id,),
                )
                for position, segment in enumerate(segments):
                    prepared, analyzed = prepare_text(segment)
                    prefix_prepared, prefix_terms = prepare_prefix_text(analyzed)
                    connection.execute(
                        f"""
                        INSERT INTO {doc_table}(
                          entry_id, segment_position, text, prefix_text
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (entry_id, position, prepared, prefix_prepared),
                    )
                    if prefix_terms:
                        connection.executemany(
                            f"""
                            INSERT OR IGNORE INTO {terms_table}(term, indexed_term)
                            VALUES (?, ?)
                            """,
                            list(prefix_terms),
                        )
                counts[entry_id] = len(segments)
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except QuailRuntimeError:
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise QuailRuntimeError(
                "Failed to write lexical corpus segments",
                repair_hint="Retry the whole exec; if it persists, rebuild the search database.",
            ) from error
        batch.clear()

    if replace_all:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"DELETE FROM {doc_table}")
            connection.execute(f"DELETE FROM {terms_table}")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as error:
            connection.rollback()
            raise QuailRuntimeError(
                "Failed to clear lexical corpus segments",
                repair_hint="Retry quail process; if it persists, rebuild the search database.",
            ) from error

    for entry_id, segments in entry_segments.items():
        batch.append((entry_id, segments))
        if len(batch) >= _ENTRY_COMMIT_BATCH_SIZE:
            _commit_batch()
    _commit_batch()
    return counts


def _fts_index_names(doc_table: str) -> tuple[str, str]:
    return f"{doc_table}_fts", f"{doc_table}_prefix_fts"


def _fts_indexes_present(search: SearchDb, corpus: LexicalCorpus) -> bool:
    fts_name, _prefix = _fts_index_names(validate_table_ident(corpus.doc_table))
    row = search.connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'index' AND name = ?
        """,
        (fts_name,),
    ).fetchone()
    return row is not None


def _drop_fts_indexes(search: SearchDb, corpus: LexicalCorpus) -> None:
    doc_table = validate_table_ident(corpus.doc_table)
    fts_name, prefix_name = _fts_index_names(doc_table)
    connection = search.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"DROP INDEX IF EXISTS {fts_name}")
        connection.execute(f"DROP INDEX IF EXISTS {prefix_name}")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as error:
        connection.rollback()
        raise QuailRuntimeError(
            "Failed to drop lexical FTS indexes",
            repair_hint="Retry quail process; if it persists, rebuild the search database.",
        ) from error


def _create_fts_indexes(search: SearchDb, corpus: LexicalCorpus) -> None:
    if _fts_indexes_present(search, corpus):
        return
    doc_table = validate_table_ident(corpus.doc_table)
    fts_name, prefix_name = _fts_index_names(doc_table)
    connection = search.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"""
            CREATE INDEX {fts_name}
            ON {doc_table} USING fts(text)
            WITH (tokenizer = 'whitespace')
            """
        )
        connection.execute(
            f"""
            CREATE INDEX {prefix_name}
            ON {doc_table} USING fts(prefix_text)
            WITH (tokenizer = 'whitespace')
            """
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as error:
        connection.rollback()
        raise QuailRuntimeError(
            "Failed to create lexical FTS indexes",
            repair_hint="Retry quail process; if it persists, rebuild the search database.",
        ) from error


def _require_safe_table(name: str) -> None:
    if not _TABLE_NAME_RE.fullmatch(name):
        raise QuailRuntimeError(
            "Lexical corpus registry contains an unsafe table name",
            repair_hint="Rebuild the search database and retry the whole exec.",
        )


def validate_table_ident(name: str) -> str:
    """Public helper for score SQL that must interpolate table names."""

    _require_safe_table(name)
    return name
