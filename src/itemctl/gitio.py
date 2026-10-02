"""Git shell: worktree → main checkout, write preconditions, commits of exact paths."""

import os
import subprocess
from pathlib import Path

from itemctl.model import ItemError


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `cwd` with C-locale output and unquoted paths; caller judges the exit status."""
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )


def checked(cwd: Path, *args: str) -> str:
    """Run git in `cwd`, raising with its stderr (else stdout) on failure; return its stdout."""
    proc = run(cwd, *args)
    if proc.returncode != 0:
        reason = proc.stderr.strip() or proc.stdout.strip()
        raise ItemError(f"git {' '.join(args)} in {cwd} failed: {reason}")
    return proc.stdout.strip()


def _config(common: Path, key: str) -> str | None:
    """`key` from the repository config in the common dir `common`, or None when unset."""
    proc = run(common, "config", "--file", str(common / "config"), "--get", key)
    if proc.returncode not in (0, 1):
        raise ItemError(f"git config --get {key} in {common} failed: {proc.stderr.strip()}")
    return proc.stdout.strip() or None


def main_checkout(path: Path) -> Path:
    """`path` mapped from a linked worktree to the same place in the main checkout.

    `path` is in the main worktree when its git dir is the common dir. From a linked
    worktree the main checkout is the common dir's `core.worktree` (a submodule), else the
    parent of a common dir named `.git`; anything else (a bare repository, a
    `--separate-git-dir` repository, which records no path to its checkout) raises.
    A path outside git is returned as it is.
    """
    proc = run(
        path,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
    )
    if proc.returncode != 0:
        if "not a git repository" in proc.stderr:
            return path
        raise ItemError(f"git rev-parse in {path} failed: {proc.stderr.strip()}")
    top, git_dir, common = (Path(line).resolve() for line in proc.stdout.splitlines())
    if git_dir == common:
        return path
    if worktree := _config(common, "core.worktree"):
        main = (common / worktree).resolve()
    elif common.name == ".git":
        main = common.parent
    else:
        bare = _config(common, "core.bare") == "true"
        why = "is bare" if bare else "records no path to its main checkout"
        raise ItemError(
            f"{path} is in a linked worktree, and its repository {common} {why}; "
            "run from the main checkout or pass --store with the store's path there"
        )
    return main / path.resolve().relative_to(top)


def check_writable(root: Path) -> None:
    """Refuse a write during a merge, rebase or cherry-pick, or on a detached HEAD."""
    git_dir = Path(checked(root, "rev-parse", "--path-format=absolute", "--git-dir"))
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        if (git_dir / marker).exists():
            raise ItemError(f"{root}: {git_dir / marker} exists; finish or abort that operation")
    if run(root, "symbolic-ref", "-q", "HEAD").returncode != 0:
        raise ItemError(f"{root}: HEAD is detached; check out a branch before writing")


def commit(root: Path, paths: list[Path], message: str) -> None:
    """Stage and commit exactly `paths` (a vanished tracked path commits as a deletion)."""
    rel = [str(p.relative_to(root)) for p in paths]
    tracked = set(checked(root, "ls-files", "-z", "--", *rel).split("\0"))
    spec = [
        r
        for r, p in zip(rel, paths, strict=True)
        if p.exists() or r in tracked or any(t.startswith(f"{r}/") for t in tracked)
    ]
    try:
        checked(root, "add", "-A", "--", *spec)
        checked(root, "commit", "-q", "-m", message, "--", *spec)
    except ItemError:
        checked(root, "reset", "-q", "--", *spec)
        raise


def check_not_ignored(root: Path, paths: list[Path]) -> None:
    """Refuse when git ignores any of `paths`, before a write that would fail to commit them."""
    proc = run(root, "check-ignore", "--", *(str(p.relative_to(root)) for p in paths))
    if proc.returncode == 0:
        ignored = ", ".join(proc.stdout.splitlines())
        raise ItemError(
            f"{root}: git ignores {ignored}, so the write could not be committed; "
            "change .gitignore or the store's layout"
        )
    if proc.returncode != 1:
        raise ItemError(f"git check-ignore in {root} failed: {proc.stderr.strip()}")


ZERO_OID = frozenset({"0" * 40, "0" * 64})


def main_branch(cwd: Path) -> str | None:
    """The ref of the branch the main worktree has checked out, or is rebasing while its HEAD is
    detached; None when it has neither (a detached HEAD outside a rebase)."""
    common = Path(checked(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    proc = run(cwd, "--git-dir", str(common), "symbolic-ref", "-q", "HEAD")
    if proc.returncode == 0:
        return proc.stdout.strip()
    if proc.returncode != 1:
        raise ItemError(f"git symbolic-ref HEAD in {common} failed: {proc.stderr.strip()}")
    for name in ("rebase-merge/head-name", "rebase-apply/head-name"):
        if (common / name).is_file():
            return (common / name).read_text("utf-8").strip()
    return None


def ref_target(cwd: Path, ref: str) -> str | None:
    """The commit `ref` names, or None when it does not exist."""
    proc = run(cwd, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
    if proc.returncode not in (0, 1):
        raise ItemError(f"git rev-parse {ref} in {cwd} failed: {proc.stderr.strip()}")
    return proc.stdout.strip() or None


def tree_blobs(cwd: Path, rev: str) -> dict[str, str]:
    """Every regular file of commit `rev`'s tree: repository path → blob id."""
    out = checked(cwd, "ls-tree", "-r", "-z", "--full-tree", rev)
    blobs = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        mode, kind, oid = meta.split()
        if kind == "blob" and mode in ("100644", "100755"):
            blobs[path] = oid
    return blobs


def read_blobs(cwd: Path, oids: list[str]) -> list[bytes]:
    """The contents of blobs `oids`, in order, through one `git cat-file --batch`."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "cat-file", "--batch"],
        input="".join(f"{oid}\n" for oid in oids).encode(),
        capture_output=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    if proc.returncode != 0:
        raise ItemError(f"git cat-file --batch in {cwd} failed: {proc.stderr.decode().strip()}")
    out, pos, contents = proc.stdout, 0, []
    for oid in oids:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ItemError(f"git cat-file --batch in {cwd}: {oid} is not a blob")
        size = int(header[2])
        contents.append(out[end + 1 : end + 1 + size])
        pos = end + 1 + size + 1
    return contents
