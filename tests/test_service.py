import csv
import json
from dataclasses import replace

import pytest

from quail import history, project, service
from quail.contracts import QuailError


def closed_session(study, name="review", tags=None):
    with service.open_dataset(study, "notes") as index:
        metadata = project.SessionMetadata(
            name, "notes", index.source.version, history.now(), index.source.id_column
        )
        project.atomic_write(study.session_path(name) / "session.toml", metadata.to_toml())
        header = history.RunHeader(
            history.RunLog.new_id(),
            history.now(),
            "tester",
            "test",
            "notes",
            index.source.hash,
            index.source.version,
            index.source.id_column,
            None,
            "audit",
        )
        log = history.RunLog(study.session_path(name) / "log", header)
        log.append(
            history.CellRecord(
                1, 12, history.now(), history.now(), "pass", "", None, False, tags or {}
            )
        )
        log.close()
    return metadata, log.path


def test_info_builds_source_without_allocating_session(study):
    result = service.info(study)
    assert result["sessions"] == []
    assert result["datasets"][0]["rows"] == 2
    assert [field["name"] for field in result["datasets"][0]["fields"]] == ["id", "body", "amount"]
    assert "manual" not in result
    assert "-m quail.cli" in result["interface"]["exec"]
    assert study.session_names() == []


def test_import_quotes_dotted_stem_and_validates_before_publication(tmp_path):
    study = project.initialize(tmp_path)
    source = tmp_path / "notes.backup.csv"
    source.write_text('id,body\na," café "\nb,\n', encoding="utf-8")
    result = service.import_csv(study, source)
    assert result["name"] == "notes.backup"
    assert list(project.load(tmp_path).datasets) == ["notes.backup"]
    before = study.manifest.read_bytes()
    source.write_text("id,body\na,x\na,y\n")
    with pytest.raises(QuailError, match="Duplicate"):
        service.import_csv(project.load(tmp_path), source, name="bad")
    assert study.manifest.read_bytes() == before


def test_stable_ids_replay_orphans_and_restore(study):
    closed_session(study, tags={"topic": {"a": "parking", "b": "staff"}})
    source = study.dataset("notes").source
    source.write_text("id,body,amount\nb,New text,4\nc,New entry,5\n")
    assert service.sessions(study)[0]["orphans"] == 1
    assert service.sessions(study)[0]["source_changed"] is True
    source.write_text("id,body,amount\na,Restored,7\nb,New text,4\n")
    assert service.sessions(study)[0]["orphans"] == 0
    result = service.export(study, "review")
    with open(result["path"], newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0] == {"id": "a", "body": "Restored", "amount": "7", "topic": "parking"}


def test_generated_identity_cannot_silently_follow_reordering(study):
    source = study.dataset("notes").source
    source.write_text("body\nfirst\nsecond\n")
    closed_session(study, tags={"topic": {"row-000001": True}})
    source.write_text("body\nsecond\nfirst\n")
    assert service.sessions(study)[0]["available"] is False
    with pytest.raises(QuailError, match="generated"):
        service.export(study, "review")
    source.write_text("id,body\nrow-000002,second\nrow-000001,first\n")
    assert service.sessions(study)[0]["available"] is True
    assert service.fields(study, "notes", "review")[-1]["present"] == 1


def test_invalid_history_isolated_during_rebuild_and_fork(study):
    _, invalid = closed_session(study, "bad", {"topic": {"a": "bad"}})
    closed_session(study, "good", {"topic": {"b": "good"}})
    with invalid.open("ab") as stream:
        stream.write(b'{"bad":true}\n')
    original = invalid.read_bytes()
    study.dataset("notes").source.write_text("id,body\na,Changed\nb,Kept\n")
    listing = {item["name"]: item for item in service.sessions(study)}
    assert listing["bad"]["available"] is False
    assert listing["good"]["available"] is True
    with pytest.raises(QuailError):
        service.fork(study, "bad", "copy")
    assert not study.session_path("copy").exists()
    with pytest.raises(QuailError):
        service.export(study, "bad")
    assert invalid.read_bytes() == original


def test_source_tag_conflict_does_not_hide_other_sessions(study):
    closed_session(study, "conflict", {"topic": {"a": True}})
    closed_session(study, "clear")
    study.dataset("notes").source.write_text("id,body,topic\na,text,source\n")
    listing = {item["name"]: item for item in service.sessions(study)}
    assert listing["conflict"]["available"] is False
    assert "conflict" in listing["conflict"]["error"]["message"]
    assert listing["clear"]["available"] is True


def test_fork_copies_valid_historical_bytes_without_source(study):
    original, log = closed_session(study, tags={"topic": {"a": {"name": "café"}}})
    study.dataset("notes").source.unlink()
    result = service.fork(study, "review", "second")
    assert result.source_version == original.source_version
    copied = study.session_path("second") / "log" / log.name
    assert copied.read_bytes() == log.read_bytes()
    assert not copied.samefile(log)
    with pytest.raises(QuailError, match="already exists"):
        service.fork(study, "review", "second")


def test_export_preserves_source_and_encodes_compound_tags(study):
    closed_session(study, tags={"z": {"a": [True, None, "café"]}, "a": {"b": "plain"}})
    result = service.export(study, "review")
    with open(result["path"], newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows[0] == ["id", "body", "amount", "a", "z"]
    assert json.loads(rows[1][-1]) == [True, None, "café"]
    assert rows[2][-2] == "plain"
    for path in (
        study.dataset("notes").source,
        study.manifest,
        study.path("sessions", "report.csv"),
        study.path("warm", "report.csv"),
    ):
        with pytest.raises(QuailError):
            service.export(study, "review", path)
    alias = study.path("alias.csv")
    alias.hardlink_to(study.dataset("notes").source)
    with pytest.raises(QuailError):
        service.export(study, "review", alias)


def test_live_view_requires_applied_marker_and_blocks_rebuild(study):
    metadata, _ = closed_session(study)
    with service.open_dataset(study, "notes") as index, study.lock("session", "review"):
        with pytest.raises(QuailError, match="initializing"):
            service.fields(study, "notes", "review")
        index.synchronize(metadata, history.snapshot(study.session_path("review") / "log"))
        assert service.fields(study, "notes", "review")
        study.dataset("notes").source.write_text("id,body\na,new\n")
        with pytest.raises(QuailError, match="active owner"):
            service.fields(study, "notes")


def test_selected_id_changes_force_rebuild(study):
    source = study.dataset("notes").source
    source.write_text("key,body\na,text\n")
    configured = replace(
        study, datasets={"notes": replace(study.dataset("notes"), id_column="key")}
    )
    with service.open_dataset(configured, "notes") as index:
        assert index.source.id_column == "key"
    with service.open_dataset(study, "notes") as index:
        assert index.source.id_column is None


def test_cloned_project_without_sessions_directory(study):
    study.path("sessions").rmdir()
    assert service.sessions(study) == []
    assert not study.path("sessions").exists()
