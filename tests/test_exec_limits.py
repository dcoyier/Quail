"""Exec time_window and always-on worker memory enforcement."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from quail.analysis.errors import QuailCpuTimeoutError, QuailRssLimitError, QuailRuntimeError
from quail.analysis.exec_host import exec_script
from quail.analysis.limits import (
    EXTENDED_LIMITS,
    MAX_MEMORY_BYTES,
    STANDARD_LIMITS,
    ExecLimits,
    limits_for_time_window,
    validate_time_window,
)
from quail.analysis.worker.client import run_worker_script
from quail.analysis.worker.protocol import ApiCall
from quail.datasets import import_csv_dataset, open_core_db
from quail.mcp.results import diagnostic_from_exception
from quail.session import create_session, get_session


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


def test_limits_for_time_window_defaults() -> None:
    assert validate_time_window(None) == "standard"
    assert limits_for_time_window(None) == STANDARD_LIMITS
    assert limits_for_time_window("standard") == STANDARD_LIMITS
    assert limits_for_time_window("extended") == EXTENDED_LIMITS
    assert STANDARD_LIMITS.wall_seconds == 30.0
    assert STANDARD_LIMITS.cpu_seconds == 15
    assert EXTENDED_LIMITS.wall_seconds == 100.0
    assert EXTENDED_LIMITS.cpu_seconds == 60
    assert STANDARD_LIMITS.max_memory_bytes == MAX_MEMORY_BYTES
    assert MAX_MEMORY_BYTES == 256 * 1024 * 1024


def test_wall_timeout_kills_blocking_host_call() -> None:
    def on_api_call(call: ApiCall) -> object:
        del call
        time.sleep(1.0)
        return 0

    with pytest.raises(QuailRuntimeError, match="wall-clock deadline") as raised:
        run_worker_script(
            "print(count())",
            on_api_call=on_api_call,
            limits=ExecLimits(wall_seconds=0.25, cpu_seconds=30),
        )
    assert raised.value.repair_hint is not None
    assert raised.value.repair_hint.startswith("Potential routes for revision:")
    assert ".where" in raised.value.repair_hint
    assert 'time_window="extended"' in raised.value.repair_hint
    assert "ranking scores the whole candidate set before limit" in raised.value.repair_hint
    diagnostic = diagnostic_from_exception(raised.value)
    assert diagnostic["repair_hint"] == raised.value.repair_hint


def test_extended_timeout_hint_omits_retry_with_extended() -> None:
    from quail.analysis.limits import time_repair_hint

    standard_hint = time_repair_hint(already_extended=False)
    extended_hint = time_repair_hint(already_extended=True)
    assert 'time_window="extended"' in standard_hint
    assert 'time_window="extended"' not in extended_hint
    assert extended_hint.startswith("Potential routes for revision:")
    assert ".where" in extended_hint
    assert EXTENDED_LIMITS.already_extended is True
    assert STANDARD_LIMITS.already_extended is False


def test_cpu_timeout_kills_busy_loop(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        with pytest.raises(QuailRuntimeError, match="CPU-time limit") as raised:
            exec_script(
                db,
                session_id=session.id,
                dataset_id="notes",
                expected_revision=0,
                code="x = 0\nwhile True:\n    x = x + 1\n",
                limits=ExecLimits(wall_seconds=30.0, cpu_seconds=1),
            )
        assert isinstance(raised.value, QuailCpuTimeoutError)
        diagnostic = diagnostic_from_exception(raised.value)
        assert diagnostic["stable_error_code"] == "cpu_timeout"
        assert raised.value.repair_hint is not None
        assert "extended" in raised.value.repair_hint
        refreshed = get_session(db, session.id)
        assert refreshed is not None
        assert refreshed.state_revision == 0


def test_memory_limit_kills_when_rss_exceeds_ceiling() -> None:
    tiny = 8 * 1024 * 1024

    def on_api_call(call: ApiCall) -> object:
        del call
        time.sleep(0.3)
        return 0

    def rss_sampler(_pid: int) -> int:
        return tiny + 1

    with pytest.raises(QuailRuntimeError, match="worker RSS limit") as raised:
        run_worker_script(
            "print(count())",
            on_api_call=on_api_call,
            limits=ExecLimits(wall_seconds=30.0, cpu_seconds=30, max_memory_bytes=tiny),
            rss_sampler=rss_sampler,
        )
    assert isinstance(raised.value, QuailRssLimitError)
    assert raised.value.repair_hint is not None
    assert "materialized" in raised.value.repair_hint
    assert "bytes" in raised.value.repair_hint
    diagnostic = diagnostic_from_exception(raised.value)
    assert diagnostic["repair_hint"] == raised.value.repair_hint
    assert diagnostic["stable_error_code"] == "rss_limit"


def test_successful_exec_under_limits(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code="print(count())",
            time_window="standard",
        )
        assert outcome.printed_output == "2\n"


def test_bytes_builtin_is_injected(tmp_path: Path) -> None:
    db, session = _seed(tmp_path)
    with db:
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code='print(len(bytes("ab", "utf-8")))\n',
        )
        assert outcome.printed_output == "2\n"
