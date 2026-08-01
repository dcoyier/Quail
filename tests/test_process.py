"""quail process warm, clear, reconfigure, and run gate."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from quail.analysis.errors import QuailRuntimeError
from quail.cli import main as cli_main
from quail.config import ConfigError, load_config
from quail.config.models import EmbeddingProfile, SearchWarmConfig
from quail.datasets import active_version, open_core_db
from quail.run import apply_config, assert_search_warm, process_config
from quail.search import open_search_db
from quail.search.cache import get_cached_vector_blob
from quail.search.vectors import text_hash
from quail.search.warm import get_warm_receipt, search_build_fingerprint


class RecordingEmbedder:
    """Deterministic embedder that records batch shapes."""

    def __init__(self, *, dimensions: int = 4) -> None:
        self.dimensions = dimensions
        self.calls: list[tuple[str, ...]] = []

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for index, char in enumerate(text.lower()):
                vector[index % self.dimensions] += float(ord(char) % 31) + 1.0
            vectors.append(vector)
        return vectors


def _write_manifest(
    tmp_path: Path,
    *,
    embedding: bool = True,
    revision: str = "test-v1",
    warm_knobs: str = "",
    extra_rows: str = "",
    embedding_fields: str = "",
    lexical_fields: str = "",
) -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    rows = "id,title,body\ne1,Hello,hydrangea care\ne2,Other,climate notes\n"
    if extra_rows:
        rows += extra_rows
    (data / "notes.csv").write_text(rows, encoding="utf-8")
    search = 'search_database = "data/quail-search.turso"\n'
    embedding_block = ""
    providers = ""
    if embedding:
        fields_line = f"\nfields = {embedding_fields}\n" if embedding_fields else ""
        embedding_block = f"""
[datasets.embedding]
provider = "ollama"
model = "embeddinggemma:latest"
dimensions = 4
revision = "{revision}"{fields_line}
"""
        providers = """
[providers.ollama]
base_url = "http://127.0.0.1:11434"
"""
    lexical_block = ""
    if lexical_fields:
        lexical_block = f"""
[datasets.lexical]
fields = {lexical_fields}
"""
    warm = ""
    if warm_knobs:
        warm = f"""
