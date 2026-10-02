"""What store.toml means: parse it into a `Schema`, map items to paths and back, read CLI values."""

import re
import tomllib
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from itemctl.model import MANAGED, Field, Item, ItemError, Kind, NoStore, Schema, State

FORMAT_VERSION = 1
FIELD_TYPES = frozenset({"str", "int", "bool", "date", "datetime", "list", "ref"})
TOP_KEYS = frozenset({"format_version", "id_prefix", "id_width", "git_commit", "kinds", "writer"})
KIND_KEYS = frozenset(
    {"dir", "dedup_key", "sections", "attachments", "index", "max_age_days", "fields", "states"}
)
STATE_KEYS = frozenset({"requires", "requires_any", "only_here", "terminal"})
ROOT_FILES = frozenset({"store.toml", ".lock", ".gitignore"})
CLOSED = "closed"
INDEX = "index.md"
ITEM_FILE = "item.md"
RESERVED_SLUGS = frozenset({"closed", "index", "item"})


def load(root: Path) -> Schema:
    """Read and parse `<root>/store.toml`; NoStore when it is absent or has no [kinds] table."""
    path = root / "store.toml"
    if not path.is_file():
        raise NoStore(f"no store.toml at {path}")
    return loads(path.read_text(encoding="utf-8"), str(path))


def loads(text: str, where: str) -> Schema:
    """Parse the text of a store.toml named `where`; NoStore when it has no [kinds] table."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ItemError(f"{where}: invalid TOML: {exc}") from exc
    if "kinds" not in data:
        raise NoStore(f"{where} has no [kinds] table, so it is not an itemctl store")
    return parse(data, where)


def _take[T](table: dict, key: str, want: type[T], default: T, where: str) -> T:
    """`table[key]` checked to be a `want`, or `default` when absent."""
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, want) or (want is int and isinstance(value, bool)):
        raise ItemError(f"{where}: {key} must be {want.__name__}, got {value!r}")
    return value


def _names(table: dict, key: str, where: str) -> tuple[str, ...]:
    """`table[key]` as a tuple of strings (empty when absent)."""
    values = _take(table, key, list, [], where)
    if not all(isinstance(v, str) for v in values):
        raise ItemError(f"{where}: {key} must be a list of strings, got {values!r}")
    return tuple(values)


def _unknown_keys(table: dict, allowed: frozenset[str], where: str) -> None:
    """Raise on any key of `table` outside `allowed`."""
    extra = sorted(set(table) - allowed)
    if extra:
        raise ItemError(f"{where}: unknown keys {extra} (allowed: {sorted(allowed)})")


def _writer(data: dict, where: str) -> str | None:
    """The optional `writer` key: the name of the tool that owns writes, or None when absent."""
    if "writer" not in data:
        return None
    value = data["writer"]
    if not isinstance(value, str) or not value:
        raise ItemError(f"{where}: writer must be a non-empty string, got {value!r}")
    return value


def parse(data: dict, where: str) -> Schema:
    """A `Schema` from store.toml's table; raise on anything undeclared or ill-typed."""
    _unknown_keys(data, TOP_KEYS, where)
    if data.get("format_version") != FORMAT_VERSION:
        raise ItemError(
            f"{where}: format_version must be {FORMAT_VERSION}, got {data.get('format_version')!r}"
        )
    prefix = _take(data, "id_prefix", str, "", where)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", prefix):
        raise ItemError(f"{where}: id_prefix must be letters then letters/digits, got {prefix!r}")
    kinds = tuple(
        _parse_kind(name, table, f"{where} [kinds.{name}]")
        for name, table in _take(data, "kinds", dict, {}, where).items()
    )
    if not kinds:
        raise ItemError(f"{where}: declares no kinds")
    dirs = [k.dir for k in kinds]
    if len(set(dirs)) != len(dirs):
        raise ItemError(f"{where}: two kinds share a directory: {[str(d) for d in dirs]}")
    width = _take(data, "id_width", int, 0, where)
    if width < 0:
        raise ItemError(f"{where}: id_width must be >= 0, got {width}")
    git_commit = _take(data, "git_commit", bool, False, where)
    return Schema(prefix, width, git_commit, kinds, _writer(data, where))


def _parse_field(name: str, spec: object, where: str) -> Field:
    """One `[kinds.K.fields]` entry: a type name, or a list of enum values."""
    if name in MANAGED:
        raise ItemError(f"{where}: {name!r} is a managed field and cannot be declared")
    if isinstance(spec, list) and spec and all(isinstance(v, str) for v in spec):
        return Field(name, "enum", tuple(spec))
    if spec in FIELD_TYPES:
        return Field(name, str(spec))
    raise ItemError(
        f"{where}: field {name!r} has type {spec!r}; expected one of {sorted(FIELD_TYPES)} "
        "or a non-empty list of enum values"
    )


def _parse_state(name: str, table: object, where: str) -> State:
    """One `[kinds.K.states]` entry."""
    if not isinstance(table, dict):
        raise ItemError(f"{where}: state {name!r} must be a table, got {table!r}")
    _unknown_keys(table, STATE_KEYS, f"{where} state {name!r}")
    return State(
        name,
        _names(table, "requires", where),
        _names(table, "requires_any", where),
        _names(table, "only_here", where),
        _take(table, "terminal", bool, False, where),
    )


