# itemctl

Markdown item stores: a store is a folder of markdown items with YAML frontmatter, described by one `store.toml`. One Python package owns every write — a library for tools built on it and a thin CLI, `itemctl`, for agents and shells. Writes take a lock, validate, and (optionally) commit to git.

Install:

```sh
uv tool install git+https://github.com/Gerenuk/ItemCtl
```

As a dependency: `itemctl @ git+https://github.com/Gerenuk/ItemCtl`.

Verbs: `add`, `set`, `claim`, `release`, `show`, `list`, `check`, and `hook` (Claude Code and git hook entry points); `itemctl --help` lists them. The design — `store.toml` format, item files, verbs, write pipeline, validation — is in [`docs/design.md`](docs/design.md).

Development: `uv run pytest -q`, `uv run ruff check`, `uv run ruff format --check`, `uv run lint-imports`. Requires Python ≥ 3.14.
