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

import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from typer.core import TyperGroup

from coppice import branch as branch_mod
from coppice import gh, repo, shell, sizes, wt

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

# Output spacing convention: one blank line before a command's first output
# line and one after its last (breathing room against the shell prompt on
# both sides), and one blank line between logical blocks within a command
# (tables, prompts, action logs, summaries). Never in --json output
# (machine-readable), stderr error paths, or 'shell init' (eval'd code).


def _fail(message: str) -> typer.Exit:
    err.print(f"[red]Error:[/] {message}")
    return typer.Exit(1)


# macOS VS Code user settings, read by `new --prompt`'s preflight. The file is
# JSONC (comments, trailing commas), so the check below is a regex heuristic,
# not a JSON parse.
_VSCODE_SETTINGS_PATH = Path.home() / "Library" / "Application Support" / "Code" / "User" / "settings.json"


def _prompt_preflight(repo_root: Path) -> None:
    """Non-blocking sanity checks for `new --prompt`: the prompt is delivered
    by the user's `wt` hooks (see README): a hook script drives the new
    window's integrated terminal directly via the Accessibility API, with a
    VS Code folder-open task as the fallback when the drive is unavailable.
    None of it is controlled by coppice from here, so a missing piece can
    mean the prompt silently goes nowhere. Warn (dim, on stderr) rather
    than fail: the worktree itself is created regardless.
    """
    if shutil.which("pi") is None:
        err.print("[dim]note: `pi` is not on PATH, the --prompt hook needs it to start a session in the new window[/]")
    try:
        settings = _VSCODE_SETTINGS_PATH.read_text()
    except OSError:
        settings = ""
    if not re.search(r'"task\.allowAutomaticTasks"\s*:\s*"on"', settings):
        err.print(
            '[dim]note: VS Code\'s "task.allowAutomaticTasks" is not "on": fine while the direct terminal '
            "delivery works, but the --prompt fallback task will not auto-run until automatic tasks are "
            "allowed once (see README)[/]"
        )
    if (repo_root / ".vscode" / "tasks.json").is_file():
        err.print(
            "[dim]note: this repo already has a .vscode/tasks.json, the --prompt fallback will merge its task into it[/]"
        )


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
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt",
            "-p",
            help="Open the worktree's VS Code window with `pi` already running this prompt in its terminal. "
            "Delivered as $COP_PROMPT to `wt`'s hooks; needs the one-time hook + VS Code setup from the README.",
        ),
    ] = None,
) -> None:
    """Create or reuse a worktree for the repo at PATH.

    When the branch (named via --branch, or the prompted-for description)
    already exists, locally or on the remote, asks before switching to its
    worktree instead (skip with --yes/-y): 'new' implies a fresh branch, so
    an existing one, most likely a typo'd --branch meant to name a new
    one, is the surprising case worth a check, and defaults to no on a
    bare Enter accordingly. No prompt when it doesn't exist yet anywhere,
    creating it is exactly what 'new' is for.

    Examples:
        coppice new ./tardis
        coppice new . --branch fix-thing --base develop
        coppice new . --prompt "fix the flaky login test"
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
        if not typer.confirm(f"Branch '{branch}' already exists. Switch to its worktree instead?", default=False):
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

    if prompt:
        _prompt_preflight(repo_root)

    try:
        result = wt.switch(
            repo_root,
            branch,
            create=create,
            base=base,
            extra_env={"COP_PROMPT": prompt} if prompt else None,
        )
    except (wt.WtNotFoundError, wt.WtCommandError) as exc:
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
    branch = w.get("branch") or "?"
    if stale:
        branch_cell = f"[{_STYLE_STALE}]{indent}{branch}[/]"
    elif w.get("is_current"):
        branch_cell = f"[{_STYLE_CURRENT}]{indent}{branch}[/]"
    else:
        branch_cell = f"{indent}{branch}"

    working_tree = "[dim]-[/]" if stale else (f"[{_STYLE_DIRTY}]dirty[/]" if _is_dirty(w) else "[dim]clean[/]")
    merge_label, merge_style = _merge_status(w)

    size_kb = _worktree_size_kb(w, size_cache) if show_size else None
    cells = [branch_cell, _age_cell(w)]
    if show_size:
        cells.append(sizes.human_kb(size_kb) if size_kb is not None else "-")
    if verbose:
        path = w.get("path")
        cells.append(f"[dim]{_short_path(Path(path), max_len=40)}[/]" if path and not stale else "[dim]-[/]")
    cells += [working_tree, f"[{merge_style}]{merge_label}[/]"]
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
    references first, they always need action, then newest first (the
    worktree you're looking for is usually a recent one), unknown ages
    last.
    """
    if _is_stale(w):
        return (0, 0.0)
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


