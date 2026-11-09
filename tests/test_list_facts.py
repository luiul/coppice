"""List facts are local, immutable snapshots, not cleanup policy."""

import copy
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coppice import list_facts as lf


def git(root, *args, env=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    environment.update(env or {})
    return (
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, env=environment)
        .stdout.decode("utf-8", "surrogateescape")
        .strip()
    )


def commit(root, message="change"):
    git(root, "add", ".")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def entry(path, branch="main", head=None, **extra):
    row = {"path": str(path), "branch": branch, "worktree": {"state": "active"}, **extra}
    if head is not None:
        row["commit"] = {"sha": head, "timestamp": 123}
    return row


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Facts test")
    git(root, "config", "user.email", "facts@example.invalid")
    git(root, "config", "commit.gpgSign", "false")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "worktrunk.default-branch", "main")
    (root / "file").write_text("initial\n")
    (root / ".gitignore").write_text("ignored\n")
    commit(root, "initial")
    return root


def collect(root, rows=None):
    return lf.collect_many({root: rows or [entry(root)]})[root]


def test_public_dataclass_contract_and_frozen():
    assert [value.name for value in fields(lf.WorktreeFacts)] == [
        "path",
        "head",
        "branch",
        "detached",
        "directory_created",
        "commit_time",
        "file_changed",
        "staged",
        "unstaged",
        "untracked",
        "unmerged",
        "local_ahead",
        "local_behind",
        "local_contains",
        "reason",
        "identity_verified",
    ]
    assert [value.name for value in fields(lf.RepoFacts)] == [
        "rows",
        "target_ref",
        "target_sha",
        "target_source",
        "reason",
    ]
    for value, name in [(lf.WorktreeFacts("/x"), "path"), (lf.RepoFacts(), "reason")]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, name, "changed")


def test_main_clean_commit_time_no_commit_fallback(repo):
    head = git(repo, "rev-parse", "HEAD")
    row = collect(repo).rows[str(repo)]
    assert (row.head, row.branch, row.detached) == (head, "main", False)
    assert row.identity_verified
    assert (row.staged, row.unstaged, row.untracked, row.unmerged) == (0, 0, 0, 0)
    assert row.file_changed is None
    assert row.commit_time == float(git(repo, "show", "-s", "--format=%ct", head))
    assert (row.local_ahead, row.local_behind, row.local_contains) == (0, 0, True)


def test_status_overlap_untracked_and_ignored_same_snapshot(repo):
    (repo / "file").write_text("staged\n")
    git(repo, "add", "file")
    (repo / "file").write_text("unstaged\n")
    (repo / "untracked\nname").write_text("new\n")
    (repo / "ignored").write_text("ignored\n")
    os.utime(repo / "file", (1000, 1000))
    os.utime(repo / "untracked\nname", (2000, 2000))
    os.utime(repo / "ignored", (9000, 9000))
    row = collect(repo).rows[str(repo)]
    assert (row.staged, row.unstaged, row.untracked, row.unmerged) == (1, 1, 1, 0)
    assert row.file_changed == 2000


def test_no_rename_detection_counts_both_paths_and_deletion_unknown(repo):
    git(repo, "mv", "file", "renamed")
    row = collect(repo).rows[str(repo)]
    assert row.staged == 2 and row.unstaged == 0
    assert row.file_changed == (repo / "renamed").lstat().st_mtime
    assert "timestamps unavailable" in row.reason


def test_unmerged_has_separate_count(repo):
    git(repo, "checkout", "-b", "side")
    (repo / "file").write_text("side\n")
    commit(repo)
    git(repo, "checkout", "main")
    (repo / "file").write_text("main\n")
    commit(repo)
    with pytest.raises(subprocess.CalledProcessError):
        git(repo, "merge", "side")
    row = collect(repo).rows[str(repo)]
    assert (row.staged, row.unstaged, row.untracked, row.unmerged) == (0, 0, 0, 1)
    assert row.file_changed == (repo / "file").stat().st_mtime


