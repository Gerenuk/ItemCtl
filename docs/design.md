# itemctl — markdown item store design

A store is a folder of markdown items with YAML frontmatter, described by one `store.toml`. One Python package owns every write: a library for tools (claude-ctl, labctl, claude-tasks) and a thin CLI `itemctl` for agents and shells. It replaces the five hand-built stores' shared core; their domain logic stays in them as wrappers.

## Concepts

| concept | is |
|---|---|
| store | a directory holding `store.toml`; resolved from a linked worktree to the main checkout |
| kind | a named item type declared in `store.toml`: fields, states, directory |
| item | one `<slug>.md` (or `<slug>/item.md` + attachments) in its kind's directory |
| state | a value of the managed `state` field; each state constrains fields; some are terminal |
| claim | a lease on an item: holder + expiry, taken by compare-and-swap |

## `store.toml`

```toml
format_version = 1
id_prefix = "FR"        # ids FR-1, FR-2, … store-wide, never reused
id_width = 0            # zero-pad the number to this many digits (4: FR-0001); default 0
git_commit = true       # commit every write, exact paths only
# writer = "claude-ctl friction"  # optional: another tool owns writes; the bare CLI refuses them

[kinds.friction]
dir = "."               # relative to the store root; default: the kind name
dedup_key = ["component"]      # add refuses while a non-terminal item matches
sections = ["Occurrences"]     # append-only body sections
attachments = false            # true: <slug>/item.md + sibling files
index = ["id", "title", "state", "component"]   # generate <dir>/index.md
max_age_days = 30              # check and the session-start line report non-terminal items older than this

[kinds.friction.fields]
title     = "str"
component = "str"
fixing_by = "str"
fixed_in  = "str"
outcome   = ["fixed", "declined"]   # a list is an enum
tags      = "list"

[kinds.friction.states]        # first listed = initial state
open          = { requires = ["title", "component"] }
worked-around = { requires = ["title", "component"] }
fixing        = { requires = ["fixing_by"], only_here = ["fixing_by"] }
resolved      = { requires = ["outcome", "fixed_in"], only_here = ["outcome"], terminal = true }
```

- Field types: `str` (one line), `int`, `bool`, `date`, `datetime`, `list` (of `str`), `ref` (an item id in this store), enum (a TOML list of values). Seven, closed set; an unknown type fails on load.
- `requires` is per state; `requires_any` (a list) needs at least one of its fields present; `only_here` fields are removed automatically when the item leaves that state.
- The reader accepts `format_version = 1` only and raises otherwise; a format change ships a migration script.
- A missing `store.toml`, or one without a `kinds` table (another tool's file), is no store: loading raises `NoStore`, which both hooks treat as "nothing to do" and the other verbs report (exit 2). A `store.toml` with `kinds` that does not parse is a broken store and raises everywhere.
- Undeclared frontmatter fields pass through unvalidated and are written back verbatim after the declared ones; they cannot appear in `requires`, `only_here`, `dedup_key` or `index`. `list`, `where` and `set` treat them as strings (`f+=v` makes a list).
- `writer` optionally names another tool that owns writes to this store (e.g. `claude-ctl friction`), for a consumer that passes its own rules to `Store(rules=…)` through the Python API — rules the bare CLI does not know and so must not bypass. A non-empty string, or absent; an empty string or a non-string value raises like any other bad key.

## Item file

```markdown
---
id: FR-12
state: fixing
created: 2026-09-26T21:10:00+02:00
claimed_by: session-ab12
claimed_at: 2026-09-26T21:15:00+02:00
lease_until: 2026-09-26T23:15:00+02:00
title: Worktree agent branched off a stale tip
component: harness
fixing_by: strand-3
---
Free markdown.

## Occurrences
- 2026-09-17 — first seen in the worktree dispatch
- 2026-09-26 — again, CliffordRust
```

- Managed fields, written only by the tool: `id`, `state`, `created`, `claimed_by`, `claimed_at`, `lease_until`. Timestamps are timezone-aware ISO 8601.
- File name = slug of the title (lowercase alnum runs, `-`, ≤ 60 chars), unique per kind; a collision is refused, naming the existing item.
- Ids: max id over all items (terminal ones included) + 1, computed under the lock; items are never deleted, so an id is never reused.
- Terminal items live in `<dir>/closed/`; leaving a terminal state moves them back.
- Empty/null values are never written; an absent field is absent.
- An item is referred to by id or slug (`REF`).

## Verbs

| CLI | library | does |
|---|---|---|
| `add KIND TITLE [f=v…] [--body-file F\|-] [--new]` | `Store.add` | create in the initial state (or `state=`); refuse on a `dedup_key` match (exit 3, names the match) unless `--new`; print the id |
| `set REF [f=v] [f+=v] [f=] [--append SECTION TEXT]` | `Store.set`, `Store.revise`, `Store.revise_many` | the only mutator (`revise` derives the edits from the item under the lock and may refuse — compare-and-swap for wrappers): assign, append to a list, unset (`f=`), append a dated line to a section — all in one atomic write. `revise_many(refs, plan)` revises each ref in order, each planned on the snapshot the earlier ones left, in one write and one commit: a refusal from `plan` or any finding writes none, two refs naming one item raise `ItemError` before any plan runs, and the new items return in ref order |
| `claim REF \| --next KIND [--where f=v…] --by H [--ttl 2h]` | `Store.claim` | lease the item if unclaimed, held by `H` (renew) or expired; `--next` takes the oldest non-terminal unclaimed match whose `ref` blockers are all terminal; print the id |
| `release REF --by H [--force]` | `Store.release` | drop the lease; holder-checked unless `--force`; automatic on entering a terminal state |
| `show REF` | `Store.item` | print the file |
| `list [KIND] [--state S] [--where f=v…] [--closed] [--json]` | `Store.items` | table, or NDJSON (frontmatter + `kind`, `path`) for `jq`/`duckdb` |
| `check` | `Store.check` | validate every item; report stale items and expired leases; exit 1 on any finding; with a declared `writer`, also print one stderr note that its own rules were not checked |
| `hook session-start \| pre-tool-use \| reference-transaction [STATE]` | `hooks` | one line per kind: counts per state, oldest non-terminal age, and the number of stale items and expired leases; deny a Write/Edit that creates an item or changes its frontmatter, and any Write/Edit under a linked worktree's copy of a store (naming the main checkout's path to edit instead, or the declared `writer` in place of `itemctl`); abort a git ref update that brings to main a record id main already uses (§Landing check) |

