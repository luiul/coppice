"""Best-effort GitHub PR lookups via the `gh` CLI.

Used by `coppice clean`'s safety rail that skips branches with an open PR.
Entirely optional: every public function returns `None` rather than raising
when `gh` isn't installed, the repo has no GitHub remote, or the lookup
itself fails for any reason, an open-PR check should never block a local
worktree cleanup.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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


def open_pr(repo: Path, branch: str) -> str | None:
    """'#123 Title' for BRANCH's open PR against REPO's GitHub repo, or None
    if there isn't one, `gh` isn't installed, or the lookup fails.
    """
    if shutil.which("gh") is None:
        return None
    slug = repo_slug(repo)
    if slug is None:
        return None
    proc = subprocess.run(
        ["gh", "pr", "list", "--repo", slug, "--head", branch, "--state", "open", "--json", "number,title"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        prs = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not prs:
        return None
    pr = prs[0]
    return f"#{pr['number']} {pr['title']}"
