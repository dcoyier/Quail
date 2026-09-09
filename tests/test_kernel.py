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
