import pytest

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
