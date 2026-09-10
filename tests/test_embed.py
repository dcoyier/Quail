import io
import json
import struct
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from quail import embed, service
from quail.contracts import QuailError, digest_bytes
from quail.index import Index, pack_vector, validate_vector
from quail.project import EmbeddingConfig


@pytest.fixture
def config():
    return EmbeddingConfig(
        "ollama/test-model", "fixed-v1", "ollama", "test-model", "http://example.invalid"
    )


def reply(document):
    return io.BytesIO(json.dumps(document).encode())


@pytest.mark.parametrize(
    "values", [[], [0.0, -0.0], [float("nan")], [float("inf")], [1e39], [1e-50], [True]]
)
def test_invalid_packed_vectors_are_rejected(values):
    with pytest.raises(QuailError):
        pack_vector(values)


@pytest.mark.parametrize("values", [[1e-45, 0.0], [3e38, 3e38], [1.0, -2.0, 3.0]])
def test_safe_norm_accepts_finite_nonzero_float32_extremes(values):
    packed = pack_vector(values)
    assert packed == struct.pack(f"<{len(values)}f", *values)
    assert validate_vector(packed) == len(values)


def test_batch_validation_precedes_any_cache_write(index, config):
    rows = [(digest_bytes(b"a"), pack_vector([1, 0])), (digest_bytes(b"b"), pack_vector([1, 0, 0]))]
    with pytest.raises(QuailError, match="dimensions"):
        index.insert_vectors(config.identity, rows)
    assert index.vector_dimensions(config.identity) is None
    assert index.vectors(config.identity, [item[0] for item in rows]) == {}


def test_concurrent_first_batches_cannot_establish_different_dimensions(index, config):
    barrier = threading.Barrier(2)

    def write(dimension):
        with Index.open(index.path) as owner:
            barrier.wait(timeout=5)
            try:
                owner.insert_vectors(
                    config.identity, [(digest_bytes(b"a"), pack_vector([1.0] * dimension))]
                )
                return dimension
            except QuailError as error:
                assert "dimensions" in str(error)
                return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(write, (2, 3)))
    assert sum(item is not None for item in outcomes) == 1
    assert index.vector_dimensions(config.identity) in outcomes


def test_same_key_race_returns_the_canonical_stored_vector(index, config):
    barrier = threading.Barrier(2)
    text_hash = digest_bytes(b"a")

    def write(values):
        with Index.open(index.path) as owner:
            barrier.wait(timeout=5)
            return owner.insert_vectors(config.identity, [(text_hash, pack_vector(values))])

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(write, ([1, 0], [0, 1])))
    assert sum(item.inserted for item in outcomes) == 1
    assert outcomes[0].vectors == outcomes[1].vectors
    assert index.vectors(config.identity, [text_hash])[text_hash] == outcomes[0].vectors[0]


def test_cache_deduplicates_preserves_order_and_works_offline(index, config):
    calls = []

    def raw(configuration, texts):
        assert not index.connection.in_transaction
        calls.append(texts)
        return [[float(len(text)), 1.0] for text in texts]

    cache = embed.Cache(index, config, raw=raw)
    first = cache.get(["café", "other", "café"])
    assert calls == [["café", "other"]]
    assert first.created == 2 and first.reused == 0
    assert first.vectors[0] == first.vectors[2]
    offline = embed.Cache(index, replace(config, key_env="UNSET_QUAIL_TEST_CREDENTIAL"))
    second = offline.get(["other", "café"])
    assert second.created == 0 and second.reused == 2
    assert second.vectors == (first.vectors[1], first.vectors[0])
    newer = embed.Cache(index, replace(config, revision="fixed-v2"), raw=raw)
    assert newer.get(["café"]).created == 1


def test_cache_returns_concurrent_winner_and_counts_provider_work(index, config):
    winner = pack_vector([0, 1])

    def raw(configuration, texts):
        with Index.open(index.path) as other:
            other.insert_vectors(config.identity, [(digest_bytes(texts[0].encode()), winner)])
        return [[1, 0]]

    result = embed.Cache(index, config, raw=raw).get(["same"])
    assert result.vectors == (winner,) and result.created == 1


