"""Authorized opaque search and get_entry helpers."""

from __future__ import annotations

from typing import Any

from quail.analysis.errors import QuailFieldError, QuailScopeError, QuailSyntaxError
from quail.analysis.exec_host import exec_script
from quail.datasets import (
    CoreDb,
    active_version,
    get_dataset,
    source_entries,
    source_fields,
    source_values,
)
from quail.mcp.rag_baseline.constants import SEARCH_FIELD
from quail.mcp.rag_baseline.parse import parse_search_output
from quail.mcp.rag_baseline.rrf import candidate_n, rrf_fuse
from quail.mcp.rag_baseline.template import build_search_script
from quail.mcp.rag_baseline.validate import normalize_query, normalize_top_k
from quail.search.runtime import SearchRuntime
from quail.session.models import Session

_SNIPPET_CHARS = 500


def run_search(
    db: CoreDb,
    *,
    workspace_id: str,
    session: Session,
    dataset_id: str,
    query: object,
    top_k: object = 8,
    search_runtime: SearchRuntime | None = None,
) -> dict[str, Any]:
    """Run canned dual-arm retrieve, host RRF, and return agent-facing hits."""

    if session.workspace_id != workspace_id:
        raise ValueError("Session does not belong to this workspace")
    cleaned_query = normalize_query(query)
    cleaned_top_k = normalize_top_k(top_k)
    ref = get_dataset(db, workspace_id, dataset_id)
    if ref is None:
        raise QuailScopeError(f"Dataset not found: {dataset_id}")
    del ref
    version = active_version(db, workspace_id, dataset_id)
    if version is None:
        raise QuailScopeError(f"Dataset has no active version: {dataset_id}")
    _require_search_field(db, workspace_id, dataset_id, version.version_id)

    n = candidate_n(cleaned_top_k)
    code = build_search_script(cleaned_query, n)
    outcome = exec_script(
        db,
        session_id=session.id,
        dataset_id=dataset_id,
        expected_revision=session.state_revision,
        code=code,
        search_runtime=search_runtime,
        time_window="standard",
    )
    lexical_ids, semantic_ids = parse_search_output(outcome.printed_output)
    fused_ids = rrf_fuse(lexical_ids, semantic_ids, top_k=cleaned_top_k)
    texts = _field_values_for_ids(
        db,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        version_id=version.version_id,
        entry_ids=fused_ids,
    )
    hits: list[dict[str, Any]] = []
    for index, entry_id in enumerate(fused_ids, start=1):
        raw = texts.get(entry_id)
        text = "" if raw is None else str(raw)
        if len(text) > _SNIPPET_CHARS:
            text = text[:_SNIPPET_CHARS]
        hits.append({"rank": index, "entry_id": entry_id, "text": text})
    return {"query": cleaned_query, "top_k": cleaned_top_k, "hits": hits}


def get_entry_payload(
    db: CoreDb,
    *,
    workspace_id: str,
    session: Session,
    dataset_id: str,
    entry_id: object,
) -> dict[str, Any]:
    """Return the full source field map for one entry_id."""

    if session.workspace_id != workspace_id:
        raise ValueError("Session does not belong to this workspace")
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise QuailSyntaxError("entry_id must be a non-empty string")
    cleaned_entry_id = entry_id.strip()
    ref = get_dataset(db, workspace_id, dataset_id)
    if ref is None:
        raise QuailScopeError(f"Dataset not found: {dataset_id}")
    del ref
    version = active_version(db, workspace_id, dataset_id)
    if version is None:
        raise QuailScopeError(f"Dataset has no active version: {dataset_id}")
    known = {
        entry.id
        for entry in source_entries(db, workspace_id, dataset_id, version.version_id)
    }
    if cleaned_entry_id not in known:
        raise QuailScopeError(f"Entry not found: {cleaned_entry_id}")
    fields = source_fields(db, workspace_id, dataset_id, version.version_id)
    payload: dict[str, Any] = {}
    for field in fields:
        values = source_values(
            db,
            workspace_id,
            dataset_id,
            version.version_id,
            field.name,
            entry_ids=[cleaned_entry_id],
        )
        payload[field.name] = values[0] if values else None
    return {"entry_id": cleaned_entry_id, "fields": payload}


def _require_search_field(
    db: CoreDb,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
) -> None:
    names = {
        field.name for field in source_fields(db, workspace_id, dataset_id, version_id)
    }
    if SEARCH_FIELD not in names:
        raise QuailFieldError(
            f"Search field {SEARCH_FIELD!r} is not present on dataset {dataset_id}"
        )


def _field_values_for_ids(
    db: CoreDb,
    *,
    workspace_id: str,
    dataset_id: str,
    version_id: str,
    entry_ids: list[str],
) -> dict[str, Any]:
    if not entry_ids:
        return {}
    values = source_values(
        db,
        workspace_id,
        dataset_id,
        version_id,
        SEARCH_FIELD,
        entry_ids=entry_ids,
    )
    return {
        entry_id: values[index]
        for index, entry_id in enumerate(entry_ids)
        if index < len(values)
    }
