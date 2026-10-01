"""Plain `git` subprocess helpers.

`wt.py` wraps the `wt` binary; this module wraps the git operations coppice
needs that `wt` doesn't provide: fetching a repo's base branch, ancestry and
merge-conflict checks via plumbing (so classification stays side-effect
free), the actual merges into a worktree, and the in-place branch switch
behind `new`'s occupied-path remedy.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


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


def fetch_base(repo: Path, base: str, *, remote: str = "origin") -> None:
    """Fetch BASE from REMOTE, updating the REMOTE/BASE remote-tracking ref
    (opportunistic update: the fetched branch is stored under
    refs/remotes/REMOTE/ per the standard fetch refspec)."""
    _git(["fetch", remote, base], cwd=repo)


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


def worktree_entries(repo_root: Path) -> list[dict[str, Any]]:
    """Every worktree of REPO_ROOT as `git worktree list --porcelain`
    reports it, parsed into the same entry shape `wt list --format json`
    produces for the fields park/unpark read: `branch` (None when
    detached), `path`, `is_main` (the entry whose path is the repo root),
    and `worktree.state` ('prunable' when the directory is gone, else
    'active').

    One subprocess per repo where `wt list` spawns about ten: park's
    resolution needs nothing more than this. Merge status, remote
    divergence, and commit details are `list`/`remove` data, still fetched
    via `wt.list_worktrees` there. `commit.timestamp` is unknown here, so
    age labels on porcelain entries fall back to the filesystem birth time
    (see `_age_seconds`). A repo that errors (missing dir, not a repo)
    yields no entries, same as `wt.list_worktrees`."""
    proc = _git(["worktree", "list", "--porcelain"], cwd=repo_root, check=False)
    if proc.returncode != 0:
        return []
    main_real = os.path.realpath(repo_root)
    entries: list[dict[str, Any]] = []
    path: str | None = None
    branch: str | None = None
    prunable = False

    def _flush() -> None:
        nonlocal path, branch, prunable
        if path is None:
            return
        entries.append(
            {
                "branch": branch,
                "path": path,
                "is_main": os.path.realpath(path) == main_real,
                "worktree": {"state": "prunable" if prunable else "active"},
            }
        )
        path, branch, prunable = None, None, False

    for line in proc.stdout.splitlines():
        if not line:
            _flush()
        elif line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch "):
            branch = line[len("branch ") :].removeprefix("refs/heads/")
        elif line.startswith("prunable"):
            prunable = True
    _flush()
    return entries


def worktree_entries_many(repos: Iterable[Path]) -> dict[Path, list[dict[str, Any]]]:
    """`worktree_entries` for every REPO in REPOS, run concurrently: same
    all-subprocess-wait overlap reasoning as `wt.list_worktrees_many`,
    including the 8-wide cap, the dedupe, and skipping the pool for 0 or 1
    repos."""
    unique = list(dict.fromkeys(repos))
    if len(unique) <= 1:
        return {r: worktree_entries(r) for r in unique}
    with ThreadPoolExecutor(max_workers=min(len(unique), 8)) as pool:
        futures = {r: pool.submit(worktree_entries, r) for r in unique}
        return {r: f.result() for r, f in futures.items()}


def dirty_flags(path: str | Path) -> dict[str, bool]:
    """The worktree at PATH's dirtiness as the same flag dict `wt list`
    reports under `working_tree` (any flag set == dirty), from one
    `git status --porcelain` (default untracked mode, same as wt's
    --untracked-files=normal). A path that isn't a worktree (anymore)
    reads clean: there's nothing to lose in it either way."""
    flags = {"staged": False, "modified": False, "untracked": False, "renamed": False, "deleted": False}
    proc = _git(["status", "--porcelain"], cwd=Path(path), check=False)
    if proc.returncode != 0:
        return flags
    for line in proc.stdout.splitlines():
        if not line:
            continue
        x, y = line[0], line[1] if len(line) > 1 else " "
        if x == "?" and y == "?":
            flags["untracked"] = True
        if x in "MADRC":
            flags["staged"] = True
        if y in "MD" or "U" in (x, y):
            # Unmerged (conflict) states ('UU', 'AU', 'UD', ...) count as
            # modified: there are uncommitted changes either way.
            flags["modified"] = True
        if "R" in (x, y):
            flags["renamed"] = True
        if "D" in (x, y):
            flags["deleted"] = True
    return flags


def switch_in_place(wt_path: Path, branch: str, *, create: bool = False, base: str | None = None) -> str:
    """Switch the worktree at WT_PATH onto BRANCH without leaving the
    directory (plain `git switch`), forking BRANCH from BASE first when
    CREATE is set. Returns git's own one-line report ('Switched to ...',
    which git prints on stderr) for the caller to echo."""
    args = ["switch"]
    if create:
        args += ["-c", branch]
        if base:
            args.append(base)
    else:
        args.append(branch)
    proc = _git(args, cwd=wt_path)
    return (proc.stderr or proc.stdout).strip()
