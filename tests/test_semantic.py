"""Semantic config, cache, and QueryEngine wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from quail.analysis.engine import QueryEngine
from quail.analysis.errors import QuailRuntimeError
from quail.analysis.exec_host import dispatch_call, exec_script, run_analysis
from quail.analysis.expression import Expression
from quail.analysis.field import Field
from quail.analysis.group import G0
from quail.analysis.operations import Lexical, Semantic
from quail.analysis.ranking import Ranking
from quail.analysis.unit import Unit
from quail.config import ConfigError, load_config
from quail.config.models import EmbeddingProfile, ProvidersConfig
from quail.datasets import import_csv_dataset, open_core_db
from quail.run import process_config
from quail.search import (
    SimilarityService,
    get_embedding_pin,
    open_search_db,
    pin_embedding_profile,
)
from quail.search.cache import get_cached_vector_blob, put_cached_vector
from quail.search.vectors import pack_unit_vector, text_hash, unit_vector
from quail.session import create_session


class FakeEmbedder:
    """Deterministic embedder: bag-of-chars hashed into fixed dims."""

    def __init__(self, *, dimensions: int = 4, wrong_dimensions: int | None = None) -> None:
        self.dimensions = dimensions
        self.wrong_dimensions = wrong_dimensions
        self.calls: list[tuple[str, ...]] = []

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        dims = self.wrong_dimensions if self.wrong_dimensions is not None else self.dimensions
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * dims
            for index, char in enumerate(text.lower()):
                vector[index % dims] += float(ord(char) % 31) + 1.0
            vectors.append(vector)
        return vectors


class KeywordEmbedder:
    """Embedder that separates hydrangea-like vs climate-like texts."""

    def __init__(self, *, dimensions: int = 4) -> None:
        self.dimensions = dimensions
        self.calls: list[tuple[str, ...]] = []

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            vector = [0.0] * self.dimensions
            if any(
                token in lowered
                for token in ("hydrangea", "garden", "petal", "flower", "water", "bloom")
            ):
                vector[0] = 1.0
            if any(token in lowered for token in ("climate", "carbon", "policy", "emission")):
                vector[1] = 1.0
            if sum(vector) == 0.0:
                vector[2] = 1.0
            vectors.append(vector)
        return vectors


def _write_semantic_manifest(tmp_path: Path, *, embedding: bool = True) -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "notes.csv").write_text(
        "id,title,body\ne1,Hello,hydrangea care\ne2,Other,climate notes\n",
        encoding="utf-8",
    )
    embedding_block = ""
    if embedding:
        embedding_block = """
[datasets.embedding]
provider = "ollama"
model = "embeddinggemma:latest"
dimensions = 4
revision = "test-v1"
"""
    providers = ""
    search = ""
    if embedding:
        search = 'search_database = "data/quail-search.turso"\n'
        providers = """
[providers.ollama]
base_url = "http://127.0.0.1:11434"
"""
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        f"""
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"
{search}
[auth]
mode = "unrestricted"
workspace = "local"

[hosting]
bind = "127.0.0.1"
port = 8765
{providers}
[[datasets]]
id = "notes"
source = "data/notes.csv"
name = "Notes"
{embedding_block}
""",
        encoding="utf-8",
    )
    return manifest


def test_config_rejects_unknown_provider(tmp_path: Path) -> None:
    manifest = _write_semantic_manifest(tmp_path)
    text = manifest.read_text(encoding="utf-8").replace(
        'provider = "ollama"', 'provider = "openai"'
    )
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="ollama|openrouter"):
        load_config(manifest)


def test_config_requires_positive_dimensions(tmp_path: Path) -> None:
    manifest = _write_semantic_manifest(tmp_path)
    text = manifest.read_text(encoding="utf-8").replace("dimensions = 4", "dimensions = 0")
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="dimensions"):
        load_config(manifest)


def test_config_rejects_search_path_equal_core(tmp_path: Path) -> None:
    manifest = _write_semantic_manifest(tmp_path)
    text = manifest.read_text(encoding="utf-8").replace(
        'search_database = "data/quail-search.turso"',
        'search_database = "data/quail.turso"',
    )
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="distinct"):
        load_config(manifest)


def test_config_requires_search_when_embedding(tmp_path: Path) -> None:
    manifest = _write_semantic_manifest(tmp_path)
    text = "\n".join(
        line
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if "search_database" not in line
    )
    manifest.write_text(text + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="search_database"):
        load_config(manifest)


def test_config_requires_provider_block(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "notes.csv").write_text("id,body\ne1,hi\n", encoding="utf-8")
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        """
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"
search_database = "data/search.turso"

