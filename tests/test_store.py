"""The write pipeline through the `Store` verbs, on a git-committed fixture copy."""

import os
import shutil
from datetime import date, timedelta

import pytest

from itemctl import gitio
from itemctl.changes import section_entries
from itemctl.model import (
    AppendLine,
    Assign,
    DuplicateError,
    Extend,
    Finding,
    InvalidStore,
    Item,
    ItemError,
    NothingClaimable,
    Unset,
)
from itemctl.store import Store
from tests.conftest import FIXTURE, git, tree

HOUR = timedelta(hours=1)


def test_add_writes_commits_and_indexes(store):
    """An add creates `<slug>.md`, regenerates index.md and commits exactly those paths."""
    item = store.add("friction", "Stale tip!", [Assign("component", "harness")])
    assert (item.id, str(item.path)) == ("FR-1", "stale-tip.md")
    text = (store.root / "stale-tip.md").read_text()
    assert text.startswith("---\nid: FR-1\nstate: open\ncreated: 2026-09-26T12:00:00+02:00\n")
    assert "| FR-1 | Stale tip! | open | harness |" in (store.root / "index.md").read_text()
    assert git(store.root, "log", "-1", "--format=%s").strip() == "friction: add FR-1 stale-tip"
    assert git(store.root, "status", "--porcelain") == ""


def test_ids_count_closed_items_and_are_never_reused(store):
    """The next id is one above every id, terminal items included."""
    store.add("friction", "a", [Assign("component", "x")])
    _close(store, "friction")
    assert store.add("friction", "b", [Assign("component", "y")]).id == "FR-2"


def test_dedup_key_refuses_a_live_match_only(store):
    """add refuses while a non-terminal item has the same dedup_key; a closed one does not block."""
    store.add("friction", "a", [Assign("component", "x")])
    with pytest.raises(DuplicateError, match="FR-1") as refused:
        store.add("friction", "b", [Assign("component", "x")])
    assert [i.id for i in refused.value.matches] == ["FR-1"]
    _close(store, "friction")
    assert store.add("friction", "b", [Assign("component", "x")]).id == "FR-2"


def test_slug_collision_names_the_existing_item(store):
    """Two titles with one slug: the second add is refused naming the first."""
    store.add("friction", "Hello world", [Assign("component", "x")])
    with pytest.raises(ItemError, match="FR-1"):
        store.add("friction", "hello, WORLD", [Assign("component", "y")])


def test_state_change_drops_only_here_fields_and_moves_to_closed(store):
    """Leaving `fixing` drops fixing_by; a terminal state moves to closed/ and back again."""
    store.add("friction", "a", [Assign("component", "x")])
    store.set("a", [Assign("state", "fixing"), Assign("fixing_by", "s1")])
    done = _close(store, "friction")
    assert str(done.path) == "closed/a.md" and "fixing_by" not in done.fields
    assert not (store.root / "a.md").exists()
    reopened = store.set("FR-1", [Assign("state", "open")])
    assert str(reopened.path) == "a.md" and "outcome" not in reopened.fields
    assert git(store.root, "status", "--porcelain") == ""


def test_attachments_move_with_the_item(store):
    """An attachments kind moves its whole `<slug>/` directory into closed/."""
    item = store.add("task", "Build it")
    (store.root / "task/build-it/notes.txt").write_text("n")
    git(store.root, "add", "-A")
    git(store.root, "commit", "-qm", "notes")
    store.set(item.id, [Assign("state", "done")])
    assert (store.root / "task/closed/build-it/notes.txt").read_text() == "n"
    assert not (store.root / "task/build-it").exists()
    assert git(store.root, "status", "--porcelain") == ""


def test_set_edits_lists_unsets_and_appends_dated_lines(store, clock):
    """`f+=v` extends, `f=` unsets, `--append` adds a dated line under its section."""
    store.add("friction", "a", [Assign("component", "x"), Assign("tags", ("t1",))], body="Intro.\n")
    store.set("a", [Extend("tags", "t2"), AppendLine("Occurrences", "first")])
    clock.now += timedelta(days=1)
    item = store.set("a", [Unset("tags"), AppendLine("Occurrences", "again"), Extend("free", "v")])
    assert "tags" not in item.fields and item.fields["free"] == ("v",)
    assert item.body == "Intro.\n\n## Occurrences\n- 2026-09-26 — first\n- 2026-09-27 — again\n"