[search.warm]
{warm_knobs}
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
{warm}
[[datasets]]
id = "notes"
source = "data/notes.csv"
name = "Notes"
{lexical_block}
{embedding_block}
""",
        encoding="utf-8",
    )
    return manifest


def test_process_warms_lexical_and_embeddings(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    config = load_config(manifest)
    fake = RecordingEmbedder(dimensions=4)
    outcome = process_config(config, embedder_factory=lambda _profile: fake)
    assert len(outcome.results) == 1
    result = outcome.results[0]
    assert result.lexical_ready is True
    assert result.embedding_ready is True
    assert result.text_count == 4  # title+body for two entries
    assert result.unique_text_count == 4
    assert result.embedded_batches >= 1
    assert fake.calls

    search = open_search_db(config.search_database)  # type: ignore[arg-type]
    try:
        receipt = get_warm_receipt(
            search,
            workspace_id="local",
            dataset_id="notes",
            version_id=result.version_id,
        )
        assert receipt is not None
        assert receipt.lexical_ready is True
        assert receipt.embedding_ready is True
        assert receipt.build_fingerprint == search_build_fingerprint(
            lexical_fields=config.datasets[0].lexical_fields,
            profile=config.datasets[0].embedding,
        )
        profile = config.datasets[0].embedding
        assert profile is not None
        for text in ("Hello", "hydrangea care", "Other", "climate notes"):
            blob = get_cached_vector_blob(
                search,
                workspace_id="local",
                dataset_id="notes",
                version_id=result.version_id,
                profile_hash=profile.profile_hash(),
                text_hash=text_hash(text),
                dimensions=4,
            )
            assert blob is not None
    finally:
        search.close()


def test_process_second_pass_is_idempotent(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    config = load_config(manifest)
    fake = RecordingEmbedder(dimensions=4)
    first = process_config(config, embedder_factory=lambda _profile: fake)
    calls_after_first = len(fake.calls)
    assert calls_after_first > 0
    second = process_config(config, embedder_factory=lambda _profile: fake)
    assert second.results[0].embedded_batches == 0
    assert len(fake.calls) == calls_after_first
    assert first.results[0].version_id == second.results[0].version_id


def test_run_gate_fails_without_process(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    config = load_config(manifest)
    db = apply_config(config)
    try:
        with pytest.raises(QuailRuntimeError, match="has not been processed"):
            assert_search_warm(db, config)
    finally:
        db.close()


def test_run_gate_passes_after_process(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    config = load_config(manifest)
    process_config(config, embedder_factory=lambda _p: RecordingEmbedder(dimensions=4))
    db = apply_config(config)
    try:
        assert_search_warm(db, config)
    finally:
        db.close()


def test_revision_change_fails_gate_until_reprocess(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, revision="v1")
    config = load_config(manifest)
    process_config(config, embedder_factory=lambda _p: RecordingEmbedder(dimensions=4))

    manifest = _write_manifest(tmp_path, revision="v2")
    config2 = load_config(manifest)
    db = apply_config(config2)
    try:
        with pytest.raises(QuailRuntimeError, match="fingerprint|warm profile|does not match"):
            assert_search_warm(db, config2)
    finally:
        db.close()

    process_config(config2, embedder_factory=lambda _p: RecordingEmbedder(dimensions=4))
    db2 = apply_config(config2)
    try:
        assert_search_warm(db2, config2)
    finally:
        db2.close()


def test_clear_wipes_then_rewarms(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    config = load_config(manifest)
    first = process_config(config, embedder_factory=lambda _p: RecordingEmbedder(dimensions=4))
    version = first.results[0].version_id
    search = open_search_db(config.search_database)  # type: ignore[arg-type]
    try:
        assert (
            get_warm_receipt(search, workspace_id="local", dataset_id="notes", version_id=version)
            is not None
        )
        corpus_row = search.connection.execute(
            """
            SELECT 1 FROM quail_lexical_corpus
            WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
            """,
            ("local", "notes", version),
        ).fetchone()
        assert corpus_row is not None
        vector_count = search.connection.execute(
            """
            SELECT COUNT(*) FROM quail_embedding_vectors
            WHERE workspace_id = ? AND dataset_id = ? AND version_id = ?
            """,
            ("local", "notes", version),
        ).fetchone()
        assert vector_count is not None and int(vector_count[0]) > 0
    finally:
        search.close()

    fake = RecordingEmbedder(dimensions=4)
    cleared = process_config(config, clear=True, embedder_factory=lambda _p: fake)
    assert cleared.results[0].embedded_batches >= 1
    assert fake.calls
    db = apply_config(config)
    try:
        assert_search_warm(db, config)
    finally:
        db.close()


def test_warm_respects_batch_size_and_concurrency(tmp_path: Path) -> None:
    extra = "".join(f"e{i},Title{i},body text {i}\n" for i in range(3, 11))
    manifest = _write_manifest(
        tmp_path,
        warm_knobs="embed_batch_size = 3\nmax_concurrent_embed_requests = 2\n",
        extra_rows=extra,
    )
    config = load_config(manifest)
    assert config.search_warm.embed_batch_size == 3
    assert config.search_warm.max_concurrent_embed_requests == 2
    fake = RecordingEmbedder(dimensions=4)
    outcome = process_config(config, embedder_factory=lambda _p: fake)
    assert outcome.results[0].embedded_batches >= 2
    assert all(1 <= len(call) <= 3 for call in fake.calls)


def test_lexical_only_warm_without_embedding(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, embedding=False)
    config = load_config(manifest)
    assert config.datasets[0].embedding is None
    outcome = process_config(config)
    assert outcome.results[0].lexical_ready is True
    assert outcome.results[0].embedding_ready is False
    assert outcome.results[0].embedded_batches == 0
    db = apply_config(config)
    try:
        assert_search_warm(db, config)
    finally:
        db.close()


def test_failed_rewarm_clears_lexical_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quail.search.warm import require_warm_ready, warm_dataset

    manifest = _write_manifest(tmp_path, embedding=False)
    config = load_config(manifest)
    outcome = process_config(config)
    version_id = outcome.results[0].version_id

    def _failing_ensure(*args: object, **kwargs: object) -> dict[str, int]:
        raise QuailRuntimeError("simulated mid-warm failure")

    monkeypatch.setattr(
        "quail.search.warm.warm.warm_entry_segments",
        _failing_ensure,
    )

    db = open_core_db(config.database)
    search = open_search_db(config.search_database)  # type: ignore[arg-type]
    try:
        with pytest.raises(QuailRuntimeError, match="simulated mid-warm failure"):
            warm_dataset(
                db,
                search,
                workspace_id="local",
                dataset_id="notes",
                version_id=version_id,
                profile=None,
                warm=config.search_warm,
                embedder_factory=lambda _p: (_ for _ in ()).throw(RuntimeError("no")),
            )
        receipt = get_warm_receipt(
            search,
            workspace_id="local",
            dataset_id="notes",
            version_id=version_id,
        )
        assert receipt is not None
        assert receipt.lexical_ready is False
        with pytest.raises(QuailRuntimeError, match="Lexical warm is incomplete"):
            require_warm_ready(
                search,
                workspace_id="local",
                dataset_id="notes",
                version_id=version_id,
                profile=None,
            )
    finally:
        search.close()
        db.close()


def test_process_without_search_database_is_apply_only(tmp_path: Path) -> None:
    csv_path = tmp_path / "data" / "notes.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("id,title\ne1,Hello\n", encoding="utf-8")
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        """
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"

