"""The landing check: git's reference-transaction hook refuses to move the main branch to a
commit bringing a record id already used there, in real repositories through real git."""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from itemctl import landing
from itemctl.entry import lands_no_store
from itemctl.store import Store
from tests.conftest import git

STORE_TOML = """format_version = 1
id_prefix = "P"
id_width = 4
git_commit = true

[kinds.note]
dir = "."

[kinds.note.fields]
title = "str"

[kinds.note.states]
open = { requires = ["title"] }
done = { terminal = true }
"""


def record(item_id: str, title: str) -> str:
    """An item file's text as itemctl writes it."""
    created = "2026-10-01T10:00:00+02:00"
    return f"---\nid: {item_id}\nstate: open\ncreated: {created}\ntitle: {title}\n---\n"


def commit_record(checkout: Path, slug: str, item_id: str) -> None:
    """Write and commit `tasks/<slug>.md` by hand, as a write into a branch's store copy does."""
    (checkout / "tasks" / f"{slug}.md").write_text(record(item_id, slug))
    git(checkout, "add", "-A")
    git(checkout, "commit", "-qm", f"add {slug}")


def run_git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `checkout` without raising; the caller judges the exit status."""
    return subprocess.run(
        ["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repository on main whose store `tasks/` holds P-0001 and P-0002, with git's
    reference-transaction hook wired to `itemctl hook reference-transaction`."""
    root = tmp_path / "repo"
    (root / "tasks").mkdir(parents=True)
    (root / "tasks" / "store.toml").write_text(STORE_TOML)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "store")
    store = Store(root / "tasks")
    store.add("note", "first")
    store.add("note", "second")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "reference-transaction"
    hook.write_text(
        '#!/bin/sh\n[ "$1" = prepared ] || exit 0\n'
        f'exec {sys.executable} -m itemctl hook reference-transaction "$@"\n'
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    git(root, "config", "core.hooksPath", str(hooks))
    return root


@pytest.fixture
def worktree(repo, tmp_path) -> Path:
    """A linked worktree on branch `strand`, branched from main."""
    path = tmp_path / "strand"
    git(repo, "worktree", "add", "-q", "-b", "strand", str(path))
    return path


def test_ff_landing_of_an_id_main_allocated_meanwhile_is_refused(repo, worktree, private_state):
    """The incident: a strand commits P-0003 into its store copy, main allocates P-0003 too, the
    strand is rebased onto main and fast-forwarded: the merge is refused, main stays put, and the
    message names the strand's record and the next free id; the refusal is logged."""
    commit_record(worktree, "strand-note", "P-0003")
    assert Store(repo / "tasks").add("note", "main note").id == "P-0003"
    git(worktree, "rebase", "-q", "main")
    tip = git(repo, "rev-parse", "main")

    merged = run_git(repo, "merge", "--ff-only", "strand")

    assert merged.returncode != 0
    assert git(repo, "rev-parse", "main") == tip
    assert (
        "tasks/strand-note.md: id P-0003 is also used by tasks/main-note.md; "
        "renumber this one to P-0004"
    ) in merged.stderr
    assert "git reset --merge" in merged.stderr
    log = (private_state / "itemctl" / "hooks.log").read_text().splitlines()
    assert "renumber this one to P-0004" in json.loads(log[-1])["message"]


def test_landing_a_fresh_id_passes(repo, worktree):
    """Nearby allow: the strand's record holds an id main never used, so the fast-forward lands."""
    commit_record(worktree, "strand-note", "P-0003")
    assert run_git(repo, "merge", "--ff-only", "strand").returncode == 0
    assert git(repo, "rev-parse", "main") == git(repo, "rev-parse", "strand")


def test_renumbered_record_lands(repo, worktree):
    """Following the refusal's fix (the `id:` line set to the named id) lets the branch land."""
    commit_record(worktree, "strand-note", "P-0003")
    Store(repo / "tasks").add("note", "main note")
    git(worktree, "rebase", "-q", "main")
    assert run_git(repo, "merge", "--ff-only", "strand").returncode != 0
    git(repo, "reset", "-q", "--merge")
    commit_record(worktree, "strand-note", "P-0004")
    assert run_git(repo, "merge", "--ff-only", "strand").returncode == 0


def test_a_branch_other_than_main_may_hold_a_duplicate(repo, worktree):
    """Nearby allow: only the branch the main worktree has checked out is guarded; the commit
    would raise if its ref update were refused."""
    commit_record(worktree, "strand-note", "P-0001")
    assert git(repo, "rev-parse", "strand") != git(repo, "rev-parse", "main")


def test_a_duplicate_main_already_had_does_not_block_other_landings(repo, worktree):
    """Nearby allow: a duplicate that is on main already is not brought by the landing."""
    git(repo, "config", "core.hooksPath", "/dev/null")
    commit_record(repo, "old-dup", "P-0001")
    git(repo, "config", "core.hooksPath", str(Path(repo).parent / "hooks"))
    git(worktree, "rebase", "-q", "main")
    (worktree / "other.txt").write_text("not a record\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "other")
    assert run_git(repo, "merge", "--ff-only", "strand").returncode == 0


def test_update_ref_from_a_linked_worktree_is_refused(repo, worktree):
    """Moving main without a merge, from a linked worktree, is checked the same way."""
    commit_record(worktree, "strand-note", "P-0002")
    moved = run_git(worktree, "update-ref", "refs/heads/main", "strand")
    assert moved.returncode != 0
    assert "tasks/strand-note.md: id P-0002 is also used by tasks/second.md;" in moved.stderr
    assert "tasks/second.md: id" not in moved.stderr


def test_rebasing_main_onto_a_branch_with_a_duplicate_is_refused(repo, worktree):
    """During a rebase of main the main worktree's HEAD is detached; its branch is still guarded."""
    commit_record(worktree, "strand-note", "P-0003")
    Store(repo / "tasks").add("note", "main note")
    rebased = run_git(repo, "rebase", "strand")
    assert rebased.returncode != 0
    assert "id P-0003 is also used by" in rebased.stderr


def test_two_incoming_records_get_consecutive_free_ids(repo):
    """Each record a landing brings with a used id is named with its own next free id, and a
    record main holds at the same path with the same id is never the one to renumber."""
    git(repo, "config", "core.hooksPath", "/dev/null")
    old = git(repo, "rev-parse", "HEAD").strip()
    commit_record(repo, "a", "P-0001")
    commit_record(repo, "b", "P-0002")
    found = landing.brought_duplicates(repo, old, git(repo, "rev-parse", "HEAD").strip())
    assert [(f.where, f.message.rsplit(" ", 1)[1]) for f in found] == [
        ("tasks/a.md", "P-0003"),
        ("tasks/b.md", "P-0004"),
    ]


def test_closing_a_record_moves_it_without_a_finding(repo):
    """A record whose path changes but whose id stays single is not a duplicate."""
    git(repo, "config", "core.hooksPath", "/dev/null")
    old = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "mv", "tasks/first.md", "tasks/renamed.md")
    git(repo, "commit", "-qm", "rename")
    assert landing.brought_duplicates(repo, old, git(repo, "rev-parse", "HEAD").strip()) == []


def test_a_broken_cache_fails_open_and_says_so(repo, worktree, private_state):
    """A failure of the check itself never blocks git: it is reported, logged, and git goes on."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    (common / "itemctl").mkdir(exist_ok=True)
    (common / landing.CACHE).write_text("{not json")
    commit_record(worktree, "strand-note", "P-0003")
    merged = run_git(repo, "merge", "--ff-only", "strand")
    assert merged.returncode == 0
    assert "record ids were not checked" in merged.stderr
    assert "not JSON" in (private_state / "itemctl" / "hooks.log").read_text()


def test_lands_no_store_only_for_commits_without_a_store_toml(repo, tmp_path, monkeypatch):
    """The stdlib fast path answers only when no moved-to commit holds a store.toml."""
    plain = tmp_path / "plain"
    plain.mkdir()
    git(plain, "init", "-q", "-b", "main")
    (plain / "a.txt").write_text("a\n")
    git(plain, "add", "-A")
    git(plain, "commit", "-qm", "a")
    zero = "0" * 40
    monkeypatch.chdir(plain)
    assert lands_no_store(f"{zero} {git(plain, 'rev-parse', 'HEAD').strip()} refs/heads/main\n")
    assert not lands_no_store("garbage\n")
    monkeypatch.chdir(repo)
    assert not lands_no_store(f"{zero} {git(repo, 'rev-parse', 'HEAD').strip()} refs/heads/main\n")
