"""The `itemctl` command: argument parsing and output formatting over `Store`."""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from itemctl import fsio, hooks, schema
from itemctl.model import (
    AppendLine,
    Assign,
    DuplicateError,
    Edit,
    Extend,
    Item,
    ItemError,
    Kind,
    NoStore,
    NothingClaimable,
    Unset,
    text_of,
    thaw,
)
from itemctl.store import Store

UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
WRITE_VERBS = frozenset({"add", "set", "claim", "release"})
LOG_DAYS = 30


def _duration(text: str) -> timedelta:
    """`90s`, `30m`, `2h`, `1d` as a timedelta."""
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", text)
    if not match:
        raise argparse.ArgumentTypeError(f"{text!r} is not a duration like 30m, 2h or 1d")
    return timedelta(**{UNITS[match[2]]: int(match[1])})


def _pairs(tokens: list[str]) -> dict[str, str]:
    """`f=v` tokens as a mapping."""
    out = {}
    for token in tokens:
        name, sep, value = token.partition("=")
        if not sep or not name:
            raise ItemError(f"{token!r} is not f=v")
        out[name] = value
    return out


def _edits(kind: Kind, tokens: list[str], appends: list[list[str]]) -> list[Edit]:
    """`f=v`, `f+=v`, `f=` tokens and `--append SECTION TEXT` pairs as edits."""
    edits: list[Edit] = []
    for token in tokens:
        match = re.fullmatch(r"([A-Za-z_][\w-]*)(\+?)=(.*)", token, re.DOTALL)
        if not match:
            raise ItemError(f"{token!r} is not f=v, f+=v or f=")
        name, plus, value = match.groups()
        if plus:
            edits.append(Extend(name, value))
        elif value == "":
            edits.append(Unset(name))
        else:
            edits.append(Assign(name, schema.value_from_text(kind, name, value)))
    edits += [AppendLine(section, text) for section, text in appends]
    return edits


def _json_line(item: Item) -> str:
    """One NDJSON record: the frontmatter plus `kind` and `path`."""
    record = {**thaw(item.fields), "kind": item.kind, "path": str(item.path)}  # type: ignore[dict-item]
    return json.dumps(record, default=lambda v: v.isoformat() if isinstance(v, date) else str(v))


def _table(items: list[Item]) -> str:
    """Aligned columns id, kind, state, title."""
    rows = [("id", "kind", "state", "title")]
    rows += [
        (i.id, i.kind, text_of(i.fields.get("state")), text_of(i.fields.get("title")))
        for i in items
    ]
    widths = [max(len(r[c]) for r in rows) for c in range(3)]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(r[:3], widths, strict=True)) + "  " + r[3]
        for r in rows
    )


def _parser() -> argparse.ArgumentParser:
    """The argument parser for every verb."""
    top = argparse.ArgumentParser(prog="itemctl", description="Markdown item stores.")
    top.add_argument("--store", type=Path, help="store directory (default: nearest above cwd)")
    verbs = top.add_subparsers(dest="verb", required=True)

    add = verbs.add_parser("add", help="create an item; print its id")
    add.add_argument("kind")
    add.add_argument("title")
    add.add_argument("fields", nargs="*", metavar="f=v")
    add.add_argument("--body-file", help="body text from a file, or - for stdin")
    add.add_argument(
        "--new", action="store_true", help="file even if a live item matches dedup_key"
    )

    set_ = verbs.add_parser("set", help="edit an item in one atomic write")
    set_.add_argument("ref")
    set_.add_argument("fields", nargs="*", metavar="f=v|f+=v|f=")
    set_.add_argument("--append", nargs=2, action="append", default=[], metavar=("SECTION", "TEXT"))

    claim = verbs.add_parser("claim", help="lease an item; print its id")
    claim.add_argument("ref", nargs="?")
    claim.add_argument("--next", metavar="KIND")
    claim.add_argument("--where", nargs="+", default=[], metavar="f=v")
    claim.add_argument("--by", required=True)
    claim.add_argument("--ttl", type=_duration, default=timedelta(hours=2))

    release = verbs.add_parser("release", help="drop an item's lease")
    release.add_argument("ref")
    release.add_argument("--by", required=True)
    release.add_argument("--force", action="store_true")

    show = verbs.add_parser("show", help="print an item file")
    show.add_argument("ref")

    list_ = verbs.add_parser("list", help="list items as a table or NDJSON")
    list_.add_argument("kind", nargs="?")
    list_.add_argument("--state")
    list_.add_argument("--where", nargs="+", default=[], metavar="f=v")
    list_.add_argument("--closed", action="store_true")
    list_.add_argument("--json", action="store_true")

    verbs.add_parser("check", help="validate; report stale items and expired leases")

    hook = verbs.add_parser("hook", help="Claude Code and git hook entry points")
    hook.add_argument("event", choices=["session-start", "pre-tool-use", "reference-transaction"])
    hook.add_argument("state", nargs="?", help="reference-transaction: git's transaction state")
    return top


def _body(source: str | None) -> str:
    """The body text named by --body-file."""
    if source is None:
        return ""
    return sys.stdin.read() if source == "-" else Path(source).read_text("utf-8")


