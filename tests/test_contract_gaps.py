"""Present-value units, nested tag None, and RE2 regex contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail.analysis.engine import QueryEngine
from quail.analysis.errors import QuailSyntaxError
from quail.analysis.exec_host import dispatch_call
from quail.analysis.field import Field
from quail.analysis.group import G0
from quail.analysis.operations import RegexSearch, RegexSub
from quail.analysis.re_helper import ReFacade
from quail.analysis.planner import plan_tag
from quail.analysis.unit import Unit
from quail.datasets import import_csv_dataset, open_core_db
from quail.session import create_session, ensure_scope, resolve_scope
from quail.session.models import FieldCreate, ValueWrite
from quail.session.overlay import commit_overlay


def _seed(tmp_path: Path):
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,title,body\ne1,Hello,hydrangea\ne2,Other,climate\ne3,Empty,notes\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    return db, session


def test_present_unit_filters_before_limit(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        scope = resolve_scope(db, session.id, "notes")
        ensure_scope(db, scope)
        commit_overlay(
            db,
            scope,
            expected_revision=0,
            mutations=[
                FieldCreate("topic"),
                ValueWrite("topic", "e1", "climate"),
                ValueWrite("topic", "e3", "garden"),
            ],
        )
        engine = QueryEngine(db, scope)
        values = dispatch_call(
            engine,
            "retrieve",
            (),
            {"unit": Unit("entries", Field("topic")), "limit": 10},
        )
        assert None not in values
        assert set(values) == {"climate", "garden"}
        assert (
            dispatch_call(
                engine,
                "count",
                (),
                {"unit": Unit("entries", Field("topic"))},
            )
            == 2
        )


def test_tag_rejects_nested_none() -> None:
    with pytest.raises(QuailSyntaxError, match="None"):
        plan_tag(G0, Field("topic"), {"a": None})
    with pytest.raises(QuailSyntaxError, match="None"):
        plan_tag(G0, Field("topic"), [1, None])


def test_regex_sub_literal_and_rejects_lookaround() -> None:
    op = RegexSub("Blue", "Green")
    assert op.kind == "RegexSub"
    with pytest.raises(QuailSyntaxError, match="literal text"):
        RegexSub("Blue", r"\1")
    with pytest.raises(QuailSyntaxError, match="literal text"):
        RegexSub("Blue", "$1")
    with pytest.raises(QuailSyntaxError, match="lookaround|backreference"):
        RegexSearch(r"(?=a)")
    with pytest.raises(QuailSyntaxError, match="backreference"):
        RegexSearch(r"(a)\1")


def test_regex_search_rejects_ascii_and_unicode_flags() -> None:
    re = ReFacade()
    assert RegexSearch("hydrangea", flags=re.I).params["flags"] == re.I
    with pytest.raises(QuailSyntaxError, match="re.A and re.U are not supported"):
        RegexSearch("a", flags=re.A)
    with pytest.raises(QuailSyntaxError, match="re.A and re.U are not supported"):
        RegexSearch("a", flags=re.U)
    with pytest.raises(QuailSyntaxError, match="re.A and re.U are not supported"):
        RegexSearch("a", flags=re.A | re.I)
