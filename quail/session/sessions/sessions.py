"""Host session create / get / close."""

from __future__ import annotations

from uuid import uuid4

from quail.datasets.db import CoreDb, ensure_workspace
from quail.datasets.db.db import _require_scope_id
from quail.session.errors import SessionClosedError, SessionSyntaxError
from quail.session.models import Session


def create_session(db: CoreDb, workspace_id: str) -> Session:
    """Create an active session bound to a workspace."""

    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    ensure_workspace(db, workspace_id)
    session_id = f"ses_{uuid4().hex}"
    db.connection.execute(
        """
        INSERT INTO quail_sessions(
          id, workspace_id, status, state_revision, created_at, last_used_at
        ) VALUES (
          ?, ?, 'active', 0,
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
        """,
        (session_id, workspace_id),
    )
    db.connection.commit()
    return Session(
        id=session_id,
        workspace_id=workspace_id,
        status="active",
        state_revision=0,
    )


def get_session(db: CoreDb, session_id: str) -> Session | None:
    session_id = _require_scope_id(session_id, label="Session id")
    row = db.connection.execute(
        """
        SELECT id, workspace_id, status, state_revision
        FROM quail_sessions
        WHERE id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return Session(
        id=str(row[0]),
        workspace_id=str(row[1]),
        status=str(row[2]),
        state_revision=int(row[3]),
    )


def close_session(db: CoreDb, session_id: str) -> None:
    session_id = _require_scope_id(session_id, label="Session id")
    session = get_session(db, session_id)
    if session is None:
        raise SessionSyntaxError("Session does not exist")
    if session.status == "closed":
        return
    db.connection.execute(
        """
        UPDATE quail_sessions
        SET status = 'closed',
            last_used_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (session_id,),
    )
    db.connection.commit()


def require_active_session(db: CoreDb, session_id: str) -> Session:
    session = get_session(db, session_id)
    if session is None:
        raise SessionSyntaxError("Session does not exist")
    if session.status != "active":
        raise SessionClosedError("Session is closed")
    return session
