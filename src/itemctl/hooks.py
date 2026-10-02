"""Hooks: Claude Code's session-start summary and guard on hand edits to item frontmatter, and
git's reference-transaction check on a landing that brings an id already used."""

from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path, PurePosixPath

from itemctl import gitio, landing, record, schema, scope, validate
from itemctl.model import ItemError, NoStore, text_of
from itemctl.store import Store


def session_start(store: Store) -> list[str]:
    """One line per kind: counts per state, oldest non-terminal age, stale items, expired leases."""
    snap, now = store.snapshot(), store.clock()
    lines = []
    for kind in store.schema.kinds:
        items = tuple(i for i in snap.items if i.kind == kind.name)
        counts = Counter(text_of(i.fields.get("state")) for i in items)
        states = ", ".join(f"{s.name} {counts[s.name]}" for s in kind.states if counts[s.name])
        live = [
            created
            for i in items
            if (s := kind.find_state(i.fields.get("state")))
            and not s.terminal
            and isinstance(created := i.fields.get("created"), datetime)
        ]
        oldest = f"oldest {(now - min(live)).days}d" if live else "none open"
        rules = Counter(f.rule for f in validate.overdue(replace(snap, items=items), now))
        lines.append(
            f"{kind.name}: {states or 'empty'} · {oldest} · "
            f"{rules['stale']} stale · {rules['expired']} expired leases"
        )
    return lines


def _text_after(tool: str, args: dict, before: str) -> str:
    """The file text a Write, Edit or MultiEdit call would leave of `before`."""
    if tool == "Write":
        return args["content"]
    edits = args["edits"] if tool == "MultiEdit" else [args]
    text = before
    for edit in edits:
        count = -1 if edit.get("replace_all") else 1
        text = text.replace(edit["old_string"], edit["new_string"], count)
    return text


def _frontmatter(text: str) -> str | None:
    """The raw frontmatter block of an item's text, or None when it has none."""
    try:
        return record.split(text, "")[0]
    except ItemError:
        return None


def pre_tool_use(payload: dict) -> str | None:
    """The reason to deny a Write/Edit/MultiEdit that creates an item, changes its frontmatter,
    or touches a linked worktree's copy of a store."""
    tool = payload.get("tool_name")
    if tool not in scope.GUARDED_TOOLS:
        return None
    args = payload["tool_input"]
    path = Path(args["file_path"]).resolve()
    found = scope.store_root(args["file_path"])
    if found is None:
        return None
    root = Path(found)
    try:
        store_schema = schema.load(root)
    except NoStore:
        return None
    main = gitio.main_checkout(root)
    if main != root:
        return (
            f"{path} is in a linked worktree's copy of the store, which itemctl never writes; "
            f"edit {main / path.relative_to(root)} in the main checkout instead"
        )
    tag, _ = schema.locate(store_schema, PurePosixPath(path.relative_to(root).as_posix()))
    if tag != "item":
        return None
    before = path.read_text("utf-8") if path.is_file() else None
    writer = store_schema.writer
    if before is None:
        cmd = writer or "itemctl add KIND TITLE"
        return f"{path} would be a new item; create items with `{cmd}`"
    old_block = _frontmatter(before)
    if old_block is not None and _frontmatter(_text_after(tool, args, before)) != old_block:
        if writer:
            return (
                f"{path}: item frontmatter is written only by `{writer}` "
                "(the body below the frontmatter may be edited freely)"
            )
        return (
            f"{path}: item frontmatter is written only by itemctl; use "
            "`itemctl set REF f=v` (the body below the frontmatter may be edited freely)"
        )
    return None


def reference_transaction(repo: Path, updates: str) -> str | None:
    """The reason to refuse a prepared git ref transaction (`updates`: its stdin, lines of
    `<old> <new> <ref>`) that moves the branch the main worktree has checked out to a commit
    bringing a record id that branch already uses (`landing.brought_duplicates`)."""
    branch = gitio.main_branch(repo)
    for line in updates.splitlines():
        old, new, ref = line.split(" ", 2)
        if ref != branch or new in gitio.ZERO_OID:
            continue
        # git passes the zero id as `old` when the update named no expected old value
        # (`git update-ref REF NEW`); the ref, locked but not yet moved, still holds it.
        start = old if old not in gitio.ZERO_OID else gitio.ref_target(repo, ref)
        if found := landing.brought_duplicates(repo, start, new):
            lines = "\n".join(f"  {f}" for f in found)
            return (
                f"itemctl: refusing to move {ref} to {new[:12]}: it brings record ids "
                f"{ref.removeprefix('refs/heads/')} already uses.\n{lines}\n"
                "Fix: on the branch being landed, change each such record's `id:` line to the "
                "free id named above, commit, and land again. If a merge already wrote the "
                "branch's files into the checkout, `git reset --merge` there puts them back."
            )
    return None