def test_linked_ahead_ancestor_diverge_and_main_not_default(repo, tmp_path):
    initial = git(repo, "rev-parse", "HEAD")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    (linked / "feature").write_text("feature\n")
    feature = commit(linked)
    rows = [entry(repo, "main", initial, is_main=True), entry(linked, "feature", feature)]
    result = collect(repo, rows)
    assert (result.rows[str(linked)].local_ahead, result.rows[str(linked)].local_behind) == (1, 0)
    assert result.rows[str(linked)].local_contains is False
    git(repo, "reset", "--hard", feature)
    result = collect(repo, [entry(linked, "feature"), entry(repo)])
    assert result.rows[str(linked)].local_contains is True
    git(repo, "reset", "--hard", initial)
    (repo / "main-only").write_text("main\n")
    commit(repo)
    result = collect(repo, [entry(linked, "feature")])
    assert (result.rows[str(linked)].local_ahead, result.rows[str(linked)].local_behind) == (1, 1)
    git(repo, "branch", "default", initial)
    git(repo, "config", "worktrunk.default-branch", "default")
    result = collect(repo)
    assert result.target_ref == "refs/heads/default"
    assert result.rows[str(repo)].local_ahead == 1
    git(repo, "branch", "old", initial)
    git(repo, "checkout", "old")
    git(repo, "config", "worktrunk.default-branch", "main")
    row = collect(repo, [entry(repo, "old")]).rows[str(repo)]
    assert (row.local_ahead, row.local_behind, row.local_contains) == (0, 1, True)


def test_target_cache_fallback_same_sources_conflict_and_no_current_branch_fallback(repo):
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", head)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert collect(repo).target_source == "cached config"
    git(repo, "config", "--unset", "worktrunk.default-branch")
    result = collect(repo)
    assert (result.target_ref, result.target_source) == ("refs/heads/main", "cached origin HEAD")
    git(repo, "branch", "other")
    git(repo, "config", "worktrunk.default-branch", "other")
    result = collect(repo)
    assert result.target_sha is None and "disagree" in result.reason
    assert result.rows[str(repo)].local_ahead is None
    git(repo, "config", "--unset", "worktrunk.default-branch")
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert collect(repo).target_ref is None


@pytest.mark.parametrize("name", ["bad..name", "-bad", "bad\nmain", "main\n", "HEAD", "main^{commit}", "missing"])
def test_invalid_or_missing_target_unknown(repo, name):
    git(repo, "config", "worktrunk.default-branch", name)
    result = collect(repo)
    assert result.target_sha is None and result.reason
    assert result.rows[str(repo)].staged == 0
    assert result.rows[str(repo)].local_contains is None


def test_dangling_origin_cache_conflict_not_silently_ignored(repo):
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/missing")
    result = collect(repo)
    assert result.target_sha is None and "disagree" in result.reason


def test_revalidation_rechecks_missing_commit_history(repo):
    head = git(repo, "rev-parse", "HEAD")
    (repo / "more").write_text("more\n")
    commit(repo)
    facts = lf.collect_many({repo: [entry(repo)]})
    assert facts[repo].rows[str(repo)].local_contains is True
    (repo / ".git/objects" / head[:2] / head[2:]).unlink()
    checked = lf.revalidate_many({repo: [entry(repo)]}, facts)[repo]
    assert checked.rows[str(repo)].local_contains is None and checked.target_sha is None


def test_replacements_do_not_change_ancestry(repo, tmp_path):
    initial = git(repo, "rev-parse", "HEAD")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    (linked / "feature").write_text("feature\n")
    head = commit(linked)
    git(repo, "replace", initial, head)
    row = collect(repo, [entry(linked, "feature")]).rows[str(linked)]
    assert (row.local_ahead, row.local_behind, row.local_contains) == (1, 0, False)


def test_detached_distinct_from_unknown(repo, tmp_path):
    git(repo, "checkout", "--detach")
    row = collect(repo, [entry(repo, None)]).rows[str(repo)]
    assert row.detached is True and row.branch is None and row.head
    assert row.staged == 0 and row.local_contains is True
    unknown = collect(repo, [entry(tmp_path / "missing", None)]).rows[str(tmp_path / "missing")]
    assert unknown.detached is None and unknown.staged is None and unknown.reason
    assert not unknown.identity_verified


