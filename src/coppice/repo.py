"""Repo resolution and the cross-repo registry `coppice` reads and writes.

`coppice` doesn't own worktree placement or lifecycle, `wt` (worktrunk) does
(see dotfiles issue #6). This module just resolves a user-supplied PATH to a
repo root, and reads/writes the same `~/.cache/wt/known-repos` registry file
that the worktrunk `registry` post-start hook already populates, so
`coppice list`/`coppice remove` see every repo that hook (or `coppice new`'s
own self-heal below) has touched.
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


def scope_repos(path: str | None) -> list[Path]:
    """Resolve the set of repos a scope-taking command should operate over.

    An explicit PATH scopes to just that one repo. Omitting it defaults to
    every registered repo, plus the repo you're standing in (if any),
    deduplicated.
    """
    if path is not None:
        return [resolve_repo_root(path)]

    repos = known_repos()
    try:
        cwd_repo = resolve_repo_root(".")
    except RepoResolutionError:
        cwd_repo = None

    if cwd_repo is not None and cwd_repo not in repos:
        repos.append(cwd_repo)

    return repos
