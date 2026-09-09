import os
import sqlite3
import subprocess
import threading
from dataclasses import replace

import pytest

from quail import kernel, project, service
from quail.contracts import QuailError


@pytest.fixture
def monitored():
    try:
        kernel._usage(os.getpid())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        pytest.skip(f"This environment cannot expose process resource counters: {error}")


@pytest.fixture
def live(study, monitored):
    with service.open_session(study, "review") as result:
        yield result


def test_cells_keep_python_and_commit_only_successful_tags(live, study):
    first = live.exec(
        "class Theme:\n    def size(self): return count()\n"
        'theme = Theme()\nx = 7\ntag(None, "topic", "parking")'
    )
    assert first.reply.error is None and sum(map(len, first.reply.tags.values())) == 2
    failed = live.exec('x += 1\ntag(None, "topic", "wrong")\nprint("before error")\n1 / 0')
    assert failed.reply.error.type == "ZeroDivisionError"
    assert failed.reply.tags == {} and "before error" in failed.reply.output
    assert (
        live.exec('(x, theme.size(), values(Field("topic")))').reply.output
        == "(8, 2, ['parking', 'parking'])\n"
    )
    run = live.runtime.run
    assert live.reset().run != run
    assert live.exec('values(Field("topic"))').reply.output == "['parking', 'parking']\n"
    assert live.exec("x").reply.error.type == "NameError"
    live.close()
    with service.open_session(study, "review") as reopened:
        assert reopened.exec('count(by=Field("topic"))').reply.output == "Counter({'parking': 2})\n"


def test_idle_child_replacement_keeps_the_new_cells_bindings(live):
    old = live.runtime.run
    live._child.process.kill()
    live._child.process.wait(timeout=3)
    result = live.exec("kept = 42")
    assert result.kernel_restarted and result.run != old
    assert live.exec("kept").reply.output == "42\n"


