"""Host-side reciprocal rank fusion for dual Lexical/Semantic lists."""

from __future__ import annotations

_RRF_CONSTANT = 60
_N_FLOOR = 10
_N_CAP = 50


def candidate_n(top_k: int) -> int:
    """Per-arm retrieve depth scaled from the agent-facing top_k."""

    return min(_N_CAP, max(_N_FLOOR, top_k * 5))


def rrf_fuse(
    lexical_ids: list[str],
    semantic_ids: list[str],
    *,
    top_k: int,
    rank_constant: int = _RRF_CONSTANT,
) -> list[str]:
    """Fuse two ranked id lists with equal-weight RRF; return up to top_k ids."""

    if top_k < 1:
        return []
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}

    def _accumulate(ids: list[str]) -> None:
        seen: set[str] = set()
        rank = 0
        for entry_id in ids:
            if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            rank += 1
            scores[entry_id] = scores.get(entry_id, 0.0) + 1.0 / (rank_constant + rank)
            prior = best_rank.get(entry_id)
            if prior is None or rank < prior:
                best_rank[entry_id] = rank

    _accumulate(lexical_ids)
    _accumulate(semantic_ids)

    ordered = sorted(
        scores.keys(),
        key=lambda entry_id: (-scores[entry_id], best_rank[entry_id], entry_id),
    )
    return ordered[:top_k]
