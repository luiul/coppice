"""Repo resolution and the cross-repo registry `coppice` reads and writes.

`coppice` doesn't own worktree placement or lifecycle, `wt` (worktrunk) does.
This module just resolves a user-supplied PATH to a repo root, and
reads/writes the same `~/.cache/wt/known-repos` registry file that the
worktrunk `registry` post-start hook already populates, so `coppice
list`/`coppice remove` see every repo that hook (or `coppice new`'s own
self-heal below) has touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REGISTRY_PATH = Path.home() / ".cache" / "wt" / "known-repos"


class RepoResolutionError(RuntimeError):
    """PATH does not resolve to a git repository."""


def resolve_repo_root(path: str | Path = ".") -> Path:
    """Resolve PATH to its repo's root.

    Uses git-common-dir (not --show-toplevel) so this also works when PATH
    is inside a linked worktree, not just the main checkout: git-common-dir
    always resolves to the *main* repo's .git, wherever it's invoked from.
    """
    target = Path(path).expanduser()
    proc = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RepoResolutionError(f"not a git repository: {target}")
    return Path(proc.stdout.strip()).parent


def known_repos() -> list[Path]:
    """Repos registered in the shared `wt`/`coppice` registry file."""
    if not REGISTRY_PATH.exists():
        return []
    return [Path(line) for line in REGISTRY_PATH.read_text().splitlines() if line.strip()]


def register_repo(repo: Path) -> None:
    """Add REPO to the shared registry (deduped, sorted).

    Self-heals the case where the worktrunk `registry` post-start hook isn't
    configured: `coppice new` calls this itself after every switch, so
    `coppice list`/`coppice remove` can still find the repo later regardless
    of hook config.
    """
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
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
