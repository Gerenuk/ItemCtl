"""Frozen values every layer shares (schema, items, snapshots, change sets) and errors."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from types import MappingProxyType

MANAGED = ("id", "state", "created", "claimed_by", "claimed_at", "lease_until")
LEASE = ("claimed_by", "claimed_at", "lease_until")


class ItemError(Exception):
    """A refused request or a malformed store; the message names the offending value."""


class NoStore(ItemError):
    """No itemctl store where one was looked for: no store.toml, or one without [kinds]."""


class NothingClaimable(ItemError):
    """`claim --next` found no item it may lease."""


@dataclass(frozen=True)
class Finding:
    """One validation or ageing result, tied to a store-relative path."""

    rule: str
    where: str
    message: str

    def __str__(self) -> str:
        """`where: message`."""
        return f"{self.where}: {self.message}"


class InvalidStore(ItemError):
    """Findings that stop a write (nothing is written) or a read of unparseable items."""

    def __init__(self, findings: list[Finding]) -> None:
        """Keep the findings and list them in the message."""
        self.findings = findings
        super().__init__("invalid store:\n" + "\n".join(f"  {f}" for f in findings))


@dataclass(frozen=True)
class Field:
    """A declared frontmatter field; `choices` is non-empty exactly for an enum."""

    name: str
    type: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class State:
    """A state: fields it requires (all; one of `requires_any`) and fields only it holds."""

    name: str
    requires: tuple[str, ...]
    requires_any: tuple[str, ...]
    only_here: tuple[str, ...]
    terminal: bool


@dataclass(frozen=True)
class Kind:
    """An item type declared in store.toml; `states[0]` is the initial state."""

    name: str
    dir: PurePosixPath
    fields: tuple[Field, ...]
    states: tuple[State, ...]
    dedup_key: tuple[str, ...]
    sections: tuple[str, ...]
    attachments: bool
    index: tuple[str, ...]
    max_age_days: int | None

    def field(self, name: str) -> Field | None:
        """The declared field `name`, or None for an undeclared one."""
        return next((f for f in self.fields if f.name == name), None)

    def find_state(self, name: object) -> State | None:
        """The state `name`, or None when the kind does not declare it."""
        return next((s for s in self.states if s.name == name), None)

    def state(self, name: object) -> State:
        """The state `name`; raise when the kind does not declare it."""
        if state := self.find_state(name):
            return state
        known = ", ".join(s.name for s in self.states)
        raise ItemError(f"kind {self.name!r} has no state {name!r} (states: {known})")


@dataclass(frozen=True)
class Schema:
    """A parsed store.toml; `writer` names another tool that owns writes, when one does."""

    id_prefix: str
    id_width: int
    git_commit: bool
    kinds: tuple[Kind, ...]
    writer: str | None = None

    def kind(self, name: str) -> Kind:
        """The kind `name`; raise when the store does not declare it."""
        for kind in self.kinds:
            if kind.name == name:
                return kind
        known = ", ".join(k.name for k in self.kinds)
        raise ItemError(f"no kind {name!r} in this store (kinds: {known})")


@dataclass(frozen=True)
class Item:
    """One item file: its kind, store-relative path, frontmatter and body."""

    kind: str
    path: PurePosixPath
    fields: Mapping[str, object]
    body: str

    @property
    def id(self) -> str:
        """The managed `id` field, as text."""
        return str(self.fields.get("id"))

    @property
    def slug(self) -> str:
        """The file name the title gave the item: `<slug>.md` or `<slug>/item.md`."""
        return self.path.parent.name if self.path.name == "item.md" else self.path.stem


class DuplicateError(ItemError):
    """An add matched non-terminal items on the kind's dedup_key; `matches` holds them."""

    def __init__(self, message: str, matches: tuple[Item, ...]) -> None:
        """Keep the matched items beside the message."""
        self.matches = matches
        super().__init__(message)


@dataclass(frozen=True)
class Snapshot:
    """Every file of a store, read once: parsed items, unparseable files and unexpected files."""

    schema: Schema
    items: tuple[Item, ...]
    broken: tuple[Finding, ...]
    stray: tuple[str, ...]


type Rule = Callable[[Snapshot], list[Finding]]
"""A caller's store-wide check, run with `validate` on every write and by `check`."""


@dataclass(frozen=True)
class ChangeSet:
    """One verb's effect: `old` (None for an add) becomes `new`."""

    verb: str
    old: Item | None
    new: Item


@dataclass(frozen=True)
class Assign:
    """`f=v`: set a field."""

    field: str
    value: object


@dataclass(frozen=True)
class Extend:
    """`f+=v`: append a value to a list field."""

    field: str
    value: str


@dataclass(frozen=True)
class Unset:
    """`f=`: remove a field."""

    field: str


@dataclass(frozen=True)
class AppendLine:
    """`--append SECTION TEXT`: add a dated line to a body section."""

    section: str
    text: str


type Edit = Assign | Extend | Unset | AppendLine


@dataclass(frozen=True)
class NewItem:
    """An add request; `dedup=False` files it even when a live item matches the dedup_key."""

    kind: str
    title: str
    edits: tuple[Edit, ...] = ()
    body: str = ""
    dedup: bool = True


def freeze(value: object) -> object:
    """A deep read-only copy: mappings become mapping proxies, lists become tuples."""
    if isinstance(value, Mapping):
        return frozen_fields(value)
    if isinstance(value, list | tuple):
        return tuple(freeze(v) for v in value)
    return value


def frozen_fields(fields: Mapping[str, object]) -> Mapping[str, object]:
    """A read-only frontmatter mapping with every value frozen."""
    return MappingProxyType({k: freeze(v) for k, v in fields.items()})


def thaw(value: object) -> object:
    """The inverse of `freeze`: plain dicts and lists, ready to serialize."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


def is_empty(value: object) -> bool:
    """Whether a field value is null or empty, which the store never writes: it omits the field."""
    return value in (None, "", ())


def text_of(value: object) -> str:
    """A scalar as `--where` and tables spell it: `true`/`false`, ISO dates, lists comma-joined."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return ", ".join(text_of(v) for v in value)
    return str(value)
