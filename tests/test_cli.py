"""The CLI in real processes: concurrency, exit codes, and the hooks."""

import json
import re
import shutil
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor

import pytest

from itemctl import schema
from itemctl.model import ItemError, NoStore
from tests.conftest import FIXTURE, git, tree


def itemctl(store_dir, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    """Run `python -m itemctl --store DIR ARGS` as its own process."""
    return subprocess.run(
        [sys.executable, "-m", "itemctl", "--store", str(store_dir), *args],
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )


def parallel(store_dir, argvs: list[list[str]]) -> list[subprocess.CompletedProcess[str]]:
    """Run every argv at once."""
    with ThreadPoolExecutor(len(argvs)) as pool:
        return list(pool.map(lambda argv: itemctl(store_dir, *argv), argvs))


def _add_writer(store_dir, writer: str) -> None:
    """Declare `writer` in the store's committed store.toml, in the top-level table."""
    toml = store_dir / "store.toml"
    text = toml.read_text().replace("\n[kinds.", f'\nwriter = "{writer}"\n\n[kinds.', 1)
    toml.write_text(text)
    git(store_dir, "commit", "-qam", "declare writer")


def test_parallel_adds_get_distinct_ids_and_leave_git_clean(store_dir):
    """N concurrent adds: N distinct ids, N commits, clean status."""
    runs = parallel(store_dir, [["add", "friction", f"t{i}", f"component=c{i}"] for i in range(8)])
    assert [r.returncode for r in runs] == [0] * 8, [r.stderr for r in runs]
    assert sorted(r.stdout.strip() for r in runs) == sorted(f"FR-{i}" for i in range(1, 9))
    assert git(store_dir, "status", "--porcelain") == ""
    assert len(git(store_dir, "log", "--format=%s").splitlines()) == 9


def test_parallel_claim_next_claims_each_item_exactly_once(store_dir):
    """N processes each `claim --next` over M < N items: each item once, the rest exit 1."""
    for i in range(5):
        assert itemctl(store_dir, "add", "task", f"t{i}").returncode == 0
    runs = parallel(store_dir, [["claim", "--next", "task", "--by", f"h{i}"] for i in range(8)])
    won = [r.stdout.strip() for r in runs if r.returncode == 0]
    assert sorted(won) == [f"FR-{i}" for i in range(1, 6)]
    assert sorted(r.returncode for r in runs) == [0] * 5 + [1] * 3


def test_exit_codes_and_output(store_dir):
    """Duplicate exits 3, an error 2, check findings 1; show and list print the store."""
    assert itemctl(store_dir, "add", "friction", "a", "component=x").stdout == "FR-1\n"
    assert itemctl(store_dir, "add", "friction", "b", "component=x").returncode == 3
    assert itemctl(store_dir, "set", "a", "state=nope").returncode == 2
    assert (
        itemctl(store_dir, "set", "a", "tags+=t", "--append", "Occurrences", "seen").returncode == 0
    )
    assert re.search(r"\n- \d{4}-\d\d-\d\d — seen\n", itemctl(store_dir, "show", "FR-1").stdout)
    listed = json.loads(itemctl(store_dir, "list", "--json").stdout)
    assert (listed["id"], listed["tags"], listed["path"]) == ("FR-1", ["t"], "a.md")
    (store_dir / "stray.txt").write_text("x")
    assert itemctl(store_dir, "check").returncode == 1


def test_a_declared_writer_refuses_every_write_verb_and_leaves_nothing_written(store_dir):
    """add/set/claim/release all refuse (exit 2, naming the writer) once the store declares one;
    files and git history stay exactly as they were, and show/list/check keep working."""
    itemctl(store_dir, "add", "friction", "a", "component=x")
    _add_writer(store_dir, "claude-ctl friction")
    before, head = tree(store_dir), git(store_dir, "rev-parse", "HEAD")
    for argv in (
        ["add", "friction", "b", "component=y"],
        ["set", "a", "component=y"],
        ["claim", "a", "--by", "h"],
        ["release", "a", "--by", "h"],
    ):
        run = itemctl(store_dir, *argv)
        assert run.returncode == 2
        assert "claude-ctl friction" in run.stderr and "does not know" in run.stderr
    assert tree(store_dir) == before
    assert git(store_dir, "rev-parse", "HEAD") == head
    assert git(store_dir, "status", "--porcelain") == ""
    assert itemctl(store_dir, "show", "a").returncode == 0
    assert itemctl(store_dir, "list").returncode == 0
    assert itemctl(store_dir, "check").returncode == 0


def test_without_a_writer_every_write_verb_runs(store_dir):
    """The nearby allow case: with no writer declared, add/set/claim/release all succeed."""
    assert itemctl(store_dir, "add", "friction", "a", "component=x").returncode == 0
    assert itemctl(store_dir, "set", "a", "component=y").returncode == 0
    assert itemctl(store_dir, "claim", "a", "--by", "h").returncode == 0
    assert itemctl(store_dir, "release", "a", "--by", "h").returncode == 0


def test_writer_refusal_precedes_reading_the_item(store_dir):
    """set/claim/release name the writer even for a REF that does not exist: the refusal is
    checked before the item is read (or the lock taken), not after a failed lookup."""
    _add_writer(store_dir, "claude-ctl friction")
    for argv in (
        ["set", "nope", "x=y"],
        ["claim", "nope", "--by", "h"],
        ["release", "nope", "--by", "h"],
    ):
        run = itemctl(store_dir, *argv)
        assert run.returncode == 2
        assert "claude-ctl friction" in run.stderr


def test_check_notes_a_declared_writer_only_when_declared(store_dir):
    """check's stderr carries the writer note only once one is declared; its exit code and
    stdout are unaffected either way."""
    itemctl(store_dir, "add", "friction", "a", "component=x")
    plain = itemctl(store_dir, "check")
    assert (plain.returncode, plain.stdout, plain.stderr) == (0, "ok\n", "")
    _add_writer(store_dir, "claude-ctl friction")
    noted = itemctl(store_dir, "check")
    assert (noted.returncode, noted.stdout) == (0, "ok\n")
    assert "claude-ctl friction" in noted.stderr and "were not checked" in noted.stderr


def test_session_start_summarises_each_kind(store_dir):
    """One line per kind with counts, age, stale and expired counts."""
    itemctl(store_dir, "add", "friction", "a", "component=x")
    out = itemctl(store_dir, "hook", "session-start").stdout.splitlines()
    assert out[0].startswith("itemctl friction: open 1 · oldest 0d · 0 stale · 0 expired leases")
    assert out[1] == "itemctl task: empty · none open · 0 stale · 0 expired leases"


def pre_tool_use(store_dir, tool: str, **tool_input) -> str:
    """The pre-tool-use hook's stdout for one tool call."""
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
    run = itemctl(store_dir, "hook", "pre-tool-use", stdin=payload)
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_pre_tool_use_guards_frontmatter_only(store_dir):
    """Deny frontmatter edits and new items by hand; allow body edits and non-item files."""
    itemctl(store_dir, "add", "friction", "a", "component=x", "--body-file", "-", stdin="Hello\n")
    path = str(store_dir / "a.md")
    assert "deny" in pre_tool_use(store_dir, "Edit", file_path=path, old_string="x", new_string="y")
    assert (
        pre_tool_use(store_dir, "Edit", file_path=path, old_string="Hello", new_string="Bye") == ""
    )
    assert "itemctl add" in pre_tool_use(
        store_dir, "Write", file_path=str(store_dir / "n.md"), content=""
    )
    assert pre_tool_use(store_dir, "Write", file_path=str(store_dir / "index.md"), content="") == ""
    assert pre_tool_use(store_dir, "Read", file_path=path) == ""


def test_pre_tool_use_denials_name_the_declared_writer(store_dir):
    """With a declared writer, both deny messages name it instead of itemctl; a body edit
    stays allowed exactly as without one."""
    itemctl(store_dir, "add", "friction", "a", "component=x", "--body-file", "-", stdin="Hello\n")
    _add_writer(store_dir, "claude-ctl friction")
    path = str(store_dir / "a.md")
    new_denial = pre_tool_use(store_dir, "Write", file_path=str(store_dir / "n.md"), content="")
    assert "claude-ctl friction" in new_denial and "itemctl add" not in new_denial
    fm_denial = pre_tool_use(store_dir, "Edit", file_path=path, old_string="x", new_string="y")
    assert "claude-ctl friction" in fm_denial and "itemctl set" not in fm_denial
    assert (
        pre_tool_use(store_dir, "Edit", file_path=path, old_string="Hello", new_string="Bye") == ""
    )


def test_hooks_pass_over_a_foreign_store_toml_but_not_a_broken_one(tmp_path):
    """A store.toml without [kinds] is another tool's: both hooks allow and stay silent.
    One with [kinds] that does not parse is a broken itemctl store: the hook fails loudly."""
    (tmp_path / "store.toml").write_text('[tool]\nname = "other"\n')
    payload = json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "a.md"), "content": ""}}
    )
    guard = itemctl(tmp_path, "hook", "pre-tool-use", stdin=payload)
    assert (guard.returncode, guard.stdout, guard.stderr) == (0, "", "")
    start = itemctl(tmp_path, "hook", "session-start")
    assert (start.returncode, start.stdout, start.stderr) == (0, "", "")
    (tmp_path / "store.toml").write_text(
        'format_version = 1\nid_prefix = "K"\n[kinds.k]\nfields = 3\n'
    )
    broken = itemctl(tmp_path, "hook", "pre-tool-use", stdin=payload)
    assert broken.returncode == 1 and "fields must be dict" in broken.stderr


