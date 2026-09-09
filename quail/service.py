"""Plain project operations and the single dataset/session opening path.

Operations own their resources with context managers. A ready Kernel receives
that ownership; failed opens unwind it. No registry, transport, or process-global
project state is needed, and inspection and warming do not load the kernel graph.
"""

from __future__ import annotations

import csv
import os
import shlex
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from quail import history
from quail import project as projects
from quail.contracts import ErrorInfo, JSONObject, QuailError
from quail.index import Applied, Index, hash_source, publish, transaction
from quail.project import Project, SessionMetadata, atomic_write, load_session, sync_directory

if TYPE_CHECKING:
    from quail.kernel import Kernel, Spawn


def _cached(project: Project, dataset: str) -> Index | None:
    path = project.index_path(dataset)
    if not path.is_file():
        return None
    try:
        return Index.open(path)
    except (sqlite3.DatabaseError, KeyError, QuailError):
        return None  # The cache is disposable; rebuilding still validates the source.


def _fresh(index: Index, project: Project, dataset: str, digest: str) -> bool:
    selected = project.dataset(dataset).id_column
    effective = index.source.id_column
    return index.source.hash == digest and (
        selected == effective if selected is not None else effective in {None, "id"}
    )


def _rebuild(project: Project, dataset: str) -> None:
    """The caller holds the exclusive dataset lock through publication."""
    destination = project.index_path(dataset)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rebuild-", dir=destination.parent) as temporary:
        with Index.build(Path(temporary) / "index.quail", project.dataset(dataset)) as replacement:
            previous = _cached(project, dataset)
            if previous is not None:
                with previous:
                    replacement.copy_vectors(previous)
            for name in project.session_names():
                try:
                    session = load_session(project, name)
                    if session.dataset != dataset:
                        continue
                    with project.lock("session", name):
                        replacement.synchronize(
                            session, history.snapshot(project.session_path(name) / "log")
                        )
                except QuailError:
                    # Invalid history or identity affects its session, not source
                    # publication. Listings re-evaluate and report the actual error.
                    continue
            publish(replacement, destination)


@contextmanager
def open_dataset(project: Project, dataset: str) -> Generator[Index, None, None]:
    config = project.dataset(dataset)
    shared = project.lock("dataset", dataset, shared=True)
    index = None
    try:
        shared.acquire()
        digest = hash_source(config)
        index = _cached(project, dataset)
        if index is None or not _fresh(index, project, dataset, digest):
            if index is not None:
                index.close()
                index = None
            shared.close()
            try:
                with project.lock("dataset", dataset):
                    digest = hash_source(config)
                    current = _cached(project, dataset)
                    fresh = current is not None and _fresh(current, project, dataset, digest)
                    if current is not None:
                        current.close()
                    if not fresh:
                        _rebuild(project, dataset)
            except QuailError as error:
                if isinstance(error.__cause__, BlockingIOError):
                    raise QuailError(
                        f"Dataset {dataset!r} needs rebuilding but has an active owner",
                        "Close kernels using this dataset, then retry",
                    ) from error
                raise
            shared.acquire()
            index = Index.open(project.index_path(dataset))
            if not _fresh(index, project, dataset, hash_source(config)):
                raise QuailError("Source changed while opening; retry when it is stable")
        yield index
    finally:
        if index is not None:
            index.close()
        shared.close()


def initialize(directory: Path) -> Project:
    return projects.initialize(directory)


def usage_manual() -> str:
    """The installed canonical manual, with a checkout-relative development fallback."""
    packaged = files("quail").joinpath("data", "USING_QUAIL.md")
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    return (Path(__file__).resolve().parent.parent / "USING_QUAIL.md").read_text(encoding="utf-8")


def import_csv(
    project: Project,
    source: Path,
    *,
    name: str | None = None,
    id_column: str | None = None,
    embed: str | None = None,
    revision: str | None = None,
) -> JSONObject:
    with project.lock("project"):
        project = projects.load(project.root)
        source = project.check_source(source.resolve())
        name = name if name is not None else source.stem
        text = projects.registration_text(
            project,
            name,
            source,
            id_column=id_column,
            embed=embed,
            revision=revision,
        )
        prospective = projects.parse(project.root, text)
        with project.lock("dataset", name):
            with tempfile.TemporaryDirectory(
                prefix="import-", dir=project.path(".quail")
            ) as directory:
                with Index.build(
                    Path(directory) / "index.quail", prospective.dataset(name)
                ) as built:
                    # Complete validation precedes manifest publication. A subsequent
                    # cache-publication failure leaves a usable registration on disk.
                    atomic_write(project.manifest, text.encode("utf-8"))
                    result = _dataset_record(prospective, name, built)
                    publish(built, prospective.index_path(name))
    return result


