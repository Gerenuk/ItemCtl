"""The `itemctl` executable: settles an edit-guard call on a file outside every store, and a
landing check of commits that hold no store, in stdlib alone, and hands every other invocation to
`cli`.

The pre-tool-use guard runs before every Write/Edit/MultiEdit in every session, and nearly every
such call names a file under no store.toml; the landing check runs whenever a main branch moves,
in every repository. Importing `cli` (YAML, git, the whole store) costs far more than those
answers, so `cli` is imported only when the answer needs it.
"""

import json
import subprocess
import sys

from itemctl.scope import GUARDED_TOOLS, STORE_FILE, store_root

#: The command line Claude Code's settings wire as the edit guard.
GUARD_ARGV = ["hook", "pre-tool-use"]
#: The command line git's reference-transaction hook runs for a prepared transaction.
LANDING_ARGV = ["hook", "reference-transaction", "prepared"]


def passes_outside_every_store(raw: str) -> bool:
    """True when the guard allows this payload without reading any store: a tool it does not
    inspect, or a file under no store.toml. False when only the full guard can answer, which
    includes every payload it would reject as malformed."""
    try:
        payload = json.loads(raw)
        if payload.get("tool_name") not in GUARDED_TOOLS:
            return True
        return store_root(payload["tool_input"]["file_path"]) is None
    except ValueError, LookupError, TypeError, AttributeError, OSError:
        return False  # malformed: the full guard reports it exactly as it always has


def lands_no_store(updates: str) -> bool:
    """True when no commit the ref transaction `updates` moves a ref to holds a store.toml, so the
    landing check has nothing to read. False when only the full check can answer, which includes
    every input it would reject as malformed and every git failure."""
    for line in updates.splitlines():
        fields = line.split(" ")
        if len(fields) != 3:
            return False
        if set(fields[1]) == {"0"}:
            continue
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--name-only", "--full-tree", fields[1]],
            capture_output=True,
            text=True,
            check=False,
        )
        if listing.returncode != 0 or any(
            path.rpartition("/")[2] == STORE_FILE for path in listing.stdout.split("\0")
        ):
            return False
    return True


def main() -> int:
    """Answer the edit guard and the landing check from stdlib when no store is involved;
    otherwise run `cli.main`."""
    argv = sys.argv[1:]
    payload = None
    if argv == GUARD_ARGV:
        payload = sys.stdin.read()
        if passes_outside_every_store(payload):
            return 0
    elif argv == LANDING_ARGV:
        payload = sys.stdin.read()
        if lands_no_store(payload):
            return 0
    from itemctl import cli  # the heavy import this module exists to skip

    return cli.main(argv, hook_payload=payload)