def test_invalid_result_writes_nothing(store):
    """A write whose result fails validation raises and leaves files and git untouched."""
    store.add("friction", "a", [Assign("component", "x")])
    before = (store.root / "a.md").read_text()
    with pytest.raises(InvalidStore, match="requires"):
        store.set("a", [Assign("state", "resolved")])
    with pytest.raises(ItemError, match="managed"):
        store.set("a", [Assign("id", "FR-9")])
    assert (store.root / "a.md").read_text() == before
    assert git(store.root, "status", "--porcelain") == ""


def test_detached_head_refuses_writes(store):
    """With git_commit, a detached HEAD refuses the write."""
    git(store.root, "checkout", "-q", "--detach")
    with pytest.raises(ItemError, match="detached"):
        store.add("friction", "a", [Assign("component", "x")])


def test_lease_expiry_renewal_and_release(store, clock):
    """A live lease blocks others, renews for its holder, and is claimable once expired."""
    store.add("friction", "a", [Assign("component", "x")])
    first = store.claim("a", holder="h1", ttl=HOUR)
    clock.now += timedelta(minutes=30)
    with pytest.raises(ItemError, match="leased by h1"):
        store.claim("a", holder="h2", ttl=HOUR)
    renewed = store.claim("a", holder="h1", ttl=HOUR)
    assert renewed.fields["claimed_at"] == first.fields["claimed_at"]
    assert renewed.fields["lease_until"] == clock.now + HOUR
    clock.now += 2 * HOUR
    assert store.claim("a", holder="h2", ttl=HOUR).fields["claimed_by"] == "h2"
    with pytest.raises(ItemError, match="held by h2"):
        store.release("a", holder="h1")
    assert "claimed_by" not in store.release("a", holder="h1", force=True).fields


def test_terminal_state_releases_the_lease(store):
    """Entering a terminal state drops the lease."""
    store.add("task", "a")
    store.claim("a", holder="h", ttl=HOUR)
    assert "claimed_by" not in store.set("a", [Assign("state", "done")]).fields


def test_claim_next_takes_oldest_unblocked_match(store, clock):
    """--next skips claimed, blocked and non-matching items and takes the oldest of the rest."""
    blocker = store.add("task", "blocker")
    clock.now += HOUR
    store.add("task", "blocked", [Assign("blocked_by", blocker.id)])
    clock.now += HOUR
    store.add("task", "urgent one", [Assign("urgent", True)])
    assert store.claim(next_kind="task", holder="h", ttl=HOUR).id == blocker.id
    assert (
        store.claim(next_kind="task", holder="h", ttl=HOUR, where={"urgent": "true"}).id == "FR-3"
    )
    with pytest.raises(NothingClaimable):
        store.claim(next_kind="task", holder="h", ttl=HOUR)
    store.set(blocker.id, [Assign("state", "done")])
    assert store.claim(next_kind="task", holder="h", ttl=HOUR).id == "FR-2"


def test_check_reports_hand_breakage(store):
    """check reports unparseable items, stray files and stale items."""
    store.add("friction", "a", [Assign("component", "x")])
    (store.root / "b.md").write_text("no frontmatter\n")
    (store.root / "notes.txt").write_text("x")
    store.clock.now += timedelta(days=31)
    assert {f.rule for f in store.check()} == {"parse_errors", "stray_files", "stale"}


def test_list_filters(store):
    """list hides terminal items unless asked, and filters by state and where."""
    store.add("friction", "a", [Assign("component", "x"), Assign("tags", ("t",))])
    store.add("task", "b")
    store.set("b", [Assign("state", "done")])
    assert [i.id for i in store.items()] == ["FR-1"]
    assert [i.id for i in store.items(closed=True)] == ["FR-1", "FR-2"]
    assert [i.id for i in store.items(state="done")] == ["FR-2"]
    assert [i.id for i in store.items(where={"tags": "t"})] == ["FR-1"]


def test_open_maps_a_linked_worktree_to_the_main_checkout(store_dir, tmp_path):
    """A store found in a linked worktree resolves to the main checkout's store."""
    worktree = tmp_path / "wt"
    git(store_dir, "worktree", "add", "-q", "-b", "side", str(worktree))
    assert Store.open(worktree / "task").root == store_dir.resolve()