[auth]
mode = "unrestricted"
workspace = "local"

[hosting]
bind = "127.0.0.1"
port = 8765

[[datasets]]
id = "notes"
source = "data/notes.csv"
""",
        encoding="utf-8",
    )
    config = load_config(manifest)
    outcome = process_config(config)
    assert outcome.results == ()
    db = open_core_db(config.database)
    try:
        assert active_version(db, "local", "notes") is not None
    finally:
        db.close()


def test_parse_search_warm_rejects_out_of_range(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, warm_knobs="embed_batch_size = 0\n")
    with pytest.raises(ConfigError, match="embed_batch_size"):
        load_config(manifest)


def test_cli_process_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path)
    fake = RecordingEmbedder(dimensions=4)

    def _factory(profile: EmbeddingProfile) -> RecordingEmbedder:
        del profile
        return fake

    monkeypatch.setattr(
        "quail.run.process.process.build_embedding_client",
        lambda profile, providers: _factory(profile),
    )
    cli_main(["process", "--config", str(manifest.resolve())])
    out = capsys.readouterr().out
    assert "quail process: notes" in out
    assert "lexical=yes" in out
    assert "embedding=yes" in out


def test_default_search_warm_config() -> None:
    warm = SearchWarmConfig()
    assert warm.embed_batch_size == 32
    assert warm.max_concurrent_embed_requests == 2


def test_embedding_fields_limits_vectors_not_lexical(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, embedding_fields='["body"]')
    config = load_config(manifest)
    assert config.datasets[0].embedding is not None
    assert config.datasets[0].embedding.fields == ("body",)
    fake = RecordingEmbedder(dimensions=4)
    outcome = process_config(config, embedder_factory=lambda _profile: fake)
    result = outcome.results[0]
    assert result.text_count == 4  # Lexical still sees title+body
    assert result.unique_text_count == 2  # only body values embedded
    embedded = {text for batch in fake.calls for text in batch}
    assert embedded == {"hydrangea care", "climate notes"}
    assert "Hello" not in embedded
    db = open_core_db(config.database)
    try:
        assert_search_warm(db, config)
    finally:
        db.close()


def test_embedding_fields_unknown_raises(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, embedding_fields='["missing"]')
    config = load_config(manifest)
    with pytest.raises(QuailRuntimeError, match="Source fields not present"):
        process_config(config, embedder_factory=lambda _p: RecordingEmbedder())


def test_lexical_fields_limits_warm_segments(tmp_path: Path) -> None:
    from quail.search.lexical.corpus import load_entry_segment_counts, resolve_corpus
    from quail.search import LexicalService

    manifest = _write_manifest(
        tmp_path,
        embedding=False,
        lexical_fields='["body"]',
    )
    config = load_config(manifest)
    assert config.datasets[0].lexical_fields == ("body",)
    outcome = process_config(config)
    result = outcome.results[0]
    assert result.text_count == 2  # only body values for Lexical
    search = open_search_db(config.search_database)  # type: ignore[arg-type]
    try:
        corpus = resolve_corpus(
            search,
            workspace_id="local",
            dataset_id="notes",
            version_id=result.version_id,
        )
        counts = load_entry_segment_counts(
            search, corpus, entry_ids=["e1", "e2"]
        )
        assert counts == {"e1": 1, "e2": 1}
        service = LexicalService(search=search)
        scores = service.lexical_scores_for_entries(
            workspace_id="local",
            dataset_id="notes",
            version_id=result.version_id,
            corpus_by_entry={"e1": "hydrangea care", "e2": "climate notes"},
            query_record={"kind": "LiteralText", "text": "hydrangea"},
            input_aggregation=None,
            target_aggregation=None,
        )
        assert scores["e1"] > 0
        assert scores["e2"] == 0.0
    finally:
        search.close()


def test_lexical_fields_do_not_narrow_unrestricted_embedding(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        lexical_fields='["body"]',
        # embedding.fields omitted → embed every source field
    )
    config = load_config(manifest)
    assert config.datasets[0].lexical_fields == ("body",)
    assert config.datasets[0].embedding is not None
    assert config.datasets[0].embedding.fields is None
    fake = RecordingEmbedder(dimensions=4)
    outcome = process_config(config, embedder_factory=lambda _p: fake)
    result = outcome.results[0]
    assert result.text_count == 2  # Lexical: body only
    assert result.unique_text_count == 4  # embed: title+body
    embedded = {text for batch in fake.calls for text in batch}
    assert embedded == {"Hello", "hydrangea care", "Other", "climate notes"}


def test_lexical_fields_unknown_raises(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, embedding=False, lexical_fields='["missing"]')
    config = load_config(manifest)
    with pytest.raises(QuailRuntimeError, match="Source fields not present"):
        process_config(config)


def test_lexical_fields_parse_rejects_empty(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="fields"):
        load_config(_write_manifest(tmp_path, embedding=False, lexical_fields="[]"))


def test_embedding_fields_parse_rejects_empty(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="fields"):
        load_config(_write_manifest(tmp_path, embedding_fields="[]"))


def test_embedding_fields_change_profile_hash() -> None:
    base = EmbeddingProfile(provider="ollama", model="m", dimensions=4, revision="r1")
    limited = EmbeddingProfile(
        provider="ollama",
        model="m",
        dimensions=4,
        revision="r1",
        fields=("content",),
    )
    assert base.profile_hash() != limited.profile_hash()


def test_lexical_fields_change_search_build_fingerprint(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, embedding=False, lexical_fields='["body"]')
    config = load_config(manifest)
    process_config(config)
    # Edit TOML lexical fields without re-process: run gate must fail.
    edited = load_config(
        _write_manifest(tmp_path, embedding=False, lexical_fields='["title", "body"]')
    )
    db = apply_config(edited)
    try:
        with pytest.raises(QuailRuntimeError, match="fingerprint|lexical"):
            assert_search_warm(db, edited)
    finally:
        db.close()


def test_process_fails_when_lease_held(tmp_path: Path) -> None:
    from quail.run.lease import acquire_deployment_lease

    manifest = _write_manifest(tmp_path, embedding=False)
    config = load_config(manifest)
    with acquire_deployment_lease(config):
        with pytest.raises(QuailRuntimeError, match="deployment lease"):
            process_config(config)


def test_crash_before_warm_keeps_prior_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _write_manifest(tmp_path, embedding=False)
    config = load_config(manifest)
    first = process_config(config)
    prior = first.results[0].version_id

    (tmp_path / "data" / "notes.csv").write_text(
        "id,title,body\ne1,Hello,hydrangea care\ne2,Other,climate notes\ne3,New,row\n",
        encoding="utf-8",
    )
    config2 = load_config(manifest)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise QuailRuntimeError("simulated warm crash")

    monkeypatch.setattr("quail.run.process.process.warm_dataset", _boom)
    with pytest.raises(QuailRuntimeError, match="simulated warm crash"):
        process_config(config2)

    db = open_core_db(config2.database)
    try:
        active = active_version(db, "local", "notes")
        assert active is not None
        assert active.version_id == prior
    finally:
        db.close()


def test_multi_dataset_warm_failure_activates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "a.csv").write_text("id,title\ne1,Alpha\n", encoding="utf-8")
    (data / "b.csv").write_text("id,title\ne1,Beta\n", encoding="utf-8")
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        """
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"
search_database = "data/quail-search.turso"

