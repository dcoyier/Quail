"""Host-side analysis driver: dispatch plans and commit overlays."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from quail.analysis.engine import QueryEngine
from quail.analysis.planner import (
    plan_count,
    plan_create_field,
    plan_retrieve,
    plan_tag,
    plan_untag,
)
from quail.datasets.db import CoreDb
from quail.session.overlay import commit_overlay, ensure_scope, resolve_scope


@dataclass(slots=True)
class PrintBuffer:
    chunks: list[str] = field(default_factory=list)

    def write(self, *values: Any, sep: str = " ", end: str = "\n") -> None:
        text = sep.join(str(value) for value in values) + end
        self.chunks.append(text)

    @property
    def text(self) -> str:
        return "".join(self.chunks)


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    printed_output: str
    state_revision: int


def dispatch_call(
    engine: QueryEngine,
    method: str,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> Any:
    """Plan then evaluate one facade method (stable shape for a future worker)."""

    kwargs = {} if kwargs is None else kwargs
    if method == "retrieve":
        return engine.retrieve(plan_retrieve(*args, **kwargs))
    if method == "count":
        return engine.count(plan_count(*args, **kwargs))
    if method == "create_field":
        return engine.create_field(plan_create_field(*args, **kwargs))
    if method == "tag":
        return engine.tag(plan_tag(*args, **kwargs))
    if method == "untag":
        return engine.untag(plan_untag(*args, **kwargs))
    if method == "entry_value":
        return engine.entry_value(*args, **kwargs)
    if method == "entry_fields":
        return engine.entry_fields(*args, **kwargs)
    raise KeyError(f"Unsupported analysis method: {method}")


def run_analysis(
    db: CoreDb,
    *,
    session_id: str,
    dataset_id: str,
    expected_revision: int,
    driver: Callable[[QueryEngine, PrintBuffer], None],
) -> ExecOutcome:
    """Run a host driver against a QueryEngine; commit overlay only on success."""

    scope = resolve_scope(db, session_id, dataset_id)
    ensure_scope(db, scope)
    engine = QueryEngine(db, scope)
    prints = PrintBuffer()
    driver(engine, prints)
    revision = commit_overlay(
        db,
        scope,
        expected_revision=expected_revision,
        mutations=engine.mutations,
    )
    return ExecOutcome(printed_output=prints.text, state_revision=revision)
