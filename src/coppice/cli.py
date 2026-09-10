"""coppice: a path-based CLI for git worktrees, built on top of `wt` (worktrunk).

Every subcommand takes an explicit PATH instead of relying on the current
working directory, and `wt` itself stays the source of truth for worktree
paths, hooks, and registration, `coppice` only shells out to it and to `git`.

Installed as two identical binaries, `coppice` and the shorter `cop` alias,
both pointing at this same `app`; use whichever you like everywhere below.

Note: `coppice`/`cop` is a plain executable, not a shell function, so it
cannot change your shell's working directory on its own the way `wt`'s own
shell integration does. Run `eval "$(coppice shell init zsh)"` in your shell
rc file (see `coppice shell init --help`) to get the same behavior: `new`
will then `cd` you into the resulting worktree.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, NamedTuple

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from typer.core import TyperGroup

from coppice import branch as branch_mod
from coppice import confirm, gh, git, repo, shell, sizes, vscode, wt
from coppice import park as park_mod

APP_HELP = """\
Path-based CLI for git worktrees, built on top of [bold]wt[/] (worktrunk).

Also installed as [cyan]cop[/], a shorter alias for the same command, use
whichever you prefer.

[bold]Requires wt (worktrunk) on PATH[/], see https://worktrunk.dev. Bare
[cyan]cop PATH[/] is shorthand for [cyan]cop new PATH[/].

