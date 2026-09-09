"""The 'parked' mark: a task-complete worktree kept on disk for follow-up.

Storage is one git config key per branch, `branch.<branch>.parked-at
<unix-ts>`, in the repo's shared local config (so it reads the same from
the main checkout and every linked worktree). Chosen over a central state
file or a dotfile in the worktree because it is:

- git-grounded: keyed by repo + branch, the same identity convention
  coppice uses everywhere else, never folder basenames.
- self-cleaning: `git branch -D` drops the whole `branch.<name>` config
  section, so `cop remove`/`wt remove` clean up the mark for free, no
  central state file to garbage-collect.
- invisible to the worktree: config lives outside the tree, so it never
  shows up as an untracked file, breaks dirty detection, or makes
  `wt remove` refuse the worktree.
- cheap to read: one `git config --get-regexp` per repo.

The mark means "nothing new since I marked it": readers compare it against
the branch head's commit time, and a newer head means follow-up already
happened, so the worktree reads as active again with no writes and no
stale-mark cleanup job (see `_has_follow_up` in cli.py).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_KEY_PREFIX = "branch."
_KEY_SUFFIX = ".parked-at"
_GET_REGEXP = r"^branch\..*\.parked-at$"


def park(repo: Path, branch: str, *, at: float | None = None) -> float:
    """Mark BRANCH parked as of AT (default: now), returning the timestamp.
    Re-parking an already-parked branch simply refreshes the mark."""
    ts = float(at if at is not None else time.time())
    subprocess.run(
        ["git", "-C", str(repo), "config", f"{_KEY_PREFIX}{branch}{_KEY_SUFFIX}", str(int(ts))],
        capture_output=True,
        text=True,
        check=True,
    )
    return ts


def unpark(repo: Path, branch: str) -> None:
    """Delete BRANCH's parked mark. A missing key is not an error (git exits
    5 for it): unparking something never parked is a no-op."""
    subprocess.run(
        ["git", "-C", str(repo), "config", "--unset", f"{_KEY_PREFIX}{branch}{_KEY_SUFFIX}"],
        capture_output=True,
        text=True,
        check=False,
    )


def parked_at(repo: Path) -> dict[str, float]:
    """{branch: parked-at unix ts} for every parked branch in REPO, from one
    `git config --get-regexp` call. Its exit 1 just means 'no matches'
    (nothing parked), not an error.

    Values are parsed from the line's END (`key value` split on the last
    space) so a branch name containing a space can't shift the split.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "config", "--get-regexp", _GET_REGEXP],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {}
    marks: dict[str, float] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.rpartition(" ")
        if not key.startswith(_KEY_PREFIX) or not key.endswith(_KEY_SUFFIX):
            continue
        try:
            marks[key[len(_KEY_PREFIX) : -len(_KEY_SUFFIX)]] = float(value)
        except ValueError:
            continue
    return marks


def parked_at_many(repos: Iterable[Path]) -> dict[Path, dict[str, float]]:
    """`parked_at` for every repo in REPOS, run concurrently: one `git
    config` subprocess per repo, overlapped rather than serialized one repo
    after another (same reasoning as `wt.list_worktrees_many`, the wait is
    all subprocess wait). Dedupes REPOS first and skips the pool for 0 or 1
    repos, there's nothing to overlap."""
    unique = list(dict.fromkeys(repos))
    if len(unique) <= 1:
        return {r: parked_at(r) for r in unique}
    with ThreadPoolExecutor(max_workers=min(len(unique), 8)) as pool:
        futures = {r: pool.submit(parked_at, r) for r in unique}
        return {r: f.result() for r, f in futures.items()}