@pytest.mark.parametrize(
    "mutation", [{"branch": "changed"}, {"branch": None}, {"commit": {"sha": "a" * 40}}, {"head": None}]
)
def test_listed_identity_mismatch_discards_facts(repo, mutation):
    row = collect(repo, [{**entry(repo), **mutation}]).rows[str(repo)]
    assert row.head is None and row.staged is None and row.commit_time is None and row.reason


def test_prunable_and_empty_no_io(monkeypatch):
    fail = Mock(side_effect=AssertionError("unexpected IO"))
    monkeypatch.setattr(lf.subprocess, "run", fail)
    monkeypatch.setattr(lf.os, "stat", fail)
    root = Path("/missing")
    rows = [entry(root, "gone", worktree={"state": "prunable"})]
    facts = lf.collect_many({root: rows})
    assert facts[root].rows[str(root)].staged is None
    assert lf.revalidate_many({root: rows}, facts) == facts
    assert lf.collect_many({}) == {} and lf.collect_many({root: []})[root].rows == {}
    fail.assert_not_called()


def test_missing_metadata_and_status_failure_not_clean(repo, monkeypatch):
    rows = [{}, {"path": str(repo)}]
    real = lf._Reader.git

    def fail_status(self, *args, **kwargs):
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 128, b"", b"unavailable")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(lf._Reader, "git", fail_status)
    result = collect(repo, rows)
    assert result.rows[""].staged is None and result.rows[""].reason
    assert result.rows[str(repo)].head and result.rows[str(repo)].staged is None
    assert result.rows[str(repo)].untracked is None


def test_birthtime_only_main_and_linked_no_fallback(repo, tmp_path, monkeypatch):
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    real = os.stat

    def stat(path, *args, **kwargs):
        if str(path) in (str(repo), str(linked)):
            actual = real(path)
            return SimpleNamespace(st_mtime=444, st_ctime=555, st_dev=actual.st_dev, st_ino=actual.st_ino)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(lf.os, "stat", stat)
    result = collect(repo, [entry(repo), entry(linked, "feature")])
    assert all(row.directory_created is None for row in result.rows.values())
    assert all(row.commit_time for row in result.rows.values())


def test_birthtime_and_stat_failure_preserve_none(repo, monkeypatch):
    (repo / "untracked").write_text("new\n")
    real = os.stat

    def stat(path, *args, **kwargs):
        if str(path) == str(repo):
            actual = real(path)
            return SimpleNamespace(
                st_birthtime=100.5, st_mtime=444, st_ctime=555, st_dev=actual.st_dev, st_ino=actual.st_ino
            )
        return real(path, *args, **kwargs)

    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if str(path) == str(repo / "untracked"):
            raise PermissionError("unreadable")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(lf.os, "stat", stat)
    monkeypatch.setattr(lf.os, "lstat", lstat)
    row = collect(repo).rows[str(repo)]
    assert row.directory_created == 100.5
    assert row.untracked == 1 and row.file_changed is None and row.commit_time


def test_parser_newline_invalid_utf8_and_overlap():
    data = (
        b"1 MM N... 100644 100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" new\nname\xff\0"
        b"? raw\xfe\0"
        b"u UU N... 100644 100644 100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" " + b"c" * 40 + b" conflict\0"
        b"! ignored\0"
    )
    assert lf._parse_status(data) == (1, 1, 1, 1, [b"new\nname\xff", b"raw\xfe", b"conflict"])
    assert lf._parse_status(b"") == (0, 0, 0, 0, [])


@pytest.mark.parametrize(
    "data",
    [b"? incomplete", b"2 R. rename\0old\0", b"1 MM short\0", b"u ZZ short\0", b"? ../outside\0", b"? /outside\0"],
)
def test_invalid_status_unknown(data):
    with pytest.raises(lf._Unavailable):
        lf._parse_status(data)


def test_actual_invalid_utf8_path(repo):
    name = os.path.join(os.fsencode(repo), b"odd\xff\nname")
    try:
        with open(name, "wb") as stream:
            stream.write(b"new\n")
    except OSError as exc:
        if exc.errno == 92:
            pytest.skip("filesystem rejects invalid UTF-8 names, covered by byte parser test")
        raise
    row = collect(repo).rows[str(repo)]
    assert row.untracked == 1 and row.file_changed == os.stat(name).st_mtime


