"""Fixed unrestricted workspace context for the thin MCP adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quail.search.runtime import SearchRuntime


DEFAULT_WORKSPACE_ID = "local"


@dataclass(slots=True)
class McpContext:
    """Process-fixed workspace, core DB path, feedback path, optional search runtime."""

    db_path: Path
    workspace_id: str
    feedback_path: Path
    api_docs_path: Path
    search_runtime: SearchRuntime | None = None
