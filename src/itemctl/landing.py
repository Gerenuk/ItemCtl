"""Record ids across git commits: the stores a commit's tree holds with each item's id, and the
ids a branch moving from one commit to another brings that the old commit already used."""

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from itemctl import fsio, gitio, record, schema
from itemctl.model import Finding, ItemError, NoStore, Schema
from itemctl.scope import STORE_FILE

#: Under the git common dir: item id by blob id (null: the blob does not parse as an item).
#: A blob never changes, so an entry never goes stale; it saves re-parsing every item per check.
CACHE = PurePosixPath("itemctl") / "item-ids.json"


@dataclass(frozen=True)
class TreeStore:
    """A store as one commit holds it: its schema and each item's id by store-relative path
    (None: the item does not parse, or has no id)."""

    schema: Schema
    ids: dict[PurePosixPath, str | None]


class _Ids:
    """Item ids by blob id, read through the cache file and written back by `save`."""

    def __init__(self, repo: Path) -> None:
        """Load the cache of `repo`'s common dir; raise naming the file when it does not parse."""
        common = gitio.checked(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.repo, self.path = repo, Path(common) / CACHE
        try:
            self.known = json.loads(self.path.read_text("utf-8")) if self.path.is_file() else {}
        except ValueError as exc:
            raise ItemError(f"{self.path} is not JSON ({exc}); delete it") from exc
        self.loaded = dict(self.known)
        self.used: dict[str, str | None] = {}

    def of(self, blobs: dict[str, str]) -> dict[str, str | None]:
        """The item id in each blob of `blobs` (path → blob id): path → id or None."""
        missing = sorted({o for o in blobs.values() if o not in self.known})
        for oid, text in zip(missing, gitio.read_blobs(self.repo, missing), strict=True):
            try:
                found = record.parse(text.decode(), oid)[0].get("id")
            except ItemError, UnicodeDecodeError:
                found = None
            self.known[oid] = None if found is None else str(found)
        self.used.update((o, self.known[o]) for o in blobs.values())
        return {path: self.known[oid] for path, oid in blobs.items()}

    def save(self) -> None:
        """Write back the entries this check used, so the file holds no more than two trees'."""
        if self.used != self.loaded:
            fsio.write(self.path, json.dumps(self.used, sort_keys=True))


def tree_stores(repo: Path, rev: str, ids: _Ids) -> dict[PurePosixPath, TreeStore]:
    """Every itemctl store committed in `rev`'s tree, by its directory relative to the repository
    top. A store.toml without [kinds] is another tool's and is skipped; a file under a nested
    store belongs to the nested one."""
    blobs = gitio.tree_blobs(repo, rev)
    tomls = [p for p in blobs if PurePosixPath(p).name == STORE_FILE]
    schemas = {}
    for path, text in zip(tomls, gitio.read_blobs(repo, [blobs[p] for p in tomls]), strict=True):
        try:
            schemas[PurePosixPath(path).parent] = schema.loads(text.decode(), f"{rev}:{path}")
        except NoStore:
            continue
    prefixes = sorted(((f"{r}/" if r.parts else "", r) for r in schemas), reverse=True)
    items: dict[PurePosixPath, dict[str, str]] = {root: {} for root in schemas}
    for path, oid in blobs.items():
        root = next((r for p, r in prefixes if path.startswith(p)), None)
        if root is not None:
            tag, _ = schema.locate(schemas[root], PurePosixPath(path).relative_to(root))
            if tag == "item":
                items[root][path] = oid
    return {
        root: TreeStore(
            schemas[root],
            {PurePosixPath(p).relative_to(root): i for p, i in ids.of(items[root]).items()},
        )
        for root in schemas
    }


def brought_duplicates(repo: Path, old: str | None, new: str) -> list[Finding]:
    """One finding per item that moving a branch from commit `old` (None: no commit) to `new`
    brings with an id used more often in `new` than in `old`, naming the items that also hold it
    and the next free id to renumber it to. An item `old` holds at the same path with the same id
    is not brought, so the record already there keeps its id. `old` is read only when `new`
    has an id used twice."""
    ids = _Ids(repo)
    counted = {
        root: (store, Counter(i for i in store.ids.values() if i is not None))
        for root, store in tree_stores(repo, new, ids).items()
    }
    twice = any(n > 1 for _, counts in counted.values() for n in counts.values())
    before = tree_stores(repo, old, ids) if old and twice else {}
    found = []
    for root, (store, counts) in counted.items() if twice else ():
        prior = before[root].ids if root in before else {}
        old_counts = Counter(i for i in prior.values() if i is not None)
        free = max((schema.id_number(store.schema, i) or 0 for i in counts), default=0)
        for path, item_id in store.ids.items():
            if (
                item_id is not None
                and counts[item_id] > max(1, old_counts[item_id])
                and prior.get(path) != item_id
            ):
                free += 1
                others = [str(root / p) for p, i in store.ids.items() if i == item_id and p != path]
                found.append(
                    Finding(
                        "brought_duplicate_ids",
                        str(root / path),
                        f"id {item_id} is also used by {', '.join(others)}; "
                        f"renumber this one to {schema.format_id(store.schema, free)}",
                    )
                )
    ids.save()
    return found