[auth]
mode = "unrestricted"
workspace = "local"

[hosting]
bind = "127.0.0.1"
port = 8765

[[datasets]]
id = "alpha"
source = "data/a.csv"

[[datasets]]
id = "beta"
source = "data/b.csv"
""",
        encoding="utf-8",
    )
    config = load_config(manifest)
    calls = {"n": 0}

    def _warm_once(*args: object, **kwargs: object) -> object:
        from quail.search.warm import WarmDatasetResult

        calls["n"] += 1
        if calls["n"] >= 2:
            raise QuailRuntimeError("second dataset warm failed")
        return WarmDatasetResult(
            workspace_id=str(kwargs["workspace_id"]),
            dataset_id=str(kwargs["dataset_id"]),
            version_id=str(kwargs["version_id"]),
            lexical_ready=True,
            embedding_ready=False,
            text_count=0,
            unique_text_count=0,
            embedded_batches=0,
            build_fingerprint="test",
        )

    monkeypatch.setattr("quail.run.process.process.warm_dataset", _warm_once)
    with pytest.raises(QuailRuntimeError, match="second dataset warm failed"):
        process_config(config)

    db = open_core_db(config.database)
    try:
        assert active_version(db, "local", "alpha") is None
        assert active_version(db, "local", "beta") is None
    finally:
        db.close()


def test_serve_gate_rejects_changed_csv_without_activating(tmp_path: Path) -> None:
    from quail.run.apply import import_declared_datasets

    manifest = _write_manifest(tmp_path, embedding=False)
    config = load_config(manifest)
    process_config(config)
    db = open_core_db(config.database)
    try:
        prior = active_version(db, "local", "notes")
        assert prior is not None
    finally:
        db.close()

    (tmp_path / "data" / "notes.csv").write_text(
        "id,title,body\ne1,Hello,hydrangea care\ne2,Other,climate notes\ne3,Extra,row\n",
        encoding="utf-8",
    )
    config2 = load_config(manifest)
    db2 = open_core_db(config2.database)
    try:
        refs = import_declared_datasets(config2, db2, activate=False)
        with pytest.raises(QuailRuntimeError, match="is not active"):
            assert_search_warm(db2, config2, refs)
        active = active_version(db2, "local", "notes")
        assert active is not None
        assert active.version_id == prior.version_id
        assert refs[0].version_id != prior.version_id
    finally:
        db2.close()


def test_process_deletes_pin_when_embedding_removed(tmp_path: Path) -> None:
    from quail.search.pin import get_embedding_pin

    manifest = _write_manifest(tmp_path, embedding=True)
    config = load_config(manifest)
    outcome = process_config(
        config, embedder_factory=lambda _p: RecordingEmbedder(dimensions=4)
    )
    version = outcome.results[0].version_id
    search = open_search_db(config.search_database)  # type: ignore[arg-type]
    try:
        assert (
            get_embedding_pin(
                search, workspace_id="local", dataset_id="notes", version_id=version
            )
            is not None
        )
    finally:
        search.close()

    config2 = load_config(_write_manifest(tmp_path, embedding=False))
    process_config(config2)
    search2 = open_search_db(config2.search_database)  # type: ignore[arg-type]
    try:
        assert (
            get_embedding_pin(
                search2, workspace_id="local", dataset_id="notes", version_id=version
            )
            is None
        )
    finally:
        search2.close()
