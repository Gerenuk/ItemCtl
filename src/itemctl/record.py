"""Parse and render one item file: YAML frontmatter between `---` lines, then a markdown body."""

import functools
import io
from collections.abc import Mapping
from datetime import datetime

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.representer import SafeRepresenter

from itemctl.model import MANAGED, ItemError, Kind, frozen_fields, thaw

OPEN = "---\n"
CLOSE = "\n---\n"


class _Representer(SafeRepresenter):
    """The safe representer, writing datetimes as ISO 8601 with a `T`."""


_Representer.add_representer(
    datetime,
    lambda rep, value: rep.represent_scalar("tag:yaml.org,2002:timestamp", value.isoformat()),
)


@functools.cache
def _yaml() -> YAML:
    """The one safe, block-style, unwrapped YAML instance every parse and render shares, built on
    first use; it loads with the ruamel.yaml.clib parser."""
    yaml = YAML(typ="safe")
    yaml.Representer = _Representer
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.width = 1_000_000
    yaml.sort_base_mapping_type_on_output = False
    return yaml


def split(text: str, source: str) -> tuple[str, str]:
    """(frontmatter YAML, body); raise naming `source` when a fence is missing."""
    if not text.startswith(OPEN):
        raise ItemError(f"{source}: must start with a '---' line")
    end = text.find(CLOSE, len(OPEN) - 1)
    if end == -1:
        raise ItemError(f"{source}: no closing '---' line")
    return text[len(OPEN) : end + 1], text[end + len(CLOSE) :]


def parse(text: str, source: str) -> tuple[Mapping[str, object], str]:
    """(frozen frontmatter in file order, body) of an item file's text."""
    yaml_text, body = split(text, source)
    try:
        data = _yaml().load(yaml_text)
    except YAMLError as exc:
        raise ItemError(f"{source}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise ItemError(f"{source}: frontmatter must be a mapping with string keys, got {data!r}")
    return frozen_fields(data), body


def render(kind: Kind, fields: Mapping[str, object], body: str) -> str:
    """The file text: managed fields, then declared ones in declaration order, then the rest."""
    declared = [f.name for f in kind.fields]
    order = [n for n in (*MANAGED, *declared) if n in fields]
    order += [n for n in fields if n not in order]
    out = io.StringIO()
    _yaml().dump({n: thaw(fields[n]) for n in order}, out)
    return f"{OPEN}{out.getvalue()}---\n{body}"