Run [cyan]eval "$(cop shell init zsh)"[/cyan] in your shell rc file so
[cyan]cop new[/cyan] can `cd` you into the resulting worktree.
"""


def _looks_like_path(value: str) -> bool:
    """coppice#9's detection rule for treating a bare first argument as a
    PATH (shorthand for 'coppice new PATH') rather than an unknown or
    mistyped subcommand: starts with '.', '~/', or '/' (covers '.', '..',
    './', '../', '~/', '/' in one prefix check), or is an existing
    directory.
    """
    if value.startswith((".", "~/", "/")):
        return True
    return Path(value).expanduser().is_dir()


class _PathShortcutGroup(TyperGroup):
    """Makes a bare 'coppice PATH' shorthand for 'coppice new PATH' (coppice#9).

    Only kicks in when the first argument doesn't already resolve to a real
    subcommand, real subcommands always win, so this never shadows
    'new'/'list'/'remove'/'clean'/'status'/'shell'. A mistyped subcommand
    name that also doesn't look like a path (per `_looks_like_path`) still
    gets Typer's normal "No such command" + suggestion instead of being
    silently swallowed as a `new` invocation here.
    """

    def resolve_command(self, ctx, args):
        if args and self.get_command(ctx, args[0]) is None and _looks_like_path(args[0]):
            args = ["new", *args]
        return super().resolve_command(ctx, args)


def _version_callback(show_version: bool) -> None:
    if not show_version:
        return
    # Imported on demand, not at module top: importlib.metadata costs ~75ms
    # of interpreter startup (its own import chain plus the distribution
    # scan), a tax on every invocation for something only --version needs.
    from importlib.metadata import PackageNotFoundError, version

    try:
        console.print(f"coppice {version('coppice')}")
    except PackageNotFoundError:
        console.print("coppice (version unknown, not installed as a package)")
    raise typer.Exit()


app = typer.Typer(
    cls=_PathShortcutGroup,
    help=APP_HELP,
    no_args_is_help=True,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode="rich",
)


@app.callback()
def _main(
    version_: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the version and exit."),
    ] = False,
) -> None:
    pass


# highlight=False: Rich's default ReprHighlighter otherwise rainbow-colors
# anything that looks like a number or a path (cyan counts, magenta slashes
# in paths), accidental noise on top of the deliberate markup everywhere
# below.
console = Console(highlight=False)
err = Console(stderr=True, highlight=False)


# Semantic color theme: one color means one thing across every command, so a
# hue can't drift into two meanings (red used to mean both 'stale' and
# 'conflict', yellow both 'dirty' and 'unmerged').
_STYLE_MERGED = "green"
_STYLE_UNMERGED = "cyan"
_STYLE_CONFLICT = "red"
_STYLE_DIRTY = "yellow"
_STYLE_STALE = "bold red"
_STYLE_CURRENT = "bold green"
_STYLE_MISMATCH = "magenta"
# The parked label stays blue inside the otherwise-dimmed parked row: the
# mark is the row's one visual signal, and dimming it away with the rest
# made parked rows indistinguishable from merely inactive ones.
_STYLE_PARKED = "blue"

# Output spacing convention: one blank line before a command's first output
# line and one after its last (breathing room against the shell prompt on
# both sides), and one blank line between logical blocks within a command
# (tables, prompts, action logs, summaries). Never in --json output
# (machine-readable), stderr error paths, or 'shell init' (eval'd code).


def _fail(message: str) -> typer.Exit:
    err.print(f"[red]Error:[/] {message}")
    return typer.Exit(1)


def _plural(n: int, singular: str) -> str:
    """'1 worktree' but '2 worktrees': English pluralization for summary lines."""
    return f"{n} {singular}" if n == 1 else f"{n} {singular}s"


def _print_existing_worktrees(repo_root: Path) -> None:
    """Show what's already in flight before prompting for a new branch,
    to avoid accidentally starting a near-duplicate of existing work.

    Only called on the interactive path (no --branch): the preview's whole
    purpose is informing the branch-description prompt, so with the name
    already decided its `wt list` subprocess would be pure latency.

    Skips the (slow, directory-walking) size column here: this preview runs
    before the user's even typed a branch name, so it stays fast rather
    than complete.
    """
    worktrees = wt.list_worktrees(repo_root)
    _enrich_parked({repo_root: worktrees}, {repo_root: park_mod.parked_at(repo_root)})
    others = [w for w in worktrees if not w.get("is_main") and not w.get("is_current")]
    if not others:
        return
    console.print(f"Existing worktrees for {_repo_header(repo_root)}:")
    table, _total_kb, _n_stale = _worktrees_table(others, show_size=False)
    console.print(table)
    console.print()


@app.command("new", rich_help_panel="Create")
def cmd_new(
    path: Annotated[str, typer.Argument(help="Repo to create/reuse a worktree in.")] = ".",
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Branch name. Prompts for a short description if omitted."),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option(
            "--base",
            "-B",
            help="Base branch/ref to create from. Defaults to the repo's actual default branch (freshly resolved from its remote).",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt when the branch already exists."),
    ] = False,
) -> None:
    """Create or reuse a worktree for the repo at PATH.

    When the branch (named via --branch, or the prompted-for description)
    already exists, locally or on the remote, asks before switching to its
    worktree instead (skip with --yes/-y): 'new' implies a fresh branch, so
    an existing one, most likely a typo'd --branch meant to name a new
    one, is the surprising case worth a check. The prompt is one keypress:
    `y` switches, `n`/`esc`/enter cancel (the default is no). No prompt
    when it doesn't exist yet anywhere, creating it is exactly what 'new'
    is for.

    Examples:
        coppice new ./tardis
        coppice new . --branch fix-thing --base develop
    """
    try:
        repo_root = repo.resolve_repo_root(path)
    except repo.RepoResolutionError as exc:
        raise _fail(str(exc)) from exc

    try:
        wt.require_wt()
    except wt.WtNotFoundError as exc:
        raise _fail(str(exc)) from exc

    console.print()

    if branch is None:
        _print_existing_worktrees(repo_root)
        description = typer.prompt(
            "Short branch description (optional, enter for a timestamp id)",
            default="",
            show_default=False,
        )
        if description.strip():
            branch = branch_mod.normalize_branch(description)
        else:
            branch = branch_mod.timestamp_branch()
            console.print(f"No description entered, using timestamp branch: [bold]{branch}[/]")

    console.print(f"Worktree branch: [bold]{branch}[/]")

    create = not (wt.branch_exists(repo_root, branch) or wt.remote_branch_exists(repo_root, branch))
    if not create and not yes:
        console.print()
        if not confirm.ask(f"Switch to the '{branch}' worktree? The branch already exists."):
            console.print()
            console.print("Cancelled.")
            console.print()
            raise typer.Exit(1)

    # Resolve the actual base ourselves rather than leaving it to `wt`'s own
    # (cached, and so potentially stale, see repo.default_branch) default-
    # branch detection, but only when the caller didn't already pick one
    # via --base, and only when we're actually forking a new branch, `base`
    # is meaningless (and wt warns + ignores it) when switching to one that
    # already exists.
    if create and base is None:
        base = repo.default_branch(repo_root)
    elif create and base is not None:
        _freshen_explicit_base(repo_root, base)

    try:
        result = _switch_with_recovery(repo_root, branch, create=create, base=base)
    except wt.WtNotFoundError as exc:
        raise _fail(str(exc)) from exc
    except wt.WtCommandError as exc:
        # A streamed failure already put `wt`'s own message on screen live;
        # piling the short "exited N" form on top of it adds nothing.
        if exc.streamed:
            raise typer.Exit(1) from exc
        raise _fail(str(exc)) from exc

    repo.register_repo(repo_root)

    created = result.get("action") == "created"
    verb = "Created" if created else "Reused"
    result_path = result.get("path")
    base_branch = result.get("base_branch")

    # Echo back what it was actually forked from whenever we created one
    # (i.e. `base_branch` is present at all): silently trusting that a new
    # branch forked from the right place is exactly the assumption that
    # broke when `wt`'s cached default-branch detection went stale, this
    # makes the actual base visible at a glance instead of requiring a dig
    # through `git log` after the fact to notice it forked from the wrong
    # place.
    console.print()
    from_suffix = f" from [bold]{base_branch}[/]" if base_branch else ""
    if result_path:
        console.print(
            f"{verb} worktree for [bold]{branch}[/]{from_suffix} @ [green]{_short_path(Path(result_path))}[/]"
        )
    else:
        console.print(f"{verb} worktree for [bold]{branch}[/]{from_suffix}")
    console.print()

    if result_path:
        shell.write_cd_file(Path(result_path))


def _freshen_explicit_base(repo_root: Path, base: str) -> None:
    """Best-effort fetch of an explicit --base from its remote, so the fork
    starts from BASE's current tip rather than a stale remote-tracking ref.

    `wt switch --create --base X` forks from X's *remote-tracking* ref as it
    stands locally right now; wt never fetches it first. The worktrunk
    pre-switch hook only freshens the *default* branch, the fork base is
    not exposed to pre-switch hooks (there, `base` means the worktree being
    switched *from*), so a non-default --base would silently fork from
    wherever origin/X last was. The default branch stays the hook's job
    (fetching it here too would double the network round trip on every
    `new`); only an explicit --base lands here.

    Best-effort like the hook's `|| true`: a flaky/offline fetch, or a BASE
    naming a ref its remote doesn't have (a local-only branch, a SHA, one
    of `wt`'s own shortcuts), must never block creating the worktree,
    forking from the local refs as they stand is the same behavior `new`
    had before this fetch existed. A `remote/branch` BASE that origin
    rejects gets one retry against its own remote (`origin/develop` fetches
    `develop` from `origin`).
    """
    try:
        git.fetch_base(repo_root, base)
        return
    except git.GitError:
        pass
    remote, sep, branch = base.partition("/")
    if sep and remote and branch:
        with suppress(git.GitError):
            git.fetch_base(repo_root, branch, remote=remote)


def _switch_with_recovery(repo_root: Path, branch: str, *, create: bool, base: str | None) -> dict[str, Any]:
    """`wt.switch` plus one recovery: when the switch fails because BRANCH's
    worktree path is occupied by a worktree sitting on a different branch,
    offer to switch that worktree onto BRANCH in place (`wt`'s own suggested
    remedy), then retry the switch so the normal flow (hooks, registration,
    success output) continues unchanged.
    """
    try:
        return wt.switch(repo_root, branch, create=create, base=base)
    except wt.WtCommandError as exc:
        if not _recover_occupied_path(repo_root, branch, exc, create=create, base=base):
            raise
        # What the retry needs depends on the remedy: an in-place switch
        # made BRANCH exist either way (`wt` refuses --create for an
        # existing branch), while a relocate freed the path but created
        # nothing, so a brand-new branch still needs --create.
        retry_create = create and not wt.branch_exists(repo_root, branch)
        return wt.switch(repo_root, branch, create=retry_create, base=base if retry_create else None)


def _recover_occupied_path(
    repo_root: Path, branch: str, exc: wt.WtCommandError, *, create: bool, base: str | None
) -> bool:
    """Offer the remedy for the occupied-path `wt switch` failure: the
    directory `wt` wants for BRANCH already hosts a worktree checked out on
    a DIFFERENT branch (a worktree created for BRANCH earlier, later `git
    switch`ed away by hand, which `wt list` reports as a
    branch_worktree_mismatch and `wt switch` refuses to touch).

    Two remedies, offered as one prompt each:

    - When the occupier is itself at the wrong path (a
      branch_worktree_mismatch), relocate it to its own expected path
      (`wt step relocate`): both worktrees survive, and BRANCH's path is
      freed for the retried switch to create fresh.
    - When the occupier is at its rightful path, the two branches
      genuinely share one templated path (a sanitize collision), so the
      only way through is switching the directory onto BRANCH in place
      (`git switch`, `wt`'s own suggested remedy), evicting the occupier.

    Returns True when the user accepted and the remedy succeeded, so the
    caller should retry the `wt switch`. Returns False when EXC is any
    other failure, the caller's normal error handling applies. A declined
    prompt cancels the whole command, like `new`'s existing-branch prompt
    does.
    """
    # Strip SGR styling first: wt colors its message when the terminal is
    # visible (the streaming path forces that with CLICOLOR_FORCE), and the
    # codes land mid-sentence ('run \x1b[4mcd <path> && git switch ...').
    stderr = re.sub(r"\x1b\[[0-9;]*m", "", exc.stderr or "")
    if "worktree at the expected path" not in stderr:
        return False
    # The absolute path comes from `wt`'s own remedy line ('... run cd
    # <path> && git switch <branch>'); the first line's copy is ~-shortened.
    match = re.search(rf"run cd (.+?) && git switch {re.escape(branch)}(?:\s|$)", stderr, re.M)
    if match is None:
        return False
    path = Path(match.group(1))
    # Cross-check the message against `wt`'s structured listing rather than
    # trusting the parse alone: the path must be a registered worktree of
    # this repo, sitting on some branch other than BRANCH.
    occupying = next(
        (
            w
            for w in wt.list_worktrees(repo_root)
            if w.get("path") and os.path.realpath(w["path"]) == os.path.realpath(path)
        ),
        None,
    )
    if occupying is None:
        return False
    current = occupying.get("branch")
    if not current or current == branch:
        return False
    if _is_mismatch(occupying):
        targets = wt.relocate_preview(repo_root, current)
        if targets:
            return _offer_relocate(repo_root, branch, current, targets)
        # `wt` can't relocate it (locked, detached, a blocked target):
        # fall through to the in-place switch offer.
    dirty_note = " Its working tree is dirty." if _is_dirty(occupying) else ""
    console.print()
    if not confirm.ask(f"Switch the worktree at {_short_path(path)} from '{current}' to '{branch}'?{dirty_note}"):
        console.print()
        console.print("Cancelled.")
        console.print()
        raise typer.Exit(1)
    try:
        report = git.switch_in_place(path, branch, create=create, base=base)
    except git.GitError as git_exc:
        raise _fail(str(git_exc)) from git_exc
    if report:
        console.print(report)
    return True


def _offer_relocate(repo_root: Path, branch: str, current: str, targets: list[dict[str, str]]) -> bool:
    """The relocate remedy for `new`'s occupied-path failure: move the
    occupier (CURRENT) to its own expected path (the moves TARGETS, from
    `wt.relocate_preview`, say it makes), freeing BRANCH's path for the
    retried `wt switch`. Always True on success; a declined prompt cancels
    the command instead of returning.
    """
    console.print()
    if not confirm.ask(
        f"Relocate '{current}' to {_short_path(Path(targets[0]['to']))}? That frees the path for '{branch}'."
    ):
        console.print()
        console.print("Cancelled.")
        console.print()
        raise typer.Exit(1)
    try:
        wt.relocate(repo_root, current, targets)
    except wt.WtCommandError as exc:
        if exc.streamed:
            raise typer.Exit(1) from exc
        raise _fail(str(exc)) from exc
    except OSError as exc:
        raise _fail(str(exc)) from exc
    return True


def _creation_ts(path: Path) -> float | None:
    """Best-effort filesystem birth time, for a "how old is this worktree"
    signal. Not available on every OS/filesystem (notably most Linux setups),
    in which case the caller falls back to the branch's last-commit time.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    ts = getattr(st, "st_birthtime", None)
    return float(ts) if ts else None


def _age_seconds(entry: dict[str, Any]) -> float | None:
    """Seconds since ENTRY's worktree was created: filesystem birth time
    where available, falling back to the branch's last-commit time (most
    Linux filesystems, or the main worktree). None if neither is known, or
    the worktree is `prunable` (its directory is already gone, so "age" is
    meaningless, it's always a removal candidate regardless).
    """
    if _is_stale(entry):
        return None
    if not entry.get("is_main") and (creation_ts := _creation_ts(Path(entry["path"]))) is not None:
        return time.time() - creation_ts
    ts = entry.get("commit", {}).get("timestamp") or 0
    if not ts:
        return None
    return time.time() - ts


def _is_stale(entry: dict[str, Any]) -> bool:
    """Whether ENTRY is a prunable/stale worktree reference: its directory
    is already gone (removed by hand, an OS temp dir that got reaped, a
    `git worktree remove` run outside `wt`, etc.), so its age, size,
    working-tree cleanliness, and merge status are all moot, `wt` still
    carries a dangling registration for it and it's always a
    `clean`/`remove` candidate regardless of every other check.

    Centralizes the single `worktree.state == "prunable"` check every one of
    those call sites used to repeat inline, so "stale" means exactly one
    thing everywhere it's asked about: here, `clean`'s scan, and `list`'s
    red-flagged row.
    """
    return entry.get("worktree", {}).get("state") == "prunable"


def _is_mismatch(entry: dict[str, Any]) -> bool:
    """Whether ENTRY's worktree sits at a path that doesn't match its
    branch (`wt`'s `worktree.state == "branch_worktree_mismatch"`): the
    directory was created for a different branch and later `git switch`ed
    by hand, so listings key the row by its branch while its path names
    another, and `wt switch` to that other branch refuses the occupied
    path. The remedy is `wt step relocate` (see `new`'s occupied-path
    recovery).
    """
    return entry.get("worktree", {}).get("state") == "branch_worktree_mismatch"


def _humanize_age(seconds: float) -> str:
    """Compact age label: '42m', '8h', '2d', '3w', '6mo'. Reads better than
    '0d' for a worktree created this morning, and stays short enough for a
    right-aligned table column.
    """
    if seconds < 3600:
        return f"{max(1, int(seconds / 60))}m"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h"
    if seconds < 7 * 86400:
        return f"{int(seconds / 86400)}d"
    if seconds < 30 * 86400:
        return f"{int(seconds / (7 * 86400))}w"
    return f"{int(seconds / (30 * 86400))}mo"


def _age_label(entry: dict[str, Any]) -> str:
    """Plain-text age label: 'stale' for a dangling reference, '?' when
    unknown, else `_humanize_age`'s compact label. Deliberately markup-free,
    its other caller (`_pick_branches_interactively`'s fzf input) renders
    this as literal text, not through Rich, so `[red]stale[/]` there would
    show up as the literal tag instead of a color. `_age_cell` below wraps
    this for Rich tables, where markup does render.
    """
    if _is_stale(entry):
        return "stale"
    seconds = _age_seconds(entry)
    return _humanize_age(seconds) if seconds is not None else "?"


def _age_cell(entry: dict[str, Any]) -> str:
    """Rich-markup Created cell for worktree tables: `_age_label`'s label, with
    a stale entry's "stale" wrapped in red so a dangling reference actually
    stands out at a glance in a table full of otherwise-similar age values,
    instead of reading like just another row.
    """
    label = _age_label(entry)
    return f"[{_STYLE_STALE}]{label}[/]" if _is_stale(entry) else label


def _is_dirty(entry: dict[str, Any]) -> bool:
    wtree = entry.get("working_tree", {})
    return any(wtree.get(k) for k in ("staged", "modified", "untracked", "deleted", "renamed"))


def _is_parked(entry: dict[str, Any]) -> bool:
    """Whether ENTRY's branch carries a parked mark (`cop park`), folded
    into the entry as `parked_at` by `_enrich_parked`."""
    return entry.get("parked_at") is not None


def _has_follow_up(entry: dict[str, Any]) -> bool:
    """Whether a parked ENTRY's branch head moved after the mark: follow-up
    already happened (review comments, QA, a hotfix), so the worktree reads
    as active again, no writes, no stale-mark cleanup job. Read-time only,
    and it works because `sync` skips parked trees, so a parked head only
    moves when someone actually works in it. An unknown head time (0) can't
    disprove the mark, so it stays parked."""
    parked_ts = entry.get("parked_at")
    if parked_ts is None:
        return False
    head_ts = entry.get("commit", {}).get("timestamp") or 0
    return bool(head_ts) and head_ts > parked_ts


def _effectively_parked(entry: dict[str, Any]) -> bool:
    """Whether ENTRY counts as parked right now: marked, with no follow-up
    since. Follow-up flips a parked worktree back to active at read time."""
    return _is_parked(entry) and not _has_follow_up(entry)


def _parked_age_label(entry: dict[str, Any]) -> str:
    """Compact 'how long has this been parked' label ('3d'), for `list`'s
    parked rows and `clean --parked`'s preview."""
    return _humanize_age(max(0.0, time.time() - entry["parked_at"]))


def _enrich_parked(
    worktrees_by_repo: dict[Path, list[dict[str, Any]]], parked_by_repo: dict[Path, dict[str, float]]
) -> None:
    """Fold each repo's parked marks into its `wt list` entries as a
    `parked_at` unix timestamp, so every renderer and check below reads one
    dict. The key also surfaces in `list --json` output, where consumers
    (e.g. understory) can compare it against `commit.timestamp` themselves."""
    for repo_root, worktrees in worktrees_by_repo.items():
        marks = parked_by_repo.get(repo_root, {})
        if not marks:
            continue
        for w in worktrees:
            if (branch := w.get("branch")) and branch in marks:
                w["parked_at"] = marks[branch]


def _short_path(path: Path, max_len: int = 48) -> str:
    """Shorten PATH for a table cell: collapse the home directory to '~',
    then middle-ellipsize anything still longer than MAX_LEN, keeping the
    tail (a repo's own name matters more than its parent directories).
    """
    s = str(path)
    home = str(Path.home())
    if s == home:
        s = "~"
    elif s.startswith(home + "/"):
        s = "~" + s[len(home) :]
    if len(s) <= max_len:
        return s
    return "\u2026" + s[-(max_len - 1) :]


def _repo_header(repo_root: Path, path: Path | None = None) -> str:
    """'[bold]name[/] [dim](short path)[/]' repo heading, the same shape
    everywhere `coppice` introduces a repo's worktrees: `list`, `new`'s
    pre-prompt preview, and `clean`'s per-repo scan results. PATH defaults
    to REPO_ROOT itself; pass e.g. a main worktree's own path when it
    differs (worktrees registered from a subdirectory, symlinks, etc.).
    """
    return f"[bold]{repo_root.name}[/] [dim]({_short_path(path or repo_root)})[/]"


def _worktree_size_kb(entry: dict[str, Any], size_cache: dict[Path, int] | None = None) -> int | None:
    """On-disk size of ENTRY's worktree in KB, or None if unknown: a
    prunable/stale entry's directory is already gone, and an entry with no
    'path' at all can't be sized.

    Looks ENTRY's path up in SIZE_CACHE when given (callers sizing more than
    one worktree should precompute it with `_sizeable_paths` +
    `sizes.dir_sizes_kb` so every worktree's directory is walked in
    parallel, rather than one at a time here). Falls back to a direct,
    single-path `dir_size_kb` call otherwise.
    """
    if _is_stale(entry):
        return None
    path = entry.get("path")
    if not path:
        return None
    if size_cache is not None:
        return size_cache.get(Path(path), 0)
    return sizes.dir_size_kb(Path(path))


def _sizeable_paths(worktrees: list[dict[str, Any]]) -> list[Path]:
    """Paths worth handing to `sizes.dir_sizes_kb`: every worktree in
    WORKTREES that isn't prunable and has a path, i.e. exactly the entries
    `_worktree_size_kb` would otherwise walk one at a time.
    """
    return [Path(path) for w in worktrees if not _is_stale(w) and (path := w.get("path"))]


# wt's documented `main_state` vocabulary (list JSON schema 1, which wt.py
# pins), bucketed by what coppice does with it. Six of the nine states used
# to land in one fallback bucket here, including the most actionable one
# (`would_conflict`, wt's `git merge-tree` simulation of the merge).
_MERGED_STATES = frozenset({"empty", "integrated", "same_commit", "behind"})
_UNMERGED_STATES = frozenset({"ahead", "diverged"})
_CONFLICT_STATES = frozenset({"would_conflict"})


def _classify_main_state(entry: dict[str, Any]) -> str:
    """Bucket ENTRY's `main_state` against main: "merged" (nothing to
    integrate, safe to remove), "unmerged" (has commits main doesn't,
    merges cleanly), "conflict" (has commits main doesn't, and merging
    would conflict), or "unknown" (wt genuinely can't relate the branch to
    main: `orphan`, `is_main`, absent, or a future value this version
    doesn't recognize).

    Single place that knows wt's vocabulary, so the `list` table
    (`_merge_status`), `clean`'s removal preview (`_merge_label`), and
    `clean --merged`'s removable set can't drift apart on what each state
    means, and the eventual schema 2 migration (the vocabulary moves to
    `display.state`) touches one function instead of three call sites.
    """
    main_state = entry.get("main_state")
    if main_state in _MERGED_STATES:
        return "merged"
    if main_state in _UNMERGED_STATES:
        return "unmerged"
    if main_state in _CONFLICT_STATES:
        return "conflict"
    return "unknown"


def _merge_status(entry: dict[str, Any]) -> tuple[str, str]:
    """(label, rich style) for ENTRY's merge status against main.

    Doesn't apply to the main worktree itself (nothing to merge it into) or
    a prunable/stale entry (its branch's relationship to main is moot once
    the worktree directory is already gone).
    """
    if entry.get("is_main") or _is_stale(entry):
        return "-", "dim"
    bucket = _classify_main_state(entry)
    if bucket == "merged":
        return "merged", _STYLE_MERGED
    if bucket == "unmerged":
        return "unmerged", _STYLE_UNMERGED
    if bucket == "conflict":
        return "conflict", _STYLE_CONFLICT
    return "unknown", "dim"


def _worktree_cells(
    w: dict[str, Any],
    *,
    show_size: bool,
    size_cache: dict[Path, int] | None,
    verbose: bool = False,
    indent: str = "",
) -> tuple[list[str], int | None]:
    """One table row for a worktree: branch ('current' conveyed by the
    bold-green style instead of its own column, one less repeated 'current'
    per row), age, optionally on-disk size and (VERBOSE) path, working-tree
    cleanliness, and merge status. Returns the cells plus the entry's size
    in KB (None when unknown or not requested), so callers can roll up
    totals without walking anything twice.

    A stale (dangling, `wt`-prunable) entry gets its own visual treatment
    instead of blending in: branch and age both render in red, and
    'Working tree' shows a plain '-' rather than computing dirty/clean
    against a `working_tree` dict that's empty because the directory is
    already gone (that would otherwise misreport it as 'clean', implying
    there's a harmless, tidy worktree sitting there rather than a dangling
    reference `clean`/`remove` should clear out).

    Shared by `list`'s sectioned table and `new`'s pre-prompt "here's
    what's already in flight" preview (via `_worktrees_table`), so a
    worktree looks the same wherever `coppice` shows one.
    """
    stale = _is_stale(w)
    parked = not stale and _effectively_parked(w)
    follow_up = not stale and _has_follow_up(w)
    branch = w.get("branch") or "?"
    if stale:
        branch_cell = f"[{_STYLE_STALE}]{indent}{branch}[/]"
    elif parked:
        # Parked rows render dimmed as a whole (below), so the current
        # worktree's bold-green highlight is suppressed too: the parked mark
        # is the stronger signal, one visual state per row.
        branch_cell = f"{indent}{branch}"
    elif w.get("is_current"):
        branch_cell = f"[{_STYLE_CURRENT}]{indent}{branch}[/]"
    else:
        branch_cell = f"{indent}{branch}"
    if not stale and _is_mismatch(w) and (path := w.get("path")):
        # The directory names a different branch than the one checked out
        # in it: name the path's branch segment, so a scan for that branch
        # (whose own worktree path this occupies) still hits this row.
        branch_cell += f" [dim]@[/] [{_STYLE_MISMATCH}]{Path(path).parent.name}/[/]"

    working_tree = "[dim]-[/]" if stale else (f"[{_STYLE_DIRTY}]dirty[/]" if _is_dirty(w) else "[dim]clean[/]")
    merge_label, merge_style = _merge_status(w)

    age_cell = _age_cell(w)
    if parked:
        # The age part dims with the rest of the row (below); the parked
        # label itself stays bright (_STYLE_PARKED), so the one signal a
        # parked row exists to show survives the dimming.
        age_cell = f"[dim]{_age_label(w)}[/] · [{_STYLE_PARKED}]parked {_parked_age_label(w)}[/]"
    elif follow_up:
        # The mark is stale (head moved since): the row renders active
        # again, with the note naming why a parked worktree isn't dimmed.
        age_cell = f"{_age_label(w)} · follow-up"

    size_kb = _worktree_size_kb(w, size_cache) if show_size else None
    cells = [branch_cell, age_cell]
    if show_size:
        cells.append(sizes.human_kb(size_kb) if size_kb is not None else "-")
    if verbose:
        path = w.get("path")
        cells.append(f"[dim]{_short_path(Path(path), max_len=40)}[/]" if path and not stale else "[dim]-[/]")
    cells += [working_tree, f"[{merge_style}]{merge_label}[/]"]
    if parked:
        # Every cell dims except the age one (index 1 by construction
        # above): it carries the bright parked label and dims only its
        # age part itself.
        cells = [age_cell if i == 1 else f"[dim]{cell}[/]" for i, cell in enumerate(cells)]
    return cells, size_kb


def _worktrees_table(
    worktrees: list[dict[str, Any]], *, show_size: bool = True, size_cache: dict[Path, int] | None = None
) -> tuple[Table, int, int]:
    """Rich table for WORKTREES, one `_worktree_cells` row each.

    Callers are expected to have already filtered out the main worktree
    (see `_render_list`/`_print_existing_worktrees`): it isn't a worktree
    coppice manages, it's the repo itself, so it never gets a row here,
    unlike every other entry which is something `remove`/`clean` could act
    on.

    Used by `new`'s pre-prompt preview (`list` renders its own sectioned
    table via `_render_list`). Returns the table, the summed on-disk size
    in KB (0 when SHOW_SIZE is False or every entry's size is unknown), and
    the count of stale entries, so callers can roll up totals without
    walking each worktree's directory or re-checking its state a second
    time.
    """
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", pad_edge=False, show_edge=False)
    table.add_column("Branch")
    table.add_column("Created", justify="right")
    if show_size:
        table.add_column("Size", justify="right")
    table.add_column("Working tree")
    table.add_column("Merge")

    total_kb = 0
    n_stale = 0
    for w in worktrees:
        n_stale += 1 if _is_stale(w) else 0
        cells, size_kb = _worktree_cells(w, show_size=show_size, size_cache=size_cache)
        total_kb += size_kb or 0
        table.add_row(*cells)

    return table, total_kb, n_stale


def _list_section_heading(repo_root: Path, main_entry: dict[str, Any] | None) -> str:
    """`list`'s repo section heading: bold repo name plus its main branch.
    Slimmer than `_repo_header` (which `new`/`clean` still use): at list's
    glance level the path is mostly the repo name's parent directories,
    noise next to the worktrees themselves. In verbose mode the path gets
    the row's Path cell instead (see `_render_list`), keeping the heading
    short enough to never wrap.

    The main worktree's branch is folded into the heading so a
    `master`/`trunk`/whatever a repo's default branch happens to be named
    is still visible at a glance, without implying it's a worktree like the
    others.
    """
    heading = f"[bold]{repo_root.name}[/]"
    if main_entry is not None and (main_branch := main_entry.get("branch")):
        heading += f" [dim](main: {main_branch})[/]"
    return heading


def _list_sort_key(w: dict[str, Any]) -> tuple[int, float]:
    """Worktree row order within a `list` repo section: stale (dangling)
    references first, they always need action, then unparked newest first
    (the worktree you're looking for is usually a recent one), unknown ages
    last, and parked worktrees after all of those, most recently parked
    first (a fresh mark is the likeliest to still see follow-up). A parked
    worktree with follow-up counts as active again and sorts unparked.
    """
    if _is_stale(w):
        return (0, 0.0)
    if _effectively_parked(w):
        return (2, max(0.0, time.time() - w["parked_at"]))
    seconds = _age_seconds(w)
    return (1, seconds if seconds is not None else float("inf"))


def _render_list(
    repos: list[Path],
    worktrees_by_repo: dict[Path, list[dict[str, Any]]],
    *,
    show_size: bool,
    size_cache: dict[Path, int] | None,
    show_all: bool,
    verbose: bool,
) -> tuple[int, int, int, int]:
    """Render `list`'s worktree listing. Returns (worktree count, summed
    on-disk size in KB, stale count, hidden-repo count) for the caller's
    closing summary.

    One table over every repo, sectioned by repo (bold heading row, a
    blank separator row between repos): columns align across repos and the
    header appears once, instead of a header + divider repeated per repo.
    Repos are sorted by name; worktrees within a repo by `_list_sort_key`.

    Repos with no extra worktrees are skipped by default, they're the
    common case and bury the repos that do have some; SHOW_ALL gives them a
    one-line dim section row instead. An explicit single-repo scope always
    shows, hiding the very repo the user asked about would be absurd.
    """
    show_empty = show_all or len(repos) == 1
    sections: list[tuple[Path, dict[str, Any] | None, list[dict[str, Any]]]] = []
    n_hidden = 0
    for repo_root in sorted(repos, key=lambda r: r.name.lower()):
        worktrees = worktrees_by_repo.get(repo_root, [])
        main_entry = next((w for w in worktrees if w.get("is_main")), None)
        others = [w for w in worktrees if not w.get("is_main")]
        if not others and not show_empty:
            n_hidden += 1
            continue
        sections.append((repo_root, main_entry, others))

    total = sum(len(others) for _, _, others in sections)
    if total == 0 and not show_empty:
        return 0, 0, 0, n_hidden

    total_kb = 0
    n_stale = 0

    def _one_liner_note(main_entry: dict[str, Any] | None) -> str:
        """Dim 'no extra worktrees' note for a repo, with the main
        checkout's own size/dirtiness folded in, the one bit of signal an
        otherwise-empty repo still has (uncommitted work in the main
        checkout)."""
        nonlocal total_kb
        bits: list[str] = []
        if main_entry is not None:
            if show_size and (size_kb := _worktree_size_kb(main_entry, size_cache)):
                total_kb += size_kb
                bits.append(f"{sizes.human_kb(size_kb)} on disk")
            bits.append("dirty" if _is_dirty(main_entry) else "clean")
        note = " \u00b7 ".join(bits)
        suffix = f" \u00b7 {note}" if note else ""
        return f"[dim]\u00b7 no extra worktrees{suffix}[/]"

    if total == 0:
        # Nothing but empty repos, and they're shown: plain one-liners read
        # better than a table with no worktree rows. No blank lines here,
        # the caller owns the outer spacing.
        for repo_root, main_entry, _ in sections:
            console.print(f"{_list_section_heading(repo_root, main_entry)} {_one_liner_note(main_entry)}")
        return 0, total_kb, 0, n_hidden

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", pad_edge=False, show_edge=False)
    table.add_column("Branch")
    table.add_column("Created", justify="right")
    if show_size:
        table.add_column("Size", justify="right")
    if verbose:
        table.add_column("Path")
    table.add_column("Working tree")
    table.add_column("Merge")

    def _section_row(repo_root: Path, main_entry: dict[str, Any] | None, *, empty: bool) -> list[str]:
        """A repo's heading row (or its one-liner row, when EMPTY): name +
        main branch in the Branch column, the repo's own path in the Path
        column in verbose mode, everything else blank."""
        heading = _list_section_heading(repo_root, main_entry)
        if empty:
            heading = f"{heading} {_one_liner_note(main_entry)}"
        cells = [heading, ""]
        if show_size:
            cells.append("")
        if verbose:
            path = Path(main_entry["path"]) if main_entry is not None and main_entry.get("path") else repo_root
            cells.append(f"[dim]{_short_path(path, max_len=40)}[/]")
        cells += ["", ""]
        return cells

    for i, (repo_root, main_entry, others) in enumerate(sections):
        last_section = i == len(sections) - 1
        # Section breaks separate a repo with worktrees from its neighbors;
        # consecutive one-liner repos stay packed together, a blank row per
        # empty repo would double the vertical space for no signal.
        next_has_worktrees = not last_section and bool(sections[i + 1][2])
        if not others:
            table.add_row(*_section_row(repo_root, main_entry, empty=True), end_section=next_has_worktrees)
            continue
        table.add_row(*_section_row(repo_root, main_entry, empty=False))
        ordered = sorted(others, key=_list_sort_key)
        for j, w in enumerate(ordered):
            n_stale += 1 if _is_stale(w) else 0
            cells, size_kb = _worktree_cells(
                w, show_size=show_size, size_cache=size_cache, verbose=verbose, indent="  "
            )
            total_kb += size_kb or 0
            table.add_row(*cells, end_section=not last_section and j == len(ordered) - 1)
    console.print(table)
    return total, total_kb, n_stale, n_hidden


@app.command("list", rich_help_panel="Inspect")
def cmd_list(
    path: Annotated[
        str | None,
        typer.Argument(help="Only list this repo. Omit to list every known repo plus the one you're standing in."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit raw JSON, tagged per item with a 'repo' field.")
    ] = False,
    show_size: Annotated[
        bool,
        typer.Option(
            "--size/--no-size",
            help="Show each worktree's on-disk size (walks its directory; disable for a faster listing).",
        ),
    ] = True,
    show_all: Annotated[
        bool,
        typer.Option(
            "--all",
            "-a",
            help="Also list repos with no extra worktrees (hidden by default, rolled up into the closing line).",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Add a Path column with each worktree's (and repo's) on-disk location.",
        ),
    ] = False,
) -> None:
    """List worktrees across every known repo, or just PATH.

    Repos with no extra worktrees are hidden by default, they're the common
    case and bury the repos that do have some. --all/-a shows them.
    """
    try:
        repos = repo.scope_repos(path)
    except repo.RepoResolutionError as exc:
        raise _fail(str(exc)) from exc

    if not repos:
        raise _fail("no known repos. Run 'coppice new' at least once, or pass a PATH.")

    try:
        wt.require_wt()
    except wt.WtNotFoundError as exc:
        raise _fail(str(exc)) from exc

    if as_json:
        import json

        worktrees_by_repo = wt.list_worktrees_many(repos)
        _enrich_parked(worktrees_by_repo, park_mod.parked_at_many(repos))
        merged: list[dict[str, Any]] = []
        for repo_root in repos:
            for entry in worktrees_by_repo[repo_root]:
                merged.append({**entry, "repo": repo_root.name})
        # Plain print, not console.print: Rich soft-wraps at console width
        # (80 columns when stdout is a pipe), inserting literal newlines
        # inside JSON strings, 'cop list --json | jq' used to get invalid
        # JSON out of that.
        print(json.dumps(merged))
        return

    # Fetch every repo's worktrees concurrently (one `wt` subprocess per
    # repo, overlapped rather than run one after another), then size them
    # all in one batch (with progress on the spinner) so that walk happens
    # in parallel across every worktree in every repo instead of one at a
    # time.
    console.print()
    with console.status("[dim]Listing worktrees…[/dim]") as spinner:
        spinner.update(f"[dim]Listing worktrees for {_plural(len(repos), 'repo')}…[/dim]")
        worktrees_by_repo: dict[Path, list[dict[str, Any]]] = wt.list_worktrees_many(repos)
        _enrich_parked(worktrees_by_repo, park_mod.parked_at_many(repos))

        size_cache: dict[Path, int] | None = None
        if show_size:
            all_paths = [p for worktrees in worktrees_by_repo.values() for p in _sizeable_paths(worktrees)]
            if all_paths:

                def _report_progress(done: int, total: int) -> None:
                    spinner.update(f"[dim]Sizing worktrees ({done}/{total})…[/dim]")

                size_cache = sizes.dir_sizes_kb(all_paths, on_progress=_report_progress)
            else:
                size_cache = {}

        spinner.stop()

    total, total_kb, total_stale, n_hidden = _render_list(
        repos,
        worktrees_by_repo,
        show_size=show_size,
        size_cache=size_cache,
        show_all=show_all,
        verbose=verbose,
    )

    if total == 0:
        if n_hidden:
            # Everything in scope is a repo with no extra worktrees, and
            # the default view hides those, so say so instead of printing
            # an empty table.
            console.print(f"No worktrees across {_plural(len(repos), 'repo')}. Create one: [cyan]cop new PATH[/]")
        console.print()
        return

    # State rollup across every listed worktree, so the closing line reads
    # as an actionable summary ('3 merged' nudges towards 'cop clean
    # --merged') rather than just a count.
    n_merged = n_dirty = n_conflict = 0
    n_repos_with = 0
    for repo_root in repos:
        others = [w for w in worktrees_by_repo[repo_root] if not w.get("is_main")]
        n_repos_with += 1 if others else 0
        for w in others:
            if _is_stale(w):
                continue
            n_dirty += 1 if _is_dirty(w) else 0
            bucket = _classify_main_state(w)
            n_merged += 1 if bucket == "merged" else 0
            n_conflict += 1 if bucket == "conflict" else 0

    console.print()
    summary = f"{_plural(total, 'worktree')} in {_plural(n_repos_with, 'repo')}"
    if show_size and total_kb:
        summary += f" \u00b7 {sizes.human_kb(total_kb)} on disk"
    if n_merged:
        summary += f" \u00b7 [{_STYLE_MERGED}]{n_merged} merged[/] [dim](cop clean --merged)[/]"
    if n_dirty:
        summary += f" \u00b7 [{_STYLE_DIRTY}]{n_dirty} dirty[/]"
    if n_conflict:
        summary += f" \u00b7 [{_STYLE_CONFLICT}]{_plural(n_conflict, 'conflict')}[/]"
    console.print(summary + ".")
    if total_stale:
        console.print(f"[red]{total_stale} stale (dangling) reference(s)[/], run 'cop clean' to remove.")
    if n_hidden:
        console.print(f"[dim]{_plural(n_hidden, 'more repo')} with no extra worktrees (show with: cop list --all)[/]")
    console.print()


def _open_vscode_windows(worktrees: dict[tuple[Path, str], Path]) -> set[tuple[Path, str]]:
    """Which WORKTREES ((repo_root, branch) -> worktree path) currently have
    a VS Code window open on their worktree, for a confirmation prompt's
    warning.

    One read of the window registry for the whole batch, matched by
    exact folder path. Best-effort: an empty mapping short-circuits,
    and an unreadable registry (see vscode.registry_window_folders)
    answers empty, so the prompt stays silent rather than ever claiming
    "not open".
    """
    if not worktrees:
        return set()
    window_folders = vscode.registry_window_folders()
    if not window_folders:
        return set()
    return {
        key
        for key, path in worktrees.items()
        if any(vscode.registry_matches_worktree(folders, path) for folders in window_folders)
    }


# Marker appended to a confirmation listing's line when that worktree has
# a VS Code window open on it, plus the note that follows such a listing.
# Wording shared by `remove` and `clean`.
_VSCODE_OPEN_MARKER = "  [yellow](VS Code window open)[/]"
_VSCODE_OPEN_NOTE = (
    "Close the marked windows first: removing a worktree out from under a "
    "VS Code window that has it open strands the window on a deleted folder."
)


def _picker_note(w: dict[str, Any]) -> str:
    """Default per-candidate detail for `_pick_branches_interactively`:
    age plus dirtiness. Markup-free, fzf renders it as literal text."""
    return f"{_age_label(w)}{', dirty' if _is_dirty(w) else ''}"


def _pick_branches_interactively(
    scope: list[Path],
    candidates_by_repo: dict[Path, list[dict[str, Any]]],
    *,
    verb: str = "Remove",
    adjective: str = "removable",
    note: Callable[[dict[str, Any]], str] | None = None,
) -> list[str] | None:
    """fzf multi-select picker over every candidate worktree in SCOPE, for
    the branch-taking commands (`remove`, `park`, `unpark`) when no BRANCH
    is given and there's no current worktree to default to. VERB and
    ADJECTIVE adapt the chrome ('Remove worktrees> ', 'no removable
    worktrees in scope') to the calling command; NOTE builds the
    parenthesized per-candidate detail (default: age plus dirtiness).

    Falls back to printing the candidates and asking for an explicit re-run
    when `fzf` isn't installed, rather than a pure-Python picker, to avoid a
    new dependency for something already optional.

    Returns None (caller exits 1) when there's nothing to pick, `fzf` isn't
    installed, or the user cancels the picker.
    """
    if note is None:
        note = _picker_note
    candidates = [(repo_root, w) for repo_root in scope for w in candidates_by_repo[repo_root]]
    if not candidates:
        err.print(f"[red]Error:[/] no {adjective} worktrees in scope.")
        return None

    if shutil.which("fzf") is None:
        err.print("No BRANCH given and fzf isn't installed. Candidates in scope:")
        for repo_root, w in candidates:
            err.print(f"  {w['branch']}  @ {repo_root.name}  ({note(w)})")
        err.print(f"Re-run: coppice {verb.lower()} BRANCH [--repo PATH]")
        return None

    # Prefix each line with its candidate index so the pick can be mapped
    # back precisely even if two entries render an identical label (e.g.
    # the same branch name in two different repos in scope); --with-nth
    # hides that column from what fzf actually displays.
    lines = [f"{i}\t{w['branch']}  @ {repo_root.name}  ({note(w)})" for i, (repo_root, w) in enumerate(candidates)]
    proc = subprocess.run(
        ["fzf", f"--prompt={verb} worktrees> ", "--height=~50%", "--multi", "--delimiter=\t", "--with-nth=2.."],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        console.print("Cancelled.")
        console.print()
        return None

    picked = [int(line.split("\t", 1)[0]) for line in proc.stdout.splitlines() if line.strip()]
    return [candidates[i][1]["branch"] for i in picked]


@app.command("remove", rich_help_panel="Remove (destructive)")
def cmd_remove(
    branches: Annotated[
        list[str] | None,
        typer.Argument(help="Branch name(s) to remove. Omit for an interactive picker (needs fzf)."),
    ] = None,
    repo_path: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-C",
            help="Scope to this repo. Defaults to the registry plus the repo you're standing in.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    force: Annotated[bool, typer.Option("--force", "-f", help="Remove even with uncommitted changes.")] = False,
    force_delete: Annotated[
        bool, typer.Option("--force-delete", "-D", help="Also delete the branch if unmerged.")
    ] = False,
) -> None:
    """Remove one or more worktrees by branch name.

    PATH is `--repo/-C` here (not a bare positional like `new`/`list`), since
    a bare positional would be ambiguous with the BRANCH list. Omit BRANCH
    entirely for an fzf multi-select picker scoped the same way.

    Asks for confirmation before removing anything, unless --yes/-y is
    passed. This prompt is coppice's own, not `wt remove`'s: `wt` is always
    invoked with `-y`, so it never asks anything itself, and confirmation
    stays in one place, asked once up front for the whole batch. During
    each removal `wt`'s stderr streams live to the terminal (progress,
    hook output); only its stdout stays captured.
    """
    try:
        scope = repo.scope_repos(repo_path)
    except repo.RepoResolutionError as exc:
        raise _fail(str(exc)) from exc

    if not scope:
        raise _fail("no known repos. Run 'coppice new' at least once, or pass --repo.")

    try:
        wt.require_wt()
    except wt.WtNotFoundError as exc:
        raise _fail(str(exc)) from exc

    # Removable worktrees per repo in scope: non-main, non-current (removing
    # the worktree you're standing in would have to switch away first, same
    # rule `wt` itself enforces). Fetched concurrently across repos, one
    # `wt` subprocess per repo run overlapped rather than one after another.
    worktrees_by_repo = wt.list_worktrees_many(scope)
    removable: dict[Path, list[dict[str, Any]]] = {
        repo_root: [
            w
            for w in worktrees_by_repo[repo_root]
            if not w.get("is_main") and not w.get("is_current") and w.get("branch")
        ]
        for repo_root in scope
    }

    if not branches:
        branches = _pick_branches_interactively(scope, removable)
        if not branches:
            raise typer.Exit(1)

    failures: list[str] = []
    targets: list[tuple[Path, str]] = []
    for branch_name in branches:
        matches = [r for r in scope if any(w["branch"] == branch_name for w in removable[r])]
        if not matches:
            err.print(f"[red]Error:[/] no worktree for branch '{branch_name}' found in scope.")
            failures.append(branch_name)
            continue
        if len(matches) > 1:
            err.print(f"[red]Error:[/] branch '{branch_name}' exists in multiple repos, disambiguate with --repo:")
            for m in matches:
                err.print(f"  {_short_path(m)}")
            failures.append(branch_name)
            continue

        targets.append((matches[0], branch_name))

    if not targets:
        err.print(f"[red]Removed 0 worktrees, {len(failures)} failed:[/]")
        for f in failures:
            err.print(f"  - {f}")
        raise typer.Exit(1)

    console.print()
    console.print(f"About to remove {_plural(len(targets), 'worktree')}:")
    open_windows = _open_vscode_windows(
        {
            (repo_root, branch_name): Path(w["path"])
            for repo_root, branch_name in targets
            for w in removable[repo_root]
            if w["branch"] == branch_name and w.get("path")
        }
    )
    for target, branch_name in targets:
        marker = _VSCODE_OPEN_MARKER if (target, branch_name) in open_windows else ""
        console.print(f"  {branch_name} @ {target.name}{marker}")
    if open_windows:
        console.print(f"[yellow]{_VSCODE_OPEN_NOTE}[/]")

    if not yes:
        console.print()
        # The consequence sentence names whatever the flags in play make
        # unrecoverable: branch deletion with -D, uncommitted work with -f.
        if force_delete:
            consequence = "Unmerged branches are deleted too."
        elif force:
            consequence = "Uncommitted changes are lost."
        else:
            consequence = "Branches survive unless already merged."
        if not confirm.ask(
            f"Remove the {_plural(len(targets), 'worktree')} listed above? {consequence}",
            tier="force" if force or force_delete else "destructive",
        ):
            console.print()
            console.print("Cancelled.")
            console.print()
            raise typer.Exit(1)

    console.print()
    n_removed = 0
    for target, branch_name in targets:
        console.print(f"Removing '{branch_name}' @ {target.name}...")
        try:
            wt.remove(target, branch_name, yes=True, force=force, force_delete=force_delete)
        except (wt.WtNotFoundError, wt.WtCommandError) as exc:
            err.print(f"[red]Error:[/] {exc}")
            failures.append(branch_name)
        else:
            n_removed += 1
            _unpark_if_branch_gone(target, branch_name)

    if failures:
        console.print()
        err.print(f"[red]Removed {_plural(n_removed, 'worktree')}, {len(failures)} failed:[/]")
        for f in failures:
            err.print(f"  - {f}")
        raise typer.Exit(1)

    console.print()
    console.print(f"Removed {_plural(n_removed, 'worktree')}.")
    console.print()


def _unpark_if_branch_gone(repo_root: Path, branch_name: str) -> None:
    """Drop BRANCH_NAME's parked mark when its branch went with the removed
    worktree. `wt remove` deletes the ref directly (`update-ref -d`), which,
    unlike `git branch -D`, leaves the `branch.<name>` config section
    behind, so the 'the mark dies with the branch' invariant needs this
    nudge. A kept branch keeps its mark: recreating its worktree later
    revives the parked state, which is exactly what the mark means."""
    if not wt.branch_exists(repo_root, branch_name):
        park_mod.unpark(repo_root, branch_name)


def _parkable(worktrees_by_repo: dict[Path, list[dict[str, Any]]]) -> dict[Path, list[dict[str, Any]]]:
    """Entries a parked mark makes sense for: non-main (the main checkout is
    the repo itself, never swept), on a branch (the mark is keyed by
    branch), and not a stale/dangling reference (its directory is already
    gone, there's nothing left to keep for follow-up)."""
    return {
        repo_root: [w for w in worktrees if not w.get("is_main") and not _is_stale(w) and w.get("branch")]
        for repo_root, worktrees in worktrees_by_repo.items()
    }


def _standing_in(parkable: dict[Path, list[dict[str, Any]]]) -> tuple[Path, dict[str, Any]] | None:
    """The (repo, entry) whose worktree directory the process is running in,
    or None. Detected by path (`git rev-parse --show-toplevel` resolves to
    the worktree's own root, from any subdirectory of it), not `wt list`'s
    `is_current`: `wt` is always invoked with `-C repo_root`, so from its
    cwd the main worktree is the current one, not the one you stand in."""
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    here = os.path.realpath(proc.stdout.strip())
    for repo_root, entries in parkable.items():
        for w in entries:
            if w.get("path") and os.path.realpath(w["path"]) == here:
                return repo_root, w
    return None


def _park_unpark(
    branches: list[str] | None,
    repo_path: str | None,
    yes: bool,
    *,
    unpark: bool,
) -> None:
    """Shared implementation of `park`/`unpark`: resolve the target
    worktrees (the current one when bare inside a worktree, an fzf picker
    when bare outside one, explicit BRANCHes otherwise), then set/delete
    each branch's `parked-at` config key. Parking a dirty worktree asks
    first (dirty and complete contradict each other) unless --yes."""
    verb = "Unpark" if unpark else "Park"
    try:
        scope = repo.scope_repos(repo_path)
    except repo.RepoResolutionError as exc:
        raise _fail(str(exc)) from exc

    if not scope:
        raise _fail("no known repos. Run 'coppice new' at least once, or pass --repo.")

    try:
        wt.require_wt()
    except wt.WtNotFoundError as exc:
        raise _fail(str(exc)) from exc

    worktrees_by_repo = wt.list_worktrees_many(scope)
    _enrich_parked(worktrees_by_repo, park_mod.parked_at_many(scope))
    parkable = _parkable(worktrees_by_repo)

    targets: list[tuple[Path, dict[str, Any]]] = []
    failures: list[str] = []
    if not branches:
        current = _standing_in(parkable)
        # Bare inside a worktree: that worktree is the target (the common
        # case, the task just finished and you're standing in it). For
        # `park` even an already-parked one (re-parking refreshes the
        # mark); for `unpark` only a parked one. Anything else (main
        # checkout, no repo) falls to the picker, same as `remove`.
        if current is not None and (not unpark or _is_parked(current[1])):
            targets = [current]
        else:
            pickable = {r: [w for w in parkable[r] if _is_parked(w) == unpark] for r in scope}
            note = (lambda w: f"parked {_parked_age_label(w)}") if unpark else None
            branches = _pick_branches_interactively(
                scope, pickable, verb=verb, adjective="parked" if unpark else "parkable", note=note
            )
            if not branches:
                raise typer.Exit(1)

    if not targets:
        # `branches` is non-None here: bare invocation either resolved the
        # current worktree above or went through the picker (which exits
        # rather than returning nothing).
        for branch_name in branches or []:
            matches = [(r, w) for r in scope for w in parkable[r] if w["branch"] == branch_name]
            if not matches:
                # Fall back to the worktree's directory name (the path's
                # basename, what `ls` of the worktrees dir shows): it's
                # often the name you actually see and type, especially
                # when it differs from a long branch name. Branch matches
                # always win on a collision, the mark is keyed by branch,
                # so the branch reading is the canonical one.
                matches = [
                    (r, w) for r in scope for w in parkable[r] if w.get("path") and Path(w["path"]).name == branch_name
                ]
            if not matches:
                err.print(
                    f"[red]Error:[/] no parkable worktree for branch or directory '{branch_name}' found in scope."
                )
                failures.append(branch_name)
                continue
            if len(matches) > 1:
                err.print(f"[red]Error:[/] branch '{branch_name}' exists in multiple repos, disambiguate with --repo:")
                for r, _ in matches:
                    err.print(f"  {_short_path(r)}")
                failures.append(branch_name)
                continue
            targets.append(matches[0])

    console.print()
    if unpark:
        # Unparking a worktree with no mark is a no-op note, not an error
        # (unlike a branch with no worktree at all, which failed above).
        still = []
        for r, w in targets:
            if _is_parked(w):
                still.append((r, w))
            else:
                console.print(f"[dim]'{w['branch']}' @ {r.name} is not parked.[/]")
        targets = still

    if not targets:
        if failures:
            raise typer.Exit(1)
        console.print()
        return

    if not unpark and not yes:
        dirty = [(r, w) for r, w in targets if _is_dirty(w)]
        if dirty:
            console.print(f"{_plural(len(dirty), 'Worktree')} with uncommitted changes:")
            for r, w in dirty:
                console.print(f"  {w['branch']} @ {r.name}")
            console.print()
            if not confirm.ask(
                f"Park the {_plural(len(dirty), 'worktree')} listed above? "
                "Parking marks a worktree task-complete, and dirty and complete contradict each other."
            ):
                console.print()
                console.print("Cancelled.")
                console.print()
                raise typer.Exit(1)
            console.print()

    for r, w in targets:
        branch_name = w["branch"]
        if unpark:
            age = _parked_age_label(w)
            park_mod.unpark(r, branch_name)
            console.print(f"Unparked '{branch_name}' @ {r.name} (was parked {age}).")
        else:
            was = w.get("parked_at")
            park_mod.park(r, branch_name)
            if was is not None:
                age = _humanize_age(max(0.0, time.time() - was))
                console.print(f"Re-parked '{branch_name}' @ {r.name} (was parked {age}).")
            else:
                console.print(f"Parked '{branch_name}' @ {r.name}.")

    if len(targets) > 1:
        console.print(f"{verb}ed {_plural(len(targets), 'worktree')}.")

    if failures:
        console.print()
        err.print(f"[red]{verb}ed {_plural(len(targets), 'worktree')}, {len(failures)} failed:[/]")
        for f in failures:
            err.print(f"  - {f}")
        raise typer.Exit(1)
    console.print()


@app.command("park", rich_help_panel="Update")
def cmd_park(
    branches: Annotated[
        list[str] | None,
        typer.Argument(
            help="Branch name(s) to park (a worktree's directory name works too). "
            "Omit inside a worktree to park it, or elsewhere for a picker."
        ),
    ] = None,
    repo_path: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-C",
            help="Scope to this repo. Defaults to the registry plus the repo you're standing in.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the prompt when a worktree is dirty.")] = False,
) -> None:
    """Mark worktrees parked: task complete, but kept on disk for follow-up
    (review comments, QA, a hotfix).

    The mark is a `branch.<branch>.parked-at` timestamp in the repo's git
    config: it dies with the branch (so `remove` cleans it up for free) and
    never dirties the worktree. Parked worktrees render dimmed in `list`,
    are skipped by `sync`, and `clean --parked` sweeps the ones parked a
    week or more ago. If the branch head moves after the mark, the worktree
    reads as active again (a 'follow-up' note in `list`), the mark simply
    means 'nothing new since I marked it'.

    Examples:
        coppice park                 # park the worktree you're standing in
        coppice park feat-a feat-b   # park by branch name (or worktree directory name)
        coppice unpark feat-a        # follow-up arrived, back to active
    """
    _park_unpark(branches, repo_path, yes, unpark=False)


@app.command("unpark", rich_help_panel="Update")
def cmd_unpark(
    branches: Annotated[
        list[str] | None,
        typer.Argument(
            help="Branch name(s) to unpark (a worktree's directory name works too). "
            "Omit inside a parked worktree, or elsewhere for a picker."
        ),
    ] = None,
    repo_path: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-C",
            help="Scope to this repo. Defaults to the registry plus the repo you're standing in.",
        ),
    ] = None,
) -> None:
    """Delete the parked mark, returning the worktree to active: `sync`
    merges into it again and `clean --parked` no longer sweeps it. Never
    prompts, unparking destroys nothing."""
    _park_unpark(branches, repo_path, True, unpark=True)


