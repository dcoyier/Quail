import csv
import operator
import re
import sqlite3
from collections import Counter

import pytest

from quail.contracts import QuailError
from quail.index import Index
from quail.language.evaluator import Evaluator
from quail.language.expressions import Predicate, constructors
from quail.language.state import State


@pytest.fixture
def analysis(study):
    with study.dataset("notes").source.open("w", newline="") as stream:
        csv.writer(stream).writerows(
            [
                ["id", "body", "amount", "other"],
                ["a", "Parking and front-desk", "1", "1"],
                ["b", "Helpful desk", "2", "02"],
                ["c", "café PARKED agreed", "", ""],
                ["d", "", "not-a-number", ""],
                ["e", "front only", "-1", ""],
                ["f", "front desk", "", ""],
                ["g", "agree", "", ""],
                ["h", "AND literal", "", ""],
            ]
        )
    with Index.build(study.index_path("notes"), study.dataset("notes")) as index:
        evaluator = Evaluator(State(index.path, index.source, "review", study.limits))
        field, random = constructors(evaluator.state)
        evaluator.begin()
        try:
            yield evaluator, field, random
        finally:
            evaluator.close()


def test_construction_is_inert_and_method_pairs_fail_early(analysis):
    engine, field, _ = analysis
    statements = []
    engine.state.connection.set_trace_callback(statements.append)
    score = field("body").lexical("parking")
    assert isinstance(score > 0, Predicate)
    assert statements == []
    with pytest.raises(QuailError, match="number"):
        field("body").length().lower()
    with pytest.raises(QuailError, match="stored"):
        field("body").lower().lexical("parking")
    with pytest.raises(QuailError, match="stored"):
        field("id").semantic("a")
    with pytest.raises(QuailError, match="Unknown field"):
        field("missing")


def test_retrieve_preserves_the_full_source_column_allowance(analysis):
    engine, _, _ = analysis
    engine.state.connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 4)
    rows = engine.retrieve(limit=2)
    assert dict(rows[0]) == {
        "id": "a",
        "body": "Parking and front-desk",
        "amount": "1",
        "other": "1",
    }
    assert rows[1]["body"] == "Helpful desk"


def test_absence_numeric_conversion_and_python_identity(analysis):
    engine, field, _ = analysis
    amount = field("amount")
    assert (amount is None) is False
    assert engine.values(amount.number()) == [1, 2, None, None, -1, None, None, None]
    assert engine.count(amount != "1") == 3
    assert engine.count(~(amount == "1")) == 7
    assert engine.count(amount == None) == 4  # noqa: E711 -- symbolic comparison
    assert engine.count(amount.length() == 0) == 0
    assert engine.count(amount == field("other")) == 1
    assert engine.count(amount.number() == field("other").number()) == 2
    assert engine.count(amount.isin([None, 1, "not-a-number"])) == 6
    assert engine.count(amount.isin([])) == 0
    for value in (True, False, amount):
        with pytest.raises(QuailError, match="Predicate"):
            engine.count(value)
    with pytest.raises(QuailError, match="truth"):
        bool(amount > 0)
    with pytest.raises(QuailError, match="iterable"):
        iter(amount)


def test_arithmetic_finite_results_and_reverse_operators(analysis):
    engine, field, _ = analysis
    n = field("amount").number()
    assert engine.values(10 - 2 * n)[:3] == [8, 6, None]
    assert engine.values(n / 0) == [None] * 8
    assert engine.values((n * 1e308) * 1e308) == [None] * 8
    with pytest.raises(QuailError, match="Arithmetic"):
        field("body") + 1


def test_tag_types_grouping_freeze_and_live_entry_mapping(analysis):
    engine, field, _ = analysis
    entries = engine.retrieve()
    literal = [True, 1, 1.0, {"b": 2, "a": [3]}]
    assert engine.tag([entries[0], entries[0]], "codes", literal) == 1
    literal.append("changed")
    saved = field("codes") == [True, 1, 1.0, {"a": [3], "b": 2}]
    assert engine.count(saved) == 1
    assert engine.count(by=field("codes")) == Counter(
        {None: 7, True: 3, ("json", '{"a":[3],"b":2}'): 1}
    )
    read = entries[0]["codes"]
    read.append("cannot mutate tags by reading")
    assert len(entries[0]["codes"]) == 4
    assert engine.tag(entries[1], "codes", []) == 1
    assert sum(engine.count(by=field("codes")).values()) == 10
    assert engine.tag(entries[0], "boolean", True) == 1
    assert engine.count(field("boolean") == 1) == 1
    assert engine.count(by=[field("boolean"), field("codes")])[(True, True)] == 3
    assert entries[1]["boolean"] is None
    assert dict(entries[0])["id"] == "a"
    with pytest.raises(KeyError):
        entries[0]["unknown"]
    with pytest.raises(TypeError):
        entries[0]["body"] = "rewrite"
    with pytest.raises(QuailError, match="source"):
        engine.tag(None, "body", "rewrite")


def test_compound_operations_and_nested_nulls(analysis):
    engine, field, _ = analysis
    entry = engine.retrieve(limit=1)[0]
    engine.tag(entry, "value", ["Ä BC", None, [1, False]])
    value = field("value")
    assert entry[value.text()] == "Ä BC\nnull\n1\nfalse"
    assert entry[value.length()] == 3
    assert entry[value.slice(-2)] == [None, [1, False]]
    assert entry[value.sub("[A-Z]", "x")] == ["Ä xx", None, "1\nfalse"]
    assert engine.count(value.contains(None)) == 1
    engine.tag(entry, "value", {"a": 1, "b": None})
    assert entry[value.length()] == 2
    assert entry[value.lower()] == '{"a":1,"b":null}'
    assert engine.count(value.contains("b")) == 1


