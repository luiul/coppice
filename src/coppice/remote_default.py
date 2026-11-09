"""Fresh remote-default evidence for display, never for removal permission.

Keep this separate from Worktrunk's local merge classification and PR history.
No fetch, ref update, persistent cache, or worktree mutation is needed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from . import gh, git

_REPO_TIMEOUT = 25
_CALL_TIMEOUT = 10
_SHA = re.compile(r"[0-9a-f]{40}")
_SLUG = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class HeadEvidence:
    state: Literal["reached", "pending", "unknown", "unsupported"]
    head: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RemoteDefaultLookup:
    cells: dict[str, HeadEvidence] = field(default_factory=dict)
    branch: str | None = None
    remote_sha: str | None = None
    checked_at: float | None = None
    reason: str | None = None


class _Unavailable(Exception):
    pass


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _Unavailable("invalid commit identity")
    return value


def _path(value: str) -> str:
    return os.path.realpath(value)


class _Reader:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.deadline = time.monotonic() + _REPO_TIMEOUT
        self.ancestry: dict[tuple[str, str], bool] = {}

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable("remote-default check timed out")
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                errors="surrogateescape",
                timeout=min(_CALL_TIMEOUT, remaining),
                env={**git.clean_git_environment(), "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1"},
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise _Unavailable("remote-default command unavailable or timed out") from exc

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run(["git", "-C", str(self.repo), *args])

    def api(self, endpoint: str, *, paginate: bool = False) -> Any:
        args = ["gh", "api", "--hostname", "github.com", "--method", "GET"]
        if paginate:
            args += ["--paginate", "--slurp"]
        proc = self.run([*args, endpoint])
        if proc.returncode != 0:
            raise _Unavailable("GitHub evidence unavailable (check gh auth and network)")
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise _Unavailable("invalid GitHub response") from exc

    def inventory(self) -> dict[str, tuple[str | None, str]]:
        proc = self.git("worktree", "list", "--porcelain", "-z")
        if proc.returncode != 0:
            raise _Unavailable("worktree identity unavailable")
        found: dict[str, tuple[str | None, str]] = {}
        path: str | None = None
        branch: str | None = None
        head: str | None = None
        for token in proc.stdout.split("\0"):
            if token.startswith("worktree "):
                path, branch, head = _path(token[9:]), None, None
            elif token.startswith("HEAD "):
                head = _sha(token[5:])
            elif token.startswith("branch "):
                ref = token[7:]
                if not ref.startswith("refs/heads/"):
                    raise _Unavailable("invalid worktree branch")
                branch = ref[11:]
            elif token == "" and path is not None:
                if head is None or path in found:
                    raise _Unavailable("ambiguous worktree identity")
                found[path] = (branch, head)
                path = None
        if path is not None or not found:
            raise _Unavailable("incomplete worktree identity")
        return found

    def ancestor(self, slug: str, source: str, target: str) -> bool:
        key = (source, target)
        if key in self.ancestry:
            return self.ancestry[key]
        if source == target:
            self.ancestry[key] = True
            return True
        # A positive local path proves ancestry even in a shallow clone.
        # A negative result needs complete history, otherwise use GitHub.
        grafts = self.git("rev-parse", "--git-path", "info/grafts")
        if grafts.returncode != 0:
            raise _Unavailable("Git history configuration unavailable")
        graft_path = Path(grafts.stdout.strip())
        if not graft_path.is_absolute():
            graft_path = self.repo / graft_path
        local = self.git("merge-base", "--is-ancestor", source, target)
        if not graft_path.exists() and local.returncode == 0:
            result = True
        else:
            shallow = self.git("rev-parse", "--is-shallow-repository")
            if (
                not graft_path.exists()
                and local.returncode == 1
                and shallow.returncode == 0
                and shallow.stdout.strip() == "false"
            ):
                result = False
            else:
                data = self.api(f"repos/{slug}/compare/{source}...{target}?per_page=1")
                if not isinstance(data, dict):
                    raise _Unavailable("invalid ancestry response")
                expected_url = f"https://api.github.com/repos/{slug}/compare/{source}...{target}"
                if not isinstance(data.get("url"), str) or data["url"].casefold() != expected_url.casefold():
                    raise _Unavailable("ancestry target identity mismatch")
                base = data.get("base_commit")
                merge_base = data.get("merge_base_commit")
                if not isinstance(base, dict) or not isinstance(merge_base, dict) or _sha(base.get("sha")) != source:
                    raise _Unavailable("ancestry identity mismatch")
                common = _sha(merge_base.get("sha"))
                ahead, behind, status = data.get("ahead_by"), data.get("behind_by"), data.get("status")
                if type(ahead) is not int or type(behind) is not int or min(ahead, behind) < 0:
                    raise _Unavailable("invalid ancestry counts")
                expected = "diverged" if ahead and behind else "ahead" if ahead else "behind" if behind else "identical"
                if status != expected or status == "identical" or (behind == 0) != (common == source):
                    raise _Unavailable("inconsistent ancestry response")
                result = behind == 0
        self.ancestry[key] = result
        return result

    def merged_head(self, slug: str, data: Any, branch: str, head: str, target: str) -> bool:
        if not isinstance(data, dict):
            raise _Unavailable("invalid merge evidence")
        base, pr_head = data.get("base"), data.get("head")
        if not isinstance(base, dict) or not isinstance(pr_head, dict):
            raise _Unavailable("missing PR identity")
        base_repo, head_repo = base.get("repo"), pr_head.get("repo")
        if (
            type(data.get("number")) is not int
            or data["number"] <= 0
            or data.get("html_url", "").casefold() != f"https://github.com/{slug}/pull/{data['number']}".casefold()
            or not isinstance(base_repo, dict)
            or not isinstance(head_repo, dict)
            or base_repo.get("full_name", "").casefold() != slug.casefold()
            or head_repo.get("full_name", "").casefold() != slug.casefold()
        ):
            raise _Unavailable("PR repository identity mismatch")
        state, merged_at = data.get("state"), data.get("merged_at")
        if state not in ("closed", "open") or "merged_at" not in data:
            raise _Unavailable("missing PR merge state")
        if not isinstance(base.get("ref"), str) or not base["ref"]:
            raise _Unavailable("missing PR target")
        pr_sha = _sha(pr_head.get("sha"))
        if state == "open":
            if merged_at is not None:
                raise _Unavailable("inconsistent PR merge state")
            return False
        if merged_at is None:
            return False
        if not isinstance(merged_at, str) or not merged_at:
            raise _Unavailable("invalid PR merge state")
        merge_sha = _sha(data.get("merge_commit_sha"))
        if base["ref"] != branch or pr_sha != head:
            return False
        return self.ancestor(slug, merge_sha, target)

    def merged_commit(self, slug: str, branch: str, head: str, target: str) -> bool:
        # Commit association survives branch renames. Pagination is complete,
        # and only exact original PR heads count as rewritten-history proof.
        pages = self.api(f"repos/{slug}/commits/{head}/pulls?per_page=100", paginate=True)
        if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
            raise _Unavailable("incomplete commit PR response")
        failure: _Unavailable | None = None
        for page in pages:
            for data in page:
                try:
                    if self.merged_head(slug, data, branch, head, target):
                        return True
                except (_Unavailable, AttributeError, TypeError, ValueError) as exc:
                    failure = exc if isinstance(exc, _Unavailable) else _Unavailable("invalid PR merge evidence")
        if failure is not None:
            raise failure
        return False


def lookup(repo: Path, entries: Iterable[dict[str, Any]]) -> RemoteDefaultLookup:
    """Check listed active branch HEADs against a fresh origin default snapshot.

    Dirty files do not change the result. Re-read checkout identities after
    checking evidence so a moved HEAD cannot inherit an earlier result.
    """
    wanted = [
        w
        for w in entries
        if not w.get("is_main")
        and w.get("branch")
        and w.get("path")
        and w.get("worktree", {}).get("state") != "prunable"
    ]
    if not wanted:
        return RemoteDefaultLookup()
    reader = _Reader(repo)
    cells: dict[str, HeadEvidence] = {}
    branch: str | None = None
    target: str | None = None
    checked_at: float | None = None
    try:
        remote = reader.git("remote", "get-url", "origin")
        if remote.returncode == 2:
            return RemoteDefaultLookup({w["path"]: HeadEvidence("unsupported") for w in wanted})
        if remote.returncode != 0:
            raise _Unavailable("origin lookup failed")
        match = gh._DISPLAY_REMOTE_RE.fullmatch(remote.stdout.strip())
        if match is None:
            return RemoteDefaultLookup({w["path"]: HeadEvidence("unsupported") for w in wanted})
        if shutil.which("gh") is None:
            raise _Unavailable("gh not installed")
        metadata = reader.api(f"repos/{match.group('slug')}")
        if not isinstance(metadata, dict):
            raise _Unavailable("invalid repository response")
        slug, default_branch = metadata.get("full_name"), metadata.get("default_branch")
        if (
            not isinstance(slug, str)
            or not _SLUG.fullmatch(slug)
            or not isinstance(default_branch, str)
            or not default_branch
        ):
            raise _Unavailable("missing remote default branch")
        branch = default_branch
        commit = reader.api(f"repos/{slug}/commits/{quote(branch, safe='')}")
        if not isinstance(commit, dict):
            raise _Unavailable("invalid remote commit response")
        target = _sha(commit.get("sha"))
        checked_at = time.time()
        initial = reader.inventory()
        heads: dict[str, HeadEvidence] = {}
        for w in wanted:
            path, local_branch = w["path"], w["branch"]
            identity = initial.get(_path(path))
            if (
                identity is None
                or identity[0] != local_branch
                or (w.get("commit", {}).get("sha") is not None and w["commit"]["sha"] != identity[1])
            ):
                cells[path] = HeadEvidence("unknown", reason="worktree branch changed or unavailable")
                continue
            head = identity[1]
            if head in heads:
                cells[path] = heads[head]
                continue
            try:
                if reader.ancestor(slug, head, target):
                    evidence = HeadEvidence("reached", head)
                else:
                    reached = reader.merged_commit(slug, branch, head, target)
                    evidence = HeadEvidence("reached" if reached else "pending", head)
                heads[head] = evidence
                cells[path] = evidence
            except _Unavailable as exc:
                cells[path] = HeadEvidence("unknown", head, str(exc))
        final = reader.inventory()
        for w in wanted:
            path = w["path"]
            if initial.get(_path(path)) != final.get(_path(path)):
                cells[path] = HeadEvidence("unknown", reason="worktree HEAD changed during check")
        reason = next((cell.reason for cell in cells.values() if cell.state == "unknown"), None)
        return RemoteDefaultLookup(cells, branch, target, checked_at, reason)
    except (_Unavailable, AttributeError, TypeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, _Unavailable) else "invalid remote-default evidence"
        return RemoteDefaultLookup(
            {w["path"]: HeadEvidence("unknown", reason=reason) for w in wanted}, branch, target, checked_at, reason
        )


def revalidate(repo: Path, entries: Iterable[dict[str, Any]], lookup: RemoteDefaultLookup) -> RemoteDefaultLookup:
    """Invalidate moved checkouts after sizing and modification-time collection."""
    if not any(cell.head for cell in lookup.cells.values()):
        return lookup
    cells = dict(lookup.cells)
    try:
        inventory = _Reader(repo).inventory()
        for w in entries:
            path = w.get("path", "")
            cell = cells.get(path)
            if cell is not None and cell.head and inventory.get(_path(path)) != (w.get("branch"), cell.head):
                cells[path] = HeadEvidence("unknown", reason="worktree HEAD changed before display")
    except (_Unavailable, UnicodeError) as exc:
        cells = {path: HeadEvidence("unknown", reason=str(exc)) if cell.head else cell for path, cell in cells.items()}
    reason = next((cell.reason for cell in cells.values() if cell.state == "unknown"), None)
    return RemoteDefaultLookup(cells, lookup.branch, lookup.remote_sha, lookup.checked_at, reason)
