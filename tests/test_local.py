import os

import pytest

from quail import local
from quail.contracts import QuailError
from quail.local import Request


@pytest.mark.parametrize(
    "change",
    [
        {"op": []},
        {"op": "other"},
        {"code": None},
        {"dataset": 4},
        {"protocol": True},
        {"protocol": 99},
        {"project": "/elsewhere"},
        {"session": "other"},
        {"extra": True},
    ],
)
def test_invalid_local_records_are_rejected_before_admission(study, change):
    request = Request("exec", str(study.root), "review", "pass").to_record()
    request.update(change)
    with pytest.raises(QuailError):
        Request.from_record(request, study, "review")


def test_explicit_close_can_stop_an_incompatible_protocol(study):
    request = Request("close", str(study.root), "review").to_record()
    request["protocol"] = 99
    assert Request.from_record(request, study, "review").operation == "close"


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("private"),
        TimeoutError("slow"),
        FileNotFoundError("starting"),
        ConnectionRefusedError("unreachable"),
    ],
)
def test_unreachable_owner_is_never_replaced(study, monkeypatch, failure):
    endpoint = local._endpoint(study, "review")
    endpoint.parent.mkdir(parents=True)
    endpoint.write_text("owned endpoint")

    def connect(path):
        raise failure

    def start(*args):
        pytest.fail("A connection error must not replace a live owner")

    monkeypatch.setattr(local, "_connect", connect)
    monkeypatch.setattr(local, "_start", start)
    with study.lock("session", "review"), pytest.raises(QuailError):
        local.call(study, "review", "exec", code="count()")
    assert endpoint.read_text() == "owned endpoint"


def test_failed_spawn_closes_startup_descriptors(study, monkeypatch):
    descriptors = []
    pipe = os.pipe

    def tracked_pipe():
        result = pipe()
        descriptors.extend(result)
        return result

    def failed_spawn(*args, **kwargs):
        raise OSError("injected spawn failure")

    monkeypatch.setattr(os, "pipe", tracked_pipe)
    monkeypatch.setattr(local.subprocess, "Popen", failed_spawn)
    with pytest.raises(OSError, match="spawn failure"):
        local._start(study, "review", None, None)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