def test_regex_flags_unicode_and_pattern_reuse(analysis):
    engine, field, _ = analysis
    body = field("body")
    assert engine.values(body.search("park[a-z]*", re.I))[:3] == ["Parking", None, "PARKED"]
    assert engine.values(body.findall("[a-z]+", re.I))[0] == ["Parking", "and", "front", "desk"]
    assert engine.values(body.upper())[2] == "CAFÉ PARKED AGREED"
    for flags in (re.A, 1 << 20, -1):
        with pytest.raises(QuailError, match="flags"):
            body.search("x", flags)
    for pattern in (r"(x)\1", "(?=x)"):
        with pytest.raises(QuailError, match="RE2"):
            body.search(pattern)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("front-desk", 4),
        ('"front-desk"', 2),
        ('"front desk"', 2),
        ("front:desk", 4),
        ("front_desk", 4),
        ("cafe", 1),
        ("park", 2),
        ("AND", 2),
        ("agreed", 2),
    ],
)
def test_lexical_tokenization_matches_the_stated_query_language(analysis, query, expected):
    engine, field, _ = analysis
    assert engine.count(field("body").lexical(query) > 0) == expected


@pytest.mark.parametrize("query", ['"front', "--:__", '""'])
def test_wordless_or_unclosed_lexical_queries_fail(analysis, query):
    engine, field, _ = analysis
    with pytest.raises(QuailError):
        engine.count(field("body").lexical(query) > 0)


def test_tag_fts_distinguishes_empty_present_and_absent(analysis):
    engine, field, _ = analysis
    entries = engine.retrieve()
    engine.tag(entries[0], "text", "")
    engine.tag(entries[1], "text", [])
    engine.tag(entries[2], "text", "parking")
    scores = engine.values(field("text").lexical("parking"))
    assert scores[:2] == [0, 0]
    assert scores[2] > 0
    assert scores[3:] == [None] * 5
    assert engine.values(field("body").lexical("not-present"))[3] is None


def test_search_reuse_invalidation_and_failed_cell_rollback(analysis):
    engine, field, _ = analysis
    engine.tag(None, "derived", field("body").lower())
    source = field("body").lexical("parking")
    derived = field("derived").lexical("parking")
    source_values = engine.values(source)
    derived_values = engine.values(derived)
    entry = engine.retrieve(rank=derived, limit=1)[0]
    frozen_score = entry.score
    source_table = engine.searches.cache[source._node].table
    engine.commit()
    engine.begin()
    engine.tag(entry, "derived", "changed")
    assert entry[derived] == 0
    assert entry.score == frozen_score
    engine.tag(entry, "temporary", True)
    temporary = field("temporary")
    engine.rollback()
    engine.begin()
    assert engine.values(source) == source_values
    assert engine.searches.cache[source._node].table == source_table
    assert engine.values(derived) == derived_values
    assert entry["derived"] != "changed"
    with pytest.raises(QuailError, match="Unknown field"):
        engine.values(temporary)


def test_tag_call_resolves_all_computed_values_before_writes(analysis):
    engine, field, _ = analysis
    engine.tag(None, "n", 0)
    engine.tag(field("n") < 1, "n", field("n").number() + 1)
    assert engine.values(field("n")) == [1] * 8
    engine.tag(None, "n", None)
    assert "n" not in {item["name"] for item in engine.fields()}
    delta = engine.commit()
    assert len(delta["n"]) == 8 and all(value is None for value in delta["n"].values())


def test_default_order_rank_none_last_and_retrieve_limits(analysis, capsys):
    engine, field, random = analysis
    assert [entry.id for entry in engine.retrieve(rank=-field("amount").number())] == [
        "e",
        "a",
        "b",
        "c",
        "d",
        "f",
        "g",
        "h",
    ]
    assert len(engine.retrieve(limit=10000)) == 8
    assert "clamped" in capsys.readouterr().out
    assert engine.values(field("id"), limit=10000) == list("abcdefgh")
    for value in (True, -1, 1.5):
        with pytest.raises(QuailError, match="integer"):
            engine.retrieve(limit=value)
    for seed in (None, 1, 1.5, "seed", b"seed", bytearray(b"seed")):
        recipe = random(seed)
        first = engine.values(recipe)
        assert all(0 <= item < 1 for item in first)
        assert engine.values(recipe) == first
        if seed is not None:
            assert engine.values(random(seed)) == first


def test_display_and_annotation_loops_do_not_rebuild_per_cell_state(analysis):
    engine, field, _ = analysis
    entries = engine.retrieve()
    statements = []
    engine.state.connection.set_trace_callback(statements.append)
    for entry in entries:
        repr(entry)
        dict(entry)
    assert not statements
    for entry in entries:
        engine.tag(entry, "coded", True)
    assert not any("CREATE " in statement.upper() for statement in statements)
    assert not any("GROUP BY" in statement.upper() for statement in statements)
    assert engine.count(field("coded") == True) == 8  # noqa: E712 -- symbolic comparison


def test_source_scores_do_not_depend_on_candidate_filter(analysis):
    engine, field, _ = analysis
    score = field("body").lexical("parking")
    full = engine.values(score)
    one = engine.retrieve(where=field("id") == "a", rank=score)[0]
    assert one.score == full[0]
    assert one[score] == full[0]
    assert engine.values(field("body").lexical("parking")) == full
    assert len(engine.searches.cache) == 1


def test_symbolic_predicates_reject_chained_truth_tests(analysis):
    _, field, _ = analysis
    for operation in (bool, operator.not_):
        with pytest.raises(QuailError):
            operation(field("amount") > 0)
    with pytest.raises(QuailError):
        _ = 0 < field("amount").number() < 3