def test_cache_failure_after_fsync_recovers_without_rerunning(live, monkeypatch):
    complete = live.index.complete
    attempts = []

    def fail_once(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("injected cache write failure")
        return complete(*args)

    monkeypatch.setattr(live.index, "complete", fail_once)
    result = live.exec('x = 1; tag(None, "checked", True)')
    assert result.reply.error is None
    assert live.index.applied("review").summary.cells == 1
    assert live.exec("x += 1; x").reply.output == "2\n"


def test_uncertain_append_stops_owner_and_recovers_exactly_one_record(live, study, monkeypatch):
    original = live._log.append
    path = live._log.path

    def uncertain(cell):
        original(cell)
        raise OSError("lost acknowledgement of fsync")

    monkeypatch.setattr(live._log, "append", uncertain)
    with pytest.raises(QuailError, match="uncertain"):
        live.exec('tag(None, "checked", True)')
    assert live.runtime.state == "stopped"
    assert len(path.read_bytes().splitlines()) == 2
    with service.open_session(study, "review") as reopened:
        assert reopened.exec('count(Field("checked") == True)').reply.output == "2\n"


def test_disconnected_observer_does_not_cancel_accepted_work(live):
    def lost(identity):
        assert live.runtime.state == "busy"
        raise BrokenPipeError("client left")

    result = live.exec('tag(None, "checked", True)', on_accept=lost)
    assert result.reply.error is None
    assert live.runtime.last_completed.to_record() == {"run": result.run, "cell": result.cell}


def test_kernel_thread_ownership_is_explicit(live):
    errors = []

    def other_thread():
        try:
            live.exec("count()")
        except QuailError as error:
            errors.append(str(error))

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join(timeout=3)
    assert errors and "thread" in errors[0]
    assert live.exec("count()").reply.output == "2\n"


def test_reset_keeps_source_and_applied_limits(live, study):
    applied = live.project.limits
    study.manifest.write_text(study.manifest.read_text() + "\n[kernel]\nmax_limit = 1\n")
    study.dataset("notes").source.write_text("id,body\na,Changed\n")
    live.reset()
    assert live.project.limits == applied
    assert live.exec("count()").reply.output == "2\n"
    assert live.runtime.limits.max_limit == 1000
    live.close()
    with service.open_session(project.load(study.root), "review") as reopened:
        assert reopened.exec("count()").reply.output == "1\n"
        assert reopened.runtime.limits.max_limit == 1


def test_caught_wall_interrupt_cannot_commit(study, monitored):
    with service.open_session(study, "review"):
        pass
    limited = replace(study, limits=replace(study.limits, wall_seconds=0.15))
    with service.open_session(limited, "review") as live:
        result = live.exec(
            "import time\ntry:\n    time.sleep(2)\nexcept BaseException:\n    pass\n"
            'tag(None, "late", True)'
        )
        assert "Wall limit" in result.reply.error.message
        assert not result.reply.tags
        assert not any(
            field["name"] == "late" for field in service.fields(study, "notes", "review")
        )


def test_persistent_resource_monitor_failure_fails_open(study, monkeypatch):
    def unavailable(pid):
        raise OSError("injected unavailable counters")

    monkeypatch.setattr(kernel, "_usage", unavailable)
    with pytest.raises(QuailError, match="monitor kernel resources"):
        service.open_session(study, "review")
    with study.lock("session", "review"), study.lock("dataset", "notes"):
        pass
    assert not list(study.path(".quail", "children").glob("kernel-*"))


def test_child_loss_before_and_after_a_valid_reply(live, monkeypatch):
    before = live.exec('import os; tag(None, "lost", True); os._exit(9)')
    assert before.reply.error is not None and not before.reply.tags
    assert before.kernel_restarted
    child = live._child
    receive = child.receive

    def die_after_reply(tick):
        reply = receive(tick)
        child.process.kill()
        child.process.wait(timeout=3)
        return reply

    monkeypatch.setattr(child, "receive", die_after_reply)
    after = live.exec('tag(None, "kept", True)')
    assert after.reply.error is None and after.kernel_restarted
    assert live.exec('count(Field("kept") == True)').reply.output == "2\n"
    assert live.exec('Field("lost")').reply.error is not None


def test_permanent_cache_failure_identifies_the_committed_cell(live, study, monkeypatch):
    path = live._log.path

    def failed(*args):
        raise sqlite3.OperationalError("injected persistent storage failure")

    monkeypatch.setattr(live.index, "complete", failed)
    monkeypatch.setattr(live.index, "synchronize", failed)
    with pytest.raises(QuailError, match="is committed") as failure:
        live.exec('tag(None, "kept", True)')
    assert str(path) in str(failure.value)
    assert len(path.read_text().splitlines()) == 2
    with service.open_session(study, "review") as reopened:
        assert reopened.exec('count(Field("kept") == True)').reply.output == "2\n"


def test_two_kernels_commit_without_snapshot_upgrade_and_release_wal(live, study):
    with service.open_session(study, "other") as other:
        assert (
            live.exec('body = Field("body"); retrieve(rank=body.lexical("parking"))').reply.error
            is None
        )
        assert other.exec('count(); tag(None, "second", True)').reply.error is None
        assert live.exec('tag(None, "first", True)').reply.error is None
        assert other.exec('tag(None, "rollback", True); 1 / 0').reply.error is not None
        # Every child is idle. Neither its bootstrap nor a completed/failed cell
        # may pin a read snapshot that prevents this ordinary checkpoint.
        live.index.checkpoint()
        assert service.fields(study, "notes", "other")[-1]["name"] == "second"


def test_rss_recovery_preserves_only_committed_state(study, monitored):
    with service.open_session(study, "review"):
        pass
    limited = replace(study, limits=replace(study.limits, memory_mb=128))
    with service.open_session(limited, "review") as live:
        result = live.exec(
            'import time; tag(None, "lost", True); '
            "buffer = bytearray(256 * 1024 * 1024); time.sleep(1)"
        )
        assert result.reply.error is not None and "RSS" in result.reply.error.message
        assert result.kernel_restarted and not result.reply.tags
        assert live.exec("count()").reply.output == "2\n"
