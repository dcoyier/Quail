"""Small on-disk studies for tests; no network services or benchmark fixtures."""

import pytest

from quail import history, project
from quail.index import Index


@pytest.fixture
def study(tmp_path):
    result = project.initialize(tmp_path / "study")
    source = result.root / "notes.csv"
    source.write_text(
        "id,body,amount\na,Parking is expensive,3\nb,Helpful staff,\n", encoding="utf-8"
    )
    manifest = project.registration_text(result, "notes", source)
    project.atomic_write(result.manifest, manifest.encode())
    return project.load(result.root)


@pytest.fixture
def index(study):
    with Index.build(study.index_path("notes"), study.dataset("notes")) as result:
        yield result


@pytest.fixture
def session(index):
    return project.SessionMetadata("review", "notes", index.source.version, history.now(), "id")


@pytest.fixture
def run_factory(study, index):
    handles = []

    def make(run_id, session_name="review"):
        header = history.RunHeader(
            run_id,
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
        log = history.RunLog(study.session_path(session_name) / "log", header)
        handles.append(log)
        return log

    yield make
    for handle in handles:
        handle.close()


@pytest.fixture
def cell_factory():
    def make(n=1, order=1, tags=None, error=None):
        return history.CellRecord(
            n, order, history.now(), history.now(), "count()", "2\n", error, False, tags or {}
        )

    return make
