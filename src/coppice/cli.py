"""coppice: a path-based CLI for git worktrees, built on top of `wt` (worktrunk).

Design rationale in https://github.com/luiul/dotfiles/issues/6. Every
subcommand takes an explicit PATH instead of relying on the current working
directory, and `wt` itself stays the source of truth for worktree paths,
hooks, and herdr registration, `coppice` only shells out to it and to `git`.

Note: `coppice` is a plain executable, not a shell function, so it cannot
change your shell's working directory on its own the way `wt`'s own shell
integration does. Run `eval "$(coppice shell init zsh)"` in your shell rc
file (see `coppice shell init --help`) to get the same behavior: `coppice
new` will then `cd` you into the resulting worktree.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from importlib.metadata import PackageNotFoundError, version
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

[bold]Requires wt (worktrunk) on PATH[/], see https://worktrunk.dev. Bare
[cyan]coppice PATH[/] is shorthand for [cyan]coppice new PATH[/].

Run [cyan]eval "$(coppice shell init zsh)"[/cyan] in your shell rc file so
[cyan]coppice new[/cyan] can `cd` you into the resulting worktree.
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


console = Console()
err = Console(stderr=True)


def _fail(message: str) -> typer.Exit:
    err.print(f"[red]Error:[/] {message}")
    return typer.Exit(1)


def _print_existing_worktrees(repo_root: Path) -> None:
    """Show what's already in flight before prompting for a new branch,
    to avoid accidentally starting a near-duplicate of existing work.

    Skips the (slow, directory-walking) size column here: this preview runs
    on every 'coppice new' before the user's even typed a branch name, so it
    stays fast rather than complete.
    """
    worktrees = wt.list_worktrees(repo_root)
    others = [w for w in worktrees if not w.get("is_main") and not w.get("is_current")]
    if not others:
        return
    console.print(f"Existing worktrees for [bold]{repo_root.name}[/]:")
    table, _total_kb = _worktrees_table(others, show_size=False)
    console.print(table)


@app.command("new", rich_help_panel="Worktrees")
def cmd_new(
    path: Annotated[str, typer.Argument(help="Repo to create/reuse a worktree in.")] = ".",
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Branch name. Prompts for a short description if omitted."),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option("--base", "-B", help="Base branch/ref to create from. Defaults to wt's own default branch."),
    ] = None,
) -> None:
    """Create or reuse a worktree for the repo at PATH.

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

    _print_existing_worktrees(repo_root)

    if branch is None:
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

    create = not wt.branch_exists(repo_root, branch)
    try:
        result = wt.switch(repo_root, branch, create=create, base=base)
    except (wt.WtNotFoundError, wt.WtCommandError) as exc:
        raise _fail(str(exc)) from exc

    repo.register_repo(repo_root)

    verb = "Created" if result.get("action") == "created" else "Reused"
    result_path = result.get("path")
    console.print(f"{verb} worktree for [bold]{branch}[/] @ [green]{result_path}[/]")

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
    if entry.get("worktree", {}).get("state") == "prunable":
        return None
    if not entry.get("is_main") and (creation_ts := _creation_ts(Path(entry["path"]))) is not None:
        return time.time() - creation_ts
    ts = entry.get("commit", {}).get("timestamp") or 0
    if not ts:
        return None
    return time.time() - ts


def _age_days(entry: dict[str, Any]) -> str:
    if entry.get("worktree", {}).get("state") == "prunable":
        return "stale"
    seconds = _age_seconds(entry)
    return f"{int(seconds / 86400)}d" if seconds is not None else "?"


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
    if entry.get("worktree", {}).get("state") == "prunable":
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
    return [
        Path(path) for w in worktrees if w.get("worktree", {}).get("state") != "prunable" and (path := w.get("path"))
    ]


def _merge_status(entry: dict[str, Any]) -> tuple[str, str]:
    """(label, rich style) for ENTRY's merge status against main.

    Doesn't apply to the main worktree itself (nothing to merge it into) or
    a prunable/stale entry (its branch's relationship to main is moot once
    the worktree directory is already gone).
    """
    if entry.get("is_main") or entry.get("worktree", {}).get("state") == "prunable":
        return "-", "dim"
    main_state = entry.get("main_state")
    if main_state in ("empty", "integrated"):
        return "merged", "green"
    if main_state == "ahead":
        return "unmerged", "yellow"
    return "unknown", "dim"


