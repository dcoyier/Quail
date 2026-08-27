"""OP_SPECS is canonical: docs stay in sync and pipeline validation derives from it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from quail.analysis import (
    AsNumber,
    Expression,
    Field,
    Length,
    QuailSyntaxError,
    Ranking,
    RegexFindAll,
    RegexSearch,
)
from quail.analysis.operations import OP_SPECS, final_pipeline_kind
from quail.analysis.worker.sandbox import validate_quail_code

_DOCS = Path(__file__).resolve().parent.parent / "docs"


def test_every_op_appears_in_api_md_table() -> None:
    api = (_DOCS / "api.md").read_text(encoding="utf-8")
    for kind in OP_SPECS:
        assert f"| `{kind}(" in api, f"{kind} missing from the api.md operations table"


def test_every_op_appears_in_api_draft_md_table() -> None:
    draft = (_DOCS / "api_draft.md").read_text(encoding="utf-8")
    for kind in OP_SPECS:
        assert f"| `{kind}(" in draft, f"{kind} missing from the api_draft.md operations table"


def test_api_draft_recipes_are_valid_quail_code() -> None:
    draft = (_DOCS / "api_draft.md").read_text(encoding="utf-8")
    recipes = []
    for block in re.findall(r"```python\n(.*?)```", draft, flags=re.S):
        stripped = block.strip()
        if "->" in block or stripped.startswith(("class ", "(self", "re.")):
            continue
        recipes.append(block)
    assert recipes, "expected executable recipes in api_draft.md"
    for recipe in recipes:
        validate_quail_code(recipe)


def test_every_op_appears_in_core_md_table() -> None:
    core = (_DOCS / "core.md").read_text(encoding="utf-8")
    for kind in OP_SPECS:
        assert f"| `{kind}` |" in core, f"{kind} missing from the core.md op table"


def test_mismatch_error_names_both_sides() -> None:
    with pytest.raises(QuailSyntaxError, match="needs text; the pipeline here produces a number"):
        Expression(Field("body"), Length(), RegexSearch("x"))


def test_asnumber_after_findall_fails_at_construction() -> None:
    # RegexFindAll produces list[text]; AsNumber can never consume it. The
    # table rejects this at build time instead of mid-exec.
    with pytest.raises(QuailSyntaxError, match="AsNumber"):
        Expression(Field("body"), RegexFindAll(r"\d+"), AsNumber())


def test_final_pipeline_kind_walks_the_table() -> None:
    assert final_pipeline_kind(Expression(Field("body"), Length()).operations) == "number"
    assert (
        final_pipeline_kind(Expression(Field("body"), RegexFindAll("x")).operations)
        == "list_text"
    )


def test_rankable_is_final_kind_number_or_score() -> None:
    Ranking(expression=Expression(Field("body"), Length()))
    with pytest.raises(QuailSyntaxError, match="Rankable"):
        Ranking(expression=Expression(Field("body"), RegexSearch("x")))
