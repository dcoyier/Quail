"""Per-version Turso FTS document and term tables inside the search DB."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from quail.analysis.errors import QuailRuntimeError
from quail.search.db import SearchDb
from quail.search.lexical.query import prepare_prefix_text, prepare_text

_TABLE_NAME_RE = re.compile(r"^quail_lex_[dt]_[0-9a-f]{32}$")
_MAX_PREFIX_TERMS = 4_096


@dataclass(frozen=True, slots=True)
class LexicalCorpus:
    """Resolved per-version document and terms table names."""

    doc_table: str
    terms_table: str


def resolve_corpus(
    search: SearchDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> LexicalCorpus:
    """Return existing or newly created per-version FTS tables."""

    connection = search.connection
    row = connection.execute(
        """
        SELECT doc_table, terms_table
        FROM quail_lexical_corpus
        WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
        """,
        (workspace_id, dataset_id, version_id),
    ).fetchone()
    if row is not None:
        doc_table = str(row[0])
        terms_table = str(row[1])
        _require_safe_table(doc_table)
        _require_safe_table(terms_table)
        return LexicalCorpus(doc_table=doc_table, terms_table=terms_table)

    key = hashlib.sha256(f"{workspace_id}\0{dataset_id}\0{version_id}".encode()).hexdigest()[:32]
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
            CREATE INDEX {doc_table}_fts
            ON {doc_table} USING fts(text)
            WITH (tokenizer = 'whitespace')
            """
        )
        connection.execute(
            f"""
            CREATE INDEX {doc_table}_prefix_fts
            ON {doc_table} USING fts(prefix_text)
            WITH (tokenizer = 'whitespace')
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
              workspace_id, dataset_id, version_id, doc_table, terms_table, created_at
            ) VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (workspace_id, dataset_id, version_id, doc_table, terms_table),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise QuailRuntimeError(
            "Failed to create lexical FTS corpus tables",
            repair_hint="Retry the whole exec; if it persists, rebuild the search database.",
        ) from None
    return LexicalCorpus(doc_table=doc_table, terms_table=terms_table)


def load_entry_segment_counts(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    entry_ids: Sequence[str],
) -> dict[str, int]:
    """Return entry_id → segment count for entries that already have FTS rows."""

    if not entry_ids:
        return {}
    doc_table = validate_table_ident(corpus.doc_table)
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


def ensure_entry_segments(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    entry_segments: dict[str, list[str]],
) -> dict[str, int]:
    """Replace indexed segments for each entry; return segment counts.

    Used by ``quail process`` warm and by score-time fallback when lexical warm
    is missing or incomplete. Score paths should prefer
    ``load_entry_segment_counts`` when the warm receipt is lexical-ready.
    """

    connection = search.connection
    counts: dict[str, int] = {}
    try:
        connection.execute("BEGIN IMMEDIATE")
        for entry_id, segments in entry_segments.items():
            connection.execute(
                f"DELETE FROM {corpus.doc_table} WHERE entry_id = ?",
                (entry_id,),
            )
            for position, segment in enumerate(segments):
                prepared, analyzed = prepare_text(segment)
                prefix_prepared, prefix_terms = prepare_prefix_text(analyzed)
                connection.execute(
                    f"""
                    INSERT INTO {corpus.doc_table}(
                      entry_id, segment_position, text, prefix_text
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (entry_id, position, prepared, prefix_prepared),
                )
                if prefix_terms:
                    connection.executemany(
                        f"""
                        INSERT OR IGNORE INTO {corpus.terms_table}(term, indexed_term)
                        VALUES (?, ?)
                        """,
                        list(prefix_terms),
                    )
            counts[entry_id] = len(segments)
        connection.commit()
    except QuailRuntimeError:
        connection.rollback()
        raise
    except Exception as error:
        connection.rollback()
        raise QuailRuntimeError(
            "Failed to index lexical corpus segments",
            repair_hint="Retry the whole exec; if it persists, rebuild the search database.",
        ) from error
    return counts


def expand_prefixes(
    search: SearchDb,
    corpus: LexicalCorpus,
    prefixes: tuple[str, ...],
    *,
    max_prefix_terms: int = _MAX_PREFIX_TERMS,
) -> dict[str, tuple[str, ...]]:
    """Expand prefix leaves against the version term dictionary."""

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
