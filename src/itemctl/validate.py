"""The one validation the writer and `check` share, plus `check`'s ageing report."""

from collections import Counter
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from typing import TypeIs

from itemctl.model import LEASE, Field, Finding, Item, ItemError, Kind, Snapshot, State, is_empty
from itemctl.schema import id_number, item_path

type Rule = Callable[[Snapshot], Iterator[Finding]]


def _kinded(snap: Snapshot) -> Iterator[tuple[Item, Kind]]:
    """Every item with its kind."""
    for item in snap.items:
        yield item, snap.schema.kind(item.kind)


def _stated(snap: Snapshot) -> Iterator[tuple[Item, Kind, State]]:
    """Every item whose state its kind declares, with its kind and state."""
    for item, kind in _kinded(snap):
        if state := kind.find_state(item.fields.get("state")):
            yield item, kind, state


def _is_datetime(value: object) -> TypeIs[datetime]:
    """A timezone-aware datetime."""
    return isinstance(value, datetime) and value.tzinfo is not None


def _type_ok(field: Field, value: object) -> bool:
    """Whether `value` has the declared field's type."""
    match field.type:
        case "str" | "ref":
            return isinstance(value, str) and "\n" not in value
        case "int":
            return type(value) is int
        case "bool":
            return isinstance(value, bool)
        case "date":
            return type(value) is date
        case "datetime":
            return _is_datetime(value)
        case "list":
            return isinstance(value, tuple) and all(isinstance(v, str) for v in value)
        case "enum":
            return value in field.choices
        case _:
            raise ItemError(f"field {field.name!r}: unknown field type {field.type!r}")


def parse_errors(snap: Snapshot) -> Iterator[Finding]:
    """Item files that did not parse."""
    yield from snap.broken


def stray_files(snap: Snapshot) -> Iterator[Finding]:
    """Files that are neither items, indexes nor store files."""
    for path in snap.stray:
        yield Finding("stray_files", path, "stray file: not an item, index or store file")


def id_format(snap: Snapshot) -> Iterator[Finding]:
    """Every id is `<id_prefix>-<n>`."""
    for item in snap.items:
        if id_number(snap.schema, item.fields.get("id")) is None:
            yield Finding("id_format", str(item.path), f"id {item.fields.get('id')!r} is malformed")


def unique_ids(snap: Snapshot) -> Iterator[Finding]:
    """No two items share an id."""
    counts = Counter(item.id for item in snap.items)
    for item in snap.items:
        if counts[item.id] > 1:
            yield Finding(
                "unique_ids", str(item.path), f"id {item.id} is used {counts[item.id]} times"
            )


def unique_slugs(snap: Snapshot) -> Iterator[Finding]:
    """No two items of one kind share a slug."""
    counts = Counter((item.kind, item.slug) for item in snap.items)
    for item in snap.items:
        if counts[item.kind, item.slug] > 1:
            yield Finding("unique_slugs", str(item.path), f"slug {item.slug!r} is used twice")


def known_state(snap: Snapshot) -> Iterator[Finding]:
    """Every state is one its kind declares."""
    for item, kind in _kinded(snap):
        if kind.find_state(item.fields.get("state")) is None:
            yield Finding(
                "known_state", str(item.path), f"unknown state {item.fields.get('state')!r}"
            )


def created_stamp(snap: Snapshot) -> Iterator[Finding]:
    """Every item has a timezone-aware `created`."""
    for item in snap.items:
        if not _is_datetime(item.fields.get("created")):
            yield Finding(
                "created_stamp", str(item.path), "created must be a timezone-aware datetime"
            )


def lease_fields(snap: Snapshot) -> Iterator[Finding]:
    """Lease fields are all present or all absent, and well-typed."""
    for item in snap.items:
        present = [f for f in LEASE if f in item.fields]
        if present and len(present) < len(LEASE):
            yield Finding("lease_fields", str(item.path), f"partial lease: only {present}")
        elif present and not (
            isinstance(item.fields["claimed_by"], str)
            and _is_datetime(item.fields["claimed_at"])
            and _is_datetime(item.fields["lease_until"])
        ):
            yield Finding("lease_fields", str(item.path), "lease fields are ill-typed")


