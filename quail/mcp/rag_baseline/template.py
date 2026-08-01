"""Canned quail_exec recipe builder for opaque hybrid search."""

from __future__ import annotations

import json
import re

from quail.analysis.errors import QuailSyntaxError
from quail.mcp.rag_baseline.constants import SEARCH_FIELD
from quail.search.lexical.query import tokenize

OUTPUT_MARKER = "QUAIL_SEARCH_V1"
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def lexical_query_text(query: str) -> str:
    """Turn natural-language text into a Lexical-safe bag of terms.

    Opaque search accepts punctuation and hyphens; Quail Lexical query syntax
    does not. Tokenize with the same analyzer Lexical indexing uses so the arm
    never hard-fails the whole search on ordinary NL punctuation.
    """

    return " ".join(tokenize(query))


def build_search_script(query: str, n: int) -> str:
    """Build sandbox-legal Quail code that prints dual ranked id lists."""

    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise QuailSyntaxError("candidate n must be a positive integer")
    if not _FIELD_NAME_RE.fullmatch(SEARCH_FIELD):
        raise QuailSyntaxError(f"SEARCH_FIELD is not a safe field name: {SEARCH_FIELD!r}")
    field_literal = json.dumps(SEARCH_FIELD, ensure_ascii=False)
    semantic_literal = json.dumps(query, ensure_ascii=False)
    lexical_text = lexical_query_text(query)
    lines = [
        f"sem = Expression(Field({field_literal}), Semantic({semantic_literal}))",
        f"sem_hits = retrieve(group=G0, rank=Ranking(expression=sem), limit={n})",
    ]
    if lexical_text:
        lexical_literal = json.dumps(lexical_text, ensure_ascii=False)
        lines.extend(
            [
                f"lex = Expression(Field({field_literal}), Lexical({lexical_literal}))",
                "lex_hits = retrieve(group=G0.where(lex > 0), "
                f"rank=Ranking(expression=lex), limit={n})",
            ]
        )
    else:
        lines.append("lex_hits = []")
    lines.extend(
        [
            f"print({json.dumps(OUTPUT_MARKER)})",
            'print("lexical", len(lex_hits))',
            "for entry in lex_hits:",
            "    print(entry.id)",
            'print("semantic", len(sem_hits))',
            "for entry in sem_hits:",
            "    print(entry.id)",
            'print("END")',
            "",
        ]
    )
    return "\n".join(lines)
