"""Fixed unrestricted workspace context for the thin MCP adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quail.datasets.db import CoreDb


DEFAULT_WORKSPACE_ID = "local"


@dataclass(slots=True)
class McpContext:
    """Process-fixed workspace plus open core DB and feedback path."""

    db: CoreDb
    workspace_id: str
    feedback_path: Path
    api_docs_path: Path