def test_shallow_and_grafts_unknown_even_same_head(repo, tmp_path):
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "--depth=1", repo.as_uri(), str(shallow))
    git(shallow, "config", "worktrunk.default-branch", "main")
    row = collect(shallow).rows[str(shallow)]
    assert row.local_ahead is None and row.local_contains is None and "shallow" in row.reason
    (repo / ".git/info/grafts").write_text(git(repo, "rev-parse", "HEAD") + "\n")
    row = collect(repo).rows[str(repo)]
    assert row.local_ahead is None and "grafts" in row.reason


def test_missing_local_object_unknown_no_lazy_fetch(repo):
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "base", head)
    (repo / "more").write_text("new\n")
    commit(repo)
    # Removing a parent makes ancestry unavailable, while HEAD stays readable.
    (repo / ".git/objects" / head[:2] / head[2:]).unlink()
    git(repo, "config", "worktrunk.default-branch", "main")
    row = collect(repo).rows[str(repo)]
    assert row.head and row.local_ahead is None and row.local_contains is None


def test_revalidate_head_branch_target_and_entries_immutable(repo):
    rows = [entry(repo)]
    before = json.dumps(rows, sort_keys=True)
    facts = lf.collect_many({repo: rows})
    snapshot = copy.deepcopy(facts)
    git(repo, "checkout", "-b", "other")
    checked = lf.revalidate_many({repo: rows}, facts)
    assert checked[repo].rows[str(repo)].head is None
    assert checked[repo].rows[str(repo)].staged is None
    assert facts == snapshot and json.dumps(rows, sort_keys=True) == before
    git(repo, "checkout", "main")
    (repo / "next").write_text("next\n")
    commit(repo)
    checked = lf.revalidate_many({repo: rows}, facts)
    assert checked[repo].target_sha is None and checked[repo].rows[str(repo)].head is None
    assert facts == snapshot


def test_target_only_move_preserves_counts_not_comparison(repo, tmp_path):
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    rows = [entry(linked, "feature")]
    facts = lf.collect_many({repo: rows})
    (repo / "next").write_text("next\n")
    commit(repo)
    checked = lf.revalidate_many({repo: rows}, facts)[repo]
    row = checked.rows[str(linked)]
    assert row.head == facts[repo].rows[str(linked)].head and row.staged == 0
    assert row.local_ahead is None and row.local_contains is None
    assert checked.target_sha is None and "target changed" in checked.reason


