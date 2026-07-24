"""In-process Lexical scoring via Turso native FTS."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from quail.analysis.errors import QuailRuntimeError
from quail.search.db import SearchDb
from quail.search.lexical.corpus import (
    LexicalCorpus,
    ensure_entry_segments,
    expand_prefixes,
    resolve_corpus,
    validate_table_ident,
)
from quail.search.lexical.query import (
    BooleanExpression,
    Expression,
    Leaf,
    LeafKind,
    OrExpression,
    collect_prefixes,
    compile_query,
    parse_queries,
)

_MAX_MATCHES = 5_000_000


@dataclass(slots=True)
class LexicalService:
    """Index corpus segments into per-version FTS tables and score with Turso."""

    search: SearchDb

    def lexical_score(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        corpus: Any,
        query_record: dict[str, Any],
        input_aggregation: str | None,
        target_aggregation: str | None,
    ) -> float:
        """Score one corpus field value against a Lexical query record."""

        scores = self.lexical_scores_for_entries(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
            corpus_by_entry={"_": corpus},
            query_record=query_record,
            input_aggregation=input_aggregation,
            target_aggregation=target_aggregation,
        )
        return scores.get("_", 0.0)

    def lexical_scores_for_entries(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        version_id: str,
        corpus_by_entry: Mapping[str, Any],
        query_record: dict[str, Any],
        input_aggregation: str | None,
        target_aggregation: str | None,
    ) -> dict[str, float]:
        """Score many entries with one ensure pass and Turso FTS queries."""

        queries = _target_queries(query_record)
        if not queries:
            raise QuailRuntimeError("Lexical query produced no text targets")

        entry_segments: dict[str, list[str]] = {}
        empty_entries: set[str] = set()
        for entry_id, corpus in corpus_by_entry.items():
            corpus_texts = _corpus_texts(corpus)
            if corpus_texts is None:
                empty_entries.add(entry_id)
                continue
            entry_segments[entry_id] = corpus_texts

        results: dict[str, float] = {entry_id: 0.0 for entry_id in empty_entries}
        if not entry_segments:
            for entry_id in corpus_by_entry:
                results.setdefault(entry_id, 0.0)
            return results

        corpus = resolve_corpus(
            self.search,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version_id=version_id,
        )
        segment_counts = ensure_entry_segments(
            self.search,
            corpus,
            entry_segments=entry_segments,
        )

        input_mode = input_aggregation or "total"
        target_mode = target_aggregation or "total"
        entry_ids = list(entry_segments.keys())
        scored = _score_entries(
            self.search,
            corpus,
            queries=queries,
            entry_ids=entry_ids,
            segment_counts=segment_counts,
            input_aggregation=input_mode,
            target_aggregation=target_mode,
        )
        results.update(scored)
        for entry_id in corpus_by_entry:
            results.setdefault(entry_id, 0.0)
        return results


def _score_entries(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    queries: Sequence[str],
    entry_ids: Sequence[str],
    segment_counts: Mapping[str, int],
    input_aggregation: str,
    target_aggregation: str,
) -> dict[str, float]:
    expressions = parse_queries(tuple(queries))
    prefixes = expand_prefixes(
        search,
        corpus,
        collect_prefixes(expressions),
    )
    if collect_prefixes(expressions):
        totals = _score_with_prefixes(
            search,
            corpus,
            expressions=expressions,
            prefixes=prefixes,
            entry_ids=entry_ids,
            segment_counts=segment_counts,
            input_aggregation=input_aggregation,
        )
    else:
        totals = _score_simple(
            search,
            corpus,
            expressions=expressions,
            prefixes=prefixes,
            entry_ids=entry_ids,
            segment_counts=segment_counts,
            input_aggregation=input_aggregation,
        )
    if target_aggregation == "avg":
        divisor = len(expressions)
        return {entry_id: score / divisor for entry_id, score in totals.items()}
    return totals


def _score_simple(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    expressions: tuple[Expression, ...],
    prefixes: dict[str, tuple[str, ...]],
    entry_ids: Sequence[str],
    segment_counts: Mapping[str, int],
    input_aggregation: str,
) -> dict[str, float]:
    doc_table = validate_table_ident(corpus.doc_table)
    connection = search.connection
    candidates_json = json.dumps(list(entry_ids), separators=(",", ":"), allow_nan=False)
    output = {entry_id: 0.0 for entry_id in entry_ids}
    match_count = 0
    for expression in expressions:
        compiled = compile_query(expression, prefixes)
        target_totals = {entry_id: 0.0 for entry_id in entry_ids}
        remaining = _MAX_MATCHES - match_count
        try:
            rows = connection.execute(
                f"""
                SELECT entry_id, segment_position, fts_score(text, :query)
                FROM {doc_table}
                WHERE fts_match(text, :query)
                  AND entry_id IN (
                    SELECT value FROM json_each(:candidate_ids)
                  )
                LIMIT :match_limit
                """,
                {
                    "query": compiled,
                    "candidate_ids": candidates_json,
                    "match_limit": remaining + 1,
                },
            ).fetchall()
        except Exception as error:
            if "fts parse error" in str(error).casefold():
                raise QuailRuntimeError("Lexical query syntax is invalid") from error
            raise QuailRuntimeError(
                "Turso lexical FTS scoring failed",
                repair_hint="Retry the whole exec; if it persists, rebuild the search database.",
            ) from error
        for row in rows:
            match_count += 1
            if match_count > _MAX_MATCHES:
                raise QuailRuntimeError(
                    f"Lexical request matched more than the {_MAX_MATCHES}-document limit"
                )
            entry_id = str(row[0])
            if entry_id not in target_totals:
                continue
            score = _finite_nonneg(row[2])
            target_totals[entry_id] += score
        for entry_id, target_total in target_totals.items():
            if input_aggregation == "avg":
                segment_count = segment_counts.get(entry_id, 0)
                input_score = target_total / segment_count if segment_count else 0.0
            else:
                input_score = target_total
            output[entry_id] += input_score
    return output


def _score_with_prefixes(
    search: SearchDb,
    corpus: LexicalCorpus,
    *,
    expressions: tuple[Expression, ...],
    prefixes: dict[str, tuple[str, ...]],
    entry_ids: Sequence[str],
    segment_counts: Mapping[str, int],
    input_aggregation: str,
) -> dict[str, float]:
    doc_table = validate_table_ident(corpus.doc_table)
    connection = search.connection
    candidates_json = json.dumps(list(entry_ids), separators=(",", ":"), allow_nan=False)
    output = {entry_id: 0.0 for entry_id in entry_ids}
    match_count = 0
    for expression in expressions:
        parameters: dict[str, Any] = {"candidate_ids": candidates_json}
        predicate = _match_predicate(expression, prefixes, parameters)
        remaining = _MAX_MATCHES - match_count
        parameters["match_limit"] = remaining + 1
        try:
            rows = connection.execute(
                f"""
                SELECT document_id, entry_id, segment_position
                FROM {doc_table}
                WHERE ({predicate})
                  AND entry_id IN (
                    SELECT value FROM json_each(:candidate_ids)
                  )
                LIMIT :match_limit
                """,
                parameters,
            ).fetchall()
        except Exception as error:
            if "fts parse error" in str(error).casefold():
                raise QuailRuntimeError("Lexical query syntax is invalid") from error
            raise QuailRuntimeError(
                "Turso lexical FTS scoring failed",
                repair_hint="Retry the whole exec; if it persists, rebuild the search database.",
            ) from error
        matched_rows: list[tuple[int, str]] = []
        for row in rows:
            match_count += 1
            if match_count > _MAX_MATCHES:
                raise QuailRuntimeError(
                    f"Lexical request matched more than the {_MAX_MATCHES}-document limit"
                )
            matched_rows.append((int(row[0]), str(row[1])))
        document_ids = tuple(document_id for document_id, _ in matched_rows)
        scores = _expression_scores(search, corpus, expression, prefixes, document_ids)
        target_totals = {entry_id: 0.0 for entry_id in entry_ids}
        for document_id, entry_id in matched_rows:
            if entry_id not in target_totals:
                continue
            score = scores.get(document_id, 0.0)
            if not math.isfinite(score) or score < 0:
                raise QuailRuntimeError("Lexical produced an invalid score")
            target_totals[entry_id] += score
        for entry_id, target_total in target_totals.items():
            if input_aggregation == "avg":
                segment_count = segment_counts.get(entry_id, 0)
                input_score = target_total / segment_count if segment_count else 0.0
            else:
                input_score = target_total
            output[entry_id] += input_score
    return output


def _expression_scores(
    search: SearchDb,
    corpus: LexicalCorpus,
    expression: Expression,
    prefixes: dict[str, tuple[str, ...]],
    document_ids: tuple[int, ...],
) -> dict[int, float]:
    if not isinstance(expression, OrExpression):
        return _positive_scores(search, corpus, expression, prefixes, document_ids)

    scores: dict[int, float] = {}
    leaves = tuple(item for item in expression.expressions if isinstance(item, Leaf))
    if leaves:
        leaf_expression: Expression = leaves[0] if len(leaves) == 1 else OrExpression(leaves)
        _merge_scores(
            scores,
            _positive_scores(search, corpus, leaf_expression, prefixes, document_ids),
        )
    for branch in expression.expressions:
        if isinstance(branch, Leaf):
            continue
        branch_document_ids = _matching_document_ids(search, corpus, branch, prefixes, document_ids)
        _merge_scores(
            scores,
            _expression_scores(search, corpus, branch, prefixes, branch_document_ids),
        )
    return scores


def _positive_scores(
    search: SearchDb,
    corpus: LexicalCorpus,
    expression: Expression,
    prefixes: dict[str, tuple[str, ...]],
    document_ids: tuple[int, ...],
) -> dict[int, float]:
    exact_queries, prefix_queries = _positive_queries(expression, prefixes)
    scores = _document_scores(search, corpus, "text", exact_queries, document_ids)
    _merge_scores(
        scores,
        _document_scores(search, corpus, "prefix_text", prefix_queries, document_ids),
    )
    return scores


def _matching_document_ids(
    search: SearchDb,
    corpus: LexicalCorpus,
    expression: Expression,
    prefixes: dict[str, tuple[str, ...]],
    document_ids: tuple[int, ...],
) -> tuple[int, ...]:
    if not document_ids:
        return ()
    doc_table = validate_table_ident(corpus.doc_table)
    parameters: dict[str, Any] = {
        "document_ids": json.dumps(document_ids, separators=(",", ":"), allow_nan=False)
    }
    predicate = _match_predicate(expression, prefixes, parameters)
    rows = search.connection.execute(
        f"""
        SELECT document_id
        FROM {doc_table}
        WHERE ({predicate})
          AND document_id IN (
            SELECT CAST(value AS INTEGER) FROM json_each(:document_ids)
          )
        """,
        parameters,
    ).fetchall()
    matched = {int(row[0]) for row in rows}
    return tuple(document_id for document_id in document_ids if document_id in matched)


def _document_scores(
    search: SearchDb,
    corpus: LexicalCorpus,
    field: str,
    queries: tuple[str, ...],
    document_ids: tuple[int, ...],
) -> dict[int, float]:
    if not queries or not document_ids:
        return {}
    if field not in {"text", "prefix_text"}:
        raise QuailRuntimeError("Lexical score field is invalid")
    doc_table = validate_table_ident(corpus.doc_table)
    query = "(" + " OR ".join(queries) + ")"
    document_ids_json = json.dumps(document_ids, separators=(",", ":"), allow_nan=False)
    rows = search.connection.execute(
        f"""
        SELECT document_id, fts_score({field}, :query)
        FROM {doc_table}
        WHERE fts_match({field}, :query)
          AND document_id IN (
            SELECT CAST(value AS INTEGER) FROM json_each(:document_ids)
          )
        """,
        {"document_ids": document_ids_json, "query": query},
    ).fetchall()
    return {int(row[0]): float(row[1]) for row in rows}


def _merge_scores(target: dict[int, float], additional: Mapping[int, float]) -> None:
    for document_id, score in additional.items():
        total = target.get(document_id, 0.0) + score
        if not math.isfinite(total) or score < 0:
            raise QuailRuntimeError("Lexical produced an invalid score")
        target[document_id] = total


def _match_predicate(
    expression: Expression,
    prefixes: dict[str, tuple[str, ...]],
    parameters: dict[str, Any],
) -> str:
    if isinstance(expression, Leaf):
        name = f"leaf_{sum(key.startswith('leaf_') for key in parameters)}"
        parameters[name] = compile_query(expression, prefixes)
        field = "prefix_text" if expression.kind is LeafKind.PREFIX else "text"
        return f"fts_match({field}, :{name})"
    if isinstance(expression, BooleanExpression):
        required = [_match_predicate(leaf, prefixes, parameters) for leaf in expression.required]
        excluded = [_match_predicate(leaf, prefixes, parameters) for leaf in expression.excluded]
        clauses = [f"({clause})" for clause in required]
        clauses.extend(f"NOT ({clause})" for clause in excluded)
        return " AND ".join(clauses)
    return " OR ".join(
        f"({_match_predicate(item, prefixes, parameters)})" for item in expression.expressions
    )


def _positive_queries(
    expression: Expression,
    prefixes: dict[str, tuple[str, ...]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    exact: list[str] = []
    prefix: list[str] = []

    def collect(item: Expression) -> None:
        if isinstance(item, Leaf):
            target = prefix if item.kind is LeafKind.PREFIX else exact
            target.append(compile_query(item, prefixes))
            return
        if isinstance(item, BooleanExpression):
            for leaf in item.required:
                collect(leaf)
            return
        for child in item.expressions:
            collect(child)

    collect(expression)
    return tuple(exact), tuple(prefix)


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
                raise QuailRuntimeError("Lexical corpus list values must be text")
            texts.append(item)
        if not texts:
            return None
        return texts
    return [str(corpus)]


def _target_queries(query_record: dict[str, Any]) -> list[str]:
    kind = query_record.get("kind")
    if kind == "LiteralText":
        text = query_record.get("text")
        if not isinstance(text, str) or not text:
            raise QuailRuntimeError("Lexical LiteralText query must be non-empty text")
        return [text]
    if kind == "LiteralTextList":
        texts = query_record.get("texts")
        if not isinstance(texts, list | tuple):
            raise QuailRuntimeError("Lexical LiteralTextList query must be a list of text")
        out = [item for item in texts if isinstance(item, str) and item]
        if not out:
            raise QuailRuntimeError("Lexical query must contain at least one non-empty text")
        return out
    raise QuailRuntimeError(
        "Lexical EntryGroup/EntryList queries must be resolved by QueryEngine; "
        "use text or list[str] query records here"
    )


def _finite_nonneg(value: object) -> float:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        raise QuailRuntimeError("Lexical produced an invalid score")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise QuailRuntimeError("Lexical produced an invalid score")
    return number
