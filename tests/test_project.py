import os
import tomllib

import pytest

from quail import project
from quail.contracts import QuailError


def test_init_preserves_ignore_file_and_clones_need_no_empty_directory(tmp_path):
    (tmp_path / ".gitignore").write_text("# my ignores\nprivate.csv")
    result = project.initialize(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == "# my ignores\nprivate.csv\n.quail/\n"
    assert result.manifest.read_text() == '[project]\nquail = "1"\n'
    (tmp_path / "sessions").rmdir()
    assert result.session_names() == []
    with pytest.raises(QuailError, match="already exists"):
        project.initialize(tmp_path)


@pytest.mark.parametrize(
    "name", ["notes.backup", 'notes"]\n[kernel]\ncpu_seconds=1\n[x."', "a\\b", ".."]
)
def test_registration_quotes_a_single_name_or_rejects_path_segments(tmp_path, name):
    result = project.initialize(tmp_path)
    if name in {"a\\b", ".."}:
        with pytest.raises(QuailError):
            project.registration_text(result, name, tmp_path / "a.csv")
        return
    text = project.registration_text(result, name, tmp_path / "a.csv")
    parsed = tomllib.loads(text)
    assert list(parsed["datasets"]) == [name]
    assert "kernel" not in parsed


def test_resolved_paths_reject_sibling_prefix_and_managed_sources(tmp_path):
    result = project.initialize(tmp_path / "study")
    other = tmp_path / "study-other"
    other.mkdir()
    (result.root / "linked").symlink_to(other, target_is_directory=True)
    with pytest.raises(QuailError, match="escapes"):
        result.check_source(result.root / "linked" / "a.csv")
    with pytest.raises(QuailError, match="managed"):
        result.check_source(result.root / "sessions" / "a.csv")


def test_local_lock_lifetime_shared_compatibility_and_noninheritance(tmp_path):
    path = tmp_path / "resource.lock"
    with project.FileLock(path, shared=True) as first, project.FileLock(path, shared=True):
        assert not os.get_inheritable(first._descriptor)
        with pytest.raises(QuailError, match="busy"):
            project.FileLock(path).acquire()
    with project.FileLock(path):
        with pytest.raises(QuailError, match="busy"):
            project.FileLock(path, shared=True).acquire()