def _pick_branches_interactively(scope: list[Path], removable: dict[Path, list[dict[str, Any]]]) -> list[str] | None:
    """fzf multi-select picker over every removable worktree in SCOPE, for
    `coppice remove` with no BRANCH given.

    Falls back to printing the candidates and asking for an explicit re-run
    when `fzf` isn't installed, rather than a pure-Python picker, to avoid a
    new dependency for something already optional.

    Returns None (caller exits 1) when there's nothing to pick, `fzf` isn't
    installed, or the user cancels the picker.
    """
    candidates = [(repo_root, w) for repo_root in scope for w in removable[repo_root]]
    if not candidates:
        err.print("[red]Error:[/] no removable worktrees in scope.")
        return None

    if shutil.which("fzf") is None:
        err.print("No BRANCH given and fzf isn't installed. Candidates in scope:")
        for repo_root, w in candidates:
            suffix = " (dirty)" if _is_dirty(w) else ""
            err.print(f"  {w['branch']}  @ {repo_root.name}{suffix}")
        err.print("Re-run: coppice remove BRANCH [--repo PATH]")
        return None

    # Prefix each line with its candidate index so the pick can be mapped
    # back precisely even if two entries render an identical label (e.g.
    # the same branch name in two different repos in scope); --with-nth
    # hides that column from what fzf actually displays.
    lines = [
        f"{i}\t{w['branch']}  @ {repo_root.name}  ({_age_label(w)}{', dirty' if _is_dirty(w) else ''})"
        for i, (repo_root, w) in enumerate(candidates)
    ]
    proc = subprocess.run(
        ["fzf", "--prompt=Remove worktrees> ", "--height=~50%", "--multi", "--delimiter=\t", "--with-nth=2.."],
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
    passed. This prompt is coppice's own, not `wt remove`'s: `wt` is run
    with its stdout/stderr captured, so it treats the call as
    non-interactive and skips its own approval prompt, `-y` and all,
    instead of blocking on it. Relying on `wt` to ask would silently remove
    worktrees with no confirmation at all.
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
    for target, branch_name in targets:
        console.print(f"  {branch_name} @ {target.name}")

    if not yes:
        console.print()
        if not typer.confirm(f"Remove the {_plural(len(targets), 'worktree')} listed above?", default=False):
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

    if failures:
        console.print()
        err.print(f"[red]Removed {_plural(n_removed, 'worktree')}, {len(failures)} failed:[/]")
        for f in failures:
            err.print(f"  - {f}")
        raise typer.Exit(1)

    console.print()
    console.print(f"Removed {_plural(n_removed, 'worktree')}.")
    console.print()


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
    days: Annotated[int, typer.Argument(help="Remove worktrees older than this many days.")] = 14,
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
    regardless of age, DAYS is ignored in that mode.

    Scope is every known repo plus the repo you're standing in ("all
    repos"), same as 'coppice list'/'coppice remove' default, or restrict to
    one repo's worktrees with --repo/-C PATH ("all worktrees" in that repo).

    Skips: the main worktree, the current worktree, dirty worktrees, and
    branches with an open GitHub PR (via 'gh', when installed). Reports an
    on-disk size estimate per candidate, a total reclaimable size, and a
    final removed/failed summary.
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

    threshold_seconds = days * 86400
    have_gh = shutil.which("gh") is not None

    console.print()
    if merged:
        console.print(f"Scanning {_plural(len(scope), 'repo')} for merged worktrees (any age)...")
    else:
        console.print(f"Scanning {_plural(len(scope), 'repo')} for worktrees older than {days}d...")

    # (repo_root, branch, size_kb); size_kb is -1 for a stale/dangling entry
    # (its directory is already gone, there's nothing to size).
    candidates: list[tuple[Path, str, int]] = []
    n_worktrees = n_young = n_dirty = n_pr = n_stale = n_unmerged = 0

    # Fetch every repo's worktrees concurrently (one `wt` subprocess per
    # repo, overlapped rather than run one after another).
    worktrees_by_repo = wt.list_worktrees_many(scope)

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
        others = [
            w
            for w in worktrees_by_repo.get(repo_root, [])
            if not w.get("is_main") and not w.get("is_current") and w.get("branch")
        ]
        if not others:
            continue

        lines: list[str | None] = []
        pending: list[tuple[int, dict[str, Any], str]] = []  # (line index, worktree, age_label)
        for w in others:
            n_worktrees += 1
            branch_name = w["branch"]

            if _is_stale(w):
                n_stale += 1
                lines.append(
                    f"  [green]rm[/]    [red]stale[/]  {branch_name}  [dim](worktree directory is gone; "
                    "cleaning up the dangling reference)[/]"
                )
                candidates.append((repo_root, branch_name, -1))
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

    for repo_root, kept in kept_by_repo.items():
        lines = lines_by_repo[repo_root]
        for idx, w, age_label in kept:
            branch_name = w["branch"]
            size_kb = size_cache.get(Path(w["path"]), 0) if w.get("path") else 0
            merge_label = _merge_label(w, force_delete=force_delete)
            lines[idx] = (
                f"  [green]rm[/]    {age_label:>5}  {branch_name}  "
                f"[dim]({sizes.human_kb(size_kb)} on disk, {merge_label})[/]"
            )
            candidates.append((repo_root, branch_name, size_kb))

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
    else:
        console.print(
            f"Scanned {_plural(len(scope), 'repo')}, {_plural(n_worktrees, 'worktree')}: {len(candidates)} removable, "
            f"{n_dirty} dirty, {n_pr} with an open PR, {n_young} under {days}d old{stale_note}."
        )

    if not candidates:
        console.print("Nothing to clean.")
        console.print()
        return

    total_kb = sum(size_kb for _, _, size_kb in candidates if size_kb > 0)
    if total_kb:
        console.print(f"Total reclaimable: {sizes.human_kb(total_kb)} across {_plural(len(candidates), 'worktree')}.")

    if dry_run:
        console.print("Dry run, nothing removed.")
        console.print()
        return

    if not yes:
        console.print()
        if not typer.confirm(f"Remove {_plural(len(candidates), 'worktree')} above?", default=False):
            console.print()
            console.print("Cancelled.")
            console.print()
            raise typer.Exit(1)

    console.print()
    n_removed = 0
    failed: list[str] = []
    for repo_root, branch_name, size_kb in candidates:
        label = "stale reference" if size_kb < 0 else sizes.human_kb(size_kb)
        console.print(f"Removing '{branch_name}' @ {repo_root.name} ({label})...")
        try:
            wt.remove(repo_root, branch_name, yes=True, force_delete=force_delete)
        except (wt.WtNotFoundError, wt.WtCommandError) as exc:
            err.print(f"[red]Error:[/] {exc}")
            failed.append(f"{branch_name} @ {repo_root.name}")
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