def _merge_label(entry: dict[str, Any], *, force_delete: bool) -> str:
    """Removal-preview label for ENTRY's merge status against main, printed
    next to each `clean` candidate. A merged branch gets deleted; every
    other bucket keeps the branch unless FORCE_DELETE, and a conflict
    spells out wt's `would_conflict` verdict so the preview reads as an
    invitation to merge or rebase, never to delete.
    """
    bucket = _classify_main_state(entry)
    if bucket == "merged":
        return "merged, branch will be deleted"
    if bucket == "conflict":
        label = "unmerged (would conflict)"
    elif bucket == "unmerged":
        label = "unmerged"
    else:
        label = "merge status unknown"
    fate = "-D will delete the branch too" if force_delete else "branch will be kept"
    return f"{label}, {fate}"


@app.command("clean", rich_help_panel="Remove (destructive)")
def cmd_clean(
    days: Annotated[
        int | None,
        typer.Argument(help="Remove worktrees older than this many days (default: 14; 7 with --parked)."),
    ] = None,
    repo_path: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-C",
            help="Scope to this repo's worktrees. Defaults to every known repo plus the repo you're standing in.",
        ),
    ] = None,
    merged: Annotated[
        bool,
        typer.Option(
            "--merged",
            "-m",
            help="Ignore DAYS and remove every merged worktree in scope instead, regardless of age "
            "(still skips dirty worktrees and ones with an open PR).",
        ),
    ] = False,
    parked: Annotated[
        bool,
        typer.Option(
            "--parked",
            "-p",
            help="Instead sweep worktrees parked (cop park) at least DAYS days ago (default DAYS: 7), "
            "regardless of worktree age (still skips dirty worktrees and ones with an open PR).",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", "-n", help="List candidates without removing anything.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    force_delete: Annotated[
        bool, typer.Option("--force-delete", "-D", help="Also delete unmerged branches (default: keep them).")
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Also list worktrees under the age threshold, for context.")
    ] = False,
) -> None:
    """Bulk-remove worktrees older than DAYS (default: 14), the natural next
    step after 'coppice list'.

    Pass --merged/-m to instead remove every merged worktree in scope
    regardless of age, or --parked/-p to sweep worktrees parked (cop park)
    at least DAYS days ago (default: 7), DAYS is reinterpreted in those
    modes. --merged and --parked are mutually exclusive.

    Scope is every known repo plus the repo you're standing in ("all
    repos"), same as 'coppice list'/'coppice remove' default, or restrict to
    one repo's worktrees with --repo/-C PATH ("all worktrees" in that repo).

    Skips: the main worktree, the current worktree, dirty worktrees, and
    branches with an open GitHub PR (via 'gh', when installed). A parked
    worktree whose branch head moved since the mark (follow-up arrived)
    counts as active again and is not swept. Reports an on-disk size
    estimate per candidate, a total reclaimable size, and a final
    removed/failed summary.
    """
    if merged and parked:
        raise _fail("--merged and --parked are mutually exclusive.")

    try:
        scope = repo.scope_repos(repo_path)
    except repo.RepoResolutionError as exc:
        raise _fail(str(exc)) from exc

    if not scope:
        raise _fail("no known repos. Run 'coppice new' at least once, or pass --repo.")

    try:
        wt.require_wt()
    except wt.WtNotFoundError as exc:
        raise _fail(str(exc)) from exc

    if days is None:
        days = 7 if parked else 14
    threshold_seconds = days * 86400
    have_gh = shutil.which("gh") is not None

    console.print()
    if merged:
        console.print(f"Scanning {_plural(len(scope), 'repo')} for merged worktrees (any age)...")
    elif parked:
        console.print(f"Scanning {_plural(len(scope), 'repo')} for worktrees parked at least {days}d ago...")
    else:
        console.print(f"Scanning {_plural(len(scope), 'repo')} for worktrees older than {days}d...")

    # (repo_root, display name, size_kb, prune path); size_kb is -1 for a
    # stale/dangling entry (its directory is already gone, there's nothing
    # to size). Prune path is set only for a branchless (detached) stale
    # entry: there's no branch to hand to `wt remove`, so its dangling
    # reference is pruned by path with git instead.
    candidates: list[tuple[Path, str, int, str | None]] = []
    n_worktrees = n_young = n_dirty = n_pr = n_stale = n_unmerged = 0
    n_unparked = n_followup = 0

    # Fetch every repo's worktrees concurrently (one `wt` subprocess per
    # repo, overlapped rather than run one after another).
    worktrees_by_repo = wt.list_worktrees_many(scope)
    if parked:
        _enrich_parked(worktrees_by_repo, park_mod.parked_at_many(scope))

    # Pass 1, per repo, local only (no subprocess): stale, too-young/
    # unmerged, dirty. Whatever's left after those needs an open-PR check
    # and an on-disk size, both of which used to happen one worktree at a
    # time, in a single repo-by-repo loop, an open-PR check shells out to
    # `gh` (a GitHub API round trip) and a size walks a whole directory
    # tree, so doing either serially, or even just repo-by-repo, is what
    # made scanning many worktrees across many repos slow. `lines` gets a
    # None placeholder for each pending worktree so its line can be filled
    # in later without disturbing the original per-worktree print order.
    lines_by_repo: dict[Path, list[str | None]] = {}
    pending_by_repo: dict[Path, list[tuple[int, dict[str, Any], str]]] = {}

    for repo_root in scope:
        # A stale entry can be detached (`branch` is null, `list` shows it
        # as '?'), so staleness substitutes for having a branch here: it's
        # an unconditional removal candidate either way.
        others = [
            w
            for w in worktrees_by_repo.get(repo_root, [])
            if not w.get("is_main") and not w.get("is_current") and (w.get("branch") or _is_stale(w))
        ]
        if not others:
            continue

        lines: list[str | None] = []
        pending: list[tuple[int, dict[str, Any], str]] = []  # (line index, worktree, age_label)
        for w in others:
            n_worktrees += 1
            branch_name = w.get("branch")

            if _is_stale(w):
                n_stale += 1
                if branch_name:
                    lines.append(
                        f"  [green]rm[/]    [red]stale[/]  {branch_name}  [dim](worktree directory is gone; "
                        "cleaning up the dangling reference)[/]"
                    )
                    candidates.append((repo_root, branch_name, -1, None))
                elif path := w.get("path"):
                    # A detached stale entry has no branch to hand to `wt
                    # remove` (which also refuses entries whose directory is
                    # already gone), so the path is its only identity and
                    # the reference gets pruned by path with git directly.
                    display = _short_path(Path(path))
                    lines.append(
                        f"  [green]rm[/]    [red]stale[/]  {display}  [dim](detached, worktree directory is "
                        "gone; cleaning up the dangling reference)[/]"
                    )
                    candidates.append((repo_root, display, -1, path))
                else:
                    # No branch and no path: nothing to remove by. Can't
                    # happen from git's worktree metadata (the path is
                    # always recorded), but don't crash the scan on it.
                    lines.append(
                        "  [yellow]skip[/]  [red]stale[/]  ?  [dim](dangling reference with no branch or "
                        "path; run 'git worktree prune' by hand)[/]"
                    )
                continue

            seconds = _age_seconds(w)
            age_label = _humanize_age(seconds) if seconds is not None else "?"

            if merged:
                # Removable = the merged bucket only. Widening it past
                # empty/integrated is deliberate: `behind` has no commits
                # main lacks, and `same_commit` equals `empty` once clean
                # (the dirty skip below protects its uncommitted changes).
                # `diverged` and `would_conflict` always stay kept: a
                # conflict label is an invitation to merge or rebase, never
                # to delete.
                if _classify_main_state(w) != "merged":
                    n_unmerged += 1
                    if verbose:
                        lines.append(f"  [dim]keep[/]  {age_label:>5}  {branch_name}  [dim](not merged)[/]")
                    continue
            elif parked:
                # Removable = parked at least DAYS ago. The parked age (not
                # the worktree age) decides, so the preview shows it per
                # row. A head newer than the mark means follow-up already
                # happened: the worktree is active again and stays kept.
                parked_ts = w.get("parked_at")
                if parked_ts is None:
                    n_unparked += 1
                    if verbose:
                        lines.append(f"  [dim]keep[/]  {age_label:>5}  {branch_name}  [dim](not parked)[/]")
                    continue
                if _has_follow_up(w):
                    n_followup += 1
                    if verbose:
                        lines.append(
                            f"  [dim]keep[/]  {age_label:>5}  {branch_name}  [dim](follow-up since it was parked)[/]"
                        )
                    continue
                parked_age = time.time() - parked_ts
                age_label = _humanize_age(parked_age)
                if parked_age < threshold_seconds:
                    n_young += 1
                    if verbose:
                        lines.append(
                            f"  [dim]keep[/]  {age_label:>5}  {branch_name}  [dim](parked less than {days}d ago)[/]"
                        )
                    continue
            else:
                if seconds is None:
                    if verbose:
                        lines.append(f"  [dim]keep[/]  {age_label:>5}  {branch_name}  [dim](age unknown)[/]")
                    continue
                if seconds < threshold_seconds:
                    n_young += 1
                    if verbose:
                        lines.append(f"  [dim]keep[/]  {age_label:>5}  {branch_name}  [dim](younger than {days}d)[/]")
                    continue

            if _is_dirty(w):
                n_dirty += 1
                lines.append(f"  [yellow]skip[/]  {age_label:>5}  {branch_name}  [dim](uncommitted changes)[/]")
                continue

            lines.append(None)
            pending.append((len(lines) - 1, w, age_label))

        lines_by_repo[repo_root] = lines
        pending_by_repo[repo_root] = pending

    # Pass 2: one 'gh pr list' call per repo that has pending branches, run
    # concurrently across every such repo (instead of one call per branch,
    # run one repo after another).
    pr_map_by_repo: dict[Path, dict[str, str]] = {}
    pending_repos = [r for r, p in pending_by_repo.items() if p]
    if have_gh and pending_repos:
        with ThreadPoolExecutor(max_workers=min(len(pending_repos), 8)) as pool:
            futures = {
                r: pool.submit(gh.open_prs, r, [w["branch"] for _, w, _ in pending_by_repo[r]]) for r in pending_repos
            }
            pr_map_by_repo = {r: f.result() for r, f in futures.items()}

    # Pass 3: resolve the PR checks, then size every worktree that survives
    # them across *every* repo in one batch (one process pool sized to the
    # machine's core count, instead of a fresh pool per repo run one repo at
    # a time), so a scan across several repos gets the same parallelism as
    # scanning one repo with the same total number of candidates.
    kept_by_repo: dict[Path, list[tuple[int, dict[str, Any], str]]] = {}
    all_kept_paths: list[Path] = []
    for repo_root, pending in pending_by_repo.items():
        pr_map = pr_map_by_repo.get(repo_root, {})
        lines = lines_by_repo[repo_root]
        kept: list[tuple[int, dict[str, Any], str]] = []
        for idx, w, age_label in pending:
            branch_name = w["branch"]
            if pr_info := pr_map.get(branch_name):
                n_pr += 1
                lines[idx] = f"  [yellow]skip[/]  {age_label:>5}  {branch_name}  [dim](open PR {pr_info})[/]"
                continue
            kept.append((idx, w, age_label))
            if w.get("path"):
                all_kept_paths.append(Path(w["path"]))
        kept_by_repo[repo_root] = kept

    size_cache = sizes.dir_sizes_kb(all_kept_paths) if all_kept_paths else {}

    # One osascript round-trip for every candidate at once; empty when the
    # windows can't be listed, in which case nothing is marked rather than
    # ever claiming "not open".
    open_windows = _open_vscode_windows(
        {
            (repo_root, w["branch"]): Path(w["path"])
            for repo_root, kept in kept_by_repo.items()
            for _, w, _ in kept
            if w.get("path")
        }
    )

    for repo_root, kept in kept_by_repo.items():
        lines = lines_by_repo[repo_root]
        for idx, w, age_label in kept:
            branch_name = w["branch"]
            size_kb = size_cache.get(Path(w["path"]), 0) if w.get("path") else 0
            merge_label = _merge_label(w, force_delete=force_delete)
            marker = _VSCODE_OPEN_MARKER if (repo_root, branch_name) in open_windows else ""
            lines[idx] = (
                f"  [green]rm[/]    {age_label:>5}  {branch_name}  "
                f"[dim]({sizes.human_kb(size_kb)} on disk, {merge_label})[/]{marker}"
            )
            candidates.append((repo_root, branch_name, size_kb, None))

    for repo_root in scope:
        repo_lines = lines_by_repo.get(repo_root)
        if repo_lines:
            console.print()
            console.print(f"{_repo_header(repo_root)}:")
            for line in repo_lines:
                console.print(line)

    console.print()
    stale_note = f", {n_stale} stale (dangling) reference(s)" if n_stale else ""
    if merged:
        unmerged_note = f", {n_unmerged} not merged" if n_unmerged else ""
        console.print(
            f"Scanned {_plural(len(scope), 'repo')}, {_plural(n_worktrees, 'worktree')}: {len(candidates)} removable, "
            f"{n_dirty} dirty, {n_pr} with an open PR{unmerged_note}{stale_note}."
        )
    elif parked:
        unparked_note = f", {n_unparked} not parked" if n_unparked else ""
        followup_note = f", {n_followup} with follow-up" if n_followup else ""
        console.print(
            f"Scanned {_plural(len(scope), 'repo')}, {_plural(n_worktrees, 'worktree')}: {len(candidates)} removable, "
            f"{n_dirty} dirty, {n_pr} with an open PR, {n_young} parked under {days}d"
            f"{unparked_note}{followup_note}{stale_note}."
        )
    else:
        console.print(
            f"Scanned {_plural(len(scope), 'repo')}, {_plural(n_worktrees, 'worktree')}: {len(candidates)} removable, "
            f"{n_dirty} dirty, {n_pr} with an open PR, {n_young} under {days}d old{stale_note}."
        )

    if open_windows:
        console.print(f"[yellow]{_VSCODE_OPEN_NOTE}[/]")

    if not candidates:
        console.print("Nothing to clean.")
        console.print()
        return

    total_kb = sum(size_kb for _, _, size_kb, _ in candidates if size_kb > 0)
    if total_kb:
        console.print(f"Total reclaimable: {sizes.human_kb(total_kb)} across {_plural(len(candidates), 'worktree')}.")

    if dry_run:
        console.print("Dry run, nothing removed.")
        console.print()
        return

    if not yes:
        console.print()
        consequence = (
            "Unmerged branches are deleted too." if force_delete else "Branches survive unless already merged."
        )
        if not confirm.ask(
            f"Remove the {_plural(len(candidates), 'worktree')} listed above? {consequence}",
            tier="force" if force_delete else "destructive",
        ):
            console.print()
            console.print("Cancelled.")
            console.print()
            raise typer.Exit(1)

    console.print()
    n_removed = 0
    failed: list[str] = []
    for repo_root, name, size_kb, prune_path in candidates:
        label = "stale reference" if size_kb < 0 else sizes.human_kb(size_kb)
        console.print(f"Removing '{name}' @ {repo_root.name} ({label})...")
        try:
            if prune_path is not None:
                wt.prune_stale(repo_root, prune_path)
            else:
                wt.remove(repo_root, name, yes=True, force_delete=force_delete)
                _unpark_if_branch_gone(repo_root, name)
        except (wt.WtNotFoundError, wt.WtCommandError) as exc:
            err.print(f"[red]Error:[/] {exc}")
            failed.append(f"{name} @ {repo_root.name}")
        else:
            n_removed += 1

    console.print()
    if not failed:
        console.print(f"Removed {_plural(n_removed, 'worktree')}.")
        console.print()
    else:
        err.print(f"[red]Removed {_plural(n_removed, 'worktree')}, {len(failed)} failed:[/]")
        for f in failed:
            err.print(f"  - {f}")
        raise typer.Exit(1)


class _SyncRow(NamedTuple):
    """One `sync` report row as data: the result label ('synced', 'skip',
    'error', ...), its Rich style, the subject (a branch name, 'main
    worktree', or 'base fetch'), and an optional detail note. Rows stay
    unstyled until render time so the closing 'Needs attention' extract can
    filter and re-render them without scraping markup back off."""

    label: str
    style: str
    subject: str
    note: str = ""


def _sync_row(label: str, style: str, subject: str, note: str = "") -> _SyncRow:
    """One `sync` report row; call sites read as (what happened, how it
    looks, which worktree, why)."""
    return _SyncRow(label, style, subject, note)


def _sync_cells(row: _SyncRow) -> tuple[str, str, str]:
    """A `_SyncRow` as Rich cells: the subject, the styled result label, and a
    dim detail note. A stale entry redifies the subject too, the same
    convention `list` uses (the whole signal goes bold red, see
    `_worktree_cells`). Shared by both sync renderers so the report table and
    the 'Needs attention' extract can't drift."""
    subject = f"[{row.style}]{row.subject}[/]" if row.style == _STYLE_STALE else row.subject
    return subject, f"[{row.style}]{row.label}[/]", f"[dim]{row.note}[/]" if row.note else ""


def _needs_attention(row: _SyncRow) -> bool:
    """Whether a sync row belongs in the closing 'Needs attention' extract:
    anything the user should act on (errors, conflicts, stale references,
    warning-tier skips) rather than an expected state. Yellow is the warning
    tier everywhere in coppice and dim the expected-state tier, so a dim skip
    (a local-only repo, the main worktree on another branch) stays out while
    a yellow one (dirty, diverged, detached) is listed."""
    return row.label in ("error", "conflict", "stale") or (row.label == "skip" and row.style == "yellow")


def _render_sync_table(sections: list[tuple[Path, str | None, list[_SyncRow]]]) -> None:
    """`sync`'s report as one Rich table sectioned by repo, the same visual
    language as `list` (see `_render_list`): SIMPLE_HEAVY box, bold header, a
    repo heading row in the first column (carrying the base ref where `list`
    carries the main branch), worktree rows indented under it, a separator
    between repos. Each section is (repo root, base branch name or None, its
    `_SyncRow`s)."""
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", pad_edge=False, show_edge=False)
    # no_wrap on the identifier columns: repo headings and branch names must
    # never fold or ellipsize on narrow terminals (a wrapped 'repo (base: ...)'
    # heading stops reading as a table at all). The Detail column is prose and
    # absorbs the squeeze by wrapping instead.
    table.add_column("Worktree", no_wrap=True)
    table.add_column("Result", no_wrap=True)
    table.add_column("Detail")

    last = len(sections) - 1
    for i, (repo_root, base_branch, rows) in enumerate(sections):
        heading = f"[bold]{repo_root.name}[/]"
        if base_branch:
            heading += f" [dim](base: origin/{base_branch})[/]"
        table.add_row(heading, "", "")
        for j, row in enumerate(rows):
            subject, result, detail = _sync_cells(row)
            table.add_row(f"  {subject}", result, detail, end_section=i < last and j == len(rows) - 1)
    console.print(table)


def _render_sync_attention(sections: list[tuple[Path, str | None, list[_SyncRow]]]) -> None:
    """`sync`'s closing 'Needs attention' extract: the rows the report table
    already showed, pre-filtered to the ones the user should act on
    (`_needs_attention`), so a long run's action items don't scroll away
    above the counts line. Borderless and headerless, an extract of the
    table above rather than a second report."""
    console.print()
    console.print("[bold]Needs attention:[/]")
    table = Table(box=None, show_header=False, show_edge=False, pad_edge=False)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column()
    for repo_root, _base_branch, rows in sections:
        table.add_row(f"[bold]{repo_root.name}[/]", "", "")
        for row in rows:
            subject, result, detail = _sync_cells(row)
            table.add_row(f"  {subject}", result, detail)
    console.print(table)


@app.command("sync", rich_help_panel="Update")
def cmd_sync(
    branches: Annotated[
        list[str] | None,
        typer.Argument(help="Only sync these branches' worktrees. Omit to sync every managed worktree in scope."),
    ] = None,
    repo_path: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-C",
            help="Scope to this repo. Defaults to every known repo plus the repo you're standing in.",
        ),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option(
            "--base",
            "-B",
            help="Base branch to sync from (fetched as origin/BASE). Defaults to the repo's actual default branch, "
            "freshly resolved from its remote.",
        ),
    ] = None,
    no_main: Annotated[
        bool,
        typer.Option("--no-main", help="Leave the main worktree's own checkout of the base branch alone."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", "-n", help="Report what would be synced without changing anything.")
    ] = False,
) -> None:
    """Merge each repo's base remote into its worktrees, keeping long-lived ones current.

    Fetches origin/<base> once per repo (BASE defaults to the repo's actual
    default branch, freshly resolved from its remote), fast-forwards the
    main worktree's checkout of the base branch when clean (--no-main
    skips), then merges origin/<base> into every eligible worktree branch.

    Skips dirty worktrees, parked worktrees (cop park, nothing to merge
    into a task-complete tree; unpark first to sync it again), stale
    (dangling) references, and detached HEADs.
    Worktrees whose merge would conflict are predicted with `git merge-tree`
    and left untouched, reported as conflicts, so a worktree is never left
    half-merged. Never prompts: syncing only adds merge commits to clean
    branches, and conflicts are skipped rather than forced. Ends with a
    'Needs attention' list of everything that did not sync, so long runs
    need no scrolling back.

    Examples:
        coppice sync                        # every worktree in every known repo
        coppice sync --repo ~/dbt-models    # ...just this one
        coppice sync feat-a feat-b          # ...just these branches' worktrees
        coppice sync --dry-run              # preview, changing nothing
    """
    try:
        scope = repo.scope_repos(repo_path)
    except repo.RepoResolutionError as exc:
        raise _fail(str(exc)) from exc

    if not scope:
        raise _fail("no known repos. Run 'coppice new' at least once, or pass --repo.")

    try:
        wt.require_wt()
    except wt.WtNotFoundError as exc:
        raise _fail(str(exc)) from exc

    worktrees_by_repo = wt.list_worktrees_many(scope)
    _enrich_parked(worktrees_by_repo, park_mod.parked_at_many(scope))

    # An explicit BRANCH filter must name real managed worktrees; a typo'd
    # name failing the whole run is safe (sync is idempotent, just re-run).
    if branches:
        known = {
            w["branch"]
            for worktrees in worktrees_by_repo.values()
            for w in worktrees
            if not w.get("is_main") and w.get("branch")
        }
        unmatched = [b for b in branches if b not in known]
        if unmatched:
            raise _fail(f"no worktree found for: {', '.join(unmatched)}")

    console.print()

    # Resolve each repo's base branch and fetch it, concurrently across
    # repos: both can hit the network (default_branch's ls-remote fallback,
    # the fetch itself), so serializing them repo-by-repo would multiply the
    # wait. A repo whose base can't be resolved or fetched is reported and
    # skipped, never fatal to the others.
    base_by_repo: dict[Path, str] = {}
    fetch_error_by_repo: dict[Path, str] = {}
    no_remote_repos: set[Path] = set()

    def _resolve_and_fetch(repo_root: Path) -> None:
        resolved = base or repo.default_branch(repo_root)
        if resolved is None:
            if base is None and not repo.has_origin(repo_root):
                # A local-only repo has nothing to sync from; that's a skip,
                # not an error.
                no_remote_repos.add(repo_root)
            else:
                fetch_error_by_repo[repo_root] = "could not resolve the default branch from origin"
            return
        try:
            git.fetch_base(repo_root, resolved)
        except git.GitError as exc:
            fetch_error_by_repo[repo_root] = str(exc)
        else:
            base_by_repo[repo_root] = resolved

    with (
        console.status("[dim]Fetching base branches…[/dim]"),
        ThreadPoolExecutor(max_workers=min(len(scope), 8)) as pool,
    ):
        list(pool.map(_resolve_and_fetch, scope))

    n_synced = n_current = n_skipped = n_conflict = n_main_ff = n_error = 0
    rows_by_repo: dict[Path, list[_SyncRow]] = {}

    for repo_root in scope:
        rows: list[_SyncRow] = []
        rows_by_repo[repo_root] = rows

        if repo_root in no_remote_repos:
            n_skipped += 1
            rows.append(_sync_row("skip", "dim", "base fetch", "no origin remote; local-only repo"))
            continue

        if repo_root in fetch_error_by_repo:
            n_error += 1
            rows.append(_sync_row("error", "red", "base fetch", fetch_error_by_repo[repo_root]))
            continue

        base_branch = base_by_repo[repo_root]
        base_ref = f"origin/{base_branch}"
        worktrees = worktrees_by_repo.get(repo_root, [])
        main_entry = next((w for w in worktrees if w.get("is_main")), None)
        others = [w for w in worktrees if not w.get("is_main")]

        # The main worktree isn't merged into (it isn't a managed worktree,
        # it's the repo itself); its checkout of the base branch is
        # fast-forwarded instead, so the local base keeps up with the remote
        # everything else here merges from.
        if not no_main and main_entry is not None and main_entry.get("path"):
            main_branch = main_entry.get("branch")
            if main_branch != base_branch:
                rows.append(
                    _sync_row("skip", "dim", "main worktree", f"has '{main_branch}' checked out, not {base_branch}")
                )
            elif _is_dirty(main_entry):
                n_skipped += 1
                rows.append(_sync_row("skip", "yellow", "main worktree", "uncommitted changes"))
            elif git.is_ancestor(repo_root, base_ref, base_branch):
                n_current += 1
                rows.append(_sync_row("current", "dim", "main worktree"))
            elif not git.is_ancestor(repo_root, base_branch, base_ref):
                n_skipped += 1
                rows.append(
                    _sync_row("skip", "yellow", "main worktree", f"local {base_branch} has diverged from {base_ref}")
                )
            else:
                n_incoming = git.commits_between(repo_root, base_branch, base_ref)
                if dry_run:
                    n_main_ff += 1
                    rows.append(
                        _sync_row(
                            "would ff", "green", "main worktree", f"{_plural(n_incoming, 'commit')} from {base_ref}"
                        )
                    )
                else:
                    try:
                        git.ff_only(Path(main_entry["path"]), base_ref)
                    except git.GitError as exc:
                        n_error += 1
                        rows.append(_sync_row("error", "red", "main worktree", f"fast-forward failed: {exc}"))
                    else:
                        n_main_ff += 1
                        rows.append(
                            _sync_row(
                                "ff", "green", "main worktree", f"{_plural(n_incoming, 'commit')} from {base_ref}"
                            )
                        )

        for w in others:
            branch_name = w.get("branch")
            if branches and branch_name not in branches:
                continue

            if _is_stale(w):
                rows.append(
                    _sync_row("stale", _STYLE_STALE, branch_name or "?", "worktree directory is gone; run 'cop clean'")
                )
                continue
            if not branch_name:
                n_skipped += 1
                rows.append(_sync_row("skip", "yellow", "?", "detached HEAD, no branch to merge into"))
                continue
            if _is_parked(w):
                # Nothing to merge into a task-complete tree, and skipping
                # cuts the conflict surface of a sync run. Dim (an expected
                # state, not a problem), so it stays out of 'Needs
                # attention'. Follow-up instead: 'cop unpark', then sync.
                n_skipped += 1
                rows.append(_sync_row("skip", "dim", branch_name, "parked; 'cop unpark' to sync it again"))
                continue
            if _is_dirty(w):
                n_skipped += 1
                rows.append(_sync_row("skip", "yellow", branch_name, "uncommitted changes"))
                continue
            if not w.get("path"):
                n_skipped += 1
                rows.append(_sync_row("skip", "yellow", branch_name, "no worktree path known"))
                continue

            if git.is_ancestor(repo_root, base_ref, branch_name):
                n_current += 1
                rows.append(_sync_row("current", "dim", branch_name))
                continue
            try:
                conflicted = git.merge_would_conflict(repo_root, branch_name, base_ref)
            except git.GitError as exc:
                n_skipped += 1
                rows.append(_sync_row("skip", "yellow", branch_name, f"cannot simulate merge: {exc}"))
                continue
            if conflicted:
                n_conflict += 1
                rows.append(_sync_row("conflict", _STYLE_CONFLICT, branch_name, "would conflict; left untouched"))
                continue

            n_incoming = git.commits_between(repo_root, branch_name, base_ref)
            if dry_run:
                n_synced += 1
                rows.append(
                    _sync_row("would sync", "green", branch_name, f"{_plural(n_incoming, 'commit')} from {base_ref}")
                )
                continue
            try:
                git.merge(Path(w["path"]), base_ref)
            except git.GitError as exc:
                # The simulation said clean but the merge still failed (the
                # tree changed between the two, a hook interfered, ...):
                # abort so the worktree is never left half-merged.
                git.merge_abort(Path(w["path"]))
                n_error += 1
                rows.append(_sync_row("error", "red", branch_name, f"merge failed, aborted: {exc}"))
                continue
            n_synced += 1
            rows.append(_sync_row("synced", "green", branch_name, f"{_plural(n_incoming, 'commit')} from {base_ref}"))

    sections = [
        (repo_root, base_by_repo.get(repo_root), rows_by_repo[repo_root])
        for repo_root in scope
        if rows_by_repo.get(repo_root)
    ]
    if not sections:
        # e.g. --no-main with no managed worktrees anywhere in scope
        console.print("No worktrees in scope.")
        console.print()
        return

    _render_sync_table(sections)
    console.print()

    if n_synced == 0 and n_main_ff == 0 and not (n_skipped or n_conflict or n_error):
        console.print("Everything is already up to date.")
    else:
        verb = "Would sync" if dry_run else "Synced"
        summary = f"{verb} {_plural(n_synced, 'worktree')}"
        if n_main_ff:
            ff_verb = "would fast-forward" if dry_run else "fast-forwarded"
            summary += f", {ff_verb} {_plural(n_main_ff, 'main checkout')}"
        if n_current:
            summary += f", {n_current} already current"
        if n_skipped:
            summary += f", {n_skipped} skipped"
        if n_conflict:
            summary += f", [{_STYLE_CONFLICT}]{_plural(n_conflict, 'conflict')}[/]"
        if n_error:
            summary += f", [red]{_plural(n_error, 'error')}[/]"
        console.print(summary + ".")

    # The closing extract of everything that did not sync correctly, so a
    # long run's action items don't scroll away above the counts line.
    # Stale references fold in here too (their note carries the 'cop clean'
    # hint) instead of getting a separate summary line.
    attention = [
        (repo_root, base_branch, [row for row in rows if _needs_attention(row)])
        for repo_root, base_branch, rows in sections
    ]
    if attention := [section for section in attention if section[2]]:
        _render_sync_attention(attention)
    if dry_run:
        console.print("Dry run, nothing changed.")
    console.print()

    if n_error:
        raise typer.Exit(1)


