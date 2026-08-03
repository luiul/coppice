"""coppice: a path-based CLI for git worktrees, built on top of `wt` (worktrunk).

`coppice` is the planned Python replacement for dotfiles' zsh `wtx` entrypoint
(https://github.com/luiul/dotfiles/issues/6). Every subcommand takes an
explicit PATH instead of relying on the current working directory, and `wt`
itself stays the source of truth for worktree paths, hooks, and herdr
registration, `coppice` only shells out to it and to `git`.

Note: `coppice` is a plain executable, not a shell function, so it cannot
change your shell's working directory the way `wt`'s own shell integration
does. `coppice new` prints the resulting path; `cd` into it yourself, or see
https://github.com/luiul/coppice/issues for planned shell integration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from coppice import branch as branch_mod
from coppice import repo, wt

APP_HELP = """\
Path-based CLI for git worktrees, built on top of [bold]wt[/] (worktrunk).

Every subcommand takes an explicit repo PATH instead of relying on the
current working directory.

[bold]Quickstart[/bold]

  [cyan]coppice new ./tardis[/cyan]     Create/reuse a worktree for the repo at ./tardis
  [cyan]coppice new .[/cyan]            ...for the repo you're standing in
  [cyan]coppice list[/cyan]             List worktrees across every known repo
  [cyan]coppice remove my-branch[/cyan] Remove a worktree by branch name
"""

app = typer.Typer(
    help=APP_HELP,
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode="rich",
)

console = Console()
err = Console(stderr=True)


def _fail(message: str) -> typer.Exit:
    err.print(f"[red]Error:[/] {message}")
    return typer.Exit(1)


def _print_existing_worktrees(repo_root: Path) -> None:
    """Show what's already in flight before prompting for a new branch,
    to avoid accidentally starting a near-duplicate of existing work.
    """
    worktrees = wt.list_worktrees(repo_root)
    others = [w for w in worktrees if not w.get("is_main") and not w.get("is_current")]
    if not others:
        return
    console.print(f"Existing worktrees for [bold]{repo_root.name}[/]:")
    for w in others:
        console.print(f"  {w.get('branch')}")


@app.command("new")
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
    console.print(f"{verb} worktree for [bold]{branch}[/] @ [green]{result.get('path')}[/]")


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


def _age_days(entry: dict[str, Any]) -> str:
    import time

    if entry.get("worktree", {}).get("state") == "prunable":
        return "stale"

    now = time.time()
    if not entry.get("is_main") and (creation_ts := _creation_ts(Path(entry["path"]))) is not None:
        return f"{int((now - creation_ts) / 86400)}d"

    ts = entry.get("commit", {}).get("timestamp") or 0
    if not ts:
        return "?"
    return f"{int((now - ts) / 86400)}d"


def _render_repo_worktrees(repo_root: Path, worktrees: list[dict[str, Any]]) -> int:
    if not worktrees:
        return 0

    console.print()
    console.print(f"[bold]{repo_root.name}[/]:")
    for w in worktrees:
        tags = []
        if w.get("is_main"):
            tags.append("[dim][main][/]")
        if w.get("is_current"):
            tags.append("[green][current][/]")

        wtree = w.get("working_tree", {})
        dirty = any(wtree.get(k) for k in ("staged", "modified", "untracked", "deleted", "renamed"))
        flags = []
        if dirty:
            flags.append("dirty")
        if w.get("main_state") in ("empty", "integrated"):
            flags.append("merged")

        tag_str = f" {' '.join(tags)}" if tags else ""
        flag_str = f" ({', '.join(flags)})" if flags else ""
        console.print(f"  {w.get('branch')}  {_age_days(w)}{tag_str}{flag_str}")

    return len(worktrees)


@app.command("list")
def cmd_list(
    path: Annotated[
        str | None,
        typer.Argument(help="Only list this repo. Omit to list every known repo plus the one you're standing in."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit raw JSON, tagged per item with a 'repo' field.")
    ] = False,
) -> None:
    """List worktrees across every known repo, or just PATH."""
    try:
        repos = repo.scope_repos(path)
    except repo.RepoResolutionError as exc:
        raise _fail(str(exc)) from exc

    if not repos:
        raise _fail("no known repos. Run 'coppice new' at least once, or pass a PATH.")

    if as_json:
        import json

        merged: list[dict[str, Any]] = []
        for repo_root in repos:
            for entry in wt.list_worktrees(repo_root):
                merged.append({**entry, "repo": repo_root.name})
        console.print(json.dumps(merged))
        return

    total = 0
    for repo_root in repos:
        total += _render_repo_worktrees(repo_root, wt.list_worktrees(repo_root))

    console.print()
    console.print(f"Total: {total} worktree(s) across {len(repos)} repo(s).")


@app.command("remove")
def cmd_remove(
    branches: Annotated[list[str], typer.Argument(help="Branch name(s) to remove.")],
    repo_path: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-C",
            help="Scope to this repo. Defaults to the repo you're standing in, or every known repo.",
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
    a bare positional would be ambiguous with the BRANCH list.
    """
    try:
        scope = repo.scope_repos(repo_path)
    except repo.RepoResolutionError as exc:
        raise _fail(str(exc)) from exc

    if not scope:
        raise _fail("no known repos. Run 'coppice new' at least once, or pass --repo.")

    # Removable worktrees per repo in scope: non-main, non-current (removing
    # the worktree you're standing in would have to switch away first, same
    # rule `wt` itself enforces).
    removable: dict[Path, set[str]] = {}
    for repo_root in scope:
        removable[repo_root] = {
            w["branch"]
            for w in wt.list_worktrees(repo_root)
            if not w.get("is_main") and not w.get("is_current") and w.get("branch")
        }

    failures: list[str] = []
    for branch_name in branches:
        matches = [r for r in scope if branch_name in removable[r]]
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

    if failures:
        raise _fail(f"failed to remove: {', '.join(failures)}")

    console.print(f"Removed {len(branches)} worktree(s).")
