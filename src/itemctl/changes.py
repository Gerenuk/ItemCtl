"""Pure change builders, (snapshot, request) → ChangeSet per verb, and item lookup."""

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta

from itemctl.model import (
    LEASE,
    MANAGED,
    AppendLine,
    Assign,
    ChangeSet,
    DuplicateError,
    Edit,
    Extend,
    Item,
    ItemError,
    Kind,
    NewItem,
    NothingClaimable,
    Snapshot,
    Unset,
    freeze,
    frozen_fields,
    is_empty,
    text_of,
)
from itemctl.schema import RESERVED_SLUGS, format_id, id_number, is_terminal, item_path

SLUG_MAX = 60


def slugify(title: str) -> str:
    """Lowercase alphanumeric runs of `title` joined by `-`, at most SLUG_MAX characters.

    Accented letters fold to their base letter (NFKD, combining marks dropped) and ß to ss, so
    `Über die Straße` gives `uber-die-strasse`; any other non-ASCII character separates words.
    """
    folded = unicodedata.normalize("NFKD", title.lower().replace("ß", "ss"))
    ascii_title = "".join(ch for ch in folded if not unicodedata.combining(ch))
    slug = "-".join(re.findall(r"[a-z0-9]+", ascii_title))[:SLUG_MAX].strip("-")
    if not slug or slug in RESERVED_SLUGS:
        raise ItemError(f"title {title!r} gives the slug {slug!r}; use a title with other words")
    return slug


