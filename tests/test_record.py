"""Round trip: parsing a rendered item gives back its frontmatter and body."""

from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from itemctl import record, schema
from itemctl.model import ItemError, frozen_fields
from tests.conftest import FIXTURE

KIND = schema.load(FIXTURE).kind("friction")
TEXT = st.text(st.characters(exclude_categories=("Cs", "Cc", "Zl", "Zp")), min_size=1)
STAMPS = st.builds(
    lambda d, minutes: d.replace(microsecond=0, tzinfo=timezone(timedelta(minutes=minutes))),
    st.datetimes(min_value=datetime(1970, 1, 2), max_value=datetime(2999, 1, 1)),
    st.integers(-14 * 60, 14 * 60),
)
EXTRA = st.one_of(
    TEXT, st.integers(), st.booleans(), st.dates(), STAMPS, st.lists(TEXT, min_size=1)
)


@given(
    title=TEXT,
    created=STAMPS,
    tags=st.lists(TEXT, min_size=1),
    extra=st.dictionaries(st.from_regex(r"x_[a-z]{1,8}", fullmatch=True), EXTRA, max_size=3),
    body=st.text(st.characters(exclude_categories=("Cs",))),
)
def test_parse_inverts_render(title, created, tags, extra, body):
    """parse ∘ render is the identity, on declared, managed and free fields alike."""
    fields = frozen_fields(
        {"id": "FR-3", "state": "open", "created": created, "title": title, "tags": tags} | extra
    )
    text = record.render(KIND, fields, body)
    assert record.parse(text, "x.md") == (fields, body)
    assert isinstance(dict(record.parse(text, "x.md")[0])["created"], datetime)


def test_render_orders_managed_then_declared_then_free():
    """Managed fields first, declared in schema order, undeclared last in their own order."""
    fields = frozen_fields(
        {
            "zeta": 1,
            "component": "c",
            "title": "t",
            "state": "open",
            "id": "FR-1",
            "due": date(2026, 1, 1),
        }
    )
    text = record.render(KIND, fields, "")
    keys = [line.split(":")[0] for line in text.splitlines()[1:-1]]
    assert keys == ["id", "state", "title", "component", "zeta", "due"]


def test_parse_refuses_a_duplicate_key():
    """A key given twice is an error naming the file, never a silent last-one-wins."""
    with pytest.raises(ItemError, match=r"(?s)x\.md: invalid YAML frontmatter.*duplicate key"):
        record.parse("---\ntitle: a\ntitle: b\n---\n", "x.md")


def test_parse_keeps_yaml_1_1_booleans_and_sexagesimals_as_strings():
    """YAML 1.2 rules: `no`, `yes`, `on` and `12:30` stay strings, not booleans or 750."""
    fields, _ = record.parse("---\na: no\nb: yes\nc: on\nd: 12:30\n---\n", "x.md")
    assert dict(fields) == {"a": "no", "b": "yes", "c": "on", "d": "12:30"}


def test_parse_errors_leave_the_shared_parser_usable():
    """A failed parse does not poison the one YAML instance the next parse reuses."""
    with pytest.raises(ItemError):
        record.parse("---\na: [\n---\n", "bad.md")
    assert dict(record.parse("---\na: 1\n---\nbody", "ok.md")[0]) == {"a": 1}
