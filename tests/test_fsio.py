"""Atomic writes: durable (fsync), mode-preserving, and leaving no temp file on failure."""

import os
import stat
from pathlib import Path

import pytest

from itemctl import fsio


@pytest.fixture
def umask_027():
    """Run the test under umask 027."""
    old = os.umask(0o027)
    yield
    os.umask(old)


def mode(path: Path) -> int:
    """The permission bits of `path`."""
    return stat.S_IMODE(path.stat().st_mode)


def test_write_keeps_the_replaced_files_mode_and_uses_the_umask_for_a_new_one(tmp_path, umask_027):
    """A new file gets 0o666 & ~umask; a replaced file keeps its own mode."""
    fresh = tmp_path / "new.md"
    fsio.write(fresh, "x")
    assert mode(fresh) == 0o640
    kept = tmp_path / "kept.md"
    kept.write_text("old")
    kept.chmod(0o604)
    fsio.write(kept, "new")
    assert (mode(kept), kept.read_text()) == (0o604, "new")


def test_write_with_a_mode_sets_it_on_a_new_and_a_replaced_file(tmp_path, umask_027):
    """An explicit mode wins over both the umask and the replaced file's mode."""
    fresh = tmp_path / "new.md"
    fsio.write(fresh, "x", mode=0o600)
    replaced = tmp_path / "replaced.md"
    replaced.write_text("old")
    replaced.chmod(0o644)
    fsio.write(replaced, "new", mode=0o600)
    assert (mode(fresh), mode(replaced), replaced.read_text()) == (0o600, 0o600, "new")


def test_a_failed_write_leaves_the_file_and_no_temp_file(tmp_path):
    """Text that cannot be encoded raises; the old bytes stay and no `.tmp-*` remains."""
    path = tmp_path / "a.md"
    path.write_text("old")
    with pytest.raises(UnicodeEncodeError):
        fsio.write(path, "bad \ud800")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.md"]
    assert path.read_text() == "old"
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x").write_text("x")
    with pytest.raises(OSError):
        fsio.write(tmp_path / "d", "cannot replace a directory")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.md", "d"]


def test_write_fsyncs_the_temp_file_and_the_directory(tmp_path, monkeypatch):
    """The data reaches disk before the rename and the rename before `write` returns."""
    synced: list[tuple[str, bool]] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        """Note which path `fd` is open on and whether the target exists yet."""
        synced.append((os.readlink(f"/proc/self/fd/{fd}"), (tmp_path / "a.md").exists()))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    fsio.write(tmp_path / "a.md", "text")
    assert any(Path(p).name.startswith(".tmp-") and not there for p, there in synced)
    assert (str(tmp_path), True) in synced


def test_move_renames_a_file_and_refuses_an_existing_target(tmp_path):
    """move keeps the inode (a rename, not a copy) and never overwrites."""
    src, dst = tmp_path / "a.md", tmp_path / "closed" / "a.md"
    src.write_text("a")
    inode = src.stat().st_ino
    fsio.move(src, dst)
    assert (dst.stat().st_ino, src.exists()) == (inode, False)
    src.write_text("b")
    with pytest.raises(FileExistsError):
        fsio.move(src, dst)
    assert (src.read_text(), dst.read_text()) == ("b", "a")
