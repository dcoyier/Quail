"""Canned quail_exec recipe builder for opaque hybrid search."""

from __future__ import annotations

import json
import re

from quail.analysis.errors import QuailSyntaxError
from quail.mcp.rag_baseline.constants import SEARCH_FIELD

OUTPUT_MARKER = "QUAIL_SEARCH_V1"
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_search_script(query: str, n: int) -> str:
    """Build sandbox-legal Quail code that prints dual ranked id lists."""

    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise QuailSyntaxError("candidate n must be a positive integer")
    if not _FIELD_NAME_RE.fullmatch(SEARCH_FIELD):
        raise QuailSyntaxError(f"SEARCH_FIELD is not a safe field name: {SEARCH_FIELD!r}")
    query_literal = json.dumps(query, ensure_ascii=False)
    field_literal = json.dumps(SEARCH_FIELD, ensure_ascii=False)
    return (
        f"lex = Expression(Field({field_literal}), Lexical({query_literal}))\n"
        f"sem = Expression(Field({field_literal}), Semantic({query_literal}))\n"
        f"lex_hits = retrieve(group=G0.where(lex > 0), rank=Ranking(expression=lex), "
        f"limit={n})\n"
        f"sem_hits = retrieve(group=G0, rank=Ranking(expression=sem), limit={n})\n"
        f'print({json.dumps(OUTPUT_MARKER)})\n'
        f'print("lexical", len(lex_hits))\n'
        f"for entry in lex_hits:\n"
        f"    print(entry.id)\n"
        f'print("semantic", len(sem_hits))\n'
        f"for entry in sem_hits:\n"
        f"    print(entry.id)\n"
        f'print("END")\n'
    )