def no_empty_values(snap: Snapshot) -> Iterator[Finding]:
    """No field is null or empty: an absent field is absent."""
    for item in snap.items:
        for name, value in item.fields.items():
            if is_empty(value):
                yield Finding(
                    "no_empty_values", str(item.path), f"{name} is empty; omit it instead"
                )


def field_types(snap: Snapshot) -> Iterator[Finding]:
    """Declared fields hold values of their type."""
    for item, kind in _kinded(snap):
        for field in kind.fields:
            value = item.fields.get(field.name)
            if not is_empty(value) and not _type_ok(field, value):
                expected = f"one of {list(field.choices)}" if field.choices else field.type
                yield Finding(
                    "field_types", str(item.path), f"{field.name}={value!r} is not {expected}"
                )


def required_fields(snap: Snapshot) -> Iterator[Finding]:
    """The state's `requires` fields are present."""
    for item, _, state in _stated(snap):
        if missing := [f for f in state.requires if f not in item.fields]:
            yield Finding(
                "required_fields", str(item.path), f"state {state.name} requires {missing}"
            )


def one_of_fields(snap: Snapshot) -> Iterator[Finding]:
    """At least one of the state's `requires_any` fields is present."""
    for item, _, state in _stated(snap):
        if state.requires_any and not any(f in item.fields for f in state.requires_any):
            yield Finding(
                "one_of_fields",
                str(item.path),
                f"state {state.name} requires one of {list(state.requires_any)}",
            )


def only_here_fields(snap: Snapshot) -> Iterator[Finding]:
    """No field another state holds exclusively, unless this state holds it too."""
    for item, kind, state in _stated(snap):
        for other in kind.states:
            for name in set(other.only_here) - set(state.only_here):
                if name in item.fields:
                    yield Finding(
                        "only_here_fields",
                        str(item.path),
                        f"{name} belongs to state {other.name}, not {state.name}",
                    )


def placement(snap: Snapshot) -> Iterator[Finding]:
    """An item is in `closed/` exactly when its state is terminal."""
    for item, kind, state in _stated(snap):
        if item.path != (want := item_path(kind, item.slug, state.terminal)):
            yield Finding("placement", str(item.path), f"state {state.name} belongs at {want}")


def refs_resolve(snap: Snapshot) -> Iterator[Finding]:
    """Every `ref` field names an existing item's id."""
    ids = {item.id for item in snap.items}
    for item, kind in _kinded(snap):
        for field in kind.fields:
            target = item.fields.get(field.name)
            if field.type == "ref" and target is not None and target not in ids:
                yield Finding(
                    "refs_resolve", str(item.path), f"{field.name}={target} names no item"
                )


RULES: tuple[Rule, ...] = (
    parse_errors,
    stray_files,
    id_format,
    unique_ids,
    unique_slugs,
    known_state,
    created_stamp,
    lease_fields,
    no_empty_values,
    field_types,
    required_fields,
    one_of_fields,
    only_here_fields,
    placement,
    refs_resolve,
)


def problems(snap: Snapshot) -> list[Finding]:
    """Every finding of every rule; a write is refused on any."""
    return [finding for rule in RULES for finding in rule(snap)]


def overdue(snap: Snapshot, now: datetime) -> list[Finding]:
    """Non-terminal items older than their kind's max_age_days, and expired leases."""
    found = []
    for item, kind in _kinded(snap):
        state = kind.find_state(item.fields.get("state"))
        created, until = item.fields.get("created"), item.fields.get("lease_until")
        if (
            state
            and not state.terminal
            and kind.max_age_days
            and _is_datetime(created)
            and now - created > timedelta(days=kind.max_age_days)
        ):
            age = (now - created).days
            found.append(Finding("stale", str(item.path), f"{item.id} open for {age} days"))
        if _is_datetime(until) and until <= now:
            holder = item.fields.get("claimed_by")
            found.append(
                Finding("expired", str(item.path), f"{item.id}: lease of {holder} expired {until}")
            )
    return found
