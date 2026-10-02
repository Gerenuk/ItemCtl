"""Every writer-reachable rule in `validate.RULES`: add/set/claim refuses it, changing nothing."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest

from itemctl import validate
from itemctl.model import Assign, InvalidStore, ItemError
from itemctl.store import Store
from tests.conftest import git, tree

HOUR = timedelta(hours=1)


def _nothing(store: Store) -> None:
    """No setup: the very first write is the violating one."""


@dataclass
class InvalidStoreCase:
    """A write that InvalidStore refuses, with findings of exactly this rule.

    `setup` runs (and may legitimately commit) before the snapshot the test compares against.
    """

    attempt: Callable[[Store], object]
    setup: Callable[[Store], object] = _nothing


@dataclass
class ItemErrorCase:
    """A write a pre-check refuses with a plain ItemError before validation ever runs."""

    attempt: Callable[[Store], object]
    match: str
    setup: Callable[[Store], object] = _nothing


def _add_hello_world(store: Store) -> None:
    """An item whose slug a second title would collide with."""
    store.add("friction", "Hello world", [Assign("component", "x")])


def _add_colliding_slug(store: Store) -> None:
    """A title that slugifies the same as the setup item's: refused, naming the first."""
    store.add("friction", "hello, WORLD", [Assign("component", "y")])


def _add_friction_a(store: Store) -> None:
    """A plain open friction item, `a`."""
    store.add("friction", "a", [Assign("component", "x")])


def _set_unknown_state(store: Store) -> None:
    """A state the kind does not declare."""
    store.set("a", [Assign("state", "bogus")])


def _claim_empty_holder(store: Store) -> None:
    """A lease with an empty holder: a lease field that is empty rather than absent."""
    store.claim("a", holder="", ttl=HOUR)


def _add_bad_field_type(store: Store) -> None:
    """A declared int field given a string."""
    store.add("task", "a", [Assign("priority", "high")])


def _add_missing_required(store: Store) -> None:
    """The initial state's required `component` is never set."""
    store.add("friction", "a")


def _add_missing_one_of(store: Store) -> None:
    """`parked` requires one of blocked_by/trigger; neither is set."""
    store.add("task", "a", [Assign("state", "parked")])


def _set_wrong_state_only_here(store: Store) -> None:
    """`fixing_by` belongs only to state `fixing`, set while still `open`."""
    store.set("a", [Assign("fixing_by", "s")])


def _add_dangling_ref(store: Store) -> None:
    """A `ref` field naming no item."""
    store.add("task", "a", [Assign("blocked_by", "FR-999")])


WRITER_CASES: dict[str, InvalidStoreCase | ItemErrorCase] = {
    "unique_slugs": ItemErrorCase(_add_colliding_slug, "FR-1", setup=_add_hello_world),
    "known_state": ItemErrorCase(_set_unknown_state, "no state 'bogus'", setup=_add_friction_a),
    "no_empty_values": InvalidStoreCase(_claim_empty_holder, setup=_add_friction_a),
    "field_types": InvalidStoreCase(_add_bad_field_type),
    "required_fields": InvalidStoreCase(_add_missing_required),
    "one_of_fields": InvalidStoreCase(_add_missing_one_of),
    "only_here_fields": InvalidStoreCase(_set_wrong_state_only_here, setup=_add_friction_a),
    "refs_resolve": InvalidStoreCase(_add_dangling_ref),
}

NOT_WRITER_REACHABLE = {
    "parse_errors": "only a hand edit that breaks the YAML frontmatter produces this",
    "stray_files": "only a hand-placed file produces this",
    "id_format": "id is managed; the writer always formats it correctly",
    "unique_ids": "ids are always freshly allocated as one past the current max",
    "created_stamp": "created is managed and always set from the clock",
    "lease_fields": "lease fields are managed; claim/release always set or clear all three",
    "placement": "path is always recomputed from the state; only a hand move desyncs it",
}


def test_every_rule_is_covered_or_excused():
    """No rule in `validate.RULES` is silently untested: a case, or a named excuse."""
    names = {rule.__name__ for rule in validate.RULES}
    assert set(WRITER_CASES) | set(NOT_WRITER_REACHABLE) == names
    assert set(WRITER_CASES).isdisjoint(NOT_WRITER_REACHABLE)


@pytest.mark.parametrize("rule", sorted(WRITER_CASES))
def test_writer_refuses_what_check_reports(store, rule):
    """The write that breaks `rule` is refused before anything changes on disk or in git."""
    case = WRITER_CASES[rule]
    case.setup(store)
    before, head = tree(store.root), git(store.root, "rev-parse", "HEAD")
    if isinstance(case, InvalidStoreCase):
        with pytest.raises(InvalidStore) as excinfo:
            case.attempt(store)
        assert {f.rule for f in excinfo.value.findings} == {rule}
    else:
        with pytest.raises(ItemError, match=case.match):
            case.attempt(store)
    assert tree(store.root) == before
    assert git(store.root, "rev-parse", "HEAD") == head
    assert git(store.root, "status", "--porcelain") == ""
