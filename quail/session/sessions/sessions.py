"""Host session create / get / close."""

from __future__ import annotations

from uuid import uuid4

from quail.datasets.db import CoreDb, ensure_workspace
from quail.datasets.db.db import _require_scope_id
from quail.session.errors import SessionClosedError, SessionSyntaxError
from quail.session.models import Session


def create_session(
    db: CoreDb,
    workspace_id: str,
    *,
    owner_user_id: str | None = None,
) -> Session:
    """Create an active session bound to a workspace.

    Pass owner_user_id (TOML [[users]].id) in Clerk mode. Leave None for
    unrestricted single-tenant sessions.
    """

    workspace_id = _require_scope_id(workspace_id, label="Workspace id")
    ensure_workspace(db, workspace_id)
    owner = _normalize_owner_user_id(owner_user_id)
    session_id = f"ses_{uuid4().hex}"
    db.connection.execute(
        """
        INSERT INTO quail_sessions(
          id, workspace_id, status, state_revision, created_at, last_used_at,
          owner_user_id
        ) VALUES (
          ?, ?, 'active', 0,
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
          strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
          ?
        )
        """,
        (session_id, workspace_id, owner),
    )
    db.connection.commit()
    return Session(
        id=session_id,
        workspace_id=workspace_id,
        status="active",
        state_revision=0,
        owner_user_id=owner,
    )


def get_session(db: CoreDb, session_id: str) -> Session | None:
    session_id = _require_scope_id(session_id, label="Session id")
    row = db.connection.execute(
        """
        SELECT id, workspace_id, status, state_revision, owner_user_id
        FROM quail_sessions
        WHERE id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    owner = row[4]
    return Session(
        id=str(row[0]),
        workspace_id=str(row[1]),
        status=str(row[2]),
        state_revision=int(row[3]),
        owner_user_id=str(owner) if owner is not None else None,
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


def require_owned_active_session(
    db: CoreDb,
    session_id: str,
    *,
    owner_user_id: str,
) -> Session:
    """Require an active session owned by this TOML user id (Clerk)."""

    session = require_active_session(db, session_id)
    _require_owner(session, owner_user_id)
    return session


def require_session_owner(
    db: CoreDb,
    session_id: str,
    *,
    owner_user_id: str,
) -> Session:
    """Require an existing session (any status) owned by this TOML user id."""

    session = get_session(db, session_id)
    if session is None:
        raise SessionSyntaxError("Session does not exist")
    _require_owner(session, owner_user_id)
    return session


def _require_owner(session: Session, owner_user_id: str) -> None:
    expected = _normalize_owner_user_id(owner_user_id)
    if expected is None:
        raise SessionSyntaxError("owner_user_id cannot be empty")
    if session.owner_user_id is None or session.owner_user_id != expected:
        raise SessionSyntaxError(
            "Session does not belong to this user. "
            "Create a session with quail_setup or quail_start_session and use that session_id."
        )


def _normalize_owner_user_id(owner_user_id: str | None) -> str | None:
    if owner_user_id is None:
        return None
    value = owner_user_id.strip()
    if not value:
        raise SessionSyntaxError("owner_user_id cannot be empty")
    return value