def _publish_session(
    project: Project, metadata: SessionMetadata, source: SessionMetadata | None = None
) -> None:
    destination = project.session_path(metadata.name)
    if destination.exists():
        raise QuailError(f"Session already exists: {metadata.name!r}")
    with tempfile.TemporaryDirectory(prefix="session-", dir=project.path(".quail")) as directory:
        staging = Path(directory) / "session"
        staging.mkdir()
        logs = staging / "log"
        logs.mkdir()
        if source is not None:
            observed = history.snapshot(project.session_path(source.name) / "log")
            for item in observed.files:
                with (
                    item.path.open("rb") as incoming,
                    (logs / item.path.name).open("xb") as outgoing,
                ):
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
            copied = history.snapshot(logs)
            if copied.hashes != observed.hashes:
                raise QuailError("Source history changed during the fork")
            for _ in history.records(copied, metadata, history.Summary()):
                pass
        sync_directory(logs)
        atomic_write(staging / "session.toml", metadata.to_toml())
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, destination)
        sync_directory(destination.parent)


def fork(project: Project, source: str, destination: str) -> SessionMetadata:
    projects.validate_name(destination, "session")
    with project.lock("project"), project.lock("session", source):
        metadata = load_session(project, source)
        result = replace(metadata, name=destination, created=history.now(), forked_from=source)
        _publish_session(project, result, metadata)
        return result


def open_session(
    project: Project,
    session: str,
    dataset: str | None = None,
    fork_from: str | None = None,
    *,
    spawn: Spawn | None = None,
) -> Kernel:
    from quail.kernel import Kernel

    projects.validate_name(session, "session")
    with ExitStack() as resources:
        # Only creation requires metadata publication. Existing execution holds
        # dataset then session locks, with no project-wide execution bottleneck.
        with ExitStack() as metadata_lock:
            exists = project.session_path(session).exists()
            if not exists:
                metadata_lock.enter_context(project.lock("project"))
                project = projects.load(project.root)
                exists = project.session_path(session).exists()
            if exists:
                metadata = load_session(project, session)
                if fork_from is not None:
                    raise QuailError("An existing session cannot specify fork_from")
                if dataset is not None and dataset != metadata.dataset:
                    raise QuailError("Dataset does not match the session")
                dataset = metadata.dataset
            else:
                original = load_session(project, fork_from) if fork_from is not None else None
                if original is not None:
                    if dataset is not None and dataset != original.dataset:
                        raise QuailError("Dataset does not match the fork source")
                    dataset = original.dataset
                if dataset is None:
                    if len(project.datasets) != 1:
                        raise QuailError(
                            "Choose a dataset", "Use --dataset with one of quail info's datasets"
                        )
                    dataset = next(iter(project.datasets))
            index = resources.enter_context(open_dataset(project, dataset))
            if not exists:
                metadata = SessionMetadata(
                    session, dataset, index.source.version, history.now(), index.source.id_column
                )
                if original is not None:
                    metadata = replace(
                        original, name=session, created=history.now(), forked_from=original.name
                    )
                    with project.lock("session", original.name):
                        _publish_session(project, metadata, original)
                else:
                    _publish_session(project, metadata)
            resources.enter_context(project.lock("session", session))
        snapshot = history.snapshot(project.session_path(session) / "log")
        applied = index.synchronize(metadata, snapshot)
        history.sync_recovered(snapshot)
        return Kernel(project, metadata, index, resources.pop_all(), applied, snapshot, spawn=spawn)


@contextmanager
def _session_view(
    project: Project, index: Index, metadata: SessionMetadata
) -> Generator[Applied, None, None]:
    """Closed sessions synchronize; live owners expose only committed WAL state."""
    lock = project.lock("session", metadata.name)
    try:
        lock.acquire()
    except QuailError as error:
        if not isinstance(error.__cause__, BlockingIOError):
            raise
        with transaction(index.connection):
            applied = index.applied(metadata.name)
            if applied is None or applied.source_version != index.source.version:
                raise QuailError(
                    "Session owner is initializing; committed state is not yet available"
                ) from error
            yield applied
    else:
        try:
            applied = index.synchronize(
                metadata, history.snapshot(project.session_path(metadata.name) / "log")
            )
            with transaction(index.connection):
                yield applied
        finally:
            lock.close()


