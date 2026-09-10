import csv
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from quail import embed, packs, project, service
from quail.contracts import QuailError, digest_bytes
from quail.packs import Header, Shard


@pytest.fixture
def warm_study(study):
    study.manifest.write_text(
        study.manifest.read_text() + '\nembed = "ollama/test"\nembed_revision = "v1"\n'
    )
    with study.dataset("notes").source.open("w", newline="") as stream:
        csv.writer(stream).writerows(
            [
                ["id", "body", "other"],
                ["a", "parking", "parking"],
                ["b", "staff", "other"],
                ["c", "parking", ""],
                ["d", "", "café"],
                ["e", "café", ""],
                ["f", "quiet", ""],
            ]
        )
    return project.load(study.root)


def raw(config, texts):
    return [[len(text), 1] for text in texts]


def offline(config, texts):
    pytest.fail(f"Unexpected provider request: {texts}")


def clone(study, destination):
    shutil.copytree(study.root, destination, ignore=shutil.ignore_patterns(".quail"))
    return project.load(destination)


def contents(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_shards_cover_balanced_ranges_and_compose_without_row_order():
    for count in range(25):
        for total in range(1, 9):
            intervals = [Shard(part, total).bounds(count) for part in range(1, total + 1)]
            assert [i for start, stop in intervals for i in range(start, stop)] == list(
                range(count)
            )
            sizes = [stop - start for start, stop in intervals]
            assert max(sizes) - min(sizes) <= 1
        assert Shard(1, 4).bounds(count)[0] == Shard(1, 2).bounds(count)[0]
        assert Shard(2, 4).bounds(count)[1] == Shard(1, 2).bounds(count)[1]
    for invalid in ("0/2", "3/2", "1/0", "1", "1/-2", "one/2", "1/2/3"):
        with pytest.raises(QuailError):
            Shard.parse(invalid)
    with pytest.raises(QuailError):
        Shard(True, 1)


def test_local_warm_and_reused_shared_part_are_complete_and_do_not_create_sessions(warm_study):
    first = service.warm(warm_study, "notes", field="body", embed_fn=raw)
    assert (first["selected"], first["new"], first["reused"], first["pack"]) == (4, 4, 0, None)
    assert not warm_study.path("warm").exists()
    shared = service.warm(warm_study, "notes", field="body", shard="1/1", embed_fn=offline)
    assert (shared["selected"], shared["new"], shared["reused"]) == (4, 0, 4)
    records = contents(shared["pack"])
    assert len(records) == 5 and records[0]["dims"] == 2
    assert [record["text_hash"] for record in records[1:]] == sorted(
        digest_bytes(text.encode()) for text in ("parking", "staff", "café", "quiet")
    )
    assert shared["estimated_bytes"] == shared["bytes"] == Path(shared["pack"]).stat().st_size
    assert warm_study.session_names() == []


def test_all_fields_deduplicate_and_empty_shards_write_nothing(warm_study):
    result = service.warm(warm_study, "notes", embed_fn=raw)
    assert result["selected"] == result["new"] == 5
    empty = service.warm(warm_study, "notes", field="body", shard="1/8", embed_fn=offline)
    assert empty["selected"] == 0 and empty["pack"] is None
    assert not warm_study.path("warm").exists()
    for field in ("id", "missing", "tag:field"):
        with pytest.raises(QuailError, match="non-ID source"):
            service.warm(warm_study, "notes", field=field, embed_fn=offline)


def test_canonical_plan_hash_and_inventory_assignment_ignore_source_row_order(warm_study):
    with service.open_dataset(warm_study, "notes") as index:
        cache = embed.Cache(index, warm_study.dataset("notes").embedding, raw=raw)
        inventory = cache.packs.inventory(("body",))
        before = list(cache.packs.rows(inventory, Shard(1, 4)))
        config = cache.config
        header = Header(
            "notes",
            index.source.version,
            index.source.hash,
            config.descriptor(),
            ("body",),
            2,
            Shard(1, 4),
        )
        assert (
            header.plan_hash
            == "sha256:420cd0afd1a530b89c3470e8750242568ada5ec7d6599e0477efb72857142e94"
        )
        assert cache.packs.inventory(("body",)) is inventory
    source = warm_study.dataset("notes").source
    rows = list(csv.reader(source.read_text().splitlines()))
    with source.open("w", newline="") as stream:
        csv.writer(stream).writerows([rows[0], *reversed(rows[1:])])
    with service.open_dataset(warm_study, "notes") as index:
        cache = embed.Cache(index, config)
        assert list(cache.packs.rows(cache.packs.inventory(("body",)), Shard(1, 4))) == before


def test_partial_merged_packs_and_mixed_shard_counts_work_on_a_cold_clone(warm_study, tmp_path):
    workers = []
    for i, shard in enumerate(("1/4", "2/4", "2/2")):
        worker = clone(warm_study, tmp_path / f"worker-{i}")
        result = service.warm(worker, "notes", field="body", shard=shard, embed_fn=raw)
        workers.append((worker, Path(result["pack"])))
    recipient = clone(warm_study, tmp_path / "recipient")
    # Only the first part has arrived. Hits in that range must already work offline.
    first_worker, first_path = workers[0]
    target = recipient.root / first_path.relative_to(first_worker.root)
    target.parent.mkdir(parents=True)
    shutil.copyfile(first_path, target)
    covered = {row["text_hash"] for row in contents(first_path)[1:]}
    text = next(
        text
        for text in ("parking", "staff", "café", "quiet")
        if digest_bytes(text.encode()) in covered
    )
    with service.open_dataset(recipient, "notes") as index:
        cache = embed.Cache(index, recipient.dataset("notes").embedding, raw=offline)
        assert cache.get([text]).reused == 1
    for worker, path in workers[1:]:
        target = recipient.root / path.relative_to(worker.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    with service.open_dataset(recipient, "notes") as index:
        cache = embed.Cache(index, recipient.dataset("notes").embedding, raw=offline)
        result = cache.get(["parking", "staff", "café", "quiet"])
        assert result.reused == 4 and result.created == 0
        assert index.connection.execute("SELECT count(*) FROM ingested").fetchone()[0] == 3


def test_info_does_not_decode_packs_and_known_receipts_skip_decoding(
    warm_study, tmp_path, monkeypatch
):
    service.warm(warm_study, "notes", field="body", shard="1/1", embed_fn=raw)
    recipient = clone(warm_study, tmp_path / "recipient")
    decode = packs.decode_json

    def forbidden(value):
        pytest.fail("This operation must not decode warm packs")

    monkeypatch.setattr(packs, "decode_json", forbidden)
    assert service.info(recipient)["sessions"] == []
    monkeypatch.setattr(packs, "decode_json", decode)
    with service.open_dataset(recipient, "notes") as index:
        config = replace(
            recipient.dataset("notes").embedding,
            base_url="http://new.invalid",
            key_env="UNSET_QUAIL_TEST_KEY",
        )
        assert embed.Cache(index, config).get(["parking"]).reused == 1
    monkeypatch.setattr(packs, "decode_json", forbidden)
    with service.open_dataset(recipient, "notes") as index:
        assert embed.Cache(index, config).get(["staff"]).reused == 1


@pytest.mark.parametrize(
    "damage", ["truncate", "duplicate", "missing", "base64", "dimension", "zero"]
)
def test_invalid_complete_pack_contributes_no_vectors(warm_study, tmp_path, damage):
    result = service.warm(warm_study, "notes", field="body", shard="1/1", embed_fn=raw)
    records = contents(result["pack"])
    if damage == "duplicate":
        records[-1] = records[-2]
    elif damage == "missing":
        records.pop()
    elif damage == "base64":
        records[-1]["vector"] = "not base64!"
    elif damage == "dimension":
        records[0]["dims"] = 3
    elif damage == "zero":
        records[-1]["vector"] = "AAAAAAAAAAA="
    text = "".join(json.dumps(row) + "\n" for row in records)
    if damage == "truncate":
        text = text[:-1]
    Path(result["pack"]).write_text(text)
    recipient = clone(warm_study, tmp_path / "recipient")
    with service.open_dataset(recipient, "notes") as index:
        cache = embed.Cache(index, recipient.dataset("notes").embedding, raw=raw)
        assert cache.get(["a new query"]).created == 1
        assert cache.packs.warnings and "Skipped warm pack" in cache.packs.warnings[0]
        assert index.connection.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1
        assert index.connection.execute("SELECT count(*) FROM ingested").fetchone()[0] == 0


def test_interrupted_valid_ingestion_keeps_batches_without_a_completion_receipt(
    warm_study, tmp_path, monkeypatch
):
    service.warm(warm_study, "notes", field="body", shard="1/1", embed_fn=raw)
    recipient = clone(warm_study, tmp_path / "recipient")
    monkeypatch.setattr(packs, "_BATCH_BYTES", 8)
    with service.open_dataset(recipient, "notes") as index:
        original = index.insert_vectors
        calls = []

        def interrupted(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("injected interruption")
            return original(*args, **kwargs)

        monkeypatch.setattr(index, "insert_vectors", interrupted)
        cache = embed.Cache(index, recipient.dataset("notes").embedding, raw=offline)
        with pytest.raises(RuntimeError, match="interruption"):
            cache.get(["parking"])
        assert index.connection.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1
        assert index.connection.execute("SELECT count(*) FROM ingested").fetchone()[0] == 0
    with service.open_dataset(recipient, "notes") as index:
        result = embed.Cache(index, recipient.dataset("notes").embedding, raw=offline).get(
            ["parking"]
        )
        assert result.created == 0
        assert index.connection.execute("SELECT count(*) FROM vectors").fetchone()[0] == 4
        assert index.connection.execute("SELECT count(*) FROM ingested").fetchone()[0] == 1


def test_changed_file_is_revalidated_and_another_revision_is_skipped(warm_study, tmp_path):
    result = service.warm(warm_study, "notes", field="body", shard="1/1", embed_fn=raw)
    recipient = clone(warm_study, tmp_path / "recipient")
    path = recipient.root / Path(result["pack"]).relative_to(warm_study.root)
    with service.open_dataset(recipient, "notes") as index:
        config = recipient.dataset("notes").embedding
        embed.Cache(index, config, raw=offline).get(["parking"])
        old_receipt = index.ingested(path.relative_to(recipient.root).as_posix())
    path.write_bytes(path.read_bytes()[:-1])
    with service.open_dataset(recipient, "notes") as index:
        cache = embed.Cache(index, config, raw=offline)
        assert cache.get(["parking"]).reused == 1
        assert (
            cache.packs.warnings
            and index.ingested(path.relative_to(recipient.root).as_posix()) == old_receipt
        )
        other = embed.Cache(index, replace(config, revision="v2"), raw=raw)
        assert other.get(["parking"]).created == 1
        assert not other.packs.warnings


def test_size_checks_refuse_large_parts_while_preserving_local_vectors(warm_study, monkeypatch):
    service.warm(warm_study, "notes", field="body", embed_fn=raw)
    with service.open_dataset(warm_study, "notes") as index:
        cache = embed.Cache(index, warm_study.dataset("notes").embedding)
        inventory = cache.packs.inventory(("body",))
        header = cache.packs.header(inventory, cache.config, Shard(1, 1))
        size = header.estimated_bytes(4)
    monkeypatch.setattr(packs, "MAX_PART_BYTES", size - 1)
    with pytest.raises(QuailError, match="Git limit") as error:
        service.warm(warm_study, "notes", field="body", shard="1/1", embed_fn=offline)
    assert "--shard" in error.value.hint and not warm_study.path("warm").exists()
    monkeypatch.setattr(packs, "MAX_PART_BYTES", size + 1)
    monkeypatch.setattr(packs, "WARN_PART_BYTES", size - 1)
    result = service.warm(warm_study, "notes", field="body", shard="1/1", embed_fn=offline)
    assert result["bytes"] == size and result["warnings"]


def test_atomic_replacement_failure_preserves_original_part(warm_study, monkeypatch):
    result = service.warm(warm_study, "notes", field="body", shard="1/1", embed_fn=raw)
    path = Path(result["pack"])
    original = path.read_bytes()

    def fail(source, destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(packs.os, "replace", fail)
    with pytest.raises(OSError, match="replace failure"):
        service.warm(warm_study, "notes", field="body", shard="1/1", embed_fn=offline)
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]
