"""Thin subprocess wrapper around the `wt` (worktrunk) binary.

`coppice` does not reimplement worktree lifecycle, hooks, or path templating,
`wt` stays the single source of truth for all of that: worktree paths,
hooks, and registration are `wt`'s job, not something duplicated here. This
module only shells out to `wt` and parses its `--format json` / `--json`
output; every side effect (worktree paths, hooks, registration) is `wt`'s
own config.
"""

from __future__ import annotations

import codecs
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast


class WtNotFoundError(RuntimeError):
    """The `wt` binary isn't on PATH."""


class WtCommandError(RuntimeError):
    """A `wt` invocation failed; carries its stderr, when it was captured.

    Streamed invocations (stream=True) tee stderr: it goes to the terminal
    live AND lands in .stderr. Their message stays the short "exited N"
    form (the detail is already on screen, re-printing it would double it),
    but callers can still inspect .stderr for what `wt` actually said, and
    .streamed tells them it was already shown.
    """

    def __init__(self, args: list[str], returncode: int, stderr: str | None, *, streamed: bool = False) -> None:
        self.wt_args = args
        self.returncode = returncode
        self.stderr = stderr
        self.streamed = streamed
        detail = "" if streamed else (stderr or "").strip()
        super().__init__(detail or f"wt {' '.join(args)} exited {returncode}")


def require_wt() -> str:
    path = shutil.which("wt")
    if path is None:
        raise WtNotFoundError("'wt' (worktrunk) is not installed. See https://worktrunk.dev")
    return path


def run(
    args: list[str],
    cwd: Path | None = None,
    check: bool = True,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    require_wt()
    cmd = ["wt"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += args
    # env is left unset: the child inherits this process's environment
    # as-is (the streaming path makes one deliberate exception, see there).
    if stream:
        return _run_streaming(cmd, args, check)
    # The machine-read, parallelized list path keeps both streams captured:
    # concurrent `wt` children would interleave on the shared terminal.
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise WtCommandError(args, proc.returncode, proc.stderr)
    return proc


def _run_streaming(cmd: list[str], wt_args: list[str], check: bool) -> subprocess.CompletedProcess[str]:
    """Run CMD with the child's stderr shown live on our own AND kept.

    stderr is `wt`'s human channel (hook progress, status lines, config
    warnings): the mutating commands must show it live, a slow pre-switch
    fetch under `cop new` otherwise looks like a hang, and a config typo's
    warning would stay invisible forever. But plainly inheriting the
    terminal (stderr=None) throws the text away, and with it the caller's
    chance to react to what `wt` said (cmd_new's occupied-path remedy
    keys off the error's wording). So stderr goes through a pipe, tee'd to
    the terminal chunk by chunk as it arrives. stdout stays piped either
    way, it carries the JSON we parse.

    A pipe hides the terminal from `wt`, which would strip its colors, so
    CLICOLOR_FORCE=1 is set when our own stderr is a terminal: the one
    deliberate exception to the inherit-the-environment-as-is rule.
    """
    err_stream = sys.stderr  # capture once; test harnesses swap it per test
    env = None
    if err_stream is not None and err_stream.isatty():
        env = {**os.environ, "CLICOLOR_FORCE": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    chunks: list[bytes] = []

    def _tee() -> None:
        assert proc.stderr is not None
        # Popen's pipes are io.BufferedReader objects at runtime; the
        # IO[Any] stubs don't expose read1, so narrow for the type checker.
        pipe = cast(io.BufferedReader, proc.stderr)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := pipe.read1(4096):
            chunks.append(chunk)
            if err_stream is not None:
                err_stream.write(decoder.decode(chunk))
                err_stream.flush()

    tee = threading.Thread(target=_tee, daemon=True)
    tee.start()
    assert proc.stdout is not None
    stdout = proc.stdout.read().decode("utf-8", errors="replace")
    returncode = proc.wait()
    tee.join()
    stderr = b"".join(chunks).decode("utf-8", errors="replace")
    if check and returncode != 0:
        raise WtCommandError(wt_args, returncode, stderr, streamed=True)
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


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
    proc = run(args, cwd=repo, stream=True)
    return _load_json(proc.stdout)


def relocate_preview(repo: Path, branch: str) -> list[dict[str, str]]:
    """Dry-run `wt step relocate BRANCH`: the [{"from":..., "to":...}] moves
    it would make. Empty when BRANCH can't be relocated (locked, detached
    HEAD, or a blocked target, `wt` reports those under 'skipped')."""
    proc = run(["step", "relocate", "--dry-run", "--format", "json", branch], cwd=repo)
    return _load_json(proc.stdout).get("entries", [])


def relocate(repo: Path, branch: str, targets: list[dict[str, str]]) -> None:
    """Relocate BRANCH's worktree to its expected path, performing the moves
    TARGETS (a `relocate_preview` result) listed.

    Pre-creates each target's parent directory first: relocate shells out
    to `git worktree move`, which refuses a target whose parent doesn't
    exist yet ("No such file or directory"), and the parent of a
    never-created branch path never exists yet.
    """
    for target in targets:
        Path(target["to"]).expanduser().parent.mkdir(parents=True, exist_ok=True)
    run(["step", "relocate", "--yes", branch], cwd=repo, stream=True)


def prune_stale(repo: Path, path: str) -> None:
    """Prune one stale (prunable) worktree reference by path.

    `wt remove` can't take these: it refuses a worktree whose directory is
    already gone ("Worktree directory missing ... run git worktree prune"),
    and a detached stale entry has no branch name to hand it in the first
    place. git's own `worktree remove --force` tolerates the missing
    directory and drops exactly this one registration, unlike a repo-wide
    `git worktree prune`.
    """
    args = ["worktree", "remove", "--force", path]
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise WtCommandError(args, proc.returncode, proc.stderr)


def remove(repo: Path, branch: str, *, yes: bool = True, force: bool = False, force_delete: bool = False) -> None:
    args = ["remove", branch]
    if yes:
        args.append("-y")
    if force:
        args.append("-f")
    if force_delete:
        args.append("-D")
    run(args, cwd=repo, stream=True)
