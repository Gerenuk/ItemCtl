"""slugify: a title becomes an ASCII file name."""

import pytest

from itemctl.changes import slugify
from itemctl.model import ItemError


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Über die Wärmeleitung", "uber-die-warmeleitung"),
        ("café résumé", "cafe-resume"),
        ("Straße STRASSE ẞ", "strasse-strasse-ss"),
        ("Fix §3 — the  --flag 🧹", "fix-3-the-flag"),
    ],
)
def test_slugify_folds_accented_letters_to_ascii(title, slug):
    """Accents drop off their letters and ß becomes ss; other non-ASCII separates words."""
    assert slugify(title) == slug


def test_slugify_refuses_a_title_with_no_foldable_letter():
    """Nothing of a CJK title survives the ASCII filter, so it is refused."""
    with pytest.raises(ItemError, match="use a title with other words"):
        slugify("日本語")