@app.command("status", rich_help_panel="Inspect")
def cmd_status(
    show_size: Annotated[
        bool,
        typer.Option(
            "--size/--no-size",
            help="Show each repo's total on-disk size (walks every worktree's directory; disable for a faster check).",
        ),
    ] = True,
) -> None:
    """wt/registry health check.

    Deliberately minimal and generic: no project-specific tool checks here,
    those belong outside coppice for whichever project cares about them.
    """
    console.print()
    wt_path = shutil.which("wt")
    if wt_path is not None:
        version_proc = subprocess.run(["wt", "--version"], capture_output=True, text=True)
        wt_version = version_proc.stdout.strip() or version_proc.stderr.strip() or "unknown version"
        # 'wt --version' prints e.g. 'wt v0.74.0'; drop the prefix so the
        # line doesn't read 'wt: found (wt v0.74.0)'.
        console.print(f"wt: [green]found[/] ({wt_version.removeprefix('wt ')}) @ {wt_path}")
    else:
        console.print("wt: [red]not found[/] on PATH. See https://worktrunk.dev")

    console.print()
    known = repo.known_repos()
    if not known:
        console.print(
            f"Known repos [dim]({_short_path(repo.REGISTRY_PATH)})[/]: none yet. Run 'coppice new' at least once."
        )
        console.print()
        return

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", pad_edge=False, show_edge=False)
    table.add_column("Repo", no_wrap=True)
    table.add_column("Extra worktrees", justify="right")
    if show_size:
        table.add_column("Size", justify="right")
    table.add_column("Status")

    total = 0
    total_kb = 0
    total_stale = 0

    # Only repos that actually exist and can be checked (wt installed) are
    # worth a `wt list` call; everything else renders a fixed row below
    # without touching the filesystem or a subprocess. Fetch every checkable
    # repo's worktrees concurrently (one `wt` subprocess per repo,
    # overlapped rather than run one after another), then size every one of
    # their worktrees in a single combined batch, instead of a fresh
    # process pool per repo processed one repo at a time.
    checkable = [r for r in known if r.exists() and wt_path is not None]

    with console.status("[dim]Checking known repos…[/dim]") as spinner:
        worktrees_by_repo: dict[Path, list[dict[str, Any]]] = {}
        if checkable:
            spinner.update(f"[dim]Checking {_plural(len(checkable), 'repo')}…[/dim]")
            worktrees_by_repo = wt.list_worktrees_many(checkable)

        size_cache: dict[Path, int] = {}
        if show_size and checkable:
            all_paths = [p for r in checkable for p in _sizeable_paths(worktrees_by_repo[r])]
            if all_paths:

                def _report_progress(done: int, total_n: int) -> None:
                    spinner.update(f"[dim]Sizing worktrees ({done}/{total_n})…[/dim]")

                size_cache = sizes.dir_sizes_kb(all_paths, on_progress=_report_progress)

        missing: list[Path] = []
        for repo_root in known:
            if not repo_root.exists():
                missing.append(repo_root)
                row = [_short_path(repo_root), "-"]
                if show_size:
                    row.append("-")
                row.append("[red]missing[/]")
                table.add_row(*row)
                continue

            if wt_path is None:
                row = [_short_path(repo_root), "?"]
                if show_size:
                    row.append("?")
                row.append("[dim]unknown (wt missing)[/]")
                table.add_row(*row)
                continue

            worktrees = worktrees_by_repo.get(repo_root, [])
            extra_count = sum(1 for w in worktrees if not w.get("is_main"))
            stale_count = sum(1 for w in worktrees if _is_stale(w))
            total += extra_count
            total_stale += stale_count
            size_kb = 0
            if show_size:
                size_kb = sum(s for w in worktrees if (s := _worktree_size_kb(w, size_cache)) is not None)
                total_kb += size_kb
            if stale_count:
                status_cell, quiet = f"[red]{stale_count} stale[/]", False
            elif extra_count:
                status_cell, quiet = "[green]ok[/]", False
            else:
                # A repo with no extra worktrees and no problems is
                # background noise next to rows that need attention; dim
                # the whole row so those stand out.
                status_cell, quiet = "[dim]ok[/]", True
            if quiet:
                row = [f"[dim]{_short_path(repo_root)}[/]", f"[dim]{extra_count}[/]"]
                if show_size:
                    row.append(f"[dim]{sizes.human_kb(size_kb)}[/]")
            else:
                row = [_short_path(repo_root), str(extra_count)]
                if show_size:
                    row.append(sizes.human_kb(size_kb))
            row.append(status_cell)
            table.add_row(*row)

        spinner.stop()

    console.print(f"Known repos [dim]({_short_path(repo.REGISTRY_PATH)})[/]:")
    console.print(table)
    console.print()

    # Registered repos vanish for reasons coppice doesn't control (a
    # scratch repo removed by hand, a `wt`-hook-registered temp repo whose
    # OS temp dir got reaped, a project simply deleted/moved). There's
    # nothing to preserve by keeping a dead entry around, it would just
    # keep showing up here, so self-heal the registry every time `status`
    # runs instead of letting `missing` rows accumulate forever.
    if missing:
        repo.prune_missing_repos()
        console.print(f"[dim]Pruned {_plural(len(missing), 'missing repo')} from the registry.[/]")

    if wt_path is not None:
        summary = f"Total: {_plural(total, 'worktree')} across {_plural(len(known) - len(missing), 'repo')}."
        if show_size and total_kb:
            summary += f" {sizes.human_kb(total_kb)} on disk."
        if total_stale:
            summary += f" [red]{total_stale} stale (dangling) reference(s)[/], run 'cop clean' to remove."
        console.print(summary)
        console.print()


shell_app = typer.Typer(help="cd integration for 'new' (see 'coppice shell init --help'). Works for 'cop' too.")
app.add_typer(shell_app, name="shell", rich_help_panel="Setup")


@shell_app.command("init")
def cmd_shell_init(
    shell_name: Annotated[str, typer.Argument(help="Shell to generate integration for.")] = "zsh",
) -> None:
    """Print shell functions that wrap 'coppice' and 'cop' and 'cd' into worktrees 'new' creates.

    coppice/cop is a plain executable, so it can't change your shell's
    working directory on its own (only a shell function running in the same
    process can). This prints functions that shadow both the 'coppice' and
    'cop' commands: each runs the matching real binary, then 'cd's if 'new'
    recorded a resulting path.

    Add this to your shell rc file:

        eval "$(coppice shell init zsh)"
    """
    template = shell.TEMPLATES.get(shell_name)
    if template is None:
        raise _fail(f"unsupported shell '{shell_name}'. Supported: {', '.join(sorted(shell.TEMPLATES))}")
    print(template, end="")
