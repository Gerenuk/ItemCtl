"""Property: reapplying a write's own resulting field values is a true no-op."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from itemctl.model import MANAGED, AppendLine, Assign, Edit, Extend, Unset
from itemctl.store import Store
from tests.conftest import TZ, Clock, committed_copy, git

SAFE_TEXT = st.from_regex(r"[A-Za-z][A-Za-z0-9]{0,12}", fullmatch=True)
SKIP_REAPPLY = set(MANAGED) - {"state"}


def _fresh_store(tmp_path_factory: pytest.TempPathFactory) -> Store:
    """A freshly committed fixture-store copy, independent of every other example."""
    return Store(committed_copy(tmp_path_factory.mktemp("repo") / "store"), Clock())


@st.composite
def _friction_edits(draw) -> list[Edit]:
    """A valid edit list for a friction item: a state (with its `requires`) plus one extra."""
    target = draw(st.sampled_from(["open", "worked-around", "fixing", "resolved"]))
    edits: list[Edit] = [Assign("state", target)]
    if target == "fixing":
        edits.append(Assign("fixing_by", draw(SAFE_TEXT)))
    elif target == "resolved":
        edits.append(Assign("outcome", draw(st.sampled_from(["fixed", "declined"]))))
        edits.append(Assign("fixed_in", draw(SAFE_TEXT)))
    match draw(st.sampled_from(["none", "assign", "extend", "unset", "append"])):
        case "assign":
            edits.append(Assign("tags", tuple(draw(st.lists(SAFE_TEXT, min_size=1, max_size=3)))))
        case "extend":
            edits.append(Extend("tags", draw(SAFE_TEXT)))
        case "unset":
            edits.append(Unset("tags"))
        case "append":
            edits.append(AppendLine("Occurrences", draw(SAFE_TEXT)))
    return edits


@st.composite
def _task_edits(draw) -> list[Edit]:
    """A valid edit list for a task item: a state (with its `requires_any`) plus one extra."""
    target = draw(st.sampled_from(["todo", "parked", "done"]))
    edits: list[Edit] = [Assign("state", target)]
    if target == "parked":
        edits.append(Assign("trigger", draw(SAFE_TEXT)))
    match draw(st.sampled_from(["none", "priority", "urgent", "due", "at", "unset"])):
        case "priority":
            edits.append(Assign("priority", draw(st.integers(min_value=0, max_value=10_000))))
        case "urgent":
            edits.append(Assign("urgent", draw(st.booleans())))
        case "due":
            edits.append(Assign("due", draw(st.dates())))
        case "at":
            edits.append(Assign("at", draw(st.datetimes(timezones=st.just(TZ)))))
        case "unset":
            edits.append(Unset("priority"))
    return edits


CASES = st.one_of(
    st.tuples(st.just("friction"), _friction_edits()),
    st.tuples(st.just("task"), _task_edits()),
)


@settings(max_examples=20, deadline=None)
@given(case=CASES)
def test_reapplying_resulting_fields_is_a_no_op(tmp_path_factory, case):
    """After any valid edit, re-Assigning every resulting field makes no further commit."""
    kind, edits = case
    store = _fresh_store(tmp_path_factory)
    item = store.add(kind, "a", [Assign("component", "c0")] if kind == "friction" else [])
    result = store.set(item.id, edits)
    head = git(store.root, "rev-parse", "HEAD")
    reapply = [
        Assign(name, value) for name, value in result.fields.items() if name not in SKIP_REAPPLY
    ]
    again = store.set(result.id, reapply)
    assert again.fields == result.fields
    assert git(store.root, "rev-parse", "HEAD") == head
    assert git(store.root, "status", "--porcelain") == ""