[auth]
mode = "unrestricted"
workspace = "local"

[hosting]
bind = "127.0.0.1"
port = 8765

[[datasets]]
id = "notes"
source = "data/notes.csv"

[datasets.embedding]
provider = "ollama"
model = "embeddinggemma:latest"
dimensions = 4
revision = "test-v1"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"providers\.ollama"):
        load_config(manifest)


def test_openrouter_requires_env_api_key(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "notes.csv").write_text("id,body\ne1,hi\n", encoding="utf-8")
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        """
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"
search_database = "data/search.turso"

[auth]
mode = "unrestricted"
workspace = "local"

[hosting]
bind = "127.0.0.1"
port = 8765

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
api_key = "sk-raw-secret"

[[datasets]]
id = "notes"
source = "data/notes.csv"

[datasets.embedding]
provider = "openrouter"
model = "text-embedding-3-small"
dimensions = 8
revision = "v1"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="env:"):
        load_config(manifest)


def test_process_pins_embedding_profile(tmp_path: Path) -> None:
    manifest = _write_semantic_manifest(tmp_path)
    config = load_config(manifest)
    process_config(config, embedder_factory=lambda _p: FakeEmbedder(dimensions=4))
    db = open_core_db(config.database)
    try:
        assert config.search_database is not None
        version_id = db.connection.execute(
            "SELECT active_version_id FROM quail_datasets WHERE id = ?",
            ("notes",),
        ).fetchone()[0]
        with open_search_db(config.search_database) as search:
            pin = get_embedding_pin(
                search,
                workspace_id="local",
                dataset_id="notes",
                version_id=version_id,
            )
            assert pin is not None
            assert pin.provider == "ollama"
            assert pin.dimensions == 4
            assert pin.revision == "test-v1"
            fields = search.connection.execute(
                """
                SELECT field_name
                FROM quail_embedding_fields
                WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
                ORDER BY field_name
                """,
                ("local", "notes", version_id),
            ).fetchall()
            assert [str(row[0]) for row in fields] == ["body", "title"]
            segments = search.connection.execute(
                """
                SELECT f.field_name, s.entry_id, s.segment_index
                FROM quail_embedding_segments AS s
                JOIN quail_embedding_fields AS f ON f.field_id = s.field_id
                WHERE f.workspace_id = ? AND f.dataset_id = ? AND f.version_id = ?
                ORDER BY f.field_name, s.entry_id, s.segment_index
                """,
                ("local", "notes", version_id),
            ).fetchall()
            assert segments == [
                ("body", "e1", 0),
                ("body", "e2", 0),
                ("title", "e1", 0),
                ("title", "e2", 0),
            ]
    finally:
        db.close()


def test_engine_uses_warmed_source_semantic_without_dynamic_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _write_semantic_manifest(tmp_path)
    (tmp_path / "data" / "notes.csv").write_text(
        "id,title,body\ne1,Hello,climate notes\ne2,Other,hydrangea care\n",
        encoding="utf-8",
    )
    config = load_config(manifest)
    process_config(config, embedder_factory=lambda _profile: KeywordEmbedder(dimensions=4))
    assert config.search_database is not None
    db = open_core_db(config.database)
    search = open_search_db(config.search_database)
    try:
        session = create_session(db, "local")
        fake = KeywordEmbedder(dimensions=4)
        similarity = SimilarityService(
            search=search,
            providers=ProvidersConfig(),
            embedder_factory=lambda _profile: fake,
        )

        def _reject_dynamic(*_args: object, **_kwargs: object) -> dict[str, float | None]:
            raise AssertionError("warmed source Semantic must not materialize dynamic corpus")

        monkeypatch.setattr(SimilarityService, "semantic_scores_for_entries", _reject_dynamic)

        def driver(engine: QueryEngine, _prints) -> None:
            score = Expression(Field("body"), Semantic("hydrangea"))
            assert dispatch_call(engine, "count", (), {"group": G0.where(score > 0.5)}) == 1
            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "group": G0,
                    "rank": Ranking(expression=score),
                    "limit": 2,
                },
            )
            assert [entry.id for entry in ranked] == ["e2", "e1"]

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            similarity=similarity,
        )
        assert fake.calls == [("hydrangea",)]
    finally:
        search.close()
        db.close()


def test_unregistered_source_semantic_keeps_dynamic_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _write_semantic_manifest(tmp_path)
    text = manifest.read_text(encoding="utf-8").replace(
        'revision = "test-v1"',
        'revision = "test-v1"\nfields = ["body"]',
    )
    manifest.write_text(text, encoding="utf-8")
    config = load_config(manifest)
    process_config(config, embedder_factory=lambda _profile: FakeEmbedder(dimensions=4))
    assert config.search_database is not None
    db = open_core_db(config.database)
    search = open_search_db(config.search_database)
    try:
        session = create_session(db, "local")
        fake = FakeEmbedder(dimensions=4)
        similarity = SimilarityService(
            search=search,
            providers=ProvidersConfig(),
            embedder_factory=lambda _profile: fake,
        )
        dynamic_calls: list[object] = []
        original = SimilarityService.semantic_scores_for_entries

        def _spy_dynamic(*args: object, **kwargs: object) -> dict[str, float | None]:
            dynamic_calls.append(kwargs["corpus_by_entry"])
            return original(*args, **kwargs)

        monkeypatch.setattr(SimilarityService, "semantic_scores_for_entries", _spy_dynamic)

        def driver(engine: QueryEngine, _prints) -> None:
            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "group": G0,
                    "rank": Ranking(expression=Expression(Field("title"), Semantic("Hello"))),
                    "limit": 1,
                },
            )
            assert ranked[0].id == "e1"

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            similarity=similarity,
        )
        assert dynamic_calls
        corpus = dynamic_calls[0]
        assert isinstance(corpus, dict)
        assert corpus.get("e1") == "Hello"
        assert corpus.get("e2") == "Other"
        assert any("Other" in call for call in fake.calls)
    finally:
        search.close()
        db.close()


def test_source_semantic_batches_targets_like_dynamic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quail.search.similarity import similarity as similarity_mod

    manifest = _write_semantic_manifest(tmp_path)
    config = load_config(manifest)
    process_config(config, embedder_factory=lambda _profile: FakeEmbedder(dimensions=4))
    assert config.search_database is not None
    db = open_core_db(config.database)
    search = open_search_db(config.search_database)
    try:
        version_id = str(
            db.connection.execute(
                "SELECT active_version_id FROM quail_datasets WHERE id = ?",
                ("notes",),
            ).fetchone()[0]
        )
        fake = FakeEmbedder(dimensions=4)
        service = SimilarityService(
            search=search,
            providers=ProvidersConfig(),
            embedder_factory=lambda _profile: fake,
        )
        query = {"kind": "LiteralTextList", "texts": ["hydrangea", "climate", "garden"]}
        corpus = {"e1": "hydrangea care", "e2": "climate notes"}
        dynamic_avg = service.semantic_scores_for_entries(
            workspace_id="local",
            dataset_id="notes",
            version_id=version_id,
            corpus_by_entry=corpus,
            query_record=query,
            input_aggregation="avg",
            target_aggregation="avg",
        )
        dynamic_total = service.semantic_scores_for_entries(
            workspace_id="local",
            dataset_id="notes",
            version_id=version_id,
            corpus_by_entry=corpus,
            query_record=query,
            input_aggregation="total",
            target_aggregation="total",
        )
        monkeypatch.setattr(similarity_mod, "_SCORE_TARGET_BATCH", 2)
        distance_sql = 0
        original_execute = search.connection.execute

        def _counting_execute(*args: object, **kwargs: object) -> object:
            nonlocal distance_sql
            sql = str(args[0] if args else kwargs.get("sql", ""))
            if "vector_distance_dot" in sql and "quail_embedding_segments" in sql:
                distance_sql += 1
            return original_execute(*args, **kwargs)

        monkeypatch.setattr(search.connection, "execute", _counting_execute)

        def _source(
            *,
            entry_ids: list[str],
            all_entries: bool,
            input_aggregation: str,
            target_aggregation: str,
        ) -> dict[str, float | None]:
            scored = service.semantic_scores_for_source_entries(
                workspace_id="local",
                dataset_id="notes",
                version_id=version_id,
                entry_ids=entry_ids,
                source_field="body",
                all_entries=all_entries,
                query_record=query,
                input_aggregation=input_aggregation,
                target_aggregation=target_aggregation,
            )
            assert scored is not None
            return scored

        source_avg = _source(
            entry_ids=["e1", "e2"],
            all_entries=True,
            input_aggregation="avg",
            target_aggregation="avg",
        )
        source_total = _source(
            entry_ids=["e1", "e2"],
            all_entries=True,
            input_aggregation="total",
            target_aggregation="total",
        )
        subset = _source(
            entry_ids=["e2"],
            all_entries=False,
            input_aggregation="avg",
            target_aggregation="avg",
        )
        assert distance_sql == 6
        assert source_avg["e1"] == pytest.approx(dynamic_avg["e1"])
        assert source_avg["e2"] == pytest.approx(dynamic_avg["e2"])
        assert source_total["e1"] == pytest.approx(dynamic_total["e1"])
        assert source_total["e2"] == pytest.approx(dynamic_total["e2"])
        assert subset["e2"] == pytest.approx(dynamic_avg["e2"])
        assert "e1" not in subset
    finally:
        search.close()
        db.close()


def test_semantic_ranks_with_fake_embedder(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea care\ne2,climate notes\ne3,hydrangea bloom\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    search = open_search_db(tmp_path / "search.turso")
    try:
        ref = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
        profile = EmbeddingProfile(
            provider="ollama",
            model="fake",
            dimensions=4,
            revision="r1",
        )
        pin_embedding_profile(
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=ref.version_id,
            profile=profile,
        )
        fake = FakeEmbedder(dimensions=4)
        similarity = SimilarityService(
            search=search,
            providers=ProvidersConfig(),
            embedder_factory=lambda _profile: fake,
        )
        session = create_session(db, "ws")

        def driver(engine: QueryEngine, prints) -> None:
            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "unit": Unit("entries", Field("body")),
                    "group": G0,
                    "rank": Ranking(expression=Expression(Field("body"), Semantic("hydrangea"))),
                    "limit": 2,
                },
            )
            prints.write(ranked[0], ranked[1])

        outcome = run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            similarity=similarity,
        )
        assert "hydrangea" in outcome.printed_output
        assert "climate" not in outcome.printed_output.split("\n")[0]
    finally:
        search.close()
        db.close()


def test_semantic_cache_hits(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
    pin_embedding_profile(
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        profile=profile,
    )
    fake = FakeEmbedder(dimensions=4)
    service = SimilarityService(
        search=search,
        providers=ProvidersConfig(),
        embedder_factory=lambda _profile: fake,
    )
    query = {"kind": "LiteralText", "text": "hello"}
    first = service.semantic_score(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus="hello world",
        query_record=query,
        input_aggregation=None,
        target_aggregation=None,
    )
    second = service.semantic_score(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus="hello world",
        query_record=query,
        input_aggregation=None,
        target_aggregation=None,
    )
    assert first is not None and second is not None
    assert abs(first - second) < 1e-6
    # One embed batch for corpus+query texts on the first call; second is cache-only.
    assert len(fake.calls) == 1
    search.close()


def test_semantic_reuses_warm_vectors_when_pin_omits_fields(tmp_path: Path) -> None:
    """Score must key the cache by stored pin hash, not recomputed fields=None hash."""

    search = open_search_db(tmp_path / "search.turso")
    profile = EmbeddingProfile(
        provider="ollama",
        model="fake",
        dimensions=4,
        revision="r1",
        fields=("body",),
    )
    pin_embedding_profile(
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        profile=profile,
    )
    warm_hash = profile.profile_hash()
    omitted_hash = EmbeddingProfile(
        provider="ollama",
        model="fake",
        dimensions=4,
        revision="r1",
    ).profile_hash()
    assert warm_hash != omitted_hash

    corpus = "hydrangea care"
    put_cached_vector(
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        profile_hash=warm_hash,
        text_hash=text_hash(corpus),
        dimensions=4,
        vector=unit_vector([1.0, 0.0, 0.0, 0.0]),
    )

    fake = FakeEmbedder(dimensions=4)
    service = SimilarityService(
        search=search,
        providers=ProvidersConfig(),
        embedder_factory=lambda _profile: fake,
    )
    score = service.semantic_score(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus=corpus,
        query_record={"kind": "LiteralText", "text": "hydrangea"},
        input_aggregation=None,
        target_aggregation=None,
    )
    assert score is not None
    assert len(fake.calls) == 1
    assert fake.calls[0] == ("hydrangea",)
    assert (
        get_cached_vector_blob(
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id="v1",
            profile_hash=omitted_hash,
            text_hash=text_hash(corpus),
            dimensions=4,
        )
        is None
    )
    search.close()


def test_semantic_wrong_dimensions(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
    pin_embedding_profile(
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        profile=profile,
    )
    fake = FakeEmbedder(dimensions=4, wrong_dimensions=8)
    service = SimilarityService(
        search=search,
        providers=ProvidersConfig(),
        embedder_factory=lambda _profile: fake,
    )
    with pytest.raises(QuailRuntimeError, match="dimensions") as raised:
        service.semantic_score(
            workspace_id="ws",
            dataset_id="notes",
            version_id="v1",
            corpus="hello",
            query_record={"kind": "LiteralText", "text": "hello"},
            input_aggregation=None,
            target_aggregation=None,
        )
    assert raised.value.repair_hint is not None
    assert "quail process" in raised.value.repair_hint
    assert "apply" not in raised.value.repair_hint.lower()
    search.close()


def test_semantic_without_pin(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    service = SimilarityService(search=search, providers=ProvidersConfig())
    with pytest.raises(QuailRuntimeError, match="pinned dataset embedding") as raised:
        service.semantic_score(
            workspace_id="ws",
            dataset_id="notes",
            version_id="v1",
            corpus="hello",
            query_record={"kind": "LiteralText", "text": "hello"},
            input_aggregation=None,
            target_aggregation=None,
        )
    assert raised.value.repair_hint is not None
    assert "quail process" in raised.value.repair_hint
    assert "apply" not in raised.value.repair_hint.lower()
    search.close()


def test_lexical_requires_search_database(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,body\ne1,hello\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    with db:

        def driver(engine: QueryEngine, _prints) -> None:
            dispatch_call(
                engine,
                "retrieve",
                (),
                {"unit": Expression(Field("body"), Lexical("hello")), "group": G0},
            )

        with pytest.raises(QuailRuntimeError, match="Lexical search is not configured"):
            run_analysis(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=0,
                driver=driver,
            )


def test_exec_script_semantic_path(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea care\ne2,climate notes\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    search = open_search_db(tmp_path / "search.turso")
    try:
        ref = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
        profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
        pin_embedding_profile(
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=ref.version_id,
            profile=profile,
        )
        fake = FakeEmbedder(dimensions=4)
        similarity = SimilarityService(
            search=search,
            providers=ProvidersConfig(),
            embedder_factory=lambda _profile: fake,
        )
        session = create_session(db, "ws")
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code=(
                "scores = retrieve("
                "Expression(Field('body'), Semantic('hydrangea')), G0, limit=2)\n"
                "print(len(scores))\n"
                "print(scores[0] > scores[1])\n"
            ),
            similarity=similarity,
        )
        assert "2" in outcome.printed_output
        assert "True" in outcome.printed_output
    finally:
        search.close()
        db.close()


def test_turso_cosine_on_packed_unit_blobs(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    left = unit_vector([1.0, 2.0, 3.0, 4.0])
    right = unit_vector([4.0, 3.0, 2.0, 1.0])
    left_blob = pack_unit_vector(left)
    right_blob = pack_unit_vector(right)
    expected = sum(a * b for a, b in zip(left, right, strict=True))
    row = search.connection.execute(
        "SELECT -vector_distance_dot(?, ?)",
        (left_blob, right_blob),
    ).fetchone()
    assert row is not None
    assert abs(float(row[0]) - expected) < 1e-5
    search.close()


def test_finite_score_rejects_null_and_non_finite() -> None:
    from quail.search.similarity.similarity import _finite_score

    assert _finite_score(0.0) == 0.0
    assert _finite_score(1) == 1.0
    for bad in (None, True, False, "1.0", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(QuailRuntimeError, match="not a finite number"):
            _finite_score(bad)


def test_orthogonal_unit_vectors_score_zero(tmp_path: Path) -> None:
    class AxisEmbedder:
        def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
            vectors: list[list[float]] = []
            for text in texts:
                if "alpha" in text:
                    vectors.append([1.0, 0.0, 0.0, 0.0])
                else:
                    vectors.append([0.0, 1.0, 0.0, 0.0])
            return vectors

    search = open_search_db(tmp_path / "search.turso")
    profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
    pin_embedding_profile(
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        profile=profile,
    )
    service = SimilarityService(
        search=search,
        providers=ProvidersConfig(),
        embedder_factory=lambda _profile: AxisEmbedder(),
    )
    score = service.semantic_score(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus="alpha",
        query_record={"kind": "LiteralText", "text": "beta"},
        input_aggregation=None,
        target_aggregation=None,
    )
    assert score == 0.0
    search.close()


def test_non_finite_turso_cosine_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    search = open_search_db(tmp_path / "search.turso")
    profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
    pin_embedding_profile(
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        profile=profile,
    )
    service = SimilarityService(
        search=search,
        providers=ProvidersConfig(),
        embedder_factory=lambda _profile: FakeEmbedder(dimensions=4),
    )
    original_execute = search.connection.execute

    def wrapped_execute(sql: object, parameters: object = ()) -> object:
        result = original_execute(sql, parameters)
        sql_text = " ".join(str(sql).split())
        if "-vector_distance_dot" in sql_text and "FROM" in sql_text:
            rows = result.fetchall()

            class _NanRows:
                def fetchall(self) -> list[tuple[object, ...]]:
                    return [(row[0], row[1], float("nan")) for row in rows]

            return _NanRows()
        return result

    monkeypatch.setattr(search.connection, "execute", wrapped_execute)
    with pytest.raises(QuailRuntimeError, match="not a finite number"):
        service.semantic_score(
            workspace_id="ws",
            dataset_id="notes",
            version_id="v1",
            corpus="hello",
            query_record={"kind": "LiteralText", "text": "hello"},
            input_aggregation=None,
            target_aggregation=None,
        )
    search.close()


def test_unit_vector_rejects_non_finite() -> None:
    with pytest.raises(ValueError, match="finite"):
        unit_vector([1.0, float("nan")])
    with pytest.raises(ValueError, match="finite"):
        unit_vector([1.0, float("inf")])


def test_provider_require_vector_rejects_non_finite() -> None:
    from quail.providers.errors import ProviderError
    from quail.providers.ollama.ollama import _require_vector as ollama_require
    from quail.providers.openrouter.openrouter import _require_vector as openrouter_require

    for require in (ollama_require, openrouter_require):
        with pytest.raises(ProviderError, match="non-finite"):
            require([1.0, float("nan")], 2, label="embedder")
        with pytest.raises(ProviderError, match="non-finite"):
            require([1.0, float("inf")], 2, label="embedder")
        with pytest.raises(ProviderError, match="dimensions") as raised:
            require([1.0], 2, label="embedder")
        assert raised.value.repair_hint is not None
        assert "quail process" in raised.value.repair_hint
        assert "apply" not in raised.value.repair_hint.lower()


def test_least_similar_avg_order_bottom(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\n"
        "e1,hydrangea garden bloom petals\n"
        "e2,climate policy carbon emissions\n"
        "e3,hydrangea care watering tips\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    search = open_search_db(tmp_path / "search.turso")
    try:
        ref = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
        profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
        pin_embedding_profile(
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=ref.version_id,
            profile=profile,
        )
        fake = KeywordEmbedder(dimensions=4)
        similarity = SimilarityService(
            search=search,
            providers=ProvidersConfig(),
            embedder_factory=lambda _profile: fake,
        )
        session = create_session(db, "ws")
        sentences = [
            "hydrangea bloom",
            "garden petals",
            "watering hydrangea",
            "flower care",
        ]

        def driver(engine: QueryEngine, prints) -> None:
            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "unit": Unit("entries", Field("body")),
                    "group": G0,
                    "rank": Ranking(
                        expression=Expression(
                            Field("body"),
                            Semantic(sentences, target_aggregation="avg"),
                        )
                    ),
                    "order": "bottom",
                    "limit": 1,
                },
            )
            prints.write(ranked[0])

        outcome = run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            similarity=similarity,
        )
        assert "climate" in outcome.printed_output
        assert "hydrangea" not in outcome.printed_output
        # One embed call for all distinct corpus + target texts.
        assert len(fake.calls) == 1
    finally:
        search.close()
        db.close()


def test_batch_scores_match_single_path(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
    pin_embedding_profile(
        search,
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        profile=profile,
    )
    fake = FakeEmbedder(dimensions=4)
    service = SimilarityService(
        search=search,
        providers=ProvidersConfig(),
        embedder_factory=lambda _profile: fake,
    )
    targets = [f"sentence {index} about hydrangea gardens" for index in range(20)]
    query = {"kind": "LiteralTextList", "texts": targets}
    corpus = {
        "e1": "hydrangea garden bloom",
        "e2": "climate policy notes",
        "e3": "watering flowers outdoors",
    }
    batch = service.semantic_scores_for_entries(
        workspace_id="ws",
        dataset_id="notes",
        version_id="v1",
        corpus_by_entry=corpus,
        query_record=query,
        input_aggregation=None,
        target_aggregation="avg",
    )
    for entry_id, text in corpus.items():
        single = service.semantic_score(
            workspace_id="ws",
            dataset_id="notes",
            version_id="v1",
            corpus=text,
            query_record=query,
            input_aggregation=None,
            target_aggregation="avg",
        )
        batch_score = batch[entry_id]
        single_score = single
        assert batch_score is not None and single_score is not None
        assert abs(batch_score - single_score) < 1e-5
    search.close()


def test_semantic_entry_group_and_entry_list_targets(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea garden bloom\ne2,climate policy notes\ne3,flower petals water\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    search = open_search_db(tmp_path / "search.turso")
    try:
        ref = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
        profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
        pin_embedding_profile(
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=ref.version_id,
            profile=profile,
        )
        fake = KeywordEmbedder(dimensions=4)
        similarity = SimilarityService(
            search=search,
            providers=ProvidersConfig(),
            embedder_factory=lambda _profile: fake,
        )
        session = create_session(db, "ws")

        def driver(engine: QueryEngine, _prints) -> None:
            climate_group = G0.where(Expression(Field("body"), Semantic("climate policy")) > 0.5)
            score = Expression(Field("body"), Semantic(climate_group))
            rows = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "group": G0,
                    "rank": Ranking(expression=score),
                    "order": "top",
                    "limit": 1,
                },
            )
            assert rows[0].id == "e2"

            entries = dispatch_call(engine, "retrieve", (), {"group": G0, "limit": 10})
            by_id = {entry.id: entry for entry in entries}
            list_score = Expression(
                Field("body"),
                Semantic([by_id["e1"], by_id["e3"]], target_aggregation="avg"),
            )
            ranked = dispatch_call(
                engine,
                "retrieve",
                (),
                {
                    "group": G0,
                    "rank": Ranking(expression=list_score),
                    "order": "top",
                    "limit": 2,
                },
            )
            assert ranked[0].id in {"e1", "e3"}

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            similarity=similarity,
        )
    finally:
        search.close()
        db.close()


def test_semantic_duplicate_entry_list_avg_weights(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea garden\ne2,climate policy\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    search = open_search_db(tmp_path / "search.turso")
    try:
        ref = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
        profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
        pin_embedding_profile(
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=ref.version_id,
            profile=profile,
        )
        fake = KeywordEmbedder(dimensions=4)
        similarity = SimilarityService(
            search=search,
            providers=ProvidersConfig(),
            embedder_factory=lambda _profile: fake,
        )
        session = create_session(db, "ws")

        def driver(engine: QueryEngine, _prints) -> None:
            entries = dispatch_call(engine, "retrieve", (), {"group": G0, "limit": 10})
            by_id = {entry.id: entry for entry in entries}
            score = Expression(
                Field("body"),
                Semantic(
                    [by_id["e1"], by_id["e2"], by_id["e1"]],
                    target_aggregation="avg",
                ),
            )
            scored = dispatch_call(
                engine,
                "retrieve",
                (),
                {"unit": score, "group": G0, "limit": 10},
            )
            ids = [entry.id for entry in entries]
            score_by_id = {ids[index]: scored[index] for index in range(len(ids))}
            assert score_by_id["e1"] > score_by_id["e2"]

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            similarity=similarity,
        )
    finally:
        search.close()
        db.close()


class _CountingSimilarity(SimilarityService):
    batch_calls: int = 0

    def semantic_scores_for_entries(self, **kwargs):  # type: ignore[no-untyped-def]
        self.batch_calls += 1
        return super().semantic_scores_for_entries(**kwargs)


def test_engine_semantic_threshold_counts_batch_once(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,body\ne1,hydrangea garden\ne2,climate policy\ne3,garden bloom\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    search = open_search_db(tmp_path / "search.turso")
    try:
        ref = import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
        profile = EmbeddingProfile(provider="ollama", model="fake", dimensions=4, revision="r1")
        pin_embedding_profile(
            search,
            workspace_id="ws",
            dataset_id="notes",
            version_id=ref.version_id,
            profile=profile,
        )
        fake = KeywordEmbedder(dimensions=4)
        similarity = _CountingSimilarity(
            search=search,
            providers=ProvidersConfig(),
            embedder_factory=lambda _profile: fake,
        )
        session = create_session(db, "ws")
        score = Expression(Field("body"), Semantic("garden bloom"))

        def driver(engine: QueryEngine, _prints) -> None:
            assert dispatch_call(engine, "count", (), {"group": G0.where(score > 0.1)}) >= 0
            assert dispatch_call(engine, "count", (), {"group": G0.where(score > 0.2)}) >= 0
            assert dispatch_call(engine, "count", (), {"group": G0.where(score > 0.3)}) >= 0
            assert similarity.batch_calls == 1

        run_analysis(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            driver=driver,
            similarity=similarity,
        )
    finally:
        search.close()
        db.close()
