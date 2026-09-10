import csv
import math
import struct

import numpy as np
import pytest

from quail import embed
from quail.contracts import QuailError, digest_bytes
from quail.index import Index, pack_vector
from quail.language import semantic
from quail.language.evaluator import Evaluator
from quail.language.expressions import constructors
from quail.language.state import State
from quail.project import EmbeddingConfig


def scalar_cosine(left, right):
    # A test reference only: production uses one vectorized implementation.
    a = struct.unpack(f"<{len(left) // 4}f", left)
    b = struct.unpack(f"<{len(right) // 4}f", right)
    return math.fsum(x / math.hypot(*a) * y / math.hypot(*b) for x, y in zip(a, b, strict=True))


@pytest.fixture
def semantic_analysis(study):
    with study.dataset("notes").source.open("w", newline="") as stream:
        csv.writer(stream).writerows(
            [["id", "body"], ["a", "parking"], ["b", "staff"], ["c", "parking"], ["d", ""]]
        )
    config = EmbeddingConfig("ollama/test", "fixed-v1", "ollama", "test", "http://unused.invalid")
    calls = []
    samples = {"parking": [1, 0, 0], "staff": [0, 1, 0], "query": [1, 1, 0]}

    def raw(configuration, texts):
        calls.append(texts)
        return [samples.get(text, [0, 0, 1]) for text in texts]

    with Index.build(study.index_path("notes"), study.dataset("notes")) as index:
        cache = embed.Cache(index, config, raw=raw)
        engine = Evaluator(
            State(index.path, index.source, "s", study.limits),
            embedding_id=config.identity,
            embed=lambda texts: cache.get(texts).vectors,
        )
        field, _ = constructors(engine.state)
        engine.begin()
        try:
            yield engine, field, calls, index, cache
        finally:
            engine.close()


@pytest.mark.parametrize(
    "vectors",
    [
        [[1, 2, 3], [-4, 5, -6], [1, 0, 0]],
        [[1e-45, 0, 0], [1e-40, -1e-40, 0], [0, 0, 1e-45]],
        [[3e38, 3e38, 0], [-3e38, 0, 3e38], [3e38, 3e38, 3e38]],
    ],
)
def test_cosine_matches_safe_scalar_reference_at_float32_extremes(vectors):
    packed = [pack_vector(value) for value in vectors]
    normalized = semantic.normalize(packed, 3)
    for query in packed:
        scores = semantic.cosine(normalized, semantic.normalize([query], 3))
        assert list(scores) == pytest.approx(
            [scalar_cosine(value, query) for value in packed], abs=1e-5
        )


def test_distinct_values_query_cache_and_entry_score_reuse(semantic_analysis, monkeypatch):
    engine, field, calls, _, _ = semantic_analysis
    scored = []
    original = semantic.cosine

    def score(matrix, query):
        scored.append(len(matrix))
        return original(matrix, query)

    monkeypatch.setattr(semantic, "cosine", score)
    recipe = field("body").semantic("query")
    assert calls == []
    values = engine.values(recipe)
    assert values == pytest.approx([2**-0.5, 2**-0.5, 2**-0.5, None], abs=1e-5)
    assert calls == [["parking", "staff"], ["query"]] and scored == [2]
    resident = next(iter(engine.searches.semantic.matrices.values()))
    engine.commit()
    engine.begin()
    assert engine.count(recipe > 0) == 3
    for entry in engine.retrieve(rank=recipe):
        assert entry[recipe] == entry.score
    assert engine.values(field("body").semantic("query")) == values
    engine.values(field("body").semantic("parking"))
    assert next(iter(engine.searches.semantic.matrices.values())) is resident
    assert calls == [["parking", "staff"], ["query"]] and scored == [2, 2]