def _worktrees_table(
    worktrees: list[dict[str, Any]], *, show_size: bool = True, size_cache: dict[Path, int] | None = None
) -> tuple[Table, int]:
    """Rich table for WORKTREES: branch, [main]/[current] tags, age,
    optionally on-disk size, working-tree cleanliness, and merge status.

    Shared by `list`'s per-repo rendering and `new`'s pre-prompt "here's
    what's already in flight" preview, so a worktree looks the same wherever
    `coppice` shows one. Returns the table plus the summed on-disk size in
    KB (0 when SHOW_SIZE is False or every entry's size is unknown), so
    callers can roll up a total without walking each worktree's directory
    a second time.
    """
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", pad_edge=False)
    table.add_column("Branch")
    table.add_column("Tags")
    table.add_column("Age", justify="right")
    if show_size:
        table.add_column("Size", justify="right")
    table.add_column("Working tree")
    table.add_column("Merge")

    total_kb = 0
    for w in worktrees:
        tags = []
        if w.get("is_main"):
            tags.append("[dim]main[/]")
        if w.get("is_current"):
            tags.append("[green]current[/]")

        branch = w.get("branch") or "?"
        branch_cell = f"[bold green]{branch}[/]" if w.get("is_current") else branch

        working_tree = "[yellow]dirty[/]" if _is_dirty(w) else "[dim]clean[/]"
        merge_label, merge_style = _merge_status(w)

        row = [branch_cell, " ".join(tags), _age_days(w)]
        if show_size:
            size_kb = _worktree_size_kb(w, size_cache)
            total_kb += size_kb or 0
            row.append(sizes.human_kb(size_kb) if size_kb is not None else "-")
        row += [working_tree, f"[{merge_style}]{merge_label}[/]"]
        table.add_row(*row)

    return table, total_kb


def _render_repo_worktrees(
    repo_root: Path,
    worktrees: list[dict[str, Any]],
    *,
    show_size: bool = True,
    size_cache: dict[Path, int] | None = None,
) -> tuple[int, int]:
    """Print ENTRY's table under a repo-name heading. Returns (worktree
    count, summed on-disk size in KB) for the caller's running total.
    """
    if not worktrees:
        return 0, 0

    table, total_kb = _worktrees_table(worktrees, show_size=show_size, size_cache=size_cache)
    console.print()
    console.print(f"[bold]{repo_root.name}[/]")
    console.print(table)
    return len(worktrees), total_kb


