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
from quail.run import apply_config
from quail.search import (
    SimilarityService,
    get_embedding_pin,
    open_search_db,
    pin_embedding_profile,
)
from quail.search.vectors import pack_unit_vector, unit_vector
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


def test_apply_pins_embedding_profile(tmp_path: Path) -> None:
    manifest = _write_semantic_manifest(tmp_path)
    config = load_config(manifest)
    db = apply_config(config)
    try:
        assert config.search_database is not None
        with open_search_db(config.search_database) as search:
            pin = get_embedding_pin(
                search,
                workspace_id="local",
                dataset_id="notes",
                version_id=db.connection.execute(
                    "SELECT active_version_id FROM quail_datasets WHERE id = ?",
                    ("notes",),
                ).fetchone()[0],
            )
            assert pin is not None
            assert pin.provider == "ollama"
            assert pin.dimensions == 4
            assert pin.revision == "test-v1"
    finally:
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
    with pytest.raises(QuailRuntimeError, match="dimensions"):
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


def test_semantic_without_pin(tmp_path: Path) -> None:
    search = open_search_db(tmp_path / "search.turso")
    service = SimilarityService(search=search, providers=ProvidersConfig())
    with pytest.raises(QuailRuntimeError, match="pinned dataset embedding"):
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


def test_lexical_still_not_wired(tmp_path: Path) -> None:
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

        with pytest.raises(QuailRuntimeError, match="Lexical is not wired"):
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