def test_provider_batches_preserve_complete_values(index, config, monkeypatch):
    monkeypatch.setattr(embed, "MAX_BATCH_ITEMS", 3)
    monkeypatch.setattr(embed, "MAX_BATCH_BYTES", 8)
    calls = []

    def raw(configuration, texts):
        calls.append(texts)
        return [[1, 0] for _ in texts]

    texts = ["a", "b", "x" * 25, "c", "d", "e"]
    result = embed.Cache(index, config, raw=raw).get(texts)
    assert [value for batch in calls for value in batch] == texts
    assert ["x" * 25] in calls
    assert result.created == len(texts)


def test_source_rebuild_preserves_compatible_vectors(study, config):
    with service.open_dataset(study, "notes") as owner:
        expected = owner.insert_vectors(
            config.identity, [(digest_bytes(b"body"), pack_vector([1, 0]))]
        )
    study.dataset("notes").source.write_text("id,body\na,changed\n")
    with service.open_dataset(study, "notes") as owner:
        assert (
            owner.vectors(config.identity, [digest_bytes(b"body")])[digest_bytes(b"body")]
            == expected.vectors[0]
        )


def test_ollama_sends_complete_text_with_truncation_disabled(config, monkeypatch):
    requests = []

    def request(value, timeout):
        requests.append(value)
        assert 0 < timeout < 60
        return reply({"embeddings": [[1, 0], [0, 1]]})

    monkeypatch.setattr(embed.urllib.request, "urlopen", request)
    texts = ["café\ncomplete text", "second"]
    assert embed.provider(config, texts) == [[1, 0], [0, 1]]
    assert requests[0].full_url == "http://example.invalid/api/embed"
    assert json.loads(requests[0].data) == {
        "model": "test-model",
        "input": texts,
        "truncate": False,
    }


def test_openai_preserves_indexed_response_order(config, monkeypatch):
    config = replace(
        config,
        embed="openai/custom/model",
        dialect="openai",
        model="custom/model",
        key_env="QUAIL_TEST_KEY",
    )
    monkeypatch.setenv("QUAIL_TEST_KEY", "test-only-value")

    def request(value, timeout):
        assert value.get_header("Authorization") == "Bearer test-only-value"
        assert value.full_url.endswith("/embeddings")
        assert json.loads(value.data) == {
            "model": "custom/model",
            "input": ["a", "b"],
            "encoding_format": "float",
        }
        return reply(
            {"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}
        )

    monkeypatch.setattr(embed.urllib.request, "urlopen", request)
    assert embed.provider(config, ["a", "b"]) == [[1, 0], [0, 1]]


@pytest.mark.parametrize(
    "status, attempts", [(400, 1), (401, 1), (403, 1), (422, 1), (429, 3), (500, 3), (503, 3)]
)
def test_http_retries_only_transient_failures(config, monkeypatch, status, attempts):
    calls = []

    def request(value, timeout):
        calls.append(value)
        raise urllib.error.HTTPError(
            value.full_url, status, "test", {}, reply({"error": "rejected"})
        )

    monkeypatch.setattr(embed.urllib.request, "urlopen", request)
    with pytest.raises(QuailError, match=f"HTTP {status}"):
        embed.provider(config, ["a"])
    assert len(calls) == attempts


def test_transport_retry_can_succeed_without_retrying_schema_errors(config, monkeypatch):
    calls = []

    def request(value, timeout):
        calls.append(value)
        if len(calls) == 1:
            raise urllib.error.URLError("temporary transport error")
        return reply({"embeddings": [[1, 0]]})

    monkeypatch.setattr(embed.urllib.request, "urlopen", request)
    assert embed.provider(config, ["a"]) == [[1, 0]] and len(calls) == 2
    calls.clear()

    def invalid(value, timeout):
        calls.append(value)
        return reply({"embeddings": [[True]]})

    monkeypatch.setattr(embed.urllib.request, "urlopen", invalid)
    with pytest.raises(QuailError, match="numbers"):
        embed.provider(config, ["a"])
    assert len(calls) == 1
