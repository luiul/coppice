"""Plain `git` subprocess helpers for `coppice sync`.

`wt.py` wraps the `wt` binary; this module wraps the git operations `sync`
needs that `wt` doesn't provide: fetching a repo's base branch, ancestry and
merge-conflict checks via plumbing (so classification stays side-effect
free), and the actual merges into a worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """A git invocation failed; carries its stderr."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.git_args = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(stderr.strip() or f"git {' '.join(args)} exited {returncode}")


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc


def fetch_base(repo: Path, base: str) -> None:
    """Fetch BASE from origin, updating the origin/BASE remote-tracking ref
    (opportunistic update: the fetched branch is stored under
    refs/remotes/origin/ per the standard fetch refspec)."""
    _git(["fetch", "origin", base], cwd=repo)


def is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    """Whether ANCESTOR is an ancestor of DESCENDANT.

    `merge-base --is-ancestor` exits 0/1 for yes/no; anything higher is a
    real error (unknown ref, not a repo) and raises.
    """
    args = ["merge-base", "--is-ancestor", ancestor, descendant]
    proc = _git(args, cwd=cwd, check=False)
    if proc.returncode > 1:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc.returncode == 0


def merge_would_conflict(cwd: Path, branch: str, ref: str) -> bool:
    """Whether merging REF into BRANCH would conflict, computed in-core by
    `git merge-tree --write-tree` (git >= 2.38) without touching the index,
    the worktree, or any ref. Exit 1 means conflicts; anything above 1 is a
    real error (e.g. unrelated histories) and raises.
    """
    args = ["merge-tree", "--write-tree", branch, ref]
    proc = _git(args, cwd=cwd, check=False)
    if proc.returncode > 1:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc.returncode == 1


def commits_between(cwd: Path, branch: str, ref: str) -> int:
    """How many commits REF is ahead of BRANCH (`rev-list --count BRANCH..REF`)."""
    return int(_git(["rev-list", "--count", f"{branch}..{ref}"], cwd=cwd).stdout.strip())


def merge(wt_path: Path, ref: str) -> None:
    """Merge REF into the branch checked out at WT_PATH (default message)."""
    _git(["merge", "--no-edit", ref], cwd=wt_path)


def merge_abort(wt_path: Path) -> None:
    """Abort an in-progress merge at WT_PATH, restoring its pre-merge state.
    Tolerates there being no merge in progress (the apply may have failed
    before one started)."""
    _git(["merge", "--abort"], cwd=wt_path, check=False)


def ff_only(wt_path: Path, ref: str) -> None:
    """Fast-forward the branch checked out at WT_PATH to REF."""
    _git(["merge", "--ff-only", ref], cwd=wt_path)
