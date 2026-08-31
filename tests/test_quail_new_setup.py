"""Host preset loads API files into one namespace and prints nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

from quail_new.setup import HOST_BOUND, SURFACE, run


def test_run_execs_files_in_order_into_one_namespace(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = x + 1\n", encoding="utf-8")
    namespace = run(api_dir=tmp_path, surface=("a.py", "b.py"))
    assert namespace["x"] == 1
    assert namespace["y"] == 2
    assert "print" not in namespace


def test_run_prints_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "a.py").write_text("n = 1\n", encoding="utf-8")
    run(api_dir=tmp_path, surface=("a.py",))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_run_forbids_print_in_loaded_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.py").write_text("print('no')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="the setup preset cannot print"):
        run(api_dir=tmp_path, surface=("a.py",))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_default_run_needs_the_api_files() -> None:
    with pytest.raises(FileNotFoundError, match="missing"):
        run()


def test_run_rejects_a_path_that_is_not_a_bare_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bare name"):
        run(api_dir=tmp_path, surface=("../secrets.py",))


def test_run_raises_when_a_surface_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="missing"):
        run(api_dir=tmp_path, surface=("field.py",))


def test_surface_and_host_bound_match_the_library_map() -> None:
    assert SURFACE == (
        "errors.py",
        "field.py",
        "operations.py",
        "expression.py",
        "predicate.py",
        "unit.py",
        "entry.py",
        "group.py",
        "ranking.py",
        "re.py",
    )
    assert HOST_BOUND == (
        "retrieve",
        "count",
        "create_field",
        "tag",
        "untag",
        "print",
    )
