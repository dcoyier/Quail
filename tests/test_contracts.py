import math
import struct

import pytest

from quail.contracts import (
    EMBED_TEXT_BYTES,
    EmbeddingReply,
    EmbeddingRequest,
    ErrorInfo,
    Limits,
    QuailError,
    canonical_json,
    decode_json,
    embedding_identity,
    json_value,
    text_value,
)


def test_canonical_identity_is_independent_of_object_insertion_order():
    assert (
        canonical_json({"revision": "v1", "embed": "m", "format": 1})
        == '{"embed":"m","format":1,"revision":"v1"}'
    )
    assert (
        embedding_identity("ollama/embeddinggemma", "study-model-v1")
        == "sha256:d8beee07e43b772a91bf964eb70e3de43e3a0e52a6c53787efec3ef09af582b6"
    )


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "1e999", '{"x":1,"x":2}', '"\\ud800"'])
def test_decode_rejects_non_json_values_and_ambiguous_objects(raw):
    with pytest.raises(QuailError):
        decode_json(raw)


def test_json_snapshot_preserves_types_and_does_not_alias_python_containers():
    original = [True, 1, 1.0, {"text": "café"}]
    copied = json_value(original)
    original[-1]["text"] = "changed"
    assert canonical_json(copied) == '[true,1,1.0,{"text":"café"}]'
    recursive = []
    recursive.append(recursive)
    with pytest.raises(QuailError, match="recursive"):
        json_value(recursive)


def test_text_rendering_is_shared_by_search_and_values():
    assert text_value(None) is None
    assert text_value([]) == ""
    assert text_value(["a", None, [1, True], {"z": 2, "a": 1}]) == 'a\nnull\n1\ntrue\n{"a":1,"z":2}'


@pytest.mark.parametrize(
    "settings", [{"max_limit": True}, {"cpu_seconds": math.inf}, {"memory_mb": 0}, {"extra": 1}]
)
def test_limits_validate_resolved_configuration(settings):
    with pytest.raises(QuailError):
        Limits.from_record(settings)
    assert Limits.from_record({"max_limit": 0}).max_limit == 0


def test_embedding_control_records_preserve_packed_bytes_and_cell_scope():
    request = EmbeddingRequest(3, ("café\ncomplete text", "second"))
    assert EmbeddingRequest.from_record(request.to_record(), 3) == request
    packed = struct.pack("<2f", 1e-40, 3e38)
    reply = EmbeddingReply(3, (packed, packed))
    assert EmbeddingReply.from_record(reply.to_record(), 3, 2) == reply
    failure = EmbeddingReply(3, error=ErrorInfo("QuailError", "unavailable"))
    assert EmbeddingReply.from_record(failure.to_record(), 3, 2) == failure
    for invalid in (
        request.to_record() | {"n": 2},
        request.to_record() | {"texts": [""]},
        request.to_record() | {"texts": ["a"] * 129},
        request.to_record() | {"texts": ["a" * EMBED_TEXT_BYTES, "b"]},
    ):
        with pytest.raises(QuailError):
            EmbeddingRequest.from_record(invalid, 3)
    for invalid in (
        reply.to_record() | {"n": True},
        reply.to_record() | {"vectors": ["!"] * 2},
        reply.to_record() | {"vectors": ["YQ=="] * 2},
        reply.to_record() | {"vectors": []},
    ):
        with pytest.raises(QuailError):
            EmbeddingReply.from_record(invalid, 3, 2)
