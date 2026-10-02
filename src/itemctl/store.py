"""The `Store` verbs: lock → snapshot → change → validate → write → index → commit."""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Self

from itemctl import changes, fsio, gitio, record, render_index, schema, validate
from itemctl.model import (
    ChangeSet,
    Edit,
    Finding,
    InvalidStore,
    Item,
    ItemError,
    Kind,
    NewItem,
    NoStore,
    Rule,
    Snapshot,
)


def now() -> datetime:
    """The current local time, timezone-aware, to the second."""
    return datetime.now().astimezone().replace(microsecond=0)


def find_root(start: Path) -> Path:
    """The nearest directory at or above `start` holding store.toml."""
    for directory in (start, *start.parents):
        if (directory / "store.toml").is_file():
            return directory
    raise NoStore(f"no store.toml at or above {start}; pass --store PATH")


def _message(made: Sequence[ChangeSet]) -> str:
    """The commit message: `<kind>: <verb> <id> <slug>` for one change, else
    `<kind>: <verb> <n> records <id>…` with the first change's kind and verb."""
    first = made[0].new
    if len(made) == 1:
        return f"{first.kind}: {made[0].verb} {first.id} {first.slug}"
    ids = " ".join(change.new.id for change in made)
    return f"{first.kind}: {made[0].verb} {len(made)} records {ids}"


