"""Bounded local list facts, separate from cleanup policy.

Each repository has a 30 second budget. Each Git call has an eight second
budget. One pool owns at most eight workers, with no nested worktree pools.
Revalidate after other collection to reject moved refs and replaced checkouts.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

_REPO_TIMEOUT = 30.0
_CALL_TIMEOUT = 8.0
_MAX_WORKERS = 8
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_Identity = tuple[str, str | None, bool]
_Location = tuple[str, tuple[int, int], str, tuple[int, int], str, tuple[int, int]]


@dataclass(frozen=True)
class WorktreeFacts:
    path: str
    head: str | None = None
    branch: str | None = None
    detached: bool | None = None
    directory_created: float | None = None
    commit_time: float | None = None
    file_changed: float | None = None
    staged: int | None = None
    unstaged: int | None = None
    untracked: int | None = None
    unmerged: int | None = None
    local_ahead: int | None = None
    local_behind: int | None = None
    local_contains: bool | None = None
    reason: str | None = None
    identity_verified: bool = False


@dataclass(frozen=True)
class RepoFacts:
    rows: dict[str, WorktreeFacts] = field(default_factory=dict)
    target_ref: str | None = None
    target_sha: str | None = None
    target_source: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _Snapshot:
    common: str
    locations: dict[str, _Location]


class _Unavailable(Exception):
    pass


class _Changed(_Unavailable):
    pass


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise _Unavailable("invalid commit identity")
    return value


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape").removesuffix("\n")


def _reason(*values: str | None) -> str | None:
    return "; ".join(dict.fromkeys(value for value in values if value)) or None


def _prunable(entry: dict) -> bool:
    worktree = entry.get("worktree")
    return isinstance(worktree, dict) and worktree.get("state") == "prunable"


def _entry_path(entry: dict) -> str:
    path = entry.get("path")
    return path if isinstance(path, str) else ""


def _fallback(entry: dict, reason: str) -> WorktreeFacts:
    """Keep a supplied name as a label, never turn missing evidence into detached."""
    branch = entry.get("branch")
    branch = branch.removeprefix("refs/heads/") if isinstance(branch, str) and branch else None
    head = None
    commit = entry.get("commit")
    candidate = commit.get("sha") if isinstance(commit, dict) else entry.get("head", entry.get("HEAD"))
    with suppress(_Unavailable):
        head = _sha(candidate)
    return WorktreeFacts(_entry_path(entry), head, branch, False if branch else None, reason=reason)


def _attach(facts: RepoFacts, snapshot: _Snapshot) -> RepoFacts:
    # Internal provenance is not a public dataclass field or a JSON entry field.
    # Keep it on the result, not in a process-global cache that could retain rows.
    object.__setattr__(facts, "_snapshot", snapshot)
    return facts


class _Reader:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.deadline = time.monotonic() + _REPO_TIMEOUT
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(
            GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0"
        )
        self.comparisons: dict[tuple[str, str], tuple[int, int]] = {}

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable("local facts timed out")
        return remaining

    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        timeout = min(_CALL_TIMEOUT, self.remaining())
        try:
            return subprocess.run(
                [
                    "git",
                    "--no-pager",
                    "--no-optional-locks",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.untrackedCache=false",
                    "-c",
                    "log.showSignature=false",
                    "-c",
                    "protocol.allow=never",
                    "-C",
                    str(cwd if cwd is not None else self.repo),
                    *args,
                ],
                capture_output=True,
                timeout=timeout,
                env=self.env,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise _Unavailable("local Git read unavailable or timed out") from exc

    def read(self, *args: str, cwd: Path | None = None) -> bytes:
        proc = self.git(*args, cwd=cwd)
        if proc.returncode != 0:
            raise _Unavailable("local Git read failed")
        return proc.stdout

    def inventory(self) -> dict[str, _Identity]:
        data = self.read("worktree", "list", "--porcelain", "-z")
        found: dict[str, _Identity] = {}
        path: str | None = None
        head: str | None = None
        branch: str | None = None
        detached = stale = False
        for token in data.split(b"\0"):
            if token.startswith(b"worktree "):
                if path is not None:
                    raise _Unavailable("incomplete worktree inventory")
                path, head, branch, detached, stale = os.fsdecode(token[9:]), None, None, False, False
            elif token.startswith(b"HEAD "):
                head = _sha(_text(token[5:]))
            elif token.startswith(b"branch "):
                ref = _text(token[7:])
                if not ref.startswith("refs/heads/") or not ref[11:]:
                    raise _Unavailable("invalid checkout branch")
                branch = ref[11:]
            elif token == b"detached":
                detached = True
            elif token.startswith(b"prunable"):
                stale = True
            elif token == b"" and path is not None:
                if not stale:
                    canonical = os.path.realpath(path)
                    if head is None or (branch is None) != detached or canonical in found:
                        raise _Unavailable("ambiguous worktree identity")
                    found[canonical] = (head, branch, detached)
                path = None
        if path is not None:
            raise _Unavailable("incomplete worktree inventory")
        return found

    def layout(self) -> tuple[str, bool]:
        data = _text(self.read("rev-parse", "--path-format=absolute", "--git-common-dir", "--is-shallow-repository"))
        common, separator, shallow = data.rpartition("\n")
        if not separator or not common or shallow not in ("true", "false"):
            raise _Unavailable("local history configuration unavailable")
        return os.path.realpath(common), shallow == "true"

    def target(self) -> tuple[str, str, str]:
        config = self.git("config", "--null", "--get-all", "worktrunk.default-branch")
        if config.returncode not in (0, 1):
            raise _Unavailable("local target cache unavailable")
        names = config.stdout.split(b"\0")[:-1] if config.returncode == 0 else []
        if config.returncode == 0 and (not config.stdout.endswith(b"\0") or len(set(names)) != 1):
            raise _Unavailable("ambiguous local target config")
        configured = names[0].decode("utf-8", "surrogateescape") if names else None
        if configured is not None and not configured.startswith("refs/heads/"):
            configured = f"refs/heads/{configured}"
        origin = self.git("symbolic-ref", "-q", "refs/remotes/origin/HEAD")
        if origin.returncode not in (0, 1):
            raise _Unavailable("local target cache unavailable")
        cached: str | None = None
        if origin.returncode == 0:
            symbolic = _text(origin.stdout)
            if not symbolic.startswith("refs/remotes/origin/") or not symbolic[20:]:
                raise _Unavailable("invalid cached origin HEAD")
            cached = f"refs/heads/{symbolic[20:]}"
        data = self.read(
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(symref)%00%(objecttype)",
            "refs/heads/",
        )
        refs: dict[str, tuple[str, str, str]] = {}
        for line in data.splitlines():
            parts = line.split(b"\0")
            if len(parts) != 4:
                raise _Unavailable("invalid local target refs")
            name, sha, symbolic, kind = (_text(value) for value in parts)
            refs[name] = (sha, symbolic, kind)
        if configured is not None and cached is not None and configured != cached:
            raise _Unavailable("local target cache sources disagree")
        ref = configured or cached
        if ref is None:
            raise _Unavailable("local target cache missing")
        self.read("check-ref-format", ref)
        if ref not in refs or refs[ref][1] or refs[ref][2] != "commit":
            raise _Unavailable("local target ref unavailable")
        return ref, _sha(refs[ref][0]), "cached config" if configured is not None else "cached origin HEAD"

    def history(self, common: str, shallow: bool, heads: list[str] | None = None) -> None:
        if shallow:
            raise _Unavailable("shallow or incomplete local history")
        self.remaining()
        try:
            os.lstat(Path(common) / "info/grafts")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _Unavailable("local history configuration unavailable") from exc
        else:
            raise _Unavailable("local history has grafts")
        if heads:
            # A symmetric range can be empty despite missing common ancestors.
            # Walk all compared tips once to verify locally complete commit history.
            self.read("rev-list", "--count", "--missing=error", *dict.fromkeys(heads), "--")

    def timestamps(self, entries: list[dict], inventory: dict[str, _Identity]) -> dict[str, float]:
        stamps: dict[str, float] = {}
        heads: set[str] = set()
        for entry in entries:
            if _prunable(entry):
                continue
            identity = inventory.get(os.path.realpath(_entry_path(entry)))
            if identity is None:
                continue
            try:
                _match_entry(entry, identity)
            except _Unavailable:
                continue
            head = identity[0]
            heads.add(head)
            commit = entry.get("commit")
            if isinstance(commit, dict) and commit.get("sha") == head:
                value = commit.get("timestamp")
                if type(value) in (int, float) and math.isfinite(value):
                    stamps[head] = float(value)
        missing = sorted(heads - stamps.keys())
        if missing:
            data = self.read("show", "--no-patch", "--no-walk", "--format=%H%x00%ct", *missing, "--")
            for line in data.splitlines():
                parts = line.split(b"\0")
                if len(parts) != 2:
                    raise _Unavailable("invalid HEAD commit time")
                head = _sha(_text(parts[0]))
                try:
                    stamp = float(int(parts[1]))
                except ValueError as exc:
                    raise _Unavailable("invalid HEAD commit time") from exc
                if head not in missing:
                    raise _Unavailable("HEAD timestamp identity mismatch")
                stamps[head] = stamp
        return stamps

    def compare(self, target: str, head: str) -> tuple[int, int]:
        key = (target, head)
        if key not in self.comparisons:
            values = self.read("rev-list", "--left-right", "--count", f"{target}...{head}").split()
            if len(values) != 2 or any(not value.isdigit() for value in values):
                raise _Unavailable("invalid local history counts")
            self.comparisons[key] = (int(values[0]), int(values[1]))
        return self.comparisons[key]


def _location(path: str, common: str) -> _Location:
    try:
        root = Path(os.path.realpath(path))
        dotgit = root / ".git"
        if dotgit.is_dir():
            gitdir = dotgit
        else:
            data = dotgit.read_bytes()
            if not data.startswith(b"gitdir: "):
                raise _Unavailable("worktree Git directory unavailable")
            gitdir = Path(os.fsdecode(data[8:].removesuffix(b"\n")))
            if not gitdir.is_absolute():
                gitdir = root / gitdir
        gitdir = Path(os.path.realpath(gitdir))
        try:
            linked_common = Path(os.fsdecode((gitdir / "commondir").read_bytes().removesuffix(b"\n")))
            if not linked_common.is_absolute():
                linked_common = gitdir / linked_common
        except FileNotFoundError:
            linked_common = gitdir
        actual_common = os.path.realpath(linked_common)
        if actual_common != common:
            raise _Changed("checkout repository changed")
        root_stat, git_stat, common_stat = os.stat(root), os.stat(gitdir), os.stat(actual_common)
        return (
            str(root),
            (root_stat.st_dev, root_stat.st_ino),
            str(gitdir),
            (git_stat.st_dev, git_stat.st_ino),
            actual_common,
            (common_stat.st_dev, common_stat.st_ino),
        )
    except (OSError, ValueError) as exc:
        raise _Unavailable("checkout directory identity unavailable") from exc


def _match_entry(entry: dict, identity: _Identity) -> None:
    head, branch, detached = identity
    commit = entry.get("commit")
    supplied = [entry[key] for key in ("head", "HEAD") if key in entry]
    if isinstance(commit, dict) and "sha" in commit:
        supplied.append(commit["sha"])
    if any(_sha(value) != head for value in supplied):
        raise _Changed("listed checkout HEAD changed")
    if "branch" in entry:
        expected = entry["branch"]
        if isinstance(expected, str):
            expected = expected.removeprefix("refs/heads/")
            if not expected:
                raise _Unavailable("invalid listed branch")
        elif expected is not None:
            raise _Unavailable("invalid listed branch")
        if expected != branch or (expected is None) != detached:
            raise _Changed("listed checkout branch changed")
    if "detached" in entry and (type(entry["detached"]) is not bool or entry["detached"] != detached):
        raise _Changed("listed checkout detached state changed")


def _parse_status(data: bytes) -> tuple[int, int, int, int, list[bytes]]:
    """Count paths from one NUL snapshot. Preserve newlines and invalid UTF-8."""
    if data and not data.endswith(b"\0"):
        raise _Unavailable("incomplete local status")
    staged = unstaged = untracked = unmerged = 0
    paths: list[bytes] = []
    for record in data.split(b"\0")[:-1]:
        if record.startswith(b"# "):
            continue
        if record.startswith(b"1 "):
            parts = record.split(b" ", 8)
            if len(parts) != 9 or len(parts[1]) != 2 or any(value not in b".MADT" for value in parts[1]):
                raise _Unavailable("invalid ordinary status record")
            staged += parts[1][0] != ord(".")
            unstaged += parts[1][1] != ord(".")
            path = parts[8]
        elif record.startswith(b"? "):
            untracked += 1
            path = record[2:]
        elif record.startswith(b"u "):
            parts = record.split(b" ", 10)
            if len(parts) != 11 or parts[1] not in (b"DD", b"AU", b"UD", b"UA", b"DU", b"AA", b"UU"):
                raise _Unavailable("invalid unmerged status record")
            unmerged += 1
            path = parts[10]
        elif record.startswith(b"! "):
            continue
        else:
            raise _Unavailable("unexpected local status record")
        if not path or path.startswith(b"/") or b".." in path.split(b"/"):
            raise _Unavailable("invalid local status path")
        paths.append(path)
    return staged, unstaged, untracked, unmerged, paths


def _status_identity(data: bytes) -> _Identity:
    headers = [record for record in data.split(b"\0") if record.startswith(b"# branch.")]
    heads = [record[13:] for record in headers if record.startswith(b"# branch.oid ")]
    branches = [record[14:] for record in headers if record.startswith(b"# branch.head ")]
    if len(heads) != 1 or len(branches) != 1:
        raise _Unavailable("status checkout identity unavailable")
    head = _sha(_text(heads[0]))
    branch = _text(branches[0])
    return (head, None, True) if branch == "(detached)" else (head, branch, False)


def _file_changed(reader: _Reader, path: str, paths: list[bytes]) -> tuple[float | None, str | None]:
    newest: float | None = None
    issue = None
    for name in paths:
        reader.remaining()
        try:
            modified = os.lstat(os.path.join(path, os.fsdecode(name))).st_mtime
        except (OSError, ValueError):
            issue = "some changed file timestamps unavailable"
            continue
        newest = modified if newest is None else max(newest, modified)
    return newest, issue


def _collect_row(
    reader: _Reader,
    entry: dict,
    inventory: dict[str, _Identity],
    locations: dict[str, _Location],
    stamps: dict[str, float],
    target: str | None,
    history_reason: str | None,
) -> WorktreeFacts:
    path = _entry_path(entry)
    if _prunable(entry):
        return WorktreeFacts(path, reason="prunable worktree")
    identity = inventory.get(os.path.realpath(path)) if path else None
    if identity is None:
        return _fallback(entry, "worktree identity unavailable")
    try:
        _match_entry(entry, identity)
    except _Unavailable as exc:
        return WorktreeFacts(path, reason=str(exc))
    head, branch, detached = identity
    row = WorktreeFacts(path, head, branch, detached, commit_time=stamps.get(head))
    if path not in locations:
        return replace(row, commit_time=None, reason="checkout directory identity unavailable")
    row = replace(row, identity_verified=True)
    problems: list[str | None] = []
    try:
        reader.remaining()
        birth = getattr(os.stat(path), "st_birthtime", None)
        row = replace(row, directory_created=float(birth) if birth is not None else None)
        if birth is None:
            problems.append("directory birth time unavailable")
    except (OSError, ValueError, _Unavailable):
        problems.append("directory birth time unavailable")
    if row.commit_time is None:
        problems.append("HEAD commit time unavailable")
    try:
        data = reader.read(
            "status",
            "--porcelain=v2",
            "--branch",
            "--no-ahead-behind",
            "-z",
            "--untracked-files=all",
            "--no-renames",
            cwd=Path(path),
        )
        if _status_identity(data) != identity:
            raise _Changed("checkout identity changed during collection")
        staged, unstaged, untracked, unmerged, paths = _parse_status(data)
        row = replace(row, staged=staged, unstaged=unstaged, untracked=untracked, unmerged=unmerged)
        modified, issue = _file_changed(reader, path, paths)
        row = replace(row, file_changed=modified)
        problems.append(issue)
    except _Changed as exc:
        return WorktreeFacts(path, reason=str(exc))
    except _Unavailable as exc:
        problems.append(str(exc))
    if target is not None and history_reason is None:
        try:
            behind, ahead = reader.compare(target, head)
            row = replace(row, local_ahead=ahead, local_behind=behind, local_contains=ahead == 0)
        except _Unavailable as exc:
            problems.append(str(exc))
    else:
        problems.append(history_reason or "local target unavailable")
    return replace(row, reason=_reason(*problems))


def _without_comparison(row: WorktreeFacts, reason: str) -> WorktreeFacts:
    return replace(row, local_ahead=None, local_behind=None, local_contains=None, reason=_reason(row.reason, reason))


def _revalidate(reader: _Reader, entries: list[dict], facts: RepoFacts) -> RepoFacts:
    rows = dict(facts.rows)
    snapshot: _Snapshot | None = getattr(facts, "_snapshot", None)
    if not any(not _prunable(entry) and _entry_path(entry) for entry in entries):
        return facts
    target_reason = None
    common = ""
    inventory: dict[str, _Identity] = {}
    identity_reason = None
    try:
        common, shallow = reader.layout()
    except _Unavailable as exc:
        identity_reason = str(exc)
    if facts.target_sha is not None:
        try:
            if identity_reason:
                raise _Unavailable(identity_reason)
            if reader.target() != (facts.target_ref, facts.target_sha, facts.target_source):
                raise _Changed("local target changed after collection")
            if any(row.local_ahead is not None for row in rows.values()):
                reader.history(
                    common,
                    shallow,
                    [
                        facts.target_sha,
                        *(row.head for row in rows.values() if row.local_ahead is not None and row.head is not None),
                    ],
                )
        except _Unavailable as exc:
            target_reason = str(exc)
            rows = {path: _without_comparison(row, target_reason) for path, row in rows.items()}
    # Read checkout tips last, after potentially slow target and history reads.
    # Filesystem identity checks below need no further Git subprocesses.
    try:
        inventory = reader.inventory()
    except _Unavailable as exc:
        identity_reason = str(exc)
    for entry in entries:
        path = _entry_path(entry)
        original = rows.get(path)
        if _prunable(entry):
            rows[path] = WorktreeFacts(path, reason="prunable worktree")
            continue
        if original is None:
            rows[path] = _fallback(entry, "worktree not in collected snapshot")
            continue
        if not original.identity_verified or original.head is None or original.detached is None:
            continue
        try:
            if identity_reason:
                raise _Unavailable(identity_reason)
            identity = inventory.get(os.path.realpath(path))
            if identity is None:
                raise _Changed("checkout no longer in worktree inventory")
            _match_entry(entry, identity)
            if identity != (original.head, original.branch, original.detached):
                raise _Changed("checkout identity changed after collection")
            if snapshot is None or path not in snapshot.locations:
                raise _Unavailable("checkout snapshot unavailable")
            if common != snapshot.common or _location(path, common) != snapshot.locations[path]:
                raise _Changed("checkout directory identity changed after collection")
        except _Changed as exc:
            rows[path] = WorktreeFacts(path, reason=str(exc))
        except _Unavailable as exc:
            rows[path] = WorktreeFacts(path, original.head, original.branch, original.detached, reason=str(exc))
    checked = replace(
        facts,
        rows=rows,
        target_sha=None if target_reason else facts.target_sha,
        reason=_reason(facts.reason, target_reason),
    )
    return _attach(checked, snapshot) if snapshot is not None else checked


def _collect_repo(repo: Path, entries: list[dict]) -> RepoFacts:
    if not any(not _prunable(entry) and _entry_path(entry) for entry in entries):
        return RepoFacts(
            {
                _entry_path(entry): WorktreeFacts(
                    _entry_path(entry), reason="prunable worktree" if _prunable(entry) else "missing worktree path"
                )
                for entry in entries
            }
        )
    reader = _Reader(repo)
    try:
        inventory = reader.inventory()
        common, shallow = reader.layout()
    except _Unavailable as exc:
        return RepoFacts(
            {
                _entry_path(entry): WorktreeFacts(_entry_path(entry), reason="prunable worktree")
                if _prunable(entry)
                else _fallback(entry, str(exc))
                for entry in entries
            },
            reason=str(exc),
        )
    locations: dict[str, _Location] = {}
    for entry in entries:
        path = _entry_path(entry)
        if path and not _prunable(entry) and os.path.realpath(path) in inventory:
            try:
                reader.remaining()
                locations[path] = _location(path, common)
            except _Unavailable:
                pass
    ref = sha = source = target_reason = history_reason = None
    try:
        ref, sha, source = reader.target()
    except _Unavailable as exc:
        target_reason = str(exc)
    active_heads = [
        inventory[os.path.realpath(_entry_path(entry))][0]
        for entry in entries
        if not _prunable(entry) and _entry_path(entry) and os.path.realpath(_entry_path(entry)) in inventory
    ]
    if sha is not None:
        try:
            reader.history(common, shallow, [sha, *active_heads])
        except _Unavailable as exc:
            history_reason = str(exc)
    try:
        stamps = reader.timestamps(entries, inventory)
    except _Unavailable:
        stamps = {}
    rows = {
        _entry_path(entry): _collect_row(reader, entry, inventory, locations, stamps, sha, history_reason)
        for entry in entries
    }
    # Parent calls revalidate_many after sizes and other collection. Do not repeat
    # target and inventory reads here. Status headers bind each row to its tip.
    return _attach(
        RepoFacts(rows, ref, sha, source, _reason(target_reason, history_reason)), _Snapshot(common, locations)
    )


def _many(entries: dict[Path, list[dict]], operation: Any) -> dict[Path, RepoFacts]:
    if len(entries) <= 1:
        return {repo: operation(repo, rows) for repo, rows in entries.items()}
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(entries))) as pool:
        futures = {repo: pool.submit(operation, repo, rows) for repo, rows in entries.items()}
        return {repo: future.result() for repo, future in futures.items()}


def collect_many(worktrees_by_repo: dict[Path, list[dict]]) -> dict[Path, RepoFacts]:
    """Collect main and linked rows. Call revalidate_many before rendering."""
    return _many(worktrees_by_repo, _collect_repo)


def revalidate_many(entries: dict[Path, list[dict]], facts: dict[Path, RepoFacts]) -> dict[Path, RepoFacts]:
    """Reject moved targets, changed HEAD/branch, or replaced checkout directories."""

    def check(repo: Path, rows: list[dict]) -> RepoFacts:
        if repo not in facts:
            return RepoFacts(
                {_entry_path(row): _fallback(row, "missing collected facts") for row in rows},
                reason="missing collected facts",
            )
        return _revalidate(_Reader(repo), rows, facts[repo])

    return _many(entries, check)