def test_open_refuses_a_separate_git_dir_worktree_and_keeps_its_main_checkout(tmp_path):
    """`--separate-git-dir` records no path to the main checkout: its linked worktree raises
    rather than passing for the main checkout; the main checkout itself opens."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    git(tmp_path, "init", "-q", "--separate-git-dir", str(tmp_path / "gitdir"), str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "store")
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))
    with pytest.raises(ItemError, match="records no path to its main checkout"):
        Store.open(worktree / "task")
    assert Store.open(repo / "task").root == repo.resolve()


def test_open_maps_a_submodule_worktree_to_the_submodule_checkout(store_dir, tmp_path):
    """A linked worktree of a submodule maps to the submodule's checkout (its core.worktree)."""
    parent = tmp_path / "parent"
    git(tmp_path, "init", "-q", str(parent))
    git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(store_dir), "sm")
    git(parent, "commit", "-qm", "submodule")
    worktree = tmp_path / "smwt"
    git(parent / "sm", "worktree", "add", "-q", "-b", "side", str(worktree))
    assert Store.open(worktree / "task").root == (parent / "sm").resolve()
    assert Store.open(parent / "sm").root == (parent / "sm").resolve()


def test_open_refuses_a_worktree_of_a_bare_repository(store_dir, tmp_path):
    """A bare repository has no main checkout to write to: opening from its worktree raises."""
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(store_dir), str(bare))
    worktree = tmp_path / "wt"
    git(bare, "worktree", "add", "-q", str(worktree), "main")
    with pytest.raises(ItemError, match="bare"):
        Store.open(worktree)


def test_new_skips_the_dedup_key(store):
    """dedup=False files a second live item with the same dedup_key."""
    store.add("friction", "a", [Assign("component", "x")])
    assert store.add("friction", "b", [Assign("component", "x")], dedup=False).id == "FR-2"


def test_id_width_pads_ids_and_rejects_unpadded_ones(store_dir, clock):
    """With id_width = 4 new ids are FR-0001, FR-0002; an unpadded id fails validation."""
    toml = store_dir / "store.toml"
    toml.write_text(toml.read_text().replace('id_prefix = "FR"', 'id_prefix = "FR"\nid_width = 4'))
    git(store_dir, "commit", "-qam", "width")
    store = Store(store_dir, clock)
    assert store.add("task", "a").id == "FR-0001"
    assert store.add("task", "b").id == "FR-0002"
    path = store_dir / "task/b/item.md"
    path.write_text(path.read_text().replace("FR-0002", "FR-2"))
    assert [f.rule for f in store.check()] == ["id_format"]


def test_revise_decides_on_the_item_under_the_lock(store):
    """`revise` hands the plan the current item; a plan that raises writes nothing."""
    store.add("friction", "a", [Assign("component", "x")])

    def reopen_only_if_closed(item):
        """Refuse unless the item is resolved."""
        if item.fields["state"] != "resolved":
            raise ItemError(f"{item.id} is not resolved")
        return [Assign("state", "open")]

    with pytest.raises(ItemError, match="not resolved"):
        store.revise("a", reopen_only_if_closed)
    assert git(store.root, "log", "-1", "--format=%s").strip() == "friction: add FR-1 a"


def test_section_entries_read_what_append_writes(store, clock):
    """The dated lines `--append` writes read back as (date, text); a foreign line raises."""
    store.add("friction", "a", [Assign("component", "x")], body="Intro.\n")
    store.set("a", [AppendLine("Occurrences", "one — with a dash")])
    clock.now += timedelta(days=2)
    item = store.set("a", [AppendLine("Occurrences", "two")])
    assert section_entries(item.body, "Occurrences") == [
        (date(2026, 9, 26), "one — with a dash"),
        (date(2026, 9, 28), "two"),
    ]
    assert section_entries(item.body, "Missing") == []
    with pytest.raises(ItemError, match="not '- YYYY-MM-DD"):
        section_entries(item.body + "- stray\n", "Occurrences")


def test_reads_raise_on_unparseable_items_and_skip_cache_dirs(store):
    """items()/item() refuse a store with a broken item; a CACHEDIR.TAG directory is ignored."""
    store.add("friction", "a", [Assign("component", "x")])
    (store.root / ".ruff_cache").mkdir()
    (store.root / ".ruff_cache/CACHEDIR.TAG").write_text(
        "Signature: 8a477f597d28d172789f06886806bc55"
    )
    (store.root / ".ruff_cache/x.md").write_text("junk")
    assert [i.id for i in store.items()] == ["FR-1"]
    (store.root / "b.md").write_text("no frontmatter\n")
    with pytest.raises(InvalidStore, match="b.md"):
        store.items()
    with pytest.raises(InvalidStore, match="b.md"):
        store.item("a")