def _log(event: str, message: str) -> None:
    """Append `message` to the hook log, `$XDG_STATE_HOME/itemctl/hooks.log` (one JSON line each,
    lines older than LOG_DAYS dropped); a log that cannot be written is reported on stderr."""
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    path = Path(state) / "itemctl" / "hooks.log"
    now = datetime.now().astimezone()
    try:
        old = path.read_text("utf-8").splitlines() if path.is_file() else []
        kept = [
            line
            for line in old
            if now - datetime.fromisoformat(json.loads(line)["at"]) <= timedelta(days=LOG_DAYS)
        ]
        entry = {"at": now.isoformat(timespec="seconds"), "event": event, "cwd": os.getcwd()}
        kept.append(json.dumps(entry | {"message": message}))
        fsio.write(path, "\n".join(kept) + "\n")
    except (OSError, ValueError, KeyError) as exc:
        print(f"itemctl hook {event}: could not write {path}: {exc}", file=sys.stderr)


def _reference_transaction(state: str | None, payload: str | None) -> int:
    """git's reference-transaction hook on `payload` (None: read stdin): exit 3 (duplicate), which
    aborts the transaction, when `hooks.reference_transaction` refuses a prepared one; a failure
    of the check itself is reported and exits 0, so a broken check never blocks git. Both are
    logged (`_log`). A caller that wants an unexpected crash not to block git either aborts on 3
    alone."""
    updates = sys.stdin.read() if payload is None else payload
    if state not in (None, "prepared"):
        return 0
    try:
        reason = hooks.reference_transaction(Path.cwd(), updates)
    except (ItemError, ValueError, OSError) as exc:
        reason = f"itemctl hook reference-transaction: {exc}; record ids were not checked"
        status = 0
    else:
        status = 3
    if reason:
        print(reason, file=sys.stderr)
        _log("reference-transaction", reason)
        return status
    return 0


def _run_hook(args: argparse.Namespace, payload: str | None) -> int:
    """Run a hook on `payload` (None: read stdin); any failure of a Claude Code hook exits 1,
    which Claude Code shows without blocking."""
    if args.event == "reference-transaction":
        return _reference_transaction(args.state, payload)
    try:
        if args.event == "session-start":
            try:
                store = Store.open(args.store)
            except NoStore:
                return 0
            print("\n".join(f"itemctl {line}" for line in hooks.session_start(store)))
            return 0
        raw = sys.stdin.read() if payload is None else payload
        reason = hooks.pre_tool_use(json.loads(raw))
    except (ItemError, KeyError, ValueError, OSError) as exc:
        print(f"itemctl hook {args.event}: {exc}", file=sys.stderr)
        return 1
    if reason:
        decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny"}
        print(json.dumps({"hookSpecificOutput": decision | {"permissionDecisionReason": reason}}))
    return 0


def _run(args: argparse.Namespace) -> int:
    """Dispatch one verb; return the exit status."""
    store = Store.open(args.store)
    if args.verb in WRITE_VERBS and store.schema.writer:
        writer = store.schema.writer
        raise ItemError(
            f"{store.root} is written by `{writer}`, whose own rules itemctl does not know; "
            f"use `{writer}`"
        )
    match args.verb:
        case "add":
            edits = _edits(store.schema.kind(args.kind), args.fields, [])
            item = store.add(args.kind, args.title, edits, _body(args.body_file), not args.new)
            print(item.id)
        case "set":
            item = store.revise(
                args.ref,
                lambda current: _edits(store.schema.kind(current.kind), args.fields, args.append),
            )
            print(item.id)
        case "claim":
            item = store.claim(
                args.ref,
                holder=args.by,
                ttl=args.ttl,
                next_kind=args.next,
                where=_pairs(args.where),
            )
            print(item.id)
        case "release":
            print(store.release(args.ref, holder=args.by, force=args.force).id)
        case "show":
            print((store.root / store.item(args.ref).path).read_text("utf-8"), end="")
        case "list":
            items = store.items(args.kind, args.state, _pairs(args.where), args.closed)
            print("\n".join(map(_json_line, items)) if args.json else _table(items))
        case "check":
            if store.schema.writer:
                print(
                    f"note: {store.root} is written by `{store.schema.writer}`; "
                    "its own rules were not checked",
                    file=sys.stderr,
                )
            findings = store.check()
            print("\n".join(str(f) for f in findings) or "ok")
            return 1 if findings else 0
        case _:
            raise ItemError(f"unknown verb {args.verb!r}")
    return 0


def main(argv: list[str] | None = None, hook_payload: str | None = None) -> int:
    """Entry point: exit 0 ok, 1 findings or nothing to claim, 2 error, 3 duplicate.

    `hook_payload` is a hook's stdin already read by `entry`; None reads stdin when needed."""
    args = _parser().parse_args(argv)
    if args.verb == "hook":
        return _run_hook(args, hook_payload)
    try:
        return _run(args)
    except DuplicateError as exc:
        print(f"itemctl: duplicate: {exc}", file=sys.stderr)
        return 3
    except NothingClaimable as exc:
        print(f"itemctl: {exc}", file=sys.stderr)
        return 1
    except ItemError as exc:
        print(f"itemctl: {exc}", file=sys.stderr)
        return 2