def resolve(snap: Snapshot, ref: str) -> Item:
    """The item whose id or slug is `ref`; raise when none or several match."""
    found = [i for i in snap.items if ref in (i.id, i.slug)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ItemError(f"no item has id or slug {ref!r}")
    raise ItemError(f"{ref!r} is ambiguous: {', '.join(f'{i.id} ({i.path})' for i in found)}")


def matches(item: Item, where: Mapping[str, str]) -> bool:
    """Whether every `f=v` holds: equality of the value's text, membership for a list."""
    for name, wanted in where.items():
        value = item.fields.get(name)
        if isinstance(value, tuple):
            if wanted not in [text_of(v) for v in value]:
                return False
        elif value is None or text_of(value) != wanted:
            return False
    return True


def apply(snap: Snapshot, change: ChangeSet) -> Snapshot:
    """The snapshot after `change`."""
    kept = tuple(i for i in snap.items if i is not change.old)
    return replace(snap, items=(*kept, change.new))


DATED_LINE = re.compile(r"- (\d{4}-\d{2}-\d{2}) — (.*)")


def _dated_line(day: date, text: str) -> str:
    """The line `--append` writes; `DATED_LINE` reads it back."""
    if "\n" in text:
        raise ItemError(f"an appended line must be one line, got {text!r}")
    return f"- {day.isoformat()} — {text}"


def _section_span(lines: list[str], section: str) -> tuple[int, int] | None:
    """(heading index, index after the section's last line) of `## section`, or None."""
    heading = f"## {section}"
    if heading not in lines:
        return None
    start = lines.index(heading)
    end = next(
        (j for j in range(start + 1, len(lines)) if re.match(r"#{1,2} ", lines[j])), len(lines)
    )
    return start, end


def section_entries(body: str, section: str) -> list[tuple[date, str]]:
    """The (date, text) lines of an appendable section; raise on a line of another shape."""
    lines = body.splitlines()
    span = _section_span(lines, section)
    entries = []
    for line in lines[span[0] + 1 : span[1]] if span else []:
        if line.strip():
            match = DATED_LINE.fullmatch(line)
            if not match:
                raise ItemError(f"section {section!r}: {line!r} is not '- YYYY-MM-DD — text'")
            entries.append((date.fromisoformat(match[1]), match[2]))
    return entries


def _append_line(body: str, section: str, line: str) -> str:
    """`body` with `line` at the end of its `## section` (the section is created if missing)."""
    lines = body.splitlines()
    span = _section_span(lines, section)
    if span:
        start, end = span
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += [""] * bool(lines) + [f"## {section}"]
        end = len(lines)
    lines.insert(end, line)
    return "\n".join(lines) + "\n"


def _edit(kind: Kind, fields: dict, body: str, edit: Edit, now: datetime) -> str:
    """Apply one edit to `fields` in place; return the new body."""
    name = getattr(edit, "field", None)
    if name in MANAGED and not (name == "state" and isinstance(edit, Assign)):
        raise ItemError(f"{name!r} is managed by itemctl; it cannot be edited this way")
    match edit:
        case Assign(name, value):
            fields[name] = freeze(value)
        case Extend(name, value):
            declared = kind.field(name)
            if declared and declared.type != "list":
                raise ItemError(f"{name!r} is {declared.type}, not list; `+=` needs a list field")
            current = fields.get(name)
            fields[name] = (
                *(current if isinstance(current, tuple) else (current,) * bool(current)),
                value,
            )
        case Unset(name):
            fields.pop(name, None)
        case AppendLine(section, text):
            if section not in kind.sections:
                raise ItemError(f"kind {kind.name!r} has no appendable section {section!r}")
            return _append_line(body, section, _dated_line(now.date(), text))
        case _:
            raise ItemError(f"unknown edit {edit!r}")
    return body


def _pruned(fields: dict) -> dict:
    """`fields` without empty values: an absent field is never written as null or empty."""
    return {k: v for k, v in fields.items() if not is_empty(v)}


def add(snap: Snapshot, request: NewItem, now: datetime) -> ChangeSet:
    """A new item in the initial state (or an assigned `state`) with the next id."""
    kind = snap.schema.kind(request.kind)
    slug = slugify(request.title)
    body = request.body
    taken = next((i for i in snap.items if i.kind == kind.name and i.slug == slug), None)
    if taken:
        raise ItemError(
            f"slug {slug!r} is taken by {taken.id} ({taken.path}); choose another title"
        )
    number = max((id_number(snap.schema, i.id) or 0 for i in snap.items), default=0) + 1
    fields = {"id": format_id(snap.schema, number), "state": kind.states[0].name}
    fields |= {"created": now, "title": request.title}
    for edit in request.edits:
        body = _edit(kind, fields, body, edit, now)
    fields = _pruned(fields)
    duplicates = (
        tuple(
            other
            for other in snap.items
            if other.kind == kind.name
            and not is_terminal(snap.schema, other)
            and all(other.fields.get(k) == fields.get(k) for k in kind.dedup_key)
        )
        if request.dedup and kind.dedup_key
        else ()
    )
    if duplicates:
        raise DuplicateError(
            ", ".join(f"{d.id} ({d.path})" for d in duplicates)
            + " already "
            + ("has " if len(duplicates) == 1 else "have ")
            + ", ".join(f"{k}={text_of(fields.get(k))}" for k in kind.dedup_key),
            duplicates,
        )
    terminal = kind.state(fields["state"]).terminal
    new = Item(kind.name, item_path(kind, slug, terminal), frozen_fields(fields), body)
    return ChangeSet("add", None, new)


def update(
    snap: Snapshot, ref: str, plan: Callable[[Item], Sequence[Edit]], now: datetime
) -> ChangeSet:
    """The item after the edits `plan` makes of it (it may raise to refuse); leaving a state
    drops its only_here fields, entering a terminal one the lease."""
    item = resolve(snap, ref)
    kind = snap.schema.kind(item.kind)
    old_state = kind.state(item.fields.get("state"))
    fields, body = dict(item.fields), item.body
    for edit in plan(item):
        body = _edit(kind, fields, body, edit, now)
    new_state = kind.state(fields.get("state"))
    dropped = (
        set(old_state.only_here) - set(new_state.only_here) if new_state != old_state else set()
    )
    dropped |= set(LEASE) if new_state.terminal else set()
    fields = _pruned({k: v for k, v in fields.items() if k not in dropped})
    path = item_path(kind, item.slug, new_state.terminal)
    return ChangeSet("set", item, Item(item.kind, path, frozen_fields(fields), body))


def _lease_live(item: Item, now: datetime) -> bool:
    """Whether the item holds a lease that has not expired."""
    until = item.fields.get("lease_until")
    return isinstance(until, datetime) and until > now


def _unblocked(snap: Snapshot, item: Item) -> bool:
    """Whether every `ref` field of the item points at a terminal item."""
    kind = snap.schema.kind(item.kind)
    for field in kind.fields:
        target = item.fields.get(field.name)
        if field.type == "ref" and target is not None:
            if not is_terminal(snap.schema, resolve(snap, str(target))):
                return False
    return True


def _next_claimable(
    snap: Snapshot, kind_name: str, where: Mapping[str, str], now: datetime
) -> Item:
    """The oldest non-terminal, unleased, unblocked item of the kind matching `where`."""
    kind = snap.schema.kind(kind_name)
    candidates = [
        i
        for i in snap.items
        if i.kind == kind.name
        and not is_terminal(snap.schema, i)
        and not _lease_live(i, now)
        and matches(i, where)
        and _unblocked(snap, i)
    ]
    if not candidates:
        raise NothingClaimable(f"no claimable {kind.name} item matches {dict(where)}")
    return min(candidates, key=lambda i: (i.fields["created"], id_number(snap.schema, i.id)))


def claim(
    snap: Snapshot,
    ref: str | None,
    next_of: tuple[str, Mapping[str, str]] | None,
    holder: str,
    ttl: timedelta,
    now: datetime,
) -> ChangeSet:
    """Lease `ref`, or the next claimable item of `next_of = (kind, where)`, to `holder`."""
    if (ref is None) == (next_of is None):
        raise ItemError("claim takes exactly one of an item reference and --next KIND")
    item = resolve(snap, ref) if next_of is None else _next_claimable(snap, *next_of, now)
    if is_terminal(snap.schema, item):
        raise ItemError(f"{item.id} is in terminal state {item.fields.get('state')!r}")
    held_by = item.fields.get("claimed_by")
    live = _lease_live(item, now)
    if live and held_by != holder:
        raise ItemError(f"{item.id} is leased by {held_by} until {item.fields['lease_until']}")
    fields = dict(item.fields)
    fields |= {
        "claimed_by": holder,
        "claimed_at": fields["claimed_at"] if live else now,  # a live lease here is a renewal
        "lease_until": now + ttl,
    }
    return ChangeSet("claim", item, replace(item, fields=frozen_fields(fields)))


def release(snap: Snapshot, ref: str, holder: str, force: bool) -> ChangeSet:
    """Drop the item's lease; only its holder may, unless `force`."""
    item = resolve(snap, ref)
    held_by = item.fields.get("claimed_by")
    if held_by is None:
        raise ItemError(f"{item.id} is not claimed")
    if held_by != holder and not force:
        raise ItemError(
            f"{item.id} is held by {held_by}, not {holder}; pass --force to take it off"
        )
    fields = {k: v for k, v in item.fields.items() if k not in LEASE}
    return ChangeSet("release", item, replace(item, fields=frozen_fields(fields)))
