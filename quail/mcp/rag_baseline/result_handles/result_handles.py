"""Session-bound capabilities for opening search results."""

from __future__ import annotations

from dataclasses import dataclass
from secrets import token_urlsafe
from threading import Lock

from quail.analysis.errors import QuailScopeError, QuailSyntaxError


@dataclass(frozen=True, slots=True)
class ResultHandleRecord:
    """The server-side identity bound to one opaque result handle."""

    session_id: str
    workspace_id: str
    dataset_id: str
    entry_id: str


class ResultHandleRegistry:
    """Mint and resolve process-local, session-bound result handles."""

    def __init__(self) -> None:
        self._records: dict[str, ResultHandleRecord] = {}
        self._handles_by_result: dict[tuple[str, str, str, str], str] = {}
        self._lock = Lock()

    def issue(
        self,
        *,
        session_id: str,
        workspace_id: str,
        dataset_id: str,
        entry_id: str,
    ) -> str:
        """Return a stable handle for this result during the server process."""

        key = (session_id, workspace_id, dataset_id, entry_id)
        with self._lock:
            existing = self._handles_by_result.get(key)
            if existing is not None:
                return existing
            handle = token_urlsafe(32)
            while handle in self._records:
                handle = token_urlsafe(32)
            self._records[handle] = ResultHandleRecord(
                session_id=session_id,
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                entry_id=entry_id,
            )
            self._handles_by_result[key] = handle
            return handle

    def resolve(
        self,
        result_handle: object,
        *,
        session_id: str,
        workspace_id: str,
    ) -> ResultHandleRecord:
        """Resolve a handle only in the session and workspace that received it."""

        if not isinstance(result_handle, str) or not result_handle.strip():
            raise QuailSyntaxError("result_handle must be a non-empty string")
        cleaned = result_handle.strip()
        with self._lock:
            record = self._records.get(cleaned)
        if (
            record is None
            or record.session_id != session_id
            or record.workspace_id != workspace_id
        ):
            raise QuailScopeError(
                "Result handle is unavailable in this session; rerun search in the "
                "current session and use a result_handle it returns"
            )
        return record