def test_a_write_that_changes_nothing_commits_nothing(store):
    """Setting a field to its current value returns the item and leaves git history alone."""
    store.add("friction", "a", [Assign("component", "x")])
    before = (store.root / "a.md").read_bytes()
    commits = git(store.root, "rev-list", "--count", "HEAD")
    assert store.set("a", [Assign("component", "x")]).fields["component"] == "x"
    assert (store.root / "a.md").read_bytes() == before
    assert git(store.root, "rev-list", "--count", "HEAD") == commits
    assert git(store.root, "status", "--porcelain") == ""
    store.set("a", [Assign("component", "y")])
    assert int(git(store.root, "rev-list", "--count", "HEAD")) == int(commits) + 1


def test_git_failure_names_stdout_when_stderr_is_empty(store_dir):
    """`git commit` on a clean tree reports on stdout only; the error carries that text."""
    with pytest.raises(ItemError, match="nothing to commit"):
        gitio.checked(store_dir, "commit", "-m", "empty")


def _close(store, kind: str) -> Item:
    """Move item `a` of `kind` into its terminal state; return it."""
    if kind == "friction":
        return store.set(
            "a", [Assign("state", "resolved"), Assign("outcome", "fixed"), Assign("fixed_in", "c")]
        )
    return store.set("a", [Assign("state", "done")])


@pytest.mark.parametrize(
    ("kind", "old", "new"),
    [("friction", "a.md", "closed/a.md"), ("task", "task/a", "task/closed/a")],
)
def test_a_terminal_move_never_shows_both_paths(store, monkeypatch, kind, old, new):
    """After every rename, replace or unlink, the item is at exactly one of its two paths."""
    seen: list[tuple[bool, bool]] = []

    def observing(real):
        """Wrap a filesystem call to record where the item is right after it."""

        def call(*args, **kwargs):
            """Run the call, then note which of the two paths exist."""
            result = real(*args, **kwargs)
            seen.append(((store.root / old).exists(), (store.root / new).exists()))
            return result

        return call

    store.add(kind, "a", [Assign("component", "x")] if kind == "friction" else [])
    for name in ("replace", "rename", "unlink", "rmdir"):
        monkeypatch.setattr(os, name, observing(getattr(os, name)))
    _close(store, kind)
    assert seen and (True, True) not in seen
    assert seen[-1] == (False, True)


