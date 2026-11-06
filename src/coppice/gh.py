"""Best-effort GitHub PR lookups via the `gh` CLI.

`cop list` uses a structured result to distinguish no open PR from an
unavailable lookup. The legacy `open_prs` helper keeps its cleanup contract.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# The repo-name group deliberately allows dots: `[^/.]+?` (excluding dots)
# used to be the pattern, which silently failed to match any GitHub repo
# whose name contains a literal dot (e.g. `owner/my.project`). The
# non-greedy `[^/]+?` here still backtracks correctly to strip an optional
# trailing `.git` suffix, since that's anchored by `(?:\.git)?$`.
_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?$")


# The display lookup accepts only exact GitHub hosts. Keep the legacy
# cleanup parser separate so this feature does not change cleanup policy.
_DISPLAY_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https?://github\.com(?::[0-9]+)?/|ssh://git@github\.com(?::[0-9]+)?/)"
    r"(?P<slug>[A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$",
    re.IGNORECASE,
)
_PR_LOOKUP_TIMEOUT = 15
_REMOTE_TIMEOUT = 5


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


@dataclass(frozen=True)
class PullRequestLookup:
    status: Literal["ok", "unknown", "unsupported"]
    prs: dict[str, tuple[PullRequest, ...]] = field(default_factory=dict)
    reason: str | None = None


def lookup_open_prs(repo: Path, branches: Iterable[str]) -> PullRequestLookup:
    """Fetch every open PR once per repo, including drafts and all pages.

    Match both head repo and branch to avoid collisions with fork branches.
    No persistent cache or cleanup policy changes. Failed or incomplete
    responses never become a successful empty result.
    """
    wanted = set(branches)
    if not wanted:
        return PullRequestLookup("unsupported")
    try:
        remote = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_REMOTE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return PullRequestLookup("unknown", reason="origin lookup failed")
    if remote.returncode != 0:
        # Git uses exit 2 for a missing remote. Other errors are not proof
        # that this repo has no supported remote.
        if remote.returncode == 2:
            return PullRequestLookup("unsupported")
        return PullRequestLookup("unknown", reason="origin lookup failed")
    match = _DISPLAY_REMOTE_RE.fullmatch(remote.stdout.strip())
    if match is None:
        return PullRequestLookup("unsupported")
    slug = match.group("slug")
    if shutil.which("gh") is None:
        return PullRequestLookup("unknown", reason="gh not installed")
    try:
        proc = subprocess.run(
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                "--paginate",
                "--slurp",
                f"repos/{slug}/pulls?state=open&per_page=100",
            ],
            capture_output=True,
            text=True,
            timeout=_PR_LOOKUP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return PullRequestLookup("unknown", reason="GitHub lookup timed out")
    except OSError:
        return PullRequestLookup("unknown", reason="gh could not run")
    if proc.returncode != 0:
        return PullRequestLookup("unknown", reason="GitHub lookup failed (check gh auth and network)")
    try:
        pages = json.loads(proc.stdout)
        if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
            raise ValueError("invalid pages")
        found: dict[str, list[PullRequest]] = {}
        canonical_slug: str | None = None
        for page in pages:
            for pr in page:
                if not isinstance(pr, dict) or pr.get("state") != "open":
                    raise ValueError("invalid PR")
                # The response base repo carries the canonical identity
                # after GitHub follows a repository rename or transfer.
                base = pr.get("base")
                base_repo = base.get("repo") if isinstance(base, dict) else None
                target = base_repo.get("full_name") if isinstance(base_repo, dict) else None
                if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", target):
                    raise ValueError("invalid base repo")
                if canonical_slug is None:
                    canonical_slug = target.casefold()
                elif canonical_slug != target.casefold():
                    raise ValueError("inconsistent base repo")
                number, url, head = pr.get("number"), pr.get("html_url"), pr.get("head")
                if (
                    type(number) is not int
                    or number <= 0
                    or not isinstance(url, str)
                    or url.casefold() != f"https://github.com/{target}/pull/{number}".casefold()
                    or not isinstance(head, dict)
                    or not isinstance(head.get("ref"), str)
                ):
                    raise ValueError("invalid PR fields")
                if "repo" not in head:
                    raise ValueError("missing head repo")
                head_repo = head["repo"]
                # A deleted head repo cannot match the local origin.
                if head_repo is None:
                    continue
                if not isinstance(head_repo, dict) or not isinstance(head_repo.get("full_name"), str):
                    raise ValueError("invalid head repo")
                branch = head["ref"]
                if branch in wanted and head_repo["full_name"].casefold() == canonical_slug:
                    found.setdefault(branch, []).append(PullRequest(number, url))
        return PullRequestLookup(
            "ok", {branch: tuple(sorted(prs, key=lambda pr: pr.number)) for branch, prs in found.items()}
        )
    except (ValueError, TypeError):
        return PullRequestLookup("unknown", reason="invalid GitHub response")


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