def _parse_kind(name: str, table: object, where: str) -> Kind:
    """One `[kinds.K]` table, with every field name it mentions checked to be declared."""
    if not isinstance(table, dict):
        raise ItemError(f"{where}: must be a table")
    _unknown_keys(table, KIND_KEYS, where)
    fields = tuple(
        _parse_field(n, s, where) for n, s in _take(table, "fields", dict, {}, where).items()
    )
    states = tuple(
        _parse_state(n, t, where) for n, t in _take(table, "states", dict, {}, where).items()
    )
    if not states:
        raise ItemError(f"{where}: declares no states")
    declared = {f.name for f in fields}
    if "title" not in declared or next(f for f in fields if f.name == "title").type != "str":
        raise ItemError(f'{where}: must declare title = "str" (an item\'s file name comes from it)')
    max_age = _take(table, "max_age_days", int, 0, where)
    kind = Kind(
        name=name,
        dir=PurePosixPath(_take(table, "dir", str, name, where)),
        fields=fields,
        states=states,
        dedup_key=_names(table, "dedup_key", where),
        sections=_names(table, "sections", where),
        attachments=_take(table, "attachments", bool, False, where),
        index=_names(table, "index", where),
        max_age_days=max_age or None,
    )
    if kind.dir.is_absolute() or ".." in kind.dir.parts:
        raise ItemError(f"{where}: dir must stay inside the store, got {str(kind.dir)!r}")
    mentioned = [("dedup_key", n) for n in kind.dedup_key]
    mentioned += [("index", n) for n in kind.index if n not in MANAGED]
    for state in states:
        mentioned += [(f"{state.name}.requires", n) for n in state.requires]
        mentioned += [(f"{state.name}.requires_any", n) for n in state.requires_any]
        mentioned += [(f"{state.name}.only_here", n) for n in state.only_here]
    for key, field in mentioned:
        if field not in declared:
            raise ItemError(f"{where}: {key} names {field!r}, which is not a declared field")
    return kind


def item_path(kind: Kind, slug: str, terminal: bool) -> PurePosixPath:
    """Where an item lives: `<dir>[/closed]/<slug>.md`, or `.../<slug>/item.md` with attachments."""
    base = kind.dir / CLOSED if terminal else kind.dir
    return base / slug / ITEM_FILE if kind.attachments else base / f"{slug}.md"


def locate(schema: Schema, rel: PurePosixPath) -> tuple[str, Kind | None]:
    """Classify a store-relative file: ("item", kind), ("known", None) or ("stray", None)."""
    if str(rel) in ROOT_FILES:
        return "known", None
    owners = [k for k in schema.kinds if k.dir == PurePosixPath(".") or rel.is_relative_to(k.dir)]
    if not owners:
        return "stray", None
    kind = max(owners, key=lambda k: len(k.dir.parts))
    sub = rel.relative_to(kind.dir).parts
    if sub == (INDEX,) and kind.index:
        return "known", None
    if sub[0] == CLOSED:
        sub = sub[1:]
    if not sub or sub[0].removesuffix(".md") in RESERVED_SLUGS:
        return "stray", None
    if not kind.attachments and len(sub) == 1 and sub[0].endswith(".md"):
        return "item", kind
    if kind.attachments and len(sub) >= 2:
        return ("item", kind) if sub[1:] == (ITEM_FILE,) else ("known", None)
    return "stray", None


def format_id(schema: Schema, number: int) -> str:
    """The id `<id_prefix>-<number>`, the number zero-padded to `id_width` digits."""
    return f"{schema.id_prefix}-{number:0{schema.id_width}d}"


def id_number(schema: Schema, item_id: object) -> int | None:
    """The number of a well-formed id (exactly as `format_id` spells it), else None."""
    match = re.fullmatch(rf"{re.escape(schema.id_prefix)}-([0-9]+)", str(item_id))
    if not match or int(match[1]) < 1 or format_id(schema, int(match[1])) != item_id:
        return None
    return int(match[1])


def is_terminal(schema: Schema, item: Item) -> bool:
    """Whether the item's state is terminal; raise on a state its kind does not declare."""
    return schema.kind(item.kind).state(item.fields.get("state")).terminal


def value_from_text(kind: Kind, name: str, text: str) -> object:
    """A CLI `f=v` value converted to the declared field's type; undeclared fields stay strings."""
    field = kind.field(name)
    kind_of = field.type if field else "str"
    try:
        match kind_of:
            case "int":
                return int(text)
            case "bool":
                return {"true": True, "false": False}[text]
            case "date":
                return date.fromisoformat(text)
            case "datetime":
                return datetime.fromisoformat(text)
            case "list":
                return (text,)
            case "str" | "ref" | "enum":
                return text
            case _:
                raise ItemError(f"field {name!r}: unknown field type {kind_of!r}")
    except (ValueError, KeyError) as exc:
        raise ItemError(f"field {name!r} is {kind_of}; cannot read {text!r} as one") from exc
