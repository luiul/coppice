"""Thin subprocess wrapper around the `wt` (worktrunk) binary.

`coppice` does not reimplement worktree lifecycle, hooks, or path templating,
`wt` stays the single source of truth for all of that: worktree paths,
hooks, and registration are `wt`'s job, not something duplicated here. This
module only shells out to `wt` and parses its `--format json` / `--json`
output; every side effect (worktree paths, hooks, registration) is `wt`'s
own config.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


class WtNotFoundError(RuntimeError):
    """The `wt` binary isn't on PATH."""


class WtCommandError(RuntimeError):
    """A `wt` invocation failed; carries its stderr."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.wt_args = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(stderr.strip() or f"wt {' '.join(args)} exited {returncode}")


def require_wt() -> str:
    path = shutil.which("wt")
    if path is None:
        raise WtNotFoundError("'wt' (worktrunk) is not installed. See https://worktrunk.dev")
    return path


def run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    require_wt()
    cmd = ["wt"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise WtCommandError(args, proc.returncode, proc.stderr)
    return proc


def _load_json(text: str) -> Any:
    # `wt list`'s JSON can carry a stray ANSI escape byte in the statusline
    # field; strip it so json.loads never chokes on a raw control character.
    return json.loads(text.replace("\x1b", ""))


def list_worktrees(repo: Path) -> list[dict[str, Any]]:
    """Every worktree of REPO, as `wt list --format json` reports them."""
    proc = run(["--config-set", "list.json-schema=1", "list", "--format", "json"], cwd=repo, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    return _load_json(proc.stdout)


def list_worktrees_many(repos: Iterable[Path]) -> dict[Path, list[dict[str, Any]]]:
    """`list_worktrees` for every REPO in REPOS, run concurrently.

    Each call is one `wt` subprocess invocation per repo; a thread pool (not
    a process pool, unlike `sizes.dir_sizes_kb`) is enough to overlap them,
    this call spends its whole time blocked in `subprocess.run` waiting on
    the child `wt` process, not holding the GIL doing Python-level work, so
    threads overlap N subprocesses' wait time instead of a caller serializing
    them one repo after another (which is what every multi-repo command used
    to do). Capped at 8 concurrent `wt` invocations so a large registry
    doesn't fork an unbounded number of subprocesses at once.

    Dedupes REPOS first (callers may pass the same repo twice, e.g. it's both
    registered and the one you're standing in), and skips the pool entirely
    for 0 or 1 repos, there's nothing to overlap.
    """
    unique = list(dict.fromkeys(repos))
    if len(unique) <= 1:
        return {r: list_worktrees(r) for r in unique}
    with ThreadPoolExecutor(max_workers=min(len(unique), 8)) as pool:
        futures = {r: pool.submit(list_worktrees, r) for r in unique}
        return {r: f.result() for r, f in futures.items()}


def branch_exists(repo: Path, branch: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    return proc.returncode == 0


def remote_branch_exists(repo: Path, branch: str) -> bool:
    """Whether BRANCH exists as a remote-tracking ref (e.g. origin/BRANCH)
    for any configured remote, even though it has no local branch yet.

    `branch_exists` alone misses this: a branch pushed by someone else but
    never checked out locally reads as "doesn't exist" from local refs, so
    callers deciding whether to pass `--create` (and fork a fresh branch
    from --base) would silently diverge from the remote branch of the same
    name instead of picking it up, the same fork `wt switch` (without
    --create) already knows how to avoid: "Switching to a remote branch
    ... creates a local tracking branch."

    Checks local remote-tracking refs only (no network calls); relies on
    those refs being reasonably fresh, same assumption `branch_exists`
    makes about local branches.
    """
    remotes = subprocess.run(
        ["git", "-C", str(repo), "remote"], capture_output=True, text=True, check=False
    ).stdout.split()
    return any(
        subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"]
        ).returncode
        == 0
        for remote in remotes
    )


def switch(
    repo: Path,
    branch: str,
    *,
    create: bool = False,
    base: str | None = None,
) -> dict[str, Any]:
    """Run `wt switch`, returning `{"action": "created"|"existing", "branch": ..., "path": ...}`."""
    args = ["switch"]
    if create:
        args.append("--create")
    if base is not None:
        args += ["--base", base]
    args += ["--no-cd", "--format", "json", branch]
    proc = run(args, cwd=repo)
    return _load_json(proc.stdout)


def run_post_start_hook(worktree: Path, name: str) -> bool:
    """Re-run one post-start hook (by NAME) inside an existing WORKTREE.

    `wt switch` without `--create` (i.e. reusing a worktree that already
    exists) skips post-start hooks entirely, they only fire at creation
    time. That's a gap for hooks whose effect isn't really about *creating*
    the worktree but about the worktree *being open right now* (e.g. the
    `herdr` hook registering it as a workspace in herdr's TUI) — a worktree
    reused from a fresh terminal, or one that predates the hook being added
    to the user's wt config, would otherwise never get that side effect.

    Runs `wt -C <worktree> hook post-start <name> -y`, `-C` the WORKTREE
    itself (not the repo root): post-start hook templates key off being run
    from inside the worktree they're acting on. `-y` skips the interactive
    approval prompt project-level hooks would otherwise trigger, matching
    how post-start hooks already run silently at creation time. Best-effort:
    returns whether it succeeded rather than raising, a hook hiccup (or `wt`
    not knowing about a hook by that name in some config) should never block
    the worktree switch that already happened.
    """
    proc = run(["hook", "post-start", name, "-y"], cwd=worktree, check=False)
    return proc.returncode == 0


def remove(repo: Path, branch: str, *, yes: bool = True, force: bool = False, force_delete: bool = False) -> None:
    args = ["remove", branch]
    if yes:
        args.append("-y")
    if force:
        args.append("-f")
    if force_delete:
        args.append("-D")
    run(args, cwd=repo)