@pytest.mark.parametrize(
    "write",
    [
        lambda s: s.add("friction", "b", [Assign("component", "y")]),
        lambda s: s.add("task", "u"),
        lambda s: s.set("a", [AppendLine("Occurrences", "seen")]),
        lambda s: _close(s, "friction"),
        lambda s: s.set("t", [Assign("state", "done")]),
        lambda s: s.claim("a", holder="h1", ttl=HOUR),
        lambda s: s.claim(next_kind="task", holder="h1", ttl=HOUR),
        lambda s: s.release("r", holder="h0"),
        lambda s: s.revise("a", lambda item: [Assign("component", "z")]),
    ],
    ids=[
        "add",
        "add-attachments",
        "set",
        "move",
        "move-attachments",
        "claim",
        "claim-next",
        "release",
        "revise",
    ],
)
def test_a_rejected_commit_leaves_files_and_git_as_they_were(store, write):
    """A pre-commit hook that fails: the error names it; files, index and status are unchanged."""
    store.add("friction", "a", [Assign("component", "x")])
    store.add("task", "t")
    store.add("task", "r")
    store.claim("r", holder="h0", ttl=HOUR)
    (store.root / "task/t/notes.txt").write_text("n")
    git(store.root, "add", "-A")
    git(store.root, "commit", "-qm", "notes")
    hook = store.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho 'pre-commit says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    files, head = tree(store.root), git(store.root, "rev-parse", "HEAD")
    with pytest.raises(ItemError, match="pre-commit says no"):
        write(store)
    assert tree(store.root) == files
    assert git(store.root, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(store.root, "rev-parse", "HEAD") == head
    hook.unlink()
    write(store)
    assert git(store.root, "status", "--porcelain") == ""


def test_a_gitignored_target_is_refused_before_anything_is_written(store):
    """With git_commit, a target path git ignores is named and nothing is written."""
    (store.root / ".gitignore").write_text(".lock\nb.md\n")
    git(store.root, "commit", "-qam", "ignore b.md")
    files = tree(store.root)
    with pytest.raises(ItemError, match=r"git ignores b\.md"):
        store.add("friction", "b", [Assign("component", "y")])
    assert tree(store.root) == files
    assert store.add("friction", "c", [Assign("component", "y")]).id == "FR-1"


def no_legacy_titles(snap):
    """A consumer rule: no item title mentions 'legacy'."""
    return [
        Finding("legacy", str(i.path), f"{i.id}: title mentions legacy")
        for i in snap.items
        if "legacy" in str(i.fields.get("title"))
    ]


def test_consumer_rules_refuse_writes_and_join_check(store_dir, clock):
    """A rule passed to Store refuses a write that would break it (nothing written) and
    reports through check; a write that keeps it passes."""
    ruled = Store(store_dir, clock, rules=[no_legacy_titles])
    with pytest.raises(InvalidStore, match="title mentions legacy"):
        ruled.add("friction", "legacy thing", [Assign("component", "x")])
    assert not (store_dir / "legacy-thing.md").exists()
    assert git(store_dir, "status", "--porcelain") == ""
    assert ruled.add("friction", "fine", [Assign("component", "x")]).id == "FR-1"
    assert ruled.check() == []
    Store(store_dir, clock).add("friction", "legacy two", [Assign("component", "y")])
    assert [f.rule for f in ruled.check()] == ["legacy"]
    opened = Store.open(store_dir, clock, rules=[no_legacy_titles])
    assert [f.rule for f in opened.check()] == ["legacy"]
    with pytest.raises(InvalidStore, match="title mentions legacy"):
        opened.set("fine", [Assign("component", "z")])


def _three(store):
    """Items a, b, c of kind friction; the count of commits after adding them."""
    for title in "abc":
        store.add("friction", title, [Assign("component", title)])
    return git(store.root, "rev-list", "--count", "HEAD")


def test_revise_many_writes_every_item_in_one_commit(store):
    """Each ref's plan sees its own item; one commit names every id; items return in ref order."""
    _three(store)
    done = store.revise_many(["c", "FR-1"], lambda item: [Assign("component", item.slug * 2)])
    assert [(i.id, i.fields["component"]) for i in done] == [("FR-3", "cc"), ("FR-1", "aa")]
    assert store.item("b").fields["component"] == "b"
    assert (
        git(store.root, "log", "-1", "--format=%s").strip() == "friction: set 2 records FR-3 FR-1"
    )
    changed = git(store.root, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(changed) == ["a.md", "c.md", "index.md"]
    assert git(store.root, "status", "--porcelain") == ""


def test_revise_many_with_one_ref_commits_as_revise(store):
    """One ref writes the message `revise` writes."""
    _three(store)
    store.revise_many(["b"], lambda _: [Assign("component", "z")])
    assert git(store.root, "log", "-1", "--format=%s").strip() == "friction: set FR-2 b"


def test_revise_many_writes_nothing_on_a_finding(store_dir, clock):
    """A finding on any one item's result refuses the whole write."""
    store = Store(store_dir, clock, rules=[no_legacy_titles])
    commits = _three(store)
    with pytest.raises(InvalidStore, match="FR-2: title mentions legacy"):
        store.revise_many(
            ["a", "b"],
            lambda item: [Assign("title", "legacy" if item.id == "FR-2" else "new")],
        )
    assert store.item("a").fields["title"] == "a"
    assert git(store.root, "rev-list", "--count", "HEAD") == commits
    assert git(store.root, "status", "--porcelain") == ""


def test_revise_many_writes_nothing_when_a_plan_refuses(store):
    """A plan that raises on a later ref leaves the earlier refs unwritten."""
    commits = _three(store)

    def refuse_c(item):
        """Refuse item c only."""
        if item.slug == "c":
            raise ItemError("not c")
        return [Assign("component", "z")]

    with pytest.raises(ItemError, match="not c"):
        store.revise_many(["a", "b", "c"], refuse_c)
    assert store.item("a").fields["component"] == "a"
    assert git(store.root, "rev-list", "--count", "HEAD") == commits


def test_revise_many_refuses_two_refs_to_one_item(store):
    """An id and the slug of one item refuse before any plan runs."""
    commits = _three(store)
    planned = []
    with pytest.raises(ItemError, match="'a' and 'FR-1' name the same item"):
        store.revise_many(["a", "b", "FR-1"], lambda item: planned.append(item) or [])
    assert planned == []
    assert git(store.root, "rev-list", "--count", "HEAD") == commits


def test_revise_many_that_changes_nothing_commits_nothing(store):
    """Plans leaving every item as it is return the items and write no commit."""
    commits = _three(store)
    done = store.revise_many(["a", "b"], lambda item: [Assign("component", item.slug)])
    assert [i.id for i in done] == ["FR-1", "FR-2"]
    assert git(store.root, "rev-list", "--count", "HEAD") == commits
    assert git(store.root, "status", "--porcelain") == ""