@app.command("list", rich_help_panel="Worktrees")
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
) -> None:
    """List worktrees across every known repo, or just PATH."""
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

        merged: list[dict[str, Any]] = []
        for repo_root in repos:
            for entry in wt.list_worktrees(repo_root):
                merged.append({**entry, "repo": repo_root.name})
        console.print(json.dumps(merged))
        return

    # `wt list` itself (one subprocess per repo) stays sequential, it's
    # already fast; the slow part is on-disk sizing, which walks every
    # worktree's directory. Fetch every repo's worktrees first, then size
    # them all in one batch (with progress on the spinner) so that walk
    # happens in parallel across every worktree in every repo instead of
    # one at a time.
    with console.status("[dim]Listing worktrees…[/dim]") as spinner:
        worktrees_by_repo: dict[Path, list[dict[str, Any]]] = {}
        for repo_root in repos:
            spinner.update(f"[dim]Listing worktrees for {repo_root.name}…[/dim]")
            worktrees_by_repo[repo_root] = wt.list_worktrees(repo_root)

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

    total = 0
    total_kb = 0
    for repo_root in repos:
        n, size_kb = _render_repo_worktrees(
            repo_root, worktrees_by_repo[repo_root], show_size=show_size, size_cache=size_cache
        )
        total += n
        total_kb += size_kb

    console.print()
    summary = f"Total: {total} worktree(s) across {len(repos)} repo(s)."
    if show_size and total_kb:
        summary += f" {sizes.human_kb(total_kb)} on disk."
    console.print(summary)


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
        f"{i}\t{w['branch']}  @ {repo_root.name}  ({_age_days(w)}{', dirty' if _is_dirty(w) else ''})"
        for i, (repo_root, w) in enumerate(candidates)
    ]
    proc = subprocess.run(
        ["fzf", "--prompt=Remove worktree(s)> ", "--height=~50%", "--multi", "--delimiter=\t", "--with-nth=2.."],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        console.print("Cancelled.")
        return None

    picked = [int(line.split("\t", 1)[0]) for line in proc.stdout.splitlines() if line.strip()]
    return [candidates[i][1]["branch"] for i in picked]


@app.command("remove", rich_help_panel="Worktrees")
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
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip wt's own confirmation prompt.")] = False,
    force: Annotated[bool, typer.Option("--force", "-f", help="Remove even with uncommitted changes.")] = False,
    force_delete: Annotated[
        bool, typer.Option("--force-delete", "-D", help="Also delete the branch if unmerged.")
    ] = False,
) -> None:
    """Remove one or more worktrees by branch name.

    PATH is `--repo/-C` here (not a bare positional like `new`/`list`), since
    a bare positional would be ambiguous with the BRANCH list. Omit BRANCH
    entirely for an fzf multi-select picker scoped the same way.
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
    # rule `wt` itself enforces).
    removable: dict[Path, list[dict[str, Any]]] = {}
    for repo_root in scope:
        removable[repo_root] = [
            w
            for w in wt.list_worktrees(repo_root)
            if not w.get("is_main") and not w.get("is_current") and w.get("branch")
        ]

    if not branches:
        branches = _pick_branches_interactively(scope, removable)
        if not branches:
            raise typer.Exit(1)

    failures: list[str] = []
    n_removed = 0
    for branch_name in branches:
        matches = [r for r in scope if any(w["branch"] == branch_name for w in removable[r])]
        if not matches:
            err.print(f"[red]Error:[/] no worktree for branch '{branch_name}' found in scope.")
            failures.append(branch_name)
            continue
        if len(matches) > 1:
            err.print(f"[red]Error:[/] branch '{branch_name}' exists in multiple repos, disambiguate with --repo:")
            for m in matches:
                err.print(f"  {m}")
            failures.append(branch_name)
            continue

        target = matches[0]
        console.print(f"Removing '{branch_name}' @ {target.name}...")
        try:
            wt.remove(target, branch_name, yes=yes, force=force, force_delete=force_delete)
        except (wt.WtNotFoundError, wt.WtCommandError) as exc:
            err.print(f"[red]Error:[/] {exc}")
            failures.append(branch_name)
        else:
            n_removed += 1

    if failures:
        err.print(f"[red]Removed {n_removed} worktree(s), {len(failures)} failed:[/]")
        for f in failures:
            err.print(f"  - {f}")
        raise typer.Exit(1)

    console.print(f"Removed {n_removed} worktree(s).")


def _merge_label(entry: dict[str, Any], *, force_delete: bool) -> str:
    main_state = entry.get("main_state")
    if main_state in ("empty", "integrated"):
        return "merged, branch will be deleted"
    label = "unmerged, branch will be kept" if main_state == "ahead" else "merge status unknown, branch will be kept"
    if force_delete and label.endswith("kept"):
        return "unmerged, -D will delete the branch too"
    return label


@app.command("clean", rich_help_panel="Worktrees")
def cmd_clean(
    weeks: Annotated[int, typer.Argument(help="Remove worktrees older than this many weeks.")] = 2,
    repo_path: Annotated[
        str | None,
        typer.Option(
            "--repo", "-C", help="Scope to this repo. Defaults to the registry plus the repo you're standing in."
        ),
    ] = None,
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
    """Bulk-remove worktrees older than WEEKS (default: 2).

    Scope is the shared registry plus the repo you're standing in, same as
    'coppice list'/'coppice remove' default, or just --repo/-C PATH.

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

    threshold_seconds = weeks * 7 * 86400
    have_gh = shutil.which("gh") is not None

    console.print(f"Scanning {len(scope)} repo(s) for worktrees older than {weeks}w...")

    # (repo_root, branch, size_kb); size_kb is -1 for a stale/dangling entry
    # (its directory is already gone, there's nothing to size).
    candidates: list[tuple[Path, str, int]] = []
    n_worktrees = n_young = n_dirty = n_pr = n_stale = 0

    for repo_root in scope:
        others = [
            w
            for w in wt.list_worktrees(repo_root)
            if not w.get("is_main") and not w.get("is_current") and w.get("branch")
        ]
        if not others:
            continue

        lines: list[str] = []
        for w in others:
            n_worktrees += 1
            branch_name = w["branch"]

            if w.get("worktree", {}).get("state") == "prunable":
                n_stale += 1
                lines.append(
                    f"  [green]rm[/]    stale  {branch_name}  (worktree directory is gone; cleaning up the dangling reference)"
                )
                candidates.append((repo_root, branch_name, -1))
                continue

            seconds = _age_seconds(w)
            if seconds is None:
                if verbose:
                    lines.append(f"  keep  ?     {branch_name}  (age unknown)")
                continue
            age_days = int(seconds / 86400)

            if seconds < threshold_seconds:
                n_young += 1
                if verbose:
                    lines.append(f"  keep  {age_days}d  {branch_name}  (younger than {weeks}w)")
                continue

            if _is_dirty(w):
                n_dirty += 1
                lines.append(f"  [yellow]skip[/]  {age_days}d  {branch_name}  (uncommitted changes)")
                continue

            if have_gh and (pr_info := gh.open_pr(repo_root, branch_name)):
                n_pr += 1
                lines.append(f"  [yellow]skip[/]  {age_days}d  {branch_name}  (open PR {pr_info})")
                continue

            size_kb = sizes.dir_size_kb(Path(w["path"])) if w.get("path") else 0
            merge_label = _merge_label(w, force_delete=force_delete)
            lines.append(
                f"  [green]rm[/]    {age_days}d  {branch_name}  ({sizes.human_kb(size_kb)} on disk, {merge_label})"
            )
            candidates.append((repo_root, branch_name, size_kb))

        if lines:
            console.print()
            console.print(f"[bold]{repo_root.name}[/]:")
            for line in lines:
                console.print(line)

    console.print()
    stale_note = f", {n_stale} stale (dangling) reference(s)" if n_stale else ""
    console.print(
        f"Scanned {len(scope)} repo(s), {n_worktrees} worktree(s): {len(candidates)} removable, "
        f"{n_dirty} dirty, {n_pr} with an open PR, {n_young} under {weeks}w old{stale_note}."
    )

    if not candidates:
        console.print("Nothing to clean.")
        return

    total_kb = sum(size_kb for _, _, size_kb in candidates if size_kb > 0)
    console.print(f"Total reclaimable: {sizes.human_kb(total_kb)} across {len(candidates)} worktree(s).")

    if dry_run:
        console.print("Dry run, nothing removed.")
        return

    if not yes and not typer.confirm(f"Remove {len(candidates)} worktree(s) above?", default=False):
        console.print("Cancelled.")
        raise typer.Exit(1)

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
        console.print(f"Removed {n_removed} worktree(s).")
    else:
        err.print(f"[red]Removed {n_removed} worktree(s), {len(failed)} failed:[/]")
        for f in failed:
            err.print(f"  - {f}")
        raise typer.Exit(1)