class Store:
    """A store directory and its parsed schema; `clock` supplies every timestamp.

    `rules` are the caller's own store-wide checks: every write is refused when one reports
    a finding on the result, exactly like a `validate` finding, and `check` reports them.
    """

    def __init__(
        self, root: Path, clock: Callable[[], datetime] = now, rules: Sequence[Rule] = ()
    ) -> None:
        """Load `<root>/store.toml`."""
        self.root = root
        self.schema = schema.load(root)
        self.clock = clock
        self.rules = tuple(rules)

    @classmethod
    def open(
        cls,
        path: Path | None = None,
        clock: Callable[[], datetime] = now,
        rules: Sequence[Rule] = (),
    ) -> Self:
        """The store at `path`, else the nearest above the cwd; a linked worktree maps to main."""
        found = find_root(path.resolve() if path else Path.cwd())
        root = gitio.main_checkout(found)
        try:
            return cls(root, clock, rules)
        except NoStore as exc:
            if root == found:
                raise
            raise NoStore(f"{exc} (the main checkout of {found}); merge the store first") from exc

    def snapshot(self) -> Snapshot:
        """Every file of the store, each item parsed once."""
        items, broken, stray = [], [], []
        for rel in fsio.walk(self.root):
            tag, kind = schema.locate(self.schema, rel)
            if tag == "stray":
                stray.append(str(rel))
            elif tag == "item" and kind is not None:
                try:
                    fields, body = record.parse((self.root / rel).read_text("utf-8"), str(rel))
                except ItemError as exc:
                    broken.append(Finding("parse_errors", str(rel), str(exc)))
                else:
                    items.append(Item(kind.name, rel, fields, body))
        return Snapshot(self.schema, tuple(items), tuple(broken), tuple(stray))

    def _write(self, build: Callable[[Snapshot], Sequence[ChangeSet]]) -> list[Item]:
        """Run the write pipeline for the changes `build` makes, applied in order; return
        each change's new item.

        A validation finding writes nothing; so do changes that leave every file as it is.
        All changes land in one commit. A failure after the first file step undoes every step
        before re-raising.
        """
        with fsio.lock(self.root):
            if self.schema.git_commit:
                gitio.check_writable(self.root)
            after = self.snapshot()
            made = build(after)
            for change in made:
                after = changes.apply(after, change)
            if findings := self._problems(after):
                raise InvalidStore(findings)
            news = [change.new for change in made]
            steps = list(dict.fromkeys(s for change in made for s in self._steps(change, after)))
            if not steps:
                return news
            touched = list(dict.fromkeys(p for step in steps for p in step.paths))
            if self.schema.git_commit:
                gitio.check_not_ignored(self.root, touched)
            journal = fsio.Journal()
            try:
                for step in steps:
                    journal.apply(step)
                if self.schema.git_commit:
                    gitio.commit(self.root, touched, _message(made))
            except BaseException:
                journal.rollback()
                raise
        return news

    def _steps(self, change: ChangeSet, after: Snapshot) -> list[fsio.Step]:
        """The file steps of a change: the item, then its kind's index.md when that changes.

        An item whose path changes is written in place, then renamed (with its attachments
        directory), so it is always at exactly one path. No steps when the item keeps its path
        and bytes.
        """
        old, new = change.old, change.new
        kind = self.schema.kind(new.kind)
        dst = self.root / new.path
        text = record.render(kind, new.fields, new.body)
        steps: list[fsio.Step]
        if old is None:
            steps = [fsio.Write(dst, text)]
        elif old.path == new.path:
            if dst.read_bytes() == text.encode("utf-8"):
                return []
            steps = [fsio.Write(dst, text)]
        else:
            src = self.root / old.path
            moved = fsio.Move(src.parent, dst.parent) if kind.attachments else fsio.Move(src, dst)
            steps = [fsio.Write(src, text), moved]
        return steps + self._index_steps(after, kind)

    def _index_steps(self, snap: Snapshot, kind: Kind) -> list[fsio.Step]:
        """Regenerate the kind's index.md when it declares one and its text changes."""
        if not kind.index:
            return []
        path = self.root / kind.dir / schema.INDEX
        text = render_index.index_text(snap, kind)
        if path.is_file() and path.read_text("utf-8") == text:
            return []
        return [fsio.Write(path, text)]

    def add(
        self, kind: str, title: str, edits: Sequence[Edit] = (), body: str = "", dedup: bool = True
    ) -> Item:
        """Create an item; `edits` may assign `state` and any field; `dedup=False` skips dedup."""
        request = NewItem(kind, title, tuple(edits), body, dedup)
        return self._write(lambda s: [changes.add(s, request, self.clock())])[0]

    def set(self, ref: str, edits: Sequence[Edit]) -> Item:
        """Apply `edits` to one item in one atomic write."""
        return self.revise(ref, lambda _: edits)

    def revise(self, ref: str, plan: Callable[[Item], Sequence[Edit]]) -> Item:
        """Apply the edits `plan` derives from the item as it is under the lock.

        `plan` raises ItemError to refuse, so a check of the item and the write it
        justifies see the same version (compare-and-swap).
        """
        return self.revise_many([ref], plan)[0]

    def revise_many(
        self, refs: Sequence[str], plan: Callable[[Item], Sequence[Edit]]
    ) -> list[Item]:
        """`revise` every item of `refs`, in order, in one write and one commit.

        Each item is planned on the snapshot the earlier refs' changes left, and the result is
        validated once, so a refusal from `plan` or any finding writes none of them. Two refs
        naming the same item raise ItemError before any plan runs. The new items come back in
        the order of `refs`.
        """

        def build(snap: Snapshot) -> list[ChangeSet]:
            """Each ref's change on the snapshot the earlier ones produced."""
            seen: dict[int, str] = {}
            for ref in refs:
                first = seen.setdefault(id(changes.resolve(snap, ref)), ref)
                if first != ref:
                    raise ItemError(f"refs {first!r} and {ref!r} name the same item")
            made = []
            for ref in refs:
                change = changes.update(snap, ref, plan, self.clock())
                snap = changes.apply(snap, change)
                made.append(change)
            return made

        return self._write(build)

    def _parsed(self) -> Snapshot:
        """A snapshot for reading; raise when any item file failed to parse."""
        snap = self.snapshot()
        if snap.broken:
            raise InvalidStore(list(snap.broken))
        return snap

    def claim(
        self,
        ref: str | None = None,
        *,
        holder: str,
        ttl: timedelta,
        next_kind: str | None = None,
        where: Mapping[str, str] | None = None,
    ) -> Item:
        """Lease `ref`, or with `next_kind` the oldest claimable matching item, to `holder`."""
        next_of = (next_kind, where or {}) if next_kind is not None else None

        def build(snap: Snapshot) -> list[ChangeSet]:
            """Select across the store only once it validates, so selection reads sound values."""
            if next_of is not None and (findings := self._problems(snap)):
                raise InvalidStore(findings)
            return [changes.claim(snap, ref, next_of, holder, ttl, self.clock())]

        return self._write(build)[0]

    def release(self, ref: str, *, holder: str, force: bool = False) -> Item:
        """Drop the item's lease."""
        return self._write(lambda s: [changes.release(s, ref, holder, force)])[0]

    def item(self, ref: str) -> Item:
        """The item with id or slug `ref`."""
        return changes.resolve(self._parsed(), ref)

    def items(
        self,
        kind: str | None = None,
        state: str | None = None,
        where: Mapping[str, str] | None = None,
        closed: bool = False,
    ) -> list[Item]:
        """Matching items; terminal ones only with `closed` or a terminal `state`."""
        found = []
        for item in self._parsed().items:
            item_state = self.schema.kind(item.kind).find_state(item.fields.get("state"))
            if (
                (kind is None or item.kind == kind)
                and (state is None or item.fields.get("state") == state)
                and (closed or state is not None or not (item_state and item_state.terminal))
                and changes.matches(item, where or {})
            ):
                found.append(item)
        return found

    def _problems(self, snap: Snapshot) -> list[Finding]:
        """The findings that refuse a write: every `validate` rule, then the caller's `rules`."""
        return validate.problems(snap) + [f for rule in self.rules for f in rule(snap)]

    def check(self, snap: Snapshot | None = None) -> list[Finding]:
        """Validation and `rules` findings, stale items and expired leases, of `snap` or a
        fresh snapshot."""
        snap = snap or self.snapshot()
        return self._problems(snap) + validate.overdue(snap, self.clock())
