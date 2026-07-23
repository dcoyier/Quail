"""Host session and analysis overlay APIs (not agent-facing MCP)."""

from quail.session.errors import (
    SessionClosedError,
    SessionConflictError,
    SessionError,
    SessionSyntaxError,
)
from quail.session.models import (
    AnalysisField,
    CatalogField,
    FieldCreate,
    Scope,
    Session,
    ValueDelete,
    ValueWrite,
)
from quail.session.overlay import (
    analysis_fields,
    analysis_values,
    catalog_fields,
    commit_overlay,
    ensure_scope,
    resolve_scope,
)
from quail.session.sessions import close_session, create_session, get_session

__all__ = [
    "AnalysisField",
    "CatalogField",
    "FieldCreate",
    "Scope",
    "Session",
    "SessionClosedError",
    "SessionConflictError",
    "SessionError",
    "SessionSyntaxError",
    "ValueDelete",
    "ValueWrite",
    "analysis_fields",
    "analysis_values",
    "catalog_fields",
    "close_session",
    "commit_overlay",
    "create_session",
    "ensure_scope",
    "get_session",
    "resolve_scope",
]