@app.command("status", rich_help_panel="Setup & diagnostics")
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

    Deliberately minimal and generic: no project-specific tool checks (e.g.
    herdr, code-review-graph) here, those belong outside coppice for
    whichever project cares about them.
    """
    wt_path = shutil.which("wt")
    if wt_path is not None:
        version_proc = subprocess.run(["wt", "--version"], capture_output=True, text=True)
        wt_version = version_proc.stdout.strip() or version_proc.stderr.strip() or "unknown version"
        console.print(f"wt: [green]found[/] ({wt_version}) @ {wt_path}")
    else:
        console.print("wt: [red]not found[/] on PATH. See https://worktrunk.dev")

    console.print()
    known = repo.known_repos()
    if not known:
        console.print(f"Known repos ({repo.REGISTRY_PATH}): none yet. Run 'coppice new' at least once.")
        return

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", pad_edge=False)
    table.add_column("Repo", no_wrap=True)
    table.add_column("Worktrees", justify="right")
    if show_size:
        table.add_column("Size", justify="right")
    table.add_column("Status")

    total = 0
    total_kb = 0
    with console.status("[dim]Checking known repos…[/dim]") as spinner:
        for repo_root in known:
            spinner.update(f"[dim]Checking {_short_path(repo_root)}…[/dim]")

            if not repo_root.exists():
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

            worktrees = wt.list_worktrees(repo_root)
            total += len(worktrees)
            row = [_short_path(repo_root), str(len(worktrees))]
            if show_size:
                sizeable = _sizeable_paths(worktrees)

                def _report_progress(done: int, n_sizeable: int, name: str = repo_root.name) -> None:
                    spinner.update(f"[dim]Sizing {name} ({done}/{n_sizeable})…[/dim]")

                size_cache = sizes.dir_sizes_kb(sizeable, on_progress=_report_progress)
                size_kb = sum(s for w in worktrees if (s := _worktree_size_kb(w, size_cache)) is not None)
                total_kb += size_kb
                row.append(sizes.human_kb(size_kb))
            row.append("[green]ok[/]")
            table.add_row(*row)

        spinner.stop()

    console.print(f"Known repos ({repo.REGISTRY_PATH}):")
    console.print(table)

    if wt_path is not None:
        summary = f"Total: {total} worktree(s) across {len(known)} repo(s)."
        if show_size and total_kb:
            summary += f" {sizes.human_kb(total_kb)} on disk."
        console.print(summary)


shell_app = typer.Typer(help="cd integration for 'coppice new' (see 'coppice shell init --help').")
app.add_typer(shell_app, name="shell", rich_help_panel="Setup & diagnostics")


@shell_app.command("init")
def cmd_shell_init(
    shell_name: Annotated[str, typer.Argument(help="Shell to generate integration for.")] = "zsh",
) -> None:
    """Print a shell function that wraps 'coppice' and 'cd's into worktrees 'coppice new' creates.

    coppice is a plain executable, so it can't change your shell's working
    directory on its own (only a shell function running in the same process
    can). This prints a function that shadows the 'coppice' command: it runs
    the real binary, then 'cd's if 'coppice new' recorded a resulting path.

    Add this to your shell rc file:

        eval "$(coppice shell init zsh)"
    """
    template = shell.TEMPLATES.get(shell_name)
    if template is None:
        raise _fail(f"unsupported shell '{shell_name}'. Supported: {', '.join(sorted(shell.TEMPLATES))}")
    print(template, end="")