def test_a_missing_or_foreign_store_toml_raises_no_store(tmp_path):
    """schema.load raises the typed NoStore for an absent file and for one without [kinds]."""
    with pytest.raises(NoStore, match="no store.toml"):
        schema.load(tmp_path)
    (tmp_path / "store.toml").write_text('[tool]\nname = "other"\n')
    with pytest.raises(NoStore, match="not an itemctl store"):
        schema.load(tmp_path)
    assert itemctl(tmp_path, "list").returncode == 2
    assert itemctl(tmp_path / "missing", "hook", "session-start").returncode == 0


def guard_as_wired(payload: str) -> tuple[subprocess.CompletedProcess[str], bool]:
    """`itemctl hook pre-tool-use` exactly as Claude Code runs it (entry's fast path first), and
    whether that process imported `itemctl.cli`."""
    run = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "itemctl", "hook", "pre-tool-use"],
        capture_output=True,
        text=True,
        input=payload,
        check=False,
    )
    timings = [line for line in run.stderr.splitlines() if line.startswith("import time:")]
    run.stderr = "".join(f"{line}\n" for line in run.stderr.splitlines() if line not in timings)
    return run, any(line.endswith("| itemctl.cli") for line in timings)


@pytest.mark.parametrize(
    ("where", "tool", "denied"),
    [
        ("store/a.md", "Edit", True),  # an item's frontmatter: denied
        ("a.md", "Edit", False),  # the same file name beside the store, under no store.toml
        ("store/a.md", "Read", False),  # a tool the guard does not inspect
    ],
)
def test_guard_fast_path_answers_as_the_full_cli(tmp_path, where, tool, denied):
    """The wired guard answers exactly as the full CLI does (`--store` bypasses the fast path),
    and settles a call outside every store without importing the CLI."""
    shutil.copytree(FIXTURE, tmp_path / "store")
    git(tmp_path, "init", "-q", str(tmp_path))
    assert itemctl(tmp_path / "store", "add", "friction", "a", "component=x").returncode == 0
    (tmp_path / "a.md").write_text("---\nx: 1\n---\n")
    tool_input = {"file_path": str(tmp_path / where), "old_string": "x", "new_string": "y"}
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
    wired, imported_cli = guard_as_wired(payload)
    full = itemctl(tmp_path / "store", "hook", "pre-tool-use", stdin=payload)
    answer = (wired.returncode, wired.stdout, wired.stderr)
    assert answer == (full.returncode, full.stdout, full.stderr)
    assert ("deny" in wired.stdout) is denied
    assert imported_cli is (where == "store/a.md" and tool != "Read")