Exit status: 0 ok; 1 `check` findings or nothing claimable; 2 any other refusal; 3 a `dedup_key` match, or a landing the landing check refuses. `f=v` on a `list` field sets a one-element list; `f+=v` appends.

On a store that declares `writer`, `add`, `set`, `claim` and `release` refuse from the bare CLI before taking the lock or reading any item (exit 2, naming the writer): the writer's own rules — passed to `Store(rules=…)` through the Python API — are ones the CLI does not run, so it never bypasses them. `show`, `list` and `check` are unaffected, and the library `Store` API is never gated (a consumer *is* the writer).

Closing is `set REF state=resolved outcome=fixed fixed_in=abc123`; friction's `recur` is `set REF state=open --append Occurrences "…"`. The store is `--store PATH`, else the nearest `store.toml` above the working directory. Found in a linked worktree (its git dir is not the common dir), it maps to the same path in the main checkout: the common dir's `core.worktree` (a submodule), else the parent of a common dir named `.git`; a bare repository or a `--separate-git-dir` one (which records no path to its checkout) raises. A store is read and written only there.

## Write pipeline

Every write, in one process:

1. Take `flock` on `<store>/.lock` (gitignored).
2. Refuse during a merge, rebase, cherry-pick or on a detached HEAD (when `git_commit`).
3. Load the snapshot: every item, parsed once, as frozen values.
4. Build the change sets with a pure function of (snapshot, request), applied to the snapshot in order (one change per verb; one per ref for `revise_many`).
5. Validate the post-change snapshot once with the same `validate` and caller rules that `check` runs; any finding raises and nothing is written. Changes that leave every item's path and bytes as they are return the items without writing, reindexing or committing.
6. Plan the file steps of every change against the post-change snapshot (item, then `index.md`), keeping the first of equal steps so an `index.md` is written once; with `git_commit`, refuse when git ignores any touched path, naming it, before writing anything.
7. Write each file to a temp file beside it (the replaced file's mode, else `0o666 & ~umask`), fsync it, `os.replace` it and fsync the directory. A move writes the new text to the old path, then renames the file (or the attachments directory) to the new one, so an item is always at exactly one path.
8. Regenerate the affected `index.md` files.
9. Commit exactly the touched paths in one commit: `<kind>: <verb> <id> <slug>` for one change, `<kind>: <verb> <n> records <id>…` for several (the first change's kind and verb, a mixed-kind `revise_many` included); a failed `git add` or commit (a rejecting hook) unstages them.

Steps 7–9 run under an undo log: every file step records its inverse (the replaced bytes or their absence, the directories it created, the rename back), and any failure after the first step undoes them newest first and re-raises, so a failed write leaves files and `git status` as they were.

Reads take no lock: `os.replace` means a reader sees a whole old or a whole new file. A read raises on an item file that does not parse; the walk skips directories tagged `CACHEDIR.TAG`. `changes.section_entries` reads back the dated lines `--append` writes.

## Landing check

Ids are allocated from the main checkout, but a record committed on another branch (a hand write, or `claude-ctl friction` writing a linked worktree's store copy) carries an id main's allocator never saw. `itemctl hook reference-transaction prepared`, run as git's `reference-transaction` hook, refuses the ref update that would bring it to main:

- **Guarded ref:** the branch the main worktree has checked out, or is rebasing while its HEAD is detached (`gitio.main_branch`). Any route that moves it runs the check: a commit, a fast-forward or other merge, the end of a rebase, `update-ref`, from any worktree.
- **Rule:** for every itemctl store committed in the new commit's tree (a `store.toml` with `[kinds]`; a file belongs to the nearest one), an id used more often than in the old commit (git's zero old id means the ref's current commit) and more than once is brought; each item holding it at a path or with an id the old commit did not have is named, with the next free id (`max + 1`, then onward) to renumber it to. An item that does not parse holds no id here.
- **Refusal:** exit 3 and a stderr message naming each item, its free id, and the fix: change its `id:` line on the branch, commit, land again; a fast-forward merge has already written the branch's files into the checkout, which `git reset --merge` restores. A failure of the check itself (git, an unparseable `store.toml`) prints one line and exits 0. Both are appended to `$XDG_STATE_HOME/itemctl/hooks.log` (JSON lines; lines older than 30 days dropped on each append).
- **Cost:** `entry` answers a transaction whose commits hold no `store.toml` with stdlib and one `git ls-tree`. Otherwise item ids are cached by blob id in `<git common dir>/itemctl/item-ids.json`, so a check parses only new blobs, and the old commit is read only when the new one holds an id twice.
- **Wiring:** `~/Scripts/global-git-hooks/reference-transaction` runs it, in state `prepared` only and only when a stdin line names the guarded ref, aborting on exit 3 alone.

## Validation (one function, writer and `check`)

- `store.toml`: known keys and types only; every `requires`/`only_here`/`dedup_key`/`index` name is a declared field.
- Item: frontmatter is a mapping; declared fields match their types; the state's `requires` present; no `only_here` field of another state; managed fields well-formed; lease fields all present or all absent.
- Store: unique ids and slugs (per kind); terminal ⇔ in `closed/`; every `ref` resolves; no stray files. Closing a `ref` target is allowed: a ref is a blocker, released when its target turns terminal.
- Caller rules: `Store(root, clock, rules=…)` and `Store.open(path, clock, rules=…)` take `Sequence[Callable[[Snapshot], list[Finding]]]` (`model.Rule`) — a wrapper's own store-wide checks. They run after `validate` on the post-change snapshot of every write (a finding refuses it, nothing written), on the snapshot `claim --next` selects from, and in `check`.

## Package layers

Imports flow down; modules in one layer do not import each other; import-linter enforces it.

1. `entry` — the `itemctl` executable: allows an edit-guard call on a file under no `store.toml`, and a landing check of commits holding no `store.toml`, with stdlib alone (the guard runs before every edit in every session, the check whenever a main branch moves), and runs `cli` for everything else
2. `cli` — argument parsing, output formatting
3. `hooks` — the session-start summary, the frontmatter guard and the landing check
4. `store` — the `Store` verbs: lock → snapshot → change → validate → write → index → commit; `landing` — the stores and item ids a commit's tree holds, and the ids a branch move brings
5. `changes`, `validate`, `render_index` — pure functions over the snapshot
6. `schema` (parse `store.toml`, item paths), `record` (parse/render one item file)
7. `fsio` (lock, atomic write and rename, undo log, file walk), `gitio` (state and ignore checks, pathspec commit, worktree → main checkout)
8. `model` — frozen dataclasses (`Kind`, `Field`, `State`, `Item`, `Snapshot`, `ChangeSet`) and errors; `scope` — the guard's inspected tools and the store root owning a path, stdlib only

Dependencies: stdlib (`tomllib`, `fcntl`) + `ruamel.yaml` for frontmatter (order-preserving round trip), loading through `ruamel.yaml.clib`. Python: newest stable CPython.

## Tests (independent oracles)

- Round trip: parse ∘ render = identity on generated items (hypothesis).
- Concurrency: N processes each `claim --next` on M items → every item claimed exactly once; N parallel `add` → N distinct ids, clean `git status`.
- Lease: expired lease is claimable by another holder, live one is not (injected clock).
- Validation: each rule has a deny case and a nearby allow case; a mutant run confirms each test catches its rule's removal.
- Isolation: tests copy a fixture store to a temp dir and assert the fixture is untouched.

## Callers and migration

| today | becomes | kinds |
|---|---|---|
| `claude-ctl friction` | wrapper: component existence check, `project:<name>` routing, report | `friction` in a global friction store (`git_commit = false`) and in each project's claude-tasks store |
| claude-tasks | wrapper: board, front door; owns every kind's shape in one store per project | `idea`, `friction` (written by claude-ctl), `handover` (`attachments = true`) |
| `/pause` handover | `claude-tasks handover`; claude-ctl takes a dead holder's claim over via `release --force` | `handover` |
| labctl inbox / issue / desk | wrapper: drain routing, bounce escalation, fuzzy nearest | `inbox`, `issue`, `desk` |
| labctl results | stays in labctl unless results become one file each | — |

Order: friction (smallest, no git) → claude-tasks → handover → labctl. Each migration ships a one-shot script and deletes the old store code in the same change.

## Not in the core

Fuzzy duplicate detection, drain routing, `trigger`/`reopens_if` predicates, HTML board, per-kind validator plugins, status-change callbacks. Each enters when a second caller needs it.