def test_collection_rechecks_branch_during_status(repo, monkeypatch):
    real = lf._Reader.git

    def changing(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        if args[0] == "status":
            git(repo, "checkout", "-b", "changed")
        return result

    monkeypatch.setattr(lf._Reader, "git", changing)
    facts = lf.collect_many({repo: [entry(repo)]})
    row = lf.revalidate_many({repo: [entry(repo)]}, facts)[repo].rows[str(repo)]
    assert row.head is None and row.staged is None and row.reason


def test_revalidation_does_not_promote_unknown_or_missing(repo):
    rows = [entry(repo)]
    assert lf.revalidate_many({repo: rows}, {})[repo].rows[str(repo)].staged is None
    unknown = {repo: lf.RepoFacts({str(repo): lf.WorktreeFacts(str(repo), reason="unknown")})}
    assert lf.revalidate_many({repo: rows}, unknown) == unknown


def test_readonly_commands_env_timeouts_and_index_unchanged(repo, monkeypatch):
    real = subprocess.run
    calls = []
    index = (repo / ".git/index").read_bytes()
    index_mtime = (repo / ".git/index").stat().st_mtime_ns
    refs = git(repo, "show-ref")
    for name in [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_SHALLOW_FILE",
    ]:
        monkeypatch.setenv(name, "/poison")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return real(args, **kwargs)

    monkeypatch.setattr(lf.subprocess, "run", run)
    result = collect(repo)
    lf.revalidate_many({repo: [entry(repo)]}, {repo: result})
    allowed = {
        "config",
        "symbolic-ref",
        "worktree",
        "for-each-ref",
        "check-ref-format",
        "rev-parse",
        "show",
        "status",
        "rev-list",
    }
    assert calls
    for args, kwargs in calls:
        assert args[0] == "git" and args[args.index("-C") + 2] in allowed
        assert 0 < kwargs["timeout"] <= 8 and kwargs["capture_output"]
        env = kwargs["env"]
        assert (
            env["GIT_OPTIONAL_LOCKS"] == "0"
            and env["GIT_NO_LAZY_FETCH"] == "1"
            and env["GIT_NO_REPLACE_OBJECTS"] == "1"
        )
        assert "GIT_DIR" not in env and "GIT_INDEX_FILE" not in env and "GIT_CONFIG_COUNT" not in env
        assert "protocol.allow=never" in args and "core.fsmonitor=false" in args
    assert (repo / ".git/index").read_bytes() == index
    assert (repo / ".git/index").stat().st_mtime_ns == index_mtime
    assert git(repo, "show-ref") == refs
    assert not (repo / ".git/index.lock").exists()


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("git", 8)])
def test_command_failure_no_uncaught_exception(tmp_path, monkeypatch, error):
    monkeypatch.setattr(lf.subprocess, "run", Mock(side_effect=error))
    result = collect(tmp_path)
    assert result.reason and result.rows[str(tmp_path)].staged is None
    assert (
        lf.revalidate_many({tmp_path: [entry(tmp_path)]}, {tmp_path: result})[tmp_path].rows[str(tmp_path)].staged
        is None
    )


