"""Process-wide quail_exec admission and agent session guidance."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from quail.analysis.admission import (
    acquire_execution_slot,
    configure_execution_slots,
    reset_execution_slots_for_tests,
)
from quail.analysis.errors import QuailServerBusyError
from quail.analysis.exec_host import exec_script
from quail.config import ConfigError, load_config
from quail.datasets import import_csv_dataset, open_core_db
from quail.mcp.instructions import clerk_instructions, unrestricted_instructions
from quail.mcp.results import diagnostic_from_exception
from quail.session import create_session, get_session


@pytest.fixture(autouse=True)
def _reset_slots() -> Generator[None, None, None]:
    reset_execution_slots_for_tests()
    yield
    reset_execution_slots_for_tests()


def _seed(tmp_path: Path):
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text(
        "id,title,body\ne1,Hello,hydrangea\ne2,Other,climate\n",
        encoding="utf-8",
    )
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    return db, session


def _write_manifest(tmp_path: Path, *, hosting_extra: str = "") -> Path:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "notes.csv").write_text("id,title\ne1,Hello\n", encoding="utf-8")
    manifest = tmp_path / "quail.toml"
    manifest.write_text(
        f"""
[core]
database = "data/quail.turso"
feedback = "data/feedback.jsonl"

[auth]
mode = "unrestricted"
workspace = "local"

[hosting]
bind = "127.0.0.1"
port = 8765
{hosting_extra}
[[datasets]]
id = "notes"
source = "data/notes.csv"
""",
        encoding="utf-8",
    )
    return manifest


def test_parse_max_concurrent_executions_default(tmp_path: Path) -> None:
    config = load_config(_write_manifest(tmp_path))
    assert config.max_concurrent_executions == 2


def test_parse_max_concurrent_executions_custom(tmp_path: Path) -> None:
    config = load_config(_write_manifest(tmp_path, hosting_extra="max_concurrent_executions = 8\n"))
    assert config.max_concurrent_executions == 8


def test_parse_rejects_out_of_range(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_concurrent_executions"):
        load_config(_write_manifest(tmp_path, hosting_extra="max_concurrent_executions = 0\n"))
    with pytest.raises(ConfigError, match="max_concurrent_executions"):
        load_config(_write_manifest(tmp_path, hosting_extra="max_concurrent_executions = 101\n"))


def test_second_exec_fails_busy_when_slots_full(tmp_path: Path) -> None:
    configure_execution_slots(1)
    db, session = _seed(tmp_path)
    with db:
        with acquire_execution_slot():
            with pytest.raises(QuailServerBusyError, match="concurrent execution limit") as raised:
                exec_script(
                    db,
                    session_id=session.id,
                    dataset_id="notes",
                    expected_revision=0,
                    code="print(count())",
                )
        assert raised.value.repair_hint is not None
        assert "Retry after another quail_exec" in raised.value.repair_hint
        diagnostic = diagnostic_from_exception(raised.value)
        assert diagnostic["stable_error_code"] == "server_busy"
        assert diagnostic["error_class"] == "QuailRuntimeError"
        refreshed = get_session(db, session.id)
        assert refreshed is not None
        assert refreshed.state_revision == 0


def test_slot_released_after_success(tmp_path: Path) -> None:
    configure_execution_slots(1)
    db, session = _seed(tmp_path)
    with db:
        first = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code="print(count())",
        )
        second = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=first.state_revision,
            code="print(count())",
        )
        assert "2" in first.printed_output
        assert "2" in second.printed_output


def test_slot_released_after_failure(tmp_path: Path) -> None:
    configure_execution_slots(1)
    db, session = _seed(tmp_path)
    with db:
        with pytest.raises(Exception):
            exec_script(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=0,
                code="print(does_not_exist)\n",
            )
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code="print(count())",
        )
        assert "2" in outcome.printed_output


def test_instructions_state_session_rules() -> None:
    unrestricted = unrestricted_instructions("local")
    clerk = clerk_instructions()
    for text in (unrestricted, clerk):
        assert "workspace-scoped" in text
        assert "quail_setup again" in text or "quail_start_session again" in text
        assert "one quail_exec in flight per session_id" in text
    assert "quail_export_csv" in unrestricted
    assert "not a download" in unrestricted
    assert "warm-path" in unrestricted
    assert "quail_export_csv" in clerk
    assert "not a download" in clerk
    assert "warm-path" in clerk
