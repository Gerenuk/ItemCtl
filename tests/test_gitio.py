"""gitio against paths that git would C-quote in its default output."""

from pathlib import Path

import pytest

from itemctl import gitio
from itemctl.model import ItemError
from tests.conftest import git


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repository holding one tracked record with a non-ASCII name."""
    git(tmp_path, "init", "-q")
    (tmp_path / "té.md").write_text("x\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def test_commit_records_deletion_of_non_ascii_tracked_path(repo):
    """A vanished tracked `té.md` commits as a deletion, and only as that."""
    (repo / "té.md").unlink()
    (repo / "other.md").write_text("unrelated\n")
    gitio.commit(repo, [repo / "té.md"], "drop")
    assert git(repo, "ls-files", "-z") == ""
    assert git(repo, "status", "--porcelain", "-z") == "?? other.md\0"


def test_commit_records_deletion_under_non_ascii_directory(repo):
    """A tracked file under a deleted non-ASCII directory commits as a deletion."""
    (repo / "dé").mkdir()
    (repo / "dé" / "a.md").write_text("y\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "dir")
    (repo / "dé" / "a.md").unlink()
    (repo / "dé").rmdir()
    (repo / "other.md").write_text("unrelated\n")
    gitio.commit(repo, [repo / "dé"], "drop dir")
    assert git(repo, "status", "--porcelain", "-z") == "?? other.md\0"


def test_check_not_ignored_names_non_ascii_path_unquoted(repo):
    """The refusal names an ignored `é.md` as written, not as `"\\303\\251.md"`."""
    (repo / ".gitignore").write_text("é.md\n")
    with pytest.raises(ItemError) as err:
        gitio.check_not_ignored(repo, [repo / "é.md"])
    assert "é.md" in str(err.value)
