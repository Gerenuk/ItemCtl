"""Shared fixtures: private git identity, a fresh fixture store copy, a settable clock."""

import hashlib
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from itemctl.store import Store

FIXTURE = Path(__file__).parent / "fixture_store"
TZ = timezone(timedelta(hours=2))


def _digest(root: Path) -> str:
    """A hash over every file path and content under `root`."""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="session", autouse=True)
def fixture_untouched():
    """Tests work on copies: the fixture store is byte-identical after the session."""
    before = _digest(FIXTURE)
    yield
    assert _digest(FIXTURE) == before


@pytest.fixture(autouse=True)
def git_identity(monkeypatch, tmp_path_factory):
    """A private git config, so the user's hooks, signing and identity never apply."""
    config = tmp_path_factory.mktemp("gitconfig") / "config"
    config.write_text(
        "[user]\n\tname = test\n\temail = test@example.invalid\n"
        "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def private_state(monkeypatch, tmp_path_factory) -> Path:
    """A private XDG_STATE_HOME, so the hook log never reaches the user's."""
    state = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state


def git(root: Path, *args: str) -> str:
    """Run git in `root`; return stdout."""
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def tree(root: Path) -> dict[str, bytes | None]:
    """Every path under `root` but `.git` and `.lock`: file bytes, None for a directory."""
    return {
        str(p.relative_to(root)): None if p.is_dir() else p.read_bytes()
        for p in root.rglob("*")
        if ".git" not in p.relative_to(root).parts and p.name != ".lock"
    }


def committed_copy(root: Path) -> Path:
    """Copy the fixture store to `root` and commit it there in its own repository."""
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "store")
    return root


@pytest.fixture
def store_dir(tmp_path) -> Path:
    """A committed copy of the fixture store in its own repository."""
    return committed_copy(tmp_path / "repo")


class Clock:
    """A settable clock for leases and ageing."""

    def __init__(self) -> None:
        """Start at a fixed timezone-aware instant."""
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=TZ)

    def __call__(self) -> datetime:
        """The current instant."""
        return self.now


@pytest.fixture
def clock() -> Clock:
    """A fresh settable clock."""
    return Clock()


@pytest.fixture
def store(store_dir, clock) -> Store:
    """The fixture store copy, on the settable clock."""
    return Store(store_dir, clock)