def test_repo_deadline_no_more_commands_and_call_cap(tmp_path, monkeypatch):
    now = [0.0]
    calls = []
    monkeypatch.setattr(lf.time, "monotonic", lambda: now[0])

    def run(args, **kwargs):
        calls.append(kwargs["timeout"])
        now[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(lf.subprocess, "run", run)
    reader = lf._Reader(tmp_path)
    for _ in range(10):
        with pytest.raises(lf._Unavailable):
            reader.read("worktree", "list", "--porcelain", "-z")
    assert sum(calls) == 30 and max(calls) == 8
    rows = [entry(tmp_path / str(value)) for value in range(20)]
    result = collect(tmp_path, rows)
    assert all(row.staged is None for row in result.rows.values())


def test_revalidation_rejects_same_tip_replacement_clone(repo, tmp_path):
    facts = lf.collect_many({repo: [entry(repo)]})
    moved = tmp_path / "original"
    repo.rename(moved)
    git(tmp_path, "clone", "--no-hardlinks", str(moved), str(repo))
    git(repo, "config", "worktrunk.default-branch", "main")
    checked = lf.revalidate_many({repo: [entry(repo)]}, facts)[repo].rows[str(repo)]
    assert checked.head is None and checked.staged is None and "directory identity changed" in checked.reason


def test_revalidation_rejects_replaced_linked_directory(repo, tmp_path):
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    rows = [entry(linked, "feature")]
    facts = lf.collect_many({repo: rows})
    saved = tmp_path / "saved"
    linked.rename(saved)
    shutil.copytree(saved, linked)
    checked = lf.revalidate_many({repo: rows}, facts)[repo].rows[str(linked)]
    assert checked.head is None and checked.staged is None and "directory identity changed" in checked.reason


def test_file_changed_skips_deleted_unreadable_and_uses_symlink_inode(repo, monkeypatch):
    (repo / "readable").write_text("new\n")
    (repo / "unreadable").write_text("new\n")
    target = repo.parent / "outside"
    target.write_text("outside\n")
    (repo / "symlink").symlink_to(target)
    os.utime(repo / "readable", (1000, 1000))
    os.utime(target, (9000, 9000))
    os.utime(repo / "symlink", (2000, 2000), follow_symlinks=False)
    (repo / "file").unlink()
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if str(path) == str(repo / "unreadable"):
            raise PermissionError("denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(lf.os, "lstat", lstat)
    row = collect(repo).rows[str(repo)]
    assert row.file_changed == 2000 and row.unstaged == 1 and row.untracked == 3
    assert "timestamps unavailable" in row.reason


def test_entry_commit_timestamp_reused_only_with_matching_sha(repo, monkeypatch):
    head = git(repo, "rev-parse", "HEAD")
    real = lf._Reader.git
    calls = []

    def recording(self, *args, **kwargs):
        calls.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(lf._Reader, "git", recording)
    row = collect(repo, [entry(repo, head=head)]).rows[str(repo)]
    assert row.commit_time == 123 and not any(args[0] == "show" for args in calls)
    calls.clear()
    row = collect(repo, [{**entry(repo), "commit": {"timestamp": 123}}]).rows[str(repo)]
    assert row.commit_time == float(git(repo, "show", "-s", "--format=%ct", head))
    assert any(args[0] == "show" for args in calls)


def test_three_rows_batched_command_budget(repo, tmp_path, monkeypatch):
    rows = [entry(repo)]
    for value in range(2):
        path = tmp_path / str(value)
        git(repo, "worktree", "add", "-b", f"feature{value}", str(path))
        rows.append(entry(path, f"feature{value}"))
    real = lf._Reader.git
    calls = []

    def recording(self, *args, **kwargs):
        calls.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(lf._Reader, "git", recording)
    facts = lf.collect_many({repo: rows})
    checked = lf.revalidate_many({repo: rows}, facts)
    assert all(row.staged == 0 and row.local_contains is True for row in checked[repo].rows.values())
    assert len(calls) <= 19
    assert sum(args[0] == "worktree" for args in calls) == 2
    assert sum(args[0] == "status" for args in calls) == 3
    assert sum(args[0] == "show" for args in calls) == 1


def test_timeout_preserves_entry_label_but_not_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(lf.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("git", 8)))
    rows = [entry(tmp_path, "known", "a" * 40), entry(tmp_path / "unknown", None)]
    result = collect(tmp_path, rows)
    attached = result.rows[str(tmp_path)]
    assert attached.head == "a" * 40 and attached.branch == "known" and attached.detached is False
    assert attached.staged is None and attached.local_contains is None and not attached.identity_verified
    assert result.rows[str(tmp_path / "unknown")].detached is None


def test_final_inventory_detects_checkout_change_during_target(repo, monkeypatch):
    rows = [entry(repo)]
    facts = lf.collect_many({repo: rows})
    real = lf._Reader.target

    def changing_target(self):
        result = real(self)
        git(repo, "checkout", "-b", "changed-during-target")
        return result

    monkeypatch.setattr(lf._Reader, "target", changing_target)
    checked = lf.revalidate_many({repo: rows}, facts)[repo].rows[str(repo)]
    assert checked.head is None and checked.staged is None and not checked.identity_verified
    assert "branch changed" in checked.reason


def test_identity_verified_survives_status_failure_but_not_revalidation_timeout(repo, monkeypatch):
    rows = [entry(repo)]
    real = lf._Reader.git

    def status_unavailable(self, *args, **kwargs):
        if args[0] == "status":
            raise lf._Unavailable("status unavailable")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(lf._Reader, "git", status_unavailable)
    facts = lf.collect_many({repo: rows})
    original = facts[repo].rows[str(repo)]
    assert original.identity_verified and original.staged is None
    checked = lf.revalidate_many({repo: rows}, facts)
    assert checked[repo].rows[str(repo)].identity_verified
    monkeypatch.setattr(lf._Reader, "git", Mock(side_effect=lf._Unavailable("timeout")))
    unknown = lf.revalidate_many({repo: rows}, facts)[repo].rows[str(repo)]
    assert not unknown.identity_verified and unknown.head == original.head and unknown.branch == original.branch
    assert unknown.staged is None and unknown.local_contains is None
    assert original.identity_verified


def test_max_eight_workers_no_nested_pool(monkeypatch, tmp_path):
    active = peak = 0
    lock = threading.Lock()

    def operation(repo, rows):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return lf.RepoFacts()

    monkeypatch.setattr(lf, "_collect_repo", operation)
    entries = {tmp_path / str(value): [] for value in range(24)}
    assert len(lf.collect_many(entries)) == 24
    assert 1 < peak <= 8 and active == 0
