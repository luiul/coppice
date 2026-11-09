"""Atomic worktree overview fields with table and stacked presentations.

One registry drives both layouts and the explanation, so a narrow terminal
never loses facts and terminology does not drift between views.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import gh, list_facts, remote_default, sizes


@dataclass(frozen=True)
class Field:
    label: str
    description: str
    width: int = 12


GROUPS: tuple[tuple[str, tuple[Field, ...]], ...] = (
    (
        "Checkout",
        (
            Field("Worktree type", "Main is the original checkout; Linked is an additional Git worktree.", 8),
            Field("Current", "Whether the command runs from this worktree.", 7),
            Field("Branch", "Checked-out local branch. Detached HEAD has no branch.", 20),
            Field(
                "HEAD state", "Attached means HEAD names a branch; Detached means HEAD points directly to a commit.", 9
            ),
            Field("HEAD commit", "Commit currently checked out in this worktree.", 12),
        ),
    ),
    (
        "Worktree registration",
        (
            Field("Prunable", "Git registration points to a missing worktree directory or .git file.", 9),
            Field("Path mismatch", "Worktrunk reports the path differs from its branch placement rule.", 13),
            Field("Disk usage", "Space used by this worktree directory, not repository object storage.", 10),
            Field("Path", "Full checkout path, with the home directory shortened to ~.", 30),
        ),
    ),
    (
        "Local changes",
        (
            Field("Staged paths", "Paths changed in the index relative to HEAD.", 12),
            Field("Unstaged paths", "Tracked paths changed in the working tree relative to the index.", 14),
            Field("Untracked paths", "Paths not tracked by Git. Ignored files are excluded.", 15),
            Field("Unmerged paths", "Paths with unresolved conflicts in the index, not predicted conflicts.", 14),
            Field("Git operation", "Operation reported by Worktrunk, such as merge or rebase.", 13),
        ),
    ),
    (
        "Activity",
        (
            Field("Directory created", "Filesystem birth time only. No commit-time fallback.", 23),
            Field("HEAD committed", "HEAD committer timestamp, not when this checkout was created.", 23),
            Field("Local file changed", "Newest mtime of changed local files. Not mixed with commit time.", 23),
        ),
    ),
    (
        "Coppice task state",
        (
            Field("Parked", "Whether the stored Coppice parked mark is present.", 7),
            Field("Parked at", "Time the task was marked parked, independent of directory creation.", 23),
            Field("Follow-up", "HEAD commit time is later than the parked mark. This is a task heuristic.", 9),
        ),
    ),
    (
        "Local branch history",
        (
            Field("Commits ahead", "HEAD commits absent from the explicitly named local comparison branch.", 13),
            Field("Commits behind", "Local comparison branch commits absent from HEAD.", 14),
            Field("HEAD contained", "HEAD is reachable from the local comparison branch, including equality.", 14),
        ),
    ),
    (
        "Worktrunk report",
        (
            Field("Reported relation", "Worktrunk relation, not proof of ancestry against local main.", 20),
            Field("Content integrated", "Worktrunk reports equivalent or already represented committed content.", 18),
            Field("Integration method", "Worktrunk's reported content-equivalence method, if present.", 20),
            Field(
                "Predicted conflicts", "Yes means Worktrunk predicts conflicts. Unknown means no explicit proof.", 19
            ),
        ),
    ),
    (
        "Cached remote-tracking branch",
        (
            Field("Tracking remote", "Remote name reported by Worktrunk for the configured tracking branch.", 16),
            Field("Tracking branch", "Branch name reported for the upstream, not the remote default branch.", 20),
            Field(
                "Tracking ahead", "Commits ahead of the cached remote-tracking branch, not a fresh server check.", 14
            ),
            Field("Tracking behind", "Commits behind the cached remote-tracking branch, not a fresh server check.", 15),
        ),
    ),
    (
        "Remote default integration",
        (
            Field(
                "HEAD integration",
                "Fresh remote-default evidence for committed HEAD, not dirty files or deployment.",
                18,
            ),
        ),
    ),
    (
        "Pull requests",
        (
            Field(
                "PR lookup", "Complete, unavailable, unsupported, or not checked. None requires a complete lookup.", 13
            ),
            Field("PR state", "Lifecycle of the primary branch-name match. Not current-HEAD integration proof.", 10),
            Field("PR number", "Primary pull request number, linked where the terminal supports links.", 10),
            Field("Additional PRs", "Number of other matching pull requests, not commits.", 14),
        ),
    ),
)


@dataclass(frozen=True)
class Cell:
    text: str
    style: str = ""

    def rich(self) -> Text:
        return Text(self.text, style=self.style, overflow="fold", no_wrap=False)


UNKNOWN = Cell("Unknown", "yellow")
NA = Cell("N/A", "dim")
NOT_CHECKED = Cell("Not checked", "dim")


def _bool(value: bool | None) -> Cell:
    return UNKNOWN if value is None else Cell("Yes" if value else "No", "cyan" if value else "dim")


def _count(value: int | None) -> Cell:
    return UNKNOWN if value is None else Cell(str(value), "yellow" if value else "dim")


def _time(value: float | None) -> Cell:
    if value is None:
        return UNKNOWN
    try:
        timestamp = dt.datetime.fromtimestamp(value, dt.UTC).astimezone()
        return Cell(timestamp.isoformat(sep=" ", timespec="minutes"))
    except (ValueError, OSError, OverflowError):
        return UNKNOWN


def _path(value: str) -> str:
    home = str(Path.home())
    return "~" + value[len(home) :] if value == home or value.startswith(home + "/") else value


def _fields(
    entry: dict[str, Any],
    facts: list_facts.WorktreeFacts | None,
    pr: gh.PullRequestLookup | None,
    remote: remote_default.RemoteDefaultLookup | None,
    size_cache: dict[Path, int] | None,
    current_path: str | None = None,
) -> dict[str, Cell]:
    """Normalize only display facts; absence is not a negative Git fact."""
    stale = entry.get("worktree", {}).get("state") == "prunable"
    # A failed active snapshot stays unknown. Restoring registration data
    # here would revive evidence deliberately invalidated by revalidation.
    verified = facts is not None and facts.identity_verified
    branch = facts.branch if verified else None if facts else entry.get("branch")
    detached = facts.detached if verified else None if facts else entry.get("worktree", {}).get("detached")
    head = facts.head if verified else None if facts else entry.get("commit", {}).get("sha")
    parked_at = entry.get("parked_at")
    parked = isinstance(parked_at, (int, float)) and not isinstance(parked_at, bool)
    follow_up = None if not facts or facts.commit_time is None else facts.commit_time > parked_at if parked else False
    state = entry.get("main_state")
    relations = {
        "empty": "Same commit",
        "same_commit": "Same commit",
        "integrated": "Different history integrated",
        "ahead": "Ahead",
        "behind": "Behind",
        "diverged": "Diverged",
        "would_conflict": "Would conflict",
        "orphan": "No common ancestor",
        "is_main": "Not compared",
    }
    integrated = True if state in {"empty", "same_commit", "behind", "integrated"} else None
    if state in {"ahead", "diverged", "would_conflict"}:
        integrated = False
    wt_known = "worktree" in entry
    current = current_path == entry.get("path") if current_path is not None else None
    values = {
        "Worktree type": Cell("Main" if entry.get("is_main") else "Linked"),
        "Current": _bool(current),
        "Branch": Cell(branch) if isinstance(branch, str) and branch else NA if detached is True else UNKNOWN,
        "HEAD state": Cell("Detached" if detached else "Attached") if type(detached) is bool else UNKNOWN,
        "HEAD commit": Cell(head[:12]) if isinstance(head, str) and head else UNKNOWN,
        "Prunable": _bool(stale) if wt_known else UNKNOWN,
        "Path mismatch": NA
        if stale
        else _bool(entry.get("worktree", {}).get("state") == "branch_worktree_mismatch")
        if wt_known
        else UNKNOWN,
        "Path": Cell(_path(str(entry.get("path", "")))) if entry.get("path") else UNKNOWN,
        "Disk usage": NA
        if stale
        else Cell(sizes.human_kb(size_cache[Path(entry["path"])]))
        if size_cache is not None
        and Path(entry.get("path", "")) in size_cache
        and size_cache[Path(entry["path"])] is not None
        else UNKNOWN,
        "Staged paths": _count(facts.staged if facts else None),
        "Unstaged paths": _count(facts.unstaged if facts else None),
        "Untracked paths": _count(facts.untracked if facts else None),
        "Unmerged paths": _count(facts.unmerged if facts else None),
        "Git operation": Cell(str(entry["operation_state"]).replace("_", " "))
        if entry.get("operation_state")
        else Cell("None reported", "dim"),
        "Directory created": _time(facts.directory_created if facts else None),
        "HEAD committed": _time(facts.commit_time if facts else None),
        "Local file changed": _time(facts.file_changed if facts else None),
        "Parked": _bool(parked),
        "Parked at": _time(parked_at) if parked else NA,
        "Follow-up": _bool(follow_up) if parked else NA,
        "Commits ahead": _count(facts.local_ahead if facts else None),
        "Commits behind": _count(facts.local_behind if facts else None),
        "HEAD contained": _bool(facts.local_contains if facts else None),
        "Reported relation": Cell("Ancestor contained")
        if state == "integrated" and entry.get("integration_reason") == "ancestor"
        else Cell(relations[state])
        if state in relations
        else UNKNOWN,
        "Content integrated": _bool(integrated) if state != "is_main" else NA,
        "Integration method": Cell("Strict ancestor")
        if entry.get("integration_reason") == "ancestor"
        else Cell(str(entry["integration_reason"]).replace("_", " "))
        if entry.get("integration_reason")
        else NA
        if state not in {"integrated"}
        else UNKNOWN,
        "Predicted conflicts": Cell("Yes", "red")
        if state == "would_conflict"
        else UNKNOWN
        if state != "is_main"
        else NA,
    }
    tracking = entry.get("remote")
    if not isinstance(tracking, dict):
        values.update(
            {label: NA for label in ("Tracking remote", "Tracking branch", "Tracking ahead", "Tracking behind")}
        )
    else:
        for label, key in (("Tracking remote", "name"), ("Tracking branch", "branch")):
            values[label] = Cell(tracking[key]) if isinstance(tracking.get(key), str) and tracking[key] else UNKNOWN
        for label, key in (("Tracking ahead", "ahead"), ("Tracking behind", "behind")):
            value = tracking.get(key)
            values[label] = _count(value if type(value) is int and value >= 0 else None)
    for label in (
        "Staged paths",
        "Unstaged paths",
        "Untracked paths",
        "Unmerged paths",
        "Git operation",
        "Directory created",
        "Local file changed",
        "Commits ahead",
        "Commits behind",
        "HEAD contained",
        "Reported relation",
        "Content integrated",
        "Predicted conflicts",
    ):
        if stale:
            values[label] = NA
    evidence = remote.cells.get(entry.get("path", "")) if remote else None
    remote_states = {
        "reached": Cell("Reached", "green"),
        "pending": Cell("Pending", "cyan"),
        "unknown": UNKNOWN,
        "unsupported": NA,
    }
    values["HEAD integration"] = (
        NA
        if stale or detached is True
        else UNKNOWN
        if evidence
        and evidence.state in {"reached", "pending"}
        and (not verified or not facts or not facts.head or facts.head != evidence.head)
        else remote_states.get(evidence.state, UNKNOWN)
        if evidence
        else NOT_CHECKED
    )
    if not isinstance(branch, str) or not branch:
        cell = NA if detached is True or stale else UNKNOWN
        values.update({"PR lookup": cell, "PR state": cell, "PR number": cell, "Additional PRs": cell})
    elif pr is None or entry.get("is_main"):
        values.update({label: NOT_CHECKED for label in ("PR lookup", "PR state", "PR number", "Additional PRs")})
    elif pr.status != "ok":
        cell = UNKNOWN if pr.status == "unknown" else NA
        values.update({label: cell for label in ("PR lookup", "PR state", "PR number", "Additional PRs")})
    else:
        matches = pr.prs.get(branch, ())
        primary = matches[0] if matches else None
        values.update(
            {
                "PR lookup": Cell("Complete", "dim"),
                "PR state": Cell(
                    primary.state.title(),
                    {"merged": "green", "closed": "dim", "draft": "dim cyan", "open": "cyan"}[primary.state],
                )
                if primary
                else Cell("None", "dim"),
                "PR number": Cell(str(primary.number), f"link {primary.url}") if primary else NA,
                "Additional PRs": _count(len(matches) - 1) if matches else Cell("0", "dim"),
            }
        )
    return values


def _metadata(console: Console, label: str, cell: Cell) -> None:
    text = Text(label + ": ", style="bold")
    text.append_text(cell.rich())
    console.print(text)


def explain(console: Console) -> None:
    console.print("cop list overview", style="bold")
    console.print(
        "Unknown: the fact could not be established. N/A: it does not apply. Not checked: collection was skipped."
    )
    console.print(
        "Times are local timestamps with a UTC offset. Path counts can overlap; renames are counted as old and new paths."
    )
    for title, fields in GROUPS:
        console.print(title, style="bold")
        for field in fields:
            _metadata(console, field.label, Cell(field.description))
    console.print("Worktree ID: a repository-local display identifier repeated across tables and collection issues.")
    console.print("Targets and sources are named separately. Cached refs are not fresh remote evidence.")
    console.print(
        "Local comparison branch: an explicit refs/heads ref selected from cached Worktrunk configuration or cached origin HEAD."
    )
    console.print("Worktrunk comparison target: Unknown when schema 1 does not name its actual comparison ref.")
    console.print("Remote name and remote default branch: separate fields for the server checked during this run.")
    console.print("Observed remote commit and remote checked at: the remote snapshot identity and observation time.")
    console.print("Parked and follow-up are Coppice task facts. Integration reports do not grant removal permission.")


def render(
    console: Console,
    repos: list[Path],
    entries: dict[Path, list[dict[str, Any]]],
    facts: dict[Path, list_facts.RepoFacts],
    *,
    show_all: bool,
    show_size: bool,
    verbose: bool,
    size_cache: dict[Path, int] | None,
    pr_lookups: dict[Path, gh.PullRequestLookup] | None,
    remote_lookups: dict[Path, remote_default.RemoteDefaultLookup] | None,
    current_path: str | None = None,
) -> None:
    main_count = linked_count = hidden = 0
    for root in sorted(repos, key=lambda value: value.name.lower()):
        rows = entries.get(root, [])
        if len(repos) > 1 and not show_all and not any(not row.get("is_main") for row in rows):
            hidden += 1
            continue
        main_count += sum(bool(row.get("is_main")) for row in rows)
        linked_count += sum(not row.get("is_main") for row in rows)
        console.print(Text(root.name, style="bold"))
        _metadata(console, "Repository path", Cell(_path(str(root))))
        local = facts.get(root)
        pr = (pr_lookups or {}).get(root)
        remote = (remote_lookups or {}).get(root)
        _metadata(console, "Local comparison branch", Cell(local.target_ref) if local and local.target_ref else UNKNOWN)
        _metadata(
            console, "Local target selection", Cell(local.target_source) if local and local.target_source else UNKNOWN
        )
        _metadata(
            console, "Local target commit", Cell(local.target_sha[:12]) if local and local.target_sha else UNKNOWN
        )
        _metadata(console, "Worktrunk comparison target", UNKNOWN)
        if remote_lookups is not None:
            _metadata(console, "Remote name", Cell("origin"))
            _metadata(console, "Remote default branch", Cell(remote.branch) if remote and remote.branch else UNKNOWN)
            _metadata(
                console,
                "Observed remote commit",
                Cell(remote.remote_sha[:12]) if remote and remote.remote_sha else UNKNOWN,
            )
            _metadata(console, "Remote checked at", _time(remote.checked_at if remote else None))
        if not rows:
            console.print("No worktree records available.", style="yellow")
            console.print()
            continue
        ordered = sorted(
            rows,
            key=lambda row: (
                not row.get("is_main"),
                row.get("worktree", {}).get("state") != "prunable",
                str(row.get("branch", "")),
                str(row.get("path", "")),
            ),
        )
        row_values = [
            (
                f"W{i + 1}",
                row,
                _fields(
                    row,
                    local.rows.get(row.get("path", "")) if local else None,
                    (pr_lookups or {}).get(root),
                    remote,
                    size_cache,
                    current_path,
                ),
            )
            for i, row in enumerate(ordered)
        ]
        groups = []
        for title, fields in GROUPS:
            chosen = tuple(
                field
                for field in fields
                if (field.label != "Path" or verbose) and (field.label != "Disk usage" or show_size)
            )
            if title == "Pull requests" and pr_lookups is None:
                continue
            if title == "Remote default integration" and remote_lookups is None:
                continue
            if chosen:
                groups.append((title, chosen))
        # Content-aware hard targets decide the layout before rendering. A
        # group that cannot fit switches the whole section to labeled cards.
        required = max(sum(min(field.width, 13) + 2 for field in fields) + 36 for _, fields in groups)
        required = max(required, max(sum(len(field.label) + 2 for field in fields) + 36 for _, fields in groups))
        tables = console.width >= max(100, required)
        if tables:
            for title, fields in groups:
                table = Table(
                    title=title,
                    title_justify="left",
                    box=box.SIMPLE_HEAVY,
                    show_edge=False,
                    pad_edge=False,
                    padding=(0, 1),
                )
                table.add_column("Worktree ID", width=11)
                if title != "Checkout":
                    table.add_column("Branch", width=24, overflow="fold")
                for field in fields:
                    table.add_column(field.label, min_width=len(field.label), overflow="fold")
                for key, _, values in row_values:
                    identity_cells = [] if title == "Checkout" else [values["Branch"].rich()]
                    table.add_row(key, *identity_cells, *(values[field.label].rich() for field in fields))
                console.print(table)
        else:
            for key, _, values in row_values:
                console.print(key, style="bold")
                for title, fields in groups:
                    console.print(title, style="bold dim")
                    for field in fields:
                        _metadata(console, field.label, values[field.label])
                console.print()
        for key, row, _ in row_values:
            row_fact = local.rows.get(row.get("path", "")) if local else None
            if row_fact and row_fact.reason:
                _metadata(console, f"{key} collection issue", Cell(row_fact.reason, "yellow"))
        if pr and pr.reason:
            _metadata(console, "PR lookup issue", Cell(pr.reason, "yellow"))
        if remote and remote.reason:
            _metadata(console, "Remote lookup issue", Cell(remote.reason, "yellow"))
        if local and local.reason:
            _metadata(console, "Local comparison issue", Cell(local.reason, "yellow"))
        console.print()
    console.print(f"Main worktrees: {main_count}")
    console.print(f"Linked worktrees: {linked_count}")
    if hidden:
        console.print(f"Hidden repositories without linked worktrees: {hidden}. Use --all to show them.", style="dim")
    console.print("Use cop list --explain for field definitions.", style="dim")
