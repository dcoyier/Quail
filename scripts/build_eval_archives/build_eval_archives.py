"""Build allowlisted evaluation ZIPs without loose repository source."""

from __future__ import annotations

import argparse
import os
import subprocess
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

_BASE_FILES = ("SETUP.md", "start.sh", "assemble.sh", "quail.toml")
_SEARCH_PREFIX = "data/quail-search.turso.part"
_SEARCH_CHECKSUM = "data/quail-search.turso.sha256"


def _tracked_data(repo: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", "data"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return [repo / item.decode() for item in completed.stdout.split(b"\0") if item]


def _arcname(root_name: str, relative: str | Path) -> str:
    return str(PurePosixPath(root_name) / PurePosixPath(relative))


def _write_file(archive: zipfile.ZipFile, source: Path, arcname: str) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    archive.write(source, arcname)


def _atomic_zip(output: Path, writer: Callable[[zipfile.ZipFile], None]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            writer(archive)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def build_archives(
    *,
    repo: Path,
    wheel: Path,
    deps: Path,
    output: Path,
    search_output: Path | None,
) -> None:
    """Build the base ZIP and optional split search-parts ZIP."""

    repo = repo.resolve()
    wheel = wheel.resolve()
    deps = deps.resolve()
    output = output.resolve()
    root_name = output.name.removesuffix(".zip")
    dependency_wheels = sorted(deps.glob("*.whl"))
    if not wheel.is_file():
        raise FileNotFoundError(wheel)
    if not dependency_wheels:
        raise ValueError(f"no dependency wheels found in {deps}")
    tracked_data = _tracked_data(repo)
    search_data = [
        path
        for path in tracked_data
        if path.relative_to(repo).as_posix().startswith(_SEARCH_PREFIX)
        or path.relative_to(repo).as_posix() == _SEARCH_CHECKSUM
    ]
    split_search = search_output is not None

    def write_base(archive: zipfile.ZipFile) -> None:
        for relative in _BASE_FILES:
            _write_file(archive, repo / relative, _arcname(root_name, relative))
        _write_file(archive, wheel, _arcname(root_name, wheel.name))
        for dependency in dependency_wheels:
            _write_file(
                archive,
                dependency,
                _arcname(root_name, PurePosixPath("deps") / dependency.name),
            )
        for source in tracked_data:
            if split_search and source in search_data:
                continue
            relative = source.relative_to(repo)
            _write_file(archive, source, _arcname(root_name, relative))

    _atomic_zip(output, write_base)

    if search_output is not None:
        if not search_data:
            raise ValueError("no tracked search parts found")

        def write_search(archive: zipfile.ZipFile) -> None:
            archive.writestr(
                _arcname(root_name, "SEARCH_PARTS.txt"),
                "Extract this archive beside the base archive contents before "
                "running start.sh.\n",
            )
            for source in search_data:
                relative = source.relative_to(repo)
                _write_file(archive, source, _arcname(root_name, relative))

        _atomic_zip(search_output.resolve(), write_search)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--deps", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--search-output", type=Path)
    args = parser.parse_args()
    build_archives(
        repo=args.repo,
        wheel=args.wheel,
        deps=args.deps,
        output=args.output,
        search_output=args.search_output,
    )


if __name__ == "__main__":
    main()
