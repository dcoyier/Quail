import os
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quail import embed, kernel, packs, project, service
from quail.contracts import Limits, QuailError


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


def configured(study):
    study.manifest.write_text(
        study.manifest.read_text() + '\nembed = "ollama/test"\nembed_revision = "v1"\n'
    )
    return project.load(study.root)


def test_cell_budget_excludes_provider_wait_but_keeps_cpu_and_grace(monkeypatch):
    clock, signals, killed = [0.0], [], []
    monkeypatch.setattr(kernel.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(kernel, "_usage", lambda pid: kernel.Usage(0, 0))
    child = SimpleNamespace(
        process=SimpleNamespace(
            pid=1, send_signal=signals.append, kill=lambda: killed.append(True)
        ),
        monitor=SimpleNamespace(latest=kernel.Usage(0, 0)),
        check=lambda: None,
    )
    budget = kernel._CellBudget(child, Limits(wall_seconds=1, cpu_seconds=1))
    with budget.provider_wait():
        clock[0] = 10
        budget.tick()
        assert not signals
    clock[0] = 10.5
    budget.tick()
    assert budget.expired is None
    with budget.provider_wait():
        child.monitor.latest = kernel.Usage(0, 2)
        budget.tick()
        assert signals == [signal.SIGPROF]
        clock[0] += 5
        with pytest.raises(QuailError, match="CPU"):
            budget.tick()
        assert killed


def test_hosted_raw_substitution_uses_shared_cache_and_survives_cell_failure(study, monitored):
    study = configured(study)
    calls = []
    owner_thread = threading.get_ident()

    def raw(config, texts):
        assert threading.get_ident() != owner_thread
        calls.append(texts)
        return [[1, 0] if "Parking" in text else [0, 1] for text in texts]

    with service.open_session(study, "review", embed_fn=raw) as live:
        progress = []
        failed = live.exec(
            'score = Field("body").semantic("Parking"); count(score > 0.5); '
            'tag(None, "lost", True); 1 / 0',
            on_progress=progress.append,
        )
        assert failed.reply.error.type == "ZeroDivisionError" and not failed.reply.tags
        assert progress and "new" in progress[-1]
        before = len(calls)
        assert live.exec("count(score > 0.5)").reply.output == "1\n"
        assert len(calls) == before
        live.reset()
        assert live.exec('count(Field("body").semantic("Parking") > 0.5)').reply.output == "1\n"
        assert len(calls) == before


def test_provider_wait_pauses_only_its_wall_time_and_allows_another_commit(study, monitored):
    study = configured(study)
    entered, committed = threading.Event(), threading.Event()
    errors = []

    def other_session():
        try:
            with service.open_session(study, "other") as other:
                assert entered.wait(timeout=10)
                result = other.exec('tag(None, "independent", True)')
                assert result.reply.error is None
                committed.set()
        except BaseException as error:
            errors.append(error)
            committed.set()

    def raw(config, texts):
        entered.set()
        assert committed.wait(timeout=10)
        time.sleep(0.5)  # Longer than the cell's wall allowance, solely in raw I/O.
        return [[1, 0] for _ in texts]

    with service.open_session(study, "review"):
        pass
    limited = replace(study, limits=replace(study.limits, wall_seconds=0.4))
    worker = threading.Thread(target=other_session)
    worker.start()
    try:
        with service.open_session(limited, "review", embed_fn=raw) as live:
            result = live.exec('count(Field("body").semantic("query") > 0.5)')
            assert result.reply.error is None, result.reply
            assert result.reply.output == "2\n" and not errors
            assert service.fields(study, "notes", "other")[-1]["name"] == "independent"
            # Local Python time after a cached search still consumes the budget.
            late = live.exec(
                'import time; count(Field("body").semantic("query") > 0.5); time.sleep(2)'
            )
            assert "Wall" in late.reply.error.message
    finally:
        entered.set()
        worker.join(timeout=12)
    assert not worker.is_alive() and not errors


@pytest.mark.parametrize("failure", ["deadline", "liveness"])
def test_aborted_provider_wait_cannot_start_an_overlapping_batch(study, monkeypatch, failure):
    study = configured(study)
    release = threading.Event()
    calls = []

    def raw(config, texts):
        calls.append(texts)
        assert release.wait(timeout=5)
        return [[1, 0] for _ in texts]

    def check():
        if failure == "liveness":
            raise QuailError("Kernel process exited")

    monkeypatch.setattr(embed, "ATTEMPT_SECONDS", 0)
    monkeypatch.setattr(embed, "REQUEST_TIMEOUT", 0)
    provider = kernel._Provider(raw)
    budget = SimpleNamespace(provider_wait=nullcontext, tick=check, expired=None)
    config = study.dataset("notes").embedding
    reason = "deadline" if failure == "deadline" else "process exited"
    try:
        with pytest.raises(QuailError, match=reason):
            provider.call(config, ["query"], budget)
        with pytest.raises(QuailError, match="previous provider request"):
            provider.call(config, ["another query"], budget)
        assert len(calls) == 1
    finally:
        release.set()
    assert provider.pending.result(timeout=2) == [[1, 0]]
    budget.tick = lambda: None
    monkeypatch.setattr(embed, "ATTEMPT_SECONDS", 1)
    assert provider.call(config, ["next cell"], budget) == [[1, 0]]
    assert len(calls) == 2


def test_pack_preparation_consumes_wall_budget_without_disabling_a_valid_pack(
    study, monitored, monkeypatch
):
    study = configured(study)
    service.warm(study, "notes", field="body", shard="1/1", embed_fn=lambda c, t: [[1, 0]] * len(t))
    limited = replace(study, limits=replace(study.limits, wall_seconds=5))
    inventory = packs.Packs.inventory

    def elapsed(self, fields):
        result = inventory(self, fields)
        # Advance just this cell's clock at the host storage boundary. Provider
        # latency is covered separately; no sleeping or large fixture is needed.
        live._budget.started -= 6
        return result

    def offline(config, texts):
        pytest.fail("An expired cell must stop before making a provider request")

    monkeypatch.setattr(packs.Packs, "inventory", elapsed)
    with service.open_session(limited, "review", embed_fn=offline) as live:
        result = live.exec(
            'kept = 7; tag(None, "lost", True); count(Field("body").semantic("new query") > 0)'
        )
        assert result.reply.error.type == "QuailError"
        assert "Wall" in result.reply.error.message and not result.reply.tags
        assert not live._embeddings.packs.warnings
        assert live.index.connection.execute("SELECT count(*) FROM ingested").fetchone()[0] == 0
        assert live.exec('(kept, count(), "lost" in [f.name for f in fields()])').reply.output == (
            "(7, 2, False)\n"
        )
