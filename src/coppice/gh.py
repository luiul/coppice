"""Best-effort GitHub PR lookups via the `gh` CLI.

Used by `coppice clean`'s safety rail that skips branches with an open PR.
Entirely optional: every public function returns an empty/None result rather
than raising when `gh` isn't installed, the repo has no GitHub remote, or
the lookup itself fails for any reason, an open-PR check should never block
a local worktree cleanup.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/](?P<slug>[^/]+/[^/.]+?)(?:\.git)?$")


def repo_slug(repo: Path) -> str | None:
    """Best-effort 'owner/repo' slug from REPO's `origin` remote, or None."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    match = _GITHUB_REMOTE_RE.search(proc.stdout.strip())
    return match.group("slug") if match else None


def open_prs(repo: Path, branches: Iterable[str]) -> dict[str, str]:
    """{branch: '#123 Title'} for every BRANCHES entry with a currently-open
    PR against REPO's GitHub repo.

    One `gh pr list` call for the whole batch, rather than one per branch:
    each `gh` invocation is a network round trip to the GitHub API, so
    checking N candidate branches one at a time (as `coppice clean` used to)
    makes wall time scale with N. Listing every open PR once and matching
    locally by head branch keeps it at one round trip per repo regardless of
    how many worktrees are being scanned.

    Returns {} if `gh` isn't installed, REPO has no GitHub remote, the
    lookup fails, or there are no open PRs among BRANCHES, an open-PR check
    should never block a local worktree cleanup.
    """
    wanted = set(branches)
    if not wanted or shutil.which("gh") is None:
        return {}
    slug = repo_slug(repo)
    if slug is None:
        return {}
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--json",
            "number,title,headRefName",
            "--limit",
            "1000",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        prs = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    result: dict[str, str] = {}
    for pr in prs:
        head = pr.get("headRefName")
        if head in wanted:
            result[head] = f"#{pr['number']} {pr['title']}"
    return result
