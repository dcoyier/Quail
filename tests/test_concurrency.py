"""Concurrency: offload, SearchDb isolation, session lock, wall cancel."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from quail.analysis.admission import configure_execution_slots, reset_execution_slots_for_tests
from quail.analysis.cancel import interrupt_connections_on_cancel, raise_if_cancelled
from quail.analysis.errors import QuailSessionBusyError
from quail.analysis.exec_host import exec_script
from quail.analysis.limits import ExecLimits
from quail.analysis.session_lock import acquire_session_lock, reset_session_locks_for_tests
from quail.datasets import import_csv_dataset, open_core_db
from quail.mcp import create_mcp_server
from quail.search import LexicalService
from quail.search.pool import open_search_pool
from quail.search.runtime import SearchRuntime
from quail.session import create_session


@pytest.fixture(autouse=True)
def _reset_admission() -> None:
    reset_execution_slots_for_tests()
    reset_session_locks_for_tests()
    yield
    reset_execution_slots_for_tests()
    reset_session_locks_for_tests()


def test_session_lock_rejects_overlap() -> None:
    with acquire_session_lock("ses_a"):
        with pytest.raises(QuailSessionBusyError, match="already running"):
            with acquire_session_lock("ses_a"):
                pass
    with acquire_session_lock("ses_a"):
        pass


def test_interrupt_connections_on_cancel_calls_interrupt() -> None:
    calls: list[int] = []

    class _Conn:
        def interrupt(self) -> None:
            calls.append(1)

    cancel = threading.Event()
    with interrupt_connections_on_cancel([_Conn()], cancel):
        cancel.set()
        time.sleep(0.05)
    assert calls == [1]
    # Successful exit must not poison a caller-owned cancel_event.
    # (Event stays set only because this test set it deliberately.)


def test_interrupt_context_does_not_set_cancel_on_success() -> None:
    cancel = threading.Event()

    class _Conn:
        def interrupt(self) -> None:
            raise AssertionError("should not interrupt on success")

    with interrupt_connections_on_cancel([_Conn()], cancel):
        pass
    assert not cancel.is_set()


def test_mcp_session_busy_diagnostic(tmp_path: Path) -> None:
    import asyncio

    from mcp.types import CallToolResult

    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,body\ne1,hello\n", encoding="utf-8")
    db_path = tmp_path / "core.turso"
    with open_core_db(db_path) as db:
        import_csv_dataset(db, "local", "notes", csv_path, activate=True)
    server = create_mcp_server(db_path, tmp_path / "feedback.jsonl", workspace_id="local")

    def as_dict(result: object) -> dict:
        if isinstance(result, CallToolResult):
            assert result.structured_content is not None
            return dict(result.structured_content)
        if isinstance(result, dict):
            return result
        raise AssertionError(type(result))

    async def run() -> None:
        session_id = as_dict(await server.call_tool("quail_start_session", {}))["session_id"]
        held = threading.Event()
        release = threading.Event()

        def hold_session() -> None:
            with acquire_session_lock(session_id):
                held.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=hold_session, daemon=True)
        thread.start()
        assert held.wait(timeout=2)
        result = await server.call_tool(
            "quail_exec",
            {
                "session_id": session_id,
                "dataset_id": "notes",
                "code": "print(1)\n",
            },
        )
        release.set()
        thread.join(timeout=2)
        assert isinstance(result, CallToolResult) and result.is_error
        payload = as_dict(result)
        assert payload["diagnostic"]["stable_error_code"] == "session_busy"

    asyncio.run(run())


def test_raise_if_cancelled_uses_wall_diagnostic() -> None:
    from quail.analysis.limits import STANDARD_LIMITS

    cancel = threading.Event()
    wall = threading.Event()
    cancel.set()
    wall.set()
    with pytest.raises(Exception, match="wall-clock"):
        raise_if_cancelled(cancel, limits=STANDARD_LIMITS, wall_exceeded=wall)


def test_search_pool_bounds_checkouts(tmp_path: Path) -> None:
    path = tmp_path / "search.turso"
    pool = open_search_pool(path, max_size=1)
    first = pool.checkout()
    with pytest.raises(Exception, match="concurrent execution limit"):
        pool.checkout()
    pool.release(first)
    second = pool.checkout()
    pool.release(second)
    pool.close()


def test_mcp_list_during_blocking_exec(tmp_path: Path) -> None:
    """Catalog tool stays responsive while another thread holds a slow exec host path."""

    import asyncio

    from mcp.types import CallToolResult

    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,body\ne1,hello\n", encoding="utf-8")
    db_path = tmp_path / "core.turso"
    with open_core_db(db_path) as db:
        import_csv_dataset(db, "local", "notes", csv_path, activate=True)
    server = create_mcp_server(db_path, tmp_path / "feedback.jsonl", workspace_id="local")

    def as_dict(result: object) -> dict:
        if isinstance(result, CallToolResult):
            assert result.structured_content is not None
            return dict(result.structured_content)
        if isinstance(result, dict):
            return result
        if hasattr(result, "keys"):
            return dict(result)  # type: ignore[arg-type]
        raise AssertionError(f"Unexpected result: {type(result)!r}")

    async def run() -> None:
        payload = as_dict(await server.call_tool("quail_start_session", {}))
        session_id = payload["session_id"]
        listed, counted = await asyncio.gather(
            server.call_tool("quail_list_datasets", {}),
            server.call_tool(
                "quail_exec",
                {
                    "session_id": session_id,
                    "dataset_id": "notes",
                    "code": "print(count(group=G0))\n",
                },
            ),
        )
        assert as_dict(listed)["datasets"][0]["dataset_id"] == "notes"
        assert "1" in as_dict(counted).get("printed_output", "")

    asyncio.run(run())


def test_two_search_connections_do_not_share_handle(tmp_path: Path) -> None:
    path = tmp_path / "search.turso"
    pool = open_search_pool(path, max_size=2)
    a = pool.checkout()
    b = pool.checkout()
    assert a.connection is not b.connection
    pool.release(a)
    pool.release(b)
    pool.close()


def test_exec_script_session_busy(tmp_path: Path) -> None:
    csv_path = tmp_path / "notes.csv"
    csv_path.write_text("id,body\ne1,hello\n", encoding="utf-8")
    db = open_core_db(tmp_path / "core.turso")
    import_csv_dataset(db, "ws", "notes", csv_path, activate=True)
    session = create_session(db, "ws")
    configure_execution_slots(2)
    held = threading.Event()
    release = threading.Event()

    def blocker() -> None:
        with acquire_session_lock(session.id):
            held.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=blocker, daemon=True)
    thread.start()
    assert held.wait(timeout=2)
    with pytest.raises(QuailSessionBusyError):
        exec_script(
            db,
            session_id=session.id,
            dataset_id="notes",
            expected_revision=0,
            code="print(1)\n",
            limits=ExecLimits(wall_seconds=5.0, cpu_seconds=5),
        )
    release.set()
    thread.join(timeout=2)
    db.close()


def test_search_runtime_bind_services(tmp_path: Path) -> None:
    from quail.config.models import ProvidersConfig

    path = tmp_path / "search.turso"
    runtime = SearchRuntime(
        path=path,
        providers=ProvidersConfig(),
        pool=open_search_pool(path, max_size=1),
    )
    with runtime.pool.connection() as search:
        similarity, lexical = runtime.bind_services(search)
        assert isinstance(lexical, LexicalService)
        assert similarity.search is search
        assert lexical.search is search
    runtime.pool.close()
