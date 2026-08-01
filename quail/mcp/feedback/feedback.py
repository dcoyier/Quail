"""Append-only JSONL feedback store (outside the core analysis DB)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Per-message cap (UTF-8 bytes) and whole-file quota before append.
_MAX_MESSAGE_BYTES = 16 * 1024
_MAX_FILE_BYTES = 64 * 1024 * 1024


def append_feedback(
    path: str | Path,
    *,
    workspace_id: str,
    message: str,
    category: str | None = None,
    session_id: str | None = None,
    dataset_id: str | None = None,
) -> None:
    """Append one feedback object as a single JSON line."""

    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    message_bytes = len(message.encode("utf-8"))
    if message_bytes > _MAX_MESSAGE_BYTES:
        raise ValueError(
            f"message exceeds {_MAX_MESSAGE_BYTES} bytes "
            f"(got {message_bytes})"
        )
    workspace_id = _require_text(workspace_id, label="workspace_id")
    category = _optional_text(category, label="category")
    session_id = _optional_text(session_id, label="session_id")
    dataset_id = _optional_text(dataset_id, label="dataset_id")

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "workspace_id": workspace_id,
        "message": message,
        "category": category,
        "session_id": session_id,
        "dataset_id": dataset_id,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size >= _MAX_FILE_BYTES:
        raise ValueError(
            f"feedback file exceeds {_MAX_FILE_BYTES} bytes; "
            "archive or truncate it before accepting more notes"
        )
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")


def _require_text(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_text(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string or None")
    stripped = value.strip()
    return stripped or None