def test_streaming_scores_do_not_allocate_a_full_matrix(semantic_analysis, monkeypatch):
    engine, field, _, _, _ = semantic_analysis
    expected = engine.values(field("body").semantic("query"))
    engine.searches.invalidate("body")
    engine.searches.semantic.matrix_budget = 0
    engine.searches.semantic.batch_budget = 300  # Force one vector per scoring batch.
    allocated = []
    original = np.empty

    def allocate(shape, *args, **kwargs):
        allocated.append(shape)
        return original(shape, *args, **kwargs)

    monkeypatch.setattr(semantic.np, "empty", allocate)
    actual = engine.values(field("body").semantic("query"))
    assert actual == pytest.approx(expected, abs=1e-5)
    assert not engine.searches.semantic.matrices and not allocated


def test_tag_rendering_invalidation_and_rollback_preserve_source_cache(semantic_analysis):
    engine, field, calls, _, _ = semantic_analysis
    source = field("body").semantic("query")
    original = engine.values(source)
    source_table = engine.searches.cache[source._node].table
    source_matrix = next(iter(engine.searches.semantic.matrices.values()))
    entries = engine.retrieve()
    engine.tag(entries[0], "derived", [])
    engine.tag(entries[1], "derived", "")
    engine.tag(entries[2], "derived", ["parking", None, {"b": 2, "a": 1}])
    derived = field("derived").semantic("query")
    scores = engine.values(derived)
    assert scores == [None, None, 0.0, None]
    assert calls[-1] == ['parking\nnull\n{"a":1,"b":2}']
    engine.commit()
    engine.begin()
    engine.tag(entries[0], "derived", "parking")
    assert engine.values(derived)[0] == pytest.approx(2**-0.5, abs=1e-5)
    engine.rollback()
    engine.begin()
    assert engine.values(derived) == scores
    assert engine.values(source) == original
    assert engine.searches.cache[source._node].table == source_table
    assert next(iter(engine.searches.semantic.matrices.values())) is source_matrix


def test_matrix_eviction_keeps_one_aggregate_budget(semantic_analysis):
    engine, field, _, _, _ = semantic_analysis
    engine.searches.semantic.matrix_budget = 40  # Two 3-d vectors plus their row IDs.
    source = field("body").semantic("query")
    expected = engine.values(source)
    for name in ("one", "two", "three"):
        engine.tag(None, name, field("body"))
        assert engine.values(field(name).semantic("query")) == expected
        matrices = engine.searches.semantic.matrices
        assert len(matrices) == 1 and sum(matrix.size for matrix in matrices.values()) <= 40
    assert engine.values(source) == expected  # The complete score table survives matrix eviction.
    engine.values(field("body").semantic("staff"))
    assert len(engine.searches.semantic.matrices) == 1


def test_failed_search_can_be_caught_and_retried_without_partial_scores(semantic_analysis):
    engine, field, _, _, cache = semantic_analysis
    original = cache.raw

    def fail_query(config, texts):
        if "query" in texts:
            raise QuailError("provider unavailable")
        return original(config, texts)

    cache.raw = fail_query
    recipe = field("body").semantic("query")
    with pytest.raises(QuailError, match="unavailable"):
        engine.values(recipe)
    assert engine.searches.cache == {}
    tables = engine.state.connection.execute(
        "SELECT name FROM temp.sqlite_master WHERE name LIKE 'scores_%'"
    ).fetchall()
    assert not tables
    cache.raw = original
    assert engine.values(recipe)[0] == pytest.approx(2**-0.5, abs=1e-5)


def test_warmed_field_uses_visible_cache_without_an_exchange(semantic_analysis):
    engine, field, calls, index, cache = semantic_analysis
    cache.get(["parking", "staff", "query"])
    engine.rollback()
    engine.begin()  # Release the older shared read snapshot before consuming cached rows.

    def forbidden(texts):
        pytest.fail("A warm corpus and query must not request embeddings")

    engine.searches.semantic.embed = forbidden
    assert engine.values(field("body").semantic("query"))[0] == pytest.approx(2**-0.5, abs=1e-5)
    assert len(calls) == 1
    assert index.vectors(cache.config.identity, [digest_bytes(b"parking")])
