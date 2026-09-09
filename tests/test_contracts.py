import math

import pytest

from quail.contracts import (
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
