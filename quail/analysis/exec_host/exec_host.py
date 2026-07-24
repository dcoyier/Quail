"""Host-side analysis driver: dispatch plans and commit overlays."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from quail.analysis.admission import acquire_execution_slot
from quail.analysis.cancel import interrupt_connections_on_cancel, raise_if_cancelled
from quail.analysis.engine import QueryEngine
from quail.analysis.limits import ExecLimits
from quail.analysis.planner import (
    plan_count,
    plan_create_field,
    plan_retrieve,
    plan_tag,
    plan_untag,
)
from quail.analysis.session_lock import acquire_session_lock
from quail.analysis.worker.client import run_worker_script
from quail.analysis.worker.protocol import ApiCall
from quail.datasets.db import CoreDb
from quail.search import LexicalService, SimilarityService
from quail.search.runtime import SearchRuntime
from quail.session.overlay import commit_overlay, ensure_scope, load_bindings, resolve_scope


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
    """Plan then evaluate one facade method (stable shape for worker RPC)."""

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
    similarity: SimilarityService | None = None,
    lexical: LexicalService | None = None,
) -> ExecOutcome:
    """Run a host driver against a QueryEngine; commit overlay only on success."""

    scope = resolve_scope(db, session_id, dataset_id)
    ensure_scope(db, scope)
    engine = QueryEngine(db, scope, similarity=similarity, lexical=lexical)
    prints = PrintBuffer()
    driver(engine, prints)
    revision = commit_overlay(
        db,
        scope,
        expected_revision=expected_revision,
        mutations=engine.mutations,
    )
    return ExecOutcome(printed_output=prints.text, state_revision=revision)


def exec_script(
    db: CoreDb,
    *,
    session_id: str,
    dataset_id: str,
    expected_revision: int,
    code: str,
    similarity: SimilarityService | None = None,
    lexical: LexicalService | None = None,
    search_runtime: SearchRuntime | None = None,
    time_window: str | None = "standard",
    limits: ExecLimits | None = None,
    cancel_event: threading.Event | None = None,
) -> ExecOutcome:
    """Run quail_exec code in a worker subprocess; commit overlay on success."""

    from quail.analysis.limits import limits_for_time_window

    scope = resolve_scope(db, session_id, dataset_id)
    ensure_scope(db, scope)
    active_limits = limits if limits is not None else limits_for_time_window(time_window)
    host_cancel = cancel_event if cancel_event is not None else threading.Event()
    initial_bindings = load_bindings(db, session_id)

    with acquire_session_lock(session_id):
        with acquire_execution_slot():
            search = None
            search_runtime_active = search_runtime
            active_similarity = similarity
            active_lexical = lexical
            interruptible = [db.connection]
            try:
                if search_runtime_active is not None:
                    search = search_runtime_active.pool.checkout()
                    interruptible.append(search.connection)
                    active_similarity, active_lexical = search_runtime_active.bind_services(
                        search
                    )
                engine = QueryEngine(
                    db,
                    scope,
                    similarity=active_similarity,
                    lexical=active_lexical,
                )

                def on_api_call(call: ApiCall) -> Any:
                    raise_if_cancelled(host_cancel, limits=active_limits)
                    try:
                        return dispatch_call(engine, call.method, call.args, call.kwargs)
                    finally:
                        raise_if_cancelled(host_cancel, limits=active_limits)

                with interrupt_connections_on_cancel(interruptible, host_cancel):
                    worker_result = run_worker_script(
                        code,
                        on_api_call=on_api_call,
                        bindings=initial_bindings,
                        limits=active_limits,
                        cancel_event=host_cancel,
                    )
                revision = commit_overlay(
                    db,
                    scope,
                    expected_revision=expected_revision,
                    mutations=engine.mutations,
                    bindings=worker_result.changed_bindings,
                    binding_deletes=worker_result.deleted_bindings,
                )
            finally:
                if search is not None and search_runtime_active is not None:
                    search_runtime_active.pool.release(search)
    return ExecOutcome(
        printed_output=worker_result.printed_output,
        state_revision=revision,
    )
