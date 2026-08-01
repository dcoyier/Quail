"""Parse tagged dual-list output from the canned search recipe."""

from __future__ import annotations

from quail.analysis.errors import QuailRuntimeError
from quail.mcp.rag_baseline.template import OUTPUT_MARKER


def parse_search_output(printed_output: str) -> tuple[list[str], list[str]]:
    """Parse one QUAIL_SEARCH_V1 dual-list record from printed_output."""

    if not isinstance(printed_output, str):
        raise QuailRuntimeError("search output must be text")
    lines = printed_output.splitlines()
    try:
        start = lines.index(OUTPUT_MARKER)
    except ValueError as error:
        raise QuailRuntimeError(
            f"search output missing {OUTPUT_MARKER} marker"
        ) from error
    if lines.count(OUTPUT_MARKER) != 1:
        raise QuailRuntimeError(f"search output must contain exactly one {OUTPUT_MARKER}")

    cursor = start + 1
    lexical_ids, cursor = _read_arm(lines, cursor, arm="lexical")
    semantic_ids, cursor = _read_arm(lines, cursor, arm="semantic")
    if cursor >= len(lines) or lines[cursor] != "END":
        raise QuailRuntimeError("search output missing END marker")
    if cursor != len(lines) - 1:
        raise QuailRuntimeError("search output has trailing content after END")
    return lexical_ids, semantic_ids


def _read_arm(lines: list[str], cursor: int, *, arm: str) -> tuple[list[str], int]:
    if cursor >= len(lines):
        raise QuailRuntimeError(f"search output truncated before {arm} header")
    header = lines[cursor].split()
    if len(header) != 2 or header[0] != arm:
        raise QuailRuntimeError(f"search output missing {arm} header")
    try:
        count = int(header[1])
    except ValueError as error:
        raise QuailRuntimeError(f"search output {arm} count is not an integer") from error
    if count < 0:
        raise QuailRuntimeError(f"search output {arm} count must be non-negative")
    cursor += 1
    end = cursor + count
    if end > len(lines):
        raise QuailRuntimeError(f"search output truncated in {arm} ids")
    ids: list[str] = []
    for line in lines[cursor:end]:
        if not line or any(ch.isspace() for ch in line):
            raise QuailRuntimeError(f"search output {arm} id must be a single token")
        ids.append(line)
    return ids, end