def fields(project: Project, dataset: str, session: str | None = None) -> list[JSONObject]:
    with open_dataset(project, dataset) as index:
        if session is None:
            return index.fields()
        metadata = load_session(project, session)
        if metadata.dataset != dataset:
            raise QuailError("Dataset does not match the session")
        with _session_view(project, index, metadata):
            return index.fields(session)


def export(project: Project, session: str, out: Path | None = None) -> JSONObject:
    metadata = load_session(project, session)
    destination = project.check_source(
        out.resolve() if out is not None else project.path("exports", session + ".csv")
    )
    if any(
        projects.same_file(destination, item.source) or destination == item.source
        for item in project.datasets.values()
    ):
        raise QuailError("Export cannot replace a registered source CSV")
    with (
        open_dataset(project, metadata.dataset) as index,
        _session_view(project, index, metadata) as applied,
    ):
        tag_fields = [str(item["name"]) for item in index.fields(session) if item["kind"] == "tag"]
        columns = [*index.source.fields, *tag_fields]
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".export-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(columns)
                writer.writerows(index.export_rows(session, tag_fields))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            sync_directory(destination.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {
            "path": str(destination),
            "rows": index.source.rows,
            "columns": len(columns),
            "orphans": applied.orphans,
        }


def _dataset_record(project: Project, name: str, index: Index) -> JSONObject:
    config = project.dataset(name)
    return {
        "name": name,
        "source": str(config.source.relative_to(project.root)),
        "source_version": index.source.version,
        "rows": index.source.rows,
        "id_column": index.source.id_column,
        "generated_ids": index.source.id_column is None,
        "fields": list(index.fields()),
        "embedding": config.embedding.descriptor() if config.embedding else None,
    }


def info(project: Project) -> JSONObject:
    datasets: list[JSONObject] = []
    sessions: list[JSONObject] = []
    metadata: list[SessionMetadata] = []
    for name in project.session_names():
        try:
            metadata.append(load_session(project, name))
        except QuailError as error:
            sessions.append(
                {
                    "name": name,
                    "available": False,
                    "error": ErrorInfo.from_exception(error).to_record(),
                }
            )
    for name in project.datasets:
        selected = [item for item in metadata if item.dataset == name]
        try:
            with open_dataset(project, name) as index:
                datasets.append(_dataset_record(project, name, index))
                for session in selected:
                    try:
                        with _session_view(project, index, session) as applied:
                            sessions.append(
                                {
                                    "name": session.name,
                                    "dataset": name,
                                    "available": True,
                                    "source_version": index.source.version,
                                    "orphans": applied.orphans,
                                    "source_changed": (
                                        applied.summary.last_source_version
                                        or session.source_version
                                    )
                                    != index.source.version,
                                    "history": applied.summary.to_record(),
                                }
                            )
                    except QuailError as error:
                        sessions.append(_unavailable(session, error))
        except (QuailError, OSError, sqlite3.Error) as error:
            datasets.append(
                {
                    "name": name,
                    "available": False,
                    "error": ErrorInfo.from_exception(error).to_record(),
                }
            )
            sessions.extend(_unavailable(session, error) for session in selected)
    for session in metadata:
        if session.dataset not in project.datasets:
            sessions.append(_unavailable(session, QuailError("Session dataset is not registered")))
    command = shlex.join([sys.executable, "-m", "quail.cli"])
    sessions.sort(key=lambda item: str(item["name"]))
    return {
        "limits": project.limits.to_record(),
        "datasets": list(datasets),
        "sessions": list(sessions),
        "interface": {
            "info": f"{command} info --json",
            "exec": f"{command} exec SESSION -c CODE [--dataset D] [--fork-from S] [--json]",
            "file": f"{command} exec SESSION FILE.py [--dataset D] [--fork-from S] [--json]",
            "reset": f"{command} exec SESSION --reset [--json]",
            "close": f"{command} exec SESSION --close [--json]",
            "export": f"{command} export SESSION --json",
        },
    }


def _unavailable(session: SessionMetadata, error: BaseException) -> JSONObject:
    return {
        "name": session.name,
        "dataset": session.dataset,
        "available": False,
        "error": ErrorInfo.from_exception(error).to_record(),
    }


def sessions(project: Project) -> list[JSONObject]:
    result = info(project)["sessions"]
    assert isinstance(result, list)
    return [item for item in result if isinstance(item, dict)]
