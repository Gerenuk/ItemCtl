"""File-system shell: the store lock, the file walk, durable atomic writes and renames."""

import fcntl
import os
import stat
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from itemctl.model import ItemError


@contextmanager
def lock(root: Path) -> Generator[None]:
    """Hold an exclusive flock on `<root>/.lock` for the block."""
    with open(root / ".lock", "ab") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def walk(root: Path) -> list[PurePosixPath]:
    """Every file under `root`, store-relative and sorted, skipping `.git` and tagged cache dirs."""
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [
            d for d in dirs if d != ".git" and not (Path(base) / d / "CACHEDIR.TAG").is_file()
        ]
        rel = PurePosixPath(Path(base).relative_to(root).as_posix())
        found += [rel / f for f in files if f != ".git"]
    return sorted(found)


def _fsync_dir(directory: Path) -> None:
    """fsync a directory, making the entries renamed or created in it durable."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _missing_dirs(directory: Path) -> list[Path]:
    """`directory` and its ancestors that do not exist yet, deepest first."""
    return [d for d in (directory, *directory.parents) if not d.exists()]


def _make_dirs(directory: Path) -> None:
    """Create `directory` and its missing parents, each new entry fsynced into its parent."""
    for new in reversed(_missing_dirs(directory)):
        new.mkdir()
        _fsync_dir(new.parent)


def _umask() -> int:
    """The process umask (read by setting and restoring it)."""
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Replace `path` by `data` atomically and durably. The result has `mode` when given, else
    the replaced file's mode (new: 0o666 & ~umask).

    The data goes to a temp file beside `path`, fsynced before `os.replace`; the directory is
    fsynced after. A failure removes the temp file and leaves `path` as it was.
    """
    _make_dirs(path.parent)
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o666 & ~_umask()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def write(path: Path, text: str, mode: int | None = None) -> None:
    """Replace `path` by `text` (UTF-8) atomically and durably; see `write_bytes`."""
    write_bytes(path, text.encode("utf-8"), mode)


def move(src: Path, dst: Path) -> None:
    """Rename a file or directory in one atomic step, durably; refuse to overwrite."""
    if dst.exists():
        raise FileExistsError(f"cannot move {src} to {dst}: target exists")
    _make_dirs(dst.parent)
    os.rename(src, dst)
    _fsync_dir(dst.parent)
    if src.parent != dst.parent:
        _fsync_dir(src.parent)


@dataclass(frozen=True)
class Write:
    """Step: replace `path` by `text`."""

    path: Path
    text: str

    @property
    def paths(self) -> tuple[Path, ...]:
        """The paths the step changes."""
        return (self.path,)


@dataclass(frozen=True)
class Move:
    """Step: rename the file or directory `src` to `dst`."""

    src: Path
    dst: Path

    @property
    def paths(self) -> tuple[Path, ...]:
        """The paths the step changes."""
        return (self.src, self.dst)


type Step = Write | Move


def _remove_dirs(created: list[Path]) -> None:
    """Remove directories a step created, deepest first."""
    for directory in created:
        directory.rmdir()
        _fsync_dir(directory.parent)


class Journal:
    """The steps of one write, each with its inverse, so a failed write can be undone."""

    def __init__(self) -> None:
        """Start with nothing done."""
        self._undo: list[tuple[Step, Callable[[], None]]] = []

    def apply(self, step: Step) -> None:
        """Take `step`, remembering how to undo it (replaced bytes, new directories)."""
        match step:
            case Write(path, text):
                before = path.read_bytes() if path.exists() else None
                created = _missing_dirs(path.parent)
                write(path, text)

                def undo() -> None:
                    """Put the old bytes back, or remove the new file and its new directories."""
                    if before is None:
                        path.unlink()
                        _fsync_dir(path.parent)
                        _remove_dirs(created)
                    else:
                        write_bytes(path, before)

            case Move(src, dst):
                created = _missing_dirs(dst.parent)
                move(src, dst)

                def undo() -> None:
                    """Rename back and remove the directories the move created."""
                    move(dst, src)
                    _remove_dirs(created)

            case _:
                raise ItemError(f"unknown file step {step!r}")
        self._undo.append((step, undo))

    def rollback(self) -> None:
        """Undo every step taken, newest first; raise naming each step that could not be undone."""
        failed = []
        while self._undo:
            step, undo = self._undo.pop()
            try:
                undo()
            except OSError as exc:
                failed.append(f"{step}: {exc}")
        if failed:
            raise ItemError(
                "could not undo a failed write:\n" + "\n".join(f"  {f}" for f in failed)
            )
