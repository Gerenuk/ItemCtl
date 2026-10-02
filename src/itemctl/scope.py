"""The hooks' scope, in stdlib alone: the tools the edit guard inspects, the store that owns a
path, and the file that marks a store.

`entry` settles most guard calls with these facts before importing anything else, and
`hooks.pre_tool_use` asks them the same questions, so the fast answer and the full one agree.
"""

import os

#: The file at a store's root; a directory holding one is a store when the file has [kinds].
STORE_FILE = "store.toml"

#: The Claude Code tools whose calls the pre-tool-use guard inspects; every other tool passes.
GUARDED_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})


def store_root(file_path: str) -> str | None:
    """The nearest proper ancestor of `file_path` (made absolute, symlinks resolved) holding a
    store.toml file, or None when no ancestor does."""
    path = os.path.realpath(file_path)
    parent = os.path.dirname(path)
    while parent != path:
        if os.path.isfile(os.path.join(parent, STORE_FILE)):
            return parent
        path, parent = parent, os.path.dirname(parent)
    return None