def test_guard_fast_path_leaves_a_malformed_payload_to_the_full_cli():
    """A payload the guard cannot read fails exactly as the full CLI reports it."""
    wired, imported_cli = guard_as_wired('{"tool_name": "Edit"}')
    assert (wired.returncode, imported_cli) == (1, True)
    assert wired.stderr == "itemctl hook pre-tool-use: 'tool_input'\n"


def test_pre_tool_use_denies_edits_to_a_linked_worktrees_store_copy(tmp_path):
    """Writers write only the main checkout: any edit under a worktree's store copy is denied,
    naming the main checkout's path; files outside the store and the main copy stay open."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo / "store")
    (repo / "README.md").write_text("readme\n")
    git(tmp_path, "init", "-q", str(repo))
    assert itemctl(repo / "store", "add", "friction", "a", "component=x").returncode == 0
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "store")
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))
    body = {"old_string": "## none", "new_string": "## body"}
    denied = pre_tool_use(repo, "Edit", file_path=str(worktree / "store/a.md"), **body)
    assert "deny" in denied and str(repo.resolve() / "store/a.md") in denied
    index = str(worktree / "store/index.md")
    assert "deny" in pre_tool_use(repo, "Write", file_path=index, content="")
    assert pre_tool_use(repo, "Write", file_path=str(worktree / "README.md"), content="") == ""
    assert pre_tool_use(repo, "Edit", file_path=str(repo / "store/a.md"), **body) == ""


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"format_version": 2}, "format_version"),
        ({"surprise": 1}, "unknown keys"),
        (
            {"kinds": {"k": {"fields": {"title": "str", "x": "float"}, "states": {"s": {}}}}},
            "float",
        ),
        (
            {"kinds": {"k": {"fields": {"title": "str"}, "states": {"s": {"requires": ["y"]}}}}},
            "'y'",
        ),
        ({"kinds": {"k": {"fields": {"name": "str"}, "states": {"s": {}}}}}, "title"),
        ({"writer": ""}, "writer"),
        ({"writer": 3}, "writer"),
    ],
)
def test_schema_refuses_unknowns(patch, match):
    """store.toml: unknown versions, keys, types and undeclared field names raise."""
    data = tomllib.loads((FIXTURE / "store.toml").read_text()) | patch
    with pytest.raises(ItemError, match=match):
        schema.parse(data, "store.toml")


def test_schema_accepts_a_declared_writer():
    """The nearby allow case: a non-empty string writer parses and round-trips on the schema."""
    data = tomllib.loads((FIXTURE / "store.toml").read_text()) | {"writer": "claude-ctl friction"}
    assert schema.parse(data, "store.toml").writer == "claude-ctl friction"
