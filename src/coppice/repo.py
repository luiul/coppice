"""Repo resolution and the cross-repo registry `coppice` reads and writes.

`coppice` doesn't own worktree placement or lifecycle, `wt` (worktrunk) does.
This module just resolves a user-supplied PATH to a repo root, and
reads/writes the same `~/.cache/wt/known-repos` registry file that the
worktrunk `registry` post-start hook already populates, so `coppice
list`/`coppice remove` see every repo that hook (or `coppice new`'s own
self-heal below) has touched.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

REGISTRY_PATH = Path.home() / ".cache" / "wt" / "known-repos"


class RepoResolutionError(RuntimeError):
    """PATH does not resolve to a git repository."""


def resolve_repo_root(path: str | Path = ".") -> Path:
    """Resolve PATH to its repo's root.

    Uses git-common-dir (not --show-toplevel) so this also works when PATH
    is inside a linked worktree, not just the main checkout: git-common-dir
    always resolves to the *main* repo's .git, wherever it's invoked from.

    For a *bare* repo, `git-common-dir` already points at the repo root
    itself, not at a `.git` subdirectory inside it, even when resolved from
    a linked worktree of that bare repo. Taking `.parent` unconditionally
    would silently walk up to the bare repo's parent directory instead, an
    unrelated non-git path. So only take `.parent` when the repo isn't bare.
    """
    target = Path(path).expanduser()
    proc = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RepoResolutionError(f"not a git repository: {target}")
    common_dir = Path(proc.stdout.strip())

    # Check bareness of common_dir itself, not of target: from a linked
    # worktree of a bare repo, `--is-bare-repository` run against the
    # worktree reports false (the worktree checkout isn't bare), even though
    # git-common-dir already points at the bare repo's own root. Querying
    # common_dir directly gets the right answer in both the main-checkout
    # and linked-worktree cases.
    bare_proc = subprocess.run(
        ["git", "-C", str(common_dir), "rev-parse", "--is-bare-repository"],
        capture_output=True,
        text=True,
    )
    is_bare = bare_proc.returncode == 0 and bare_proc.stdout.strip() == "true"
    return common_dir if is_bare else common_dir.parent


def known_repos() -> list[Path]:
    """Repos registered in the shared `wt`/`coppice` registry file."""
    if not REGISTRY_PATH.exists():
        return []
    return [Path(line) for line in REGISTRY_PATH.read_text().splitlines() if line.strip()]


@contextmanager
def _locked_registry():
    """Hold an exclusive lock across a read-modify-write of the registry.

    `register_repo`/`prune_missing_repos` are both a plain read-JSON-then-
    overwrite with no locking otherwise: two concurrent writers (two `cop
    new` invocations, or a `wt` post-start `registry` hook firing mid-write)
    can both read the same stale contents, and whichever writes last
    silently clobbers the other's addition/removal. A sidecar `.lock` file
    (rather than locking REGISTRY_PATH itself) keeps plain readers like
    `known_repos` lock-free.
    """
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = REGISTRY_PATH.with_name(REGISTRY_PATH.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def register_repo(repo: Path) -> None:
    """Add REPO to the shared registry (deduped, sorted).

    Self-heals the case where the worktrunk `registry` post-start hook isn't
    configured: `coppice new` calls this itself after every switch, so
    `coppice list`/`coppice remove` can still find the repo later regardless
    of hook config.
    """
    with _locked_registry():
        repos = {str(r) for r in known_repos()}
        repos.add(str(repo))
        REGISTRY_PATH.write_text("\n".join(sorted(repos)) + "\n")


def prune_missing_repos() -> list[Path]:
    """Drop registered repos whose path no longer exists on disk, rewriting
    the registry, and return what got dropped.

    Registered repos can vanish for reasons `coppice` has no control over:
    a scratch repo removed by hand, a `wt`-hook-registered temp repo whose
    OS temp dir got reaped, a project directory that was simply deleted or
    moved. None of that is reversible, so there's nothing to preserve by
    keeping the entry around, it would just show up as a permanent
    `missing` row in `coppice status` (and a wasted `wt` subprocess call in
    `list`/`remove`/`clean`) until someone edits the registry file by hand.
    Called on every scope resolution so the registry self-heals on its own
    over time instead of accumulating dead entries.
    """
    with _locked_registry():
        repos = known_repos()
        missing = [r for r in repos if not r.exists()]
        if missing:
            remaining = {str(r) for r in repos if r.exists()}
            if remaining:
                REGISTRY_PATH.write_text("\n".join(sorted(remaining)) + "\n")
            else:
                REGISTRY_PATH.unlink(missing_ok=True)
    return missing


def scope_repos(path: str | None) -> list[Path]:
    """Resolve the set of repos a scope-taking command should operate over.

    An explicit PATH scopes to just that one repo. Omitting it defaults to
    every registered repo, plus the repo you're standing in (if any),
    deduplicated.
    """
    if path is not None:
        return [resolve_repo_root(path)]

    prune_missing_repos()
    repos = known_repos()
    try:
        cwd_repo = resolve_repo_root(".")
    except RepoResolutionError:
        cwd_repo = None

    if cwd_repo is not None and cwd_repo not in repos:
        repos.append(cwd_repo)

    return repos
