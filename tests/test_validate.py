"""Each validation rule: a deny case, a nearby allow case, and the rule's removal caught."""

from datetime import datetime
from pathlib import PurePosixPath

import pytest

from itemctl import schema, validate
from itemctl.model import Finding, Item, Snapshot, frozen_fields
from tests.conftest import FIXTURE, TZ

SCHEMA = schema.load(FIXTURE)
T0 = datetime(2026, 9, 1, tzinfo=TZ)


def friction(path: str, **fields) -> Item:
    """A valid open friction item at `path`, with `fields` overriding (None removes)."""
    base = {"id": "FR-1", "state": "open", "created": T0, "title": "t", "component": "c"}
    merged = {k: v for k, v in (base | fields).items() if v is not None}
    return Item("friction", PurePosixPath(path), frozen_fields(merged), "")


def task(slug: str, **fields) -> Item:
    """A valid todo task in `task/<slug>/item.md`, with `fields` overriding."""
    merged = {"id": "FR-2", "state": "todo", "created": T0, "title": "t"} | fields
    return Item("task", PurePosixPath(f"task/{slug}/item.md"), frozen_fields(merged), "")


def snap(*items: Item, broken=(), stray=()) -> Snapshot:
    """A snapshot of `items` over the fixture schema."""
    return Snapshot(SCHEMA, items, tuple(broken), tuple(stray))


RESOLVED = {"state": "resolved", "outcome": "fixed", "fixed_in": "abc"}
LEASE = {"claimed_by": "h", "claimed_at": T0, "lease_until": T0}
CASES = {  # rule: (deny, allow)
    "parse_errors": (snap(broken=[Finding("parse_errors", "x.md", "bad")]), snap()),
    "stray_files": (snap(stray=["notes.txt"]), snap()),
    "id_format": (snap(friction("a.md", id="FR-01")), snap(friction("a.md", id="FR-10"))),
    "unique_ids": (
        snap(friction("a.md"), task("b", id="FR-1")),
        snap(friction("a.md"), task("b")),
    ),
    "unique_slugs": (
        snap(friction("a.md"), friction("closed/a.md", id="FR-2", **RESOLVED)),
        snap(friction("a.md"), friction("closed/b.md", id="FR-2", **RESOLVED)),
    ),
    "known_state": (
        snap(friction("a.md", state="bogus")),
        snap(friction("a.md", state="worked-around")),
    ),
    "created_stamp": (
        snap(friction("a.md", created=T0.replace(tzinfo=None))),
        snap(friction("a.md", created=T0.astimezone())),
    ),
    "lease_fields": (
        snap(friction("a.md", claimed_by="h")),
        snap(friction("a.md", **LEASE)),
    ),
    "no_empty_values": (snap(friction("a.md", note="")), snap(friction("a.md", note="n"))),
    "field_types": (snap(task("a", priority="high")), snap(task("a", priority=2))),
    "required_fields": (snap(friction("a.md", component=None)), snap(friction("a.md"))),
    "one_of_fields": (
        snap(task("a", state="parked")),
        snap(task("a", state="parked", trigger="t")),
    ),
    "only_here_fields": (
        snap(friction("a.md", fixing_by="s")),
        snap(friction("a.md", state="fixing", fixing_by="s")),
    ),
    "placement": (
        snap(friction("a.md", **RESOLVED)),
        snap(friction("closed/a.md", **RESOLVED)),
    ),
    "refs_resolve": (
        snap(friction("a.md"), task("b", blocked_by="FR-9")),
        snap(friction("a.md"), task("b", blocked_by="FR-1")),
    ),
}


def test_every_rule_has_a_case():
    """No rule goes untested."""
    assert sorted(CASES) == sorted(rule.__name__ for rule in validate.RULES)


@pytest.mark.parametrize("rule", sorted(CASES))
def test_deny_and_allow(rule):
    """The deny case yields findings of this rule; the allow case yields none at all."""
    deny, allow = CASES[rule]
    assert {f.rule for f in validate.problems(deny)} == {rule}
    assert validate.problems(allow) == []


@pytest.mark.parametrize("rule", sorted(CASES))
def test_removing_the_rule_lets_its_deny_case_through(rule, monkeypatch):
    """Mutant: without this rule the deny case validates, so the deny test catches its removal."""
    kept = tuple(r for r in validate.RULES if r.__name__ != rule)
    monkeypatch.setattr(validate, "RULES", kept)
    assert validate.problems(CASES[rule][0]) == []


@pytest.mark.parametrize(
    "value",
    ["a\nb", 3],
)
def test_str_fields_are_one_line_strings(value):
    """A str field refuses multi-line text and non-strings."""
    assert [f.rule for f in validate.problems(snap(friction("a.md", component=value)))] == [
        "field_types"
    ]


def test_overdue_reports_stale_items_and_expired_leases():
    """Stale: open past max_age_days; expired: lease_until passed."""
    s = snap(friction("a.md", **LEASE))
    assert validate.overdue(s, T0.replace(day=15)) == [
        Finding("expired", "a.md", f"FR-1: lease of h expired {T0}")
    ]
    later = T0.replace(month=10, day=5)
    assert {f.rule for f in validate.overdue(s, later)} == {"stale", "expired"}
