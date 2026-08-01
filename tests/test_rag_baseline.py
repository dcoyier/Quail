"""Unit tests for rag-baseline helpers (RRF, template, parse, validate)."""

from __future__ import annotations

import pytest

from quail.analysis.errors import QuailRuntimeError, QuailSyntaxError
from quail.mcp.rag_baseline import (
    OUTPUT_MARKER,
    SEARCH_FIELD,
    build_search_script,
    candidate_n,
    normalize_query,
    normalize_top_k,
    rrf_fuse,
)
from quail.mcp.rag_baseline.parse import parse_search_output


def test_candidate_n_scales_and_caps() -> None:
    assert candidate_n(1) == 10
    assert candidate_n(2) == 10
    assert candidate_n(3) == 15
    assert candidate_n(8) == 40
    assert candidate_n(20) == 50


def test_rrf_fuse_prefers_shared_high_ranks() -> None:
    fused = rrf_fuse(["a", "b", "c"], ["b", "a", "d"], top_k=3)
    # a and b share the same fused score; tie-break is best arm rank then id.
    assert set(fused[:2]) == {"a", "b"}
    assert fused[0] in {"a", "b"}
    assert len(fused) == 3
    assert "d" in fused or "c" in fused


def test_rrf_fuse_dedupes_within_arm_and_deterministic_ties() -> None:
    fused = rrf_fuse(["a", "a", "b"], ["c"], top_k=10)
    assert fused.count("a") == 1
    assert "a" in fused and "b" in fused and "c" in fused


def test_normalize_query_and_top_k() -> None:
    assert normalize_query("  hello  ") == "hello"
    with pytest.raises(QuailSyntaxError):
        normalize_query("   ")
    with pytest.raises(QuailSyntaxError):
        normalize_query(1)  # type: ignore[arg-type]
    assert normalize_top_k(None) == 8
    assert normalize_top_k(5) == 5
    with pytest.raises(QuailSyntaxError):
        normalize_top_k(True)  # type: ignore[arg-type]
    with pytest.raises(QuailSyntaxError):
        normalize_top_k(0)
    with pytest.raises(QuailSyntaxError):
        normalize_top_k(21)


def test_build_search_script_escapes_query() -> None:
    script = build_search_script('say "hi"\nworld', 10)
    assert SEARCH_FIELD in script
    assert "Lexical(" in script and "Semantic(" in script
    assert '"say \\"hi\\"\\nworld"' in script or '\\"hi\\"' in script
    assert "limit=10" in script
    assert OUTPUT_MARKER in script


def test_parse_search_output_round_trip_shape() -> None:
    printed = "\n".join(
        [
            OUTPUT_MARKER,
            "lexical 2",
            "e1",
            "e2",
            "semantic 1",
            "e2",
            "END",
        ]
    )
    lexical, semantic = parse_search_output(printed)
    assert lexical == ["e1", "e2"]
    assert semantic == ["e2"]


def test_parse_search_output_rejects_bad_shapes() -> None:
    with pytest.raises(QuailRuntimeError, match="missing"):
        parse_search_output("no marker\n")
    with pytest.raises(QuailRuntimeError, match="END"):
        parse_search_output(f"{OUTPUT_MARKER}\nlexical 0\nsemantic 0\n")
    with pytest.raises(QuailRuntimeError, match="truncated|missing"):
        parse_search_output(f"{OUTPUT_MARKER}\nlexical 2\ne1\n")
