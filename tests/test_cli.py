"""`wt` is coppice's hard prerequisite (see README's Install section and
`coppice.wt.require_wt`). Every command that shells out to it must fail with
a clear, actionable message when it's missing from PATH, never a raw
traceback, regardless of which `wt`-calling code path a command happens to
hit first.

Also covers `clean`/`remove`'s picker/`status`, all synthesized against
fake `wt.list_worktrees`/`gh.open_prs` results rather than a real `wt`/`gh`
install, so these pass in CI the same as locally.
"""

import subprocess
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from coppice import cli, gh, repo, wt
from coppice.cli import app

runner = CliRunner()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "--allow-empty", "-q", "-m", "init"], check=True)
    return path


def _hide_wt(monkeypatch):
    """Simulate `wt` not being installed, independent of the real PATH."""
    monkeypatch.setattr(wt.shutil, "which", lambda name: None)


def _stub_wt(monkeypatch, *, which: dict[str, str | None] | None = None):
    """Simulate `wt` being installed (require_wt succeeds) without shelling
    out to a real binary, and control any other `shutil.which` lookups
    (fzf/gh) a test cares about. `shutil` is the same module object from
    every import site, so this one patch covers wt.py's and cli.py's calls.
    """
    table = {"wt": "/usr/bin/wt", **(which or {})}
    monkeypatch.setattr(wt.shutil, "which", lambda name: table.get(name))


def _entry(
    branch: str,
    path: Path,
    *,
    is_main: bool = False,
    is_current: bool = False,
    commit_ts: float = 0,
    dirty: bool = False,
    main_state: str = "ahead",
    stale: bool = False,
) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    return {
        "branch": branch,
        "path": str(path),
        "is_main": is_main,
        "is_current": is_current,
        "commit": {"timestamp": commit_ts},
        "working_tree": {"modified": dirty},
        "main_state": main_state,
        "worktree": {"state": "prunable" if stale else "active"},
    }


def test_new_without_wt_fails_clearly(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "some-branch"])

    assert result.exit_code != 0
    assert "wt" in result.output
    assert "worktrunk.dev" in result.output
    assert "Traceback" not in result.output


def test_list_without_wt_fails_clearly(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["list", str(repo_dir)])

    assert result.exit_code != 0
    assert "wt" in result.output
    assert "worktrunk.dev" in result.output
    assert "Traceback" not in result.output


def test_remove_without_wt_fails_clearly(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["remove", "some-branch", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "wt" in result.output
    assert "worktrunk.dev" in result.output
    assert "Traceback" not in result.output


def test_clean_without_wt_fails_clearly(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "wt" in result.output
    assert "worktrunk.dev" in result.output
    assert "Traceback" not in result.output


def test_clean_dry_run_categorizes_candidates(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch, which={"gh": "/usr/bin/gh"})
    # Force the commit-timestamp fallback everywhere instead of filesystem
    # birth time, so age is deterministic across OSes (tmp_path dirs are
    # freshly created "now", which would otherwise always read as 0d old).
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)

    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)

    old_seconds = 30 * 86400  # older than the 14-day default threshold
    young_seconds = 3 * 86400
    entries = [
        _entry("young-branch", tmp_path / "young", commit_ts=now - young_seconds),
        _entry("dirty-branch", tmp_path / "dirty", commit_ts=now - old_seconds, dirty=True),
        _entry("pr-branch", tmp_path / "pr", commit_ts=now - old_seconds),
        _entry("mergeable-branch", tmp_path / "mergeable", commit_ts=now - old_seconds, main_state="integrated"),
        _entry("unmerged-branch", tmp_path / "unmerged", commit_ts=now - old_seconds, main_state="ahead"),
        _entry("stale-branch", tmp_path / "stale", stale=True),
        _entry("main", tmp_path / "repo", is_main=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(
        gh, "open_prs", lambda _repo, branches: {"pr-branch": "#42 Some PR"} if "pr-branch" in branches else {}
    )

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--dry-run", "--verbose"])

    assert result.exit_code == 0, result.output
    assert "young-branch" in result.output and "younger than 14d" in result.output
    assert "dirty-branch" in result.output and "uncommitted changes" in result.output
    assert "pr-branch" in result.output and "open PR #42 Some PR" in result.output
    assert "mergeable-branch" in result.output and "merged" in result.output
    assert "unmerged-branch" in result.output and "unmerged" in result.output
    assert "stale-branch" in result.output and "stale" in result.output
    assert "main" not in result.output.replace("Scanned", "").replace("remain", "")
    assert "Dry run, nothing removed." in result.output
    # 3 removable: mergeable-branch, unmerged-branch, stale-branch.
    assert "3 removable" in result.output


def test_clean_nothing_to_clean(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)
    monkeypatch.setattr(cli.time, "time", lambda: 2_000_000_000.0)
    monkeypatch.setattr(
        wt, "list_worktrees", lambda _repo: [_entry("young", tmp_path / "young", commit_ts=2_000_000_000.0)]
    )

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Nothing to clean." in result.output


def test_clean_merged_ignores_age(tmp_path, monkeypatch):
    """--merged sweeps up every merged worktree regardless of DAYS, but still
    leaves young-but-unmerged branches alone.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)

    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)
    young_seconds = 1 * 86400

    entries = [
        _entry("young-merged", tmp_path / "young-merged", commit_ts=now - young_seconds, main_state="integrated"),
        _entry("young-unmerged", tmp_path / "young-unmerged", commit_ts=now - young_seconds, main_state="ahead"),
        _entry("main", tmp_path / "repo", is_main=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--merged", "--dry-run", "--verbose"])

    assert result.exit_code == 0, result.output
    assert "young-merged" in result.output and "merged, branch will be deleted" in result.output
    assert "young-unmerged" in result.output and "not merged" in result.output
    assert "1 removable" in result.output


def test_remove_no_branch_no_candidates(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [])

    result = runner.invoke(app, ["remove", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "no removable worktrees in scope" in result.output


def test_remove_partial_failure_reports_both_counts(tmp_path, monkeypatch):
    """On a partial failure, the summary must say how many succeeded, not
    just list what failed (previously it only reported the failures).
    """
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(wt, "remove", lambda *a, **k: None)

    result = runner.invoke(app, ["remove", "good-branch", "missing-branch", "--repo", str(repo_dir), "--yes"])

    assert result.exit_code != 0
    assert "Removed 1 worktree(s), 1 failed" in result.output
    assert "missing-branch" in result.output


def test_remove_without_yes_prompts_and_cancels_on_no(tmp_path, monkeypatch):
    """Without --yes, `remove` must ask for confirmation itself rather than
    relying on `wt remove`'s own prompt: `wt.run` captures stdout/stderr, so
    `wt` treats the call as non-interactive and never actually prompts,
    which would otherwise remove worktrees with no confirmation at all.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    remove_calls: list[Any] = []
    monkeypatch.setattr(wt, "remove", lambda *a, **k: remove_calls.append((a, k)))

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir)], input="n\n")

    assert result.exit_code != 0
    assert "Remove the worktree(s) above?" in result.output
    assert "Cancelled." in result.output
    assert remove_calls == []


def test_remove_without_yes_prompts_and_proceeds_on_yes(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    remove_calls: list[Any] = []
    monkeypatch.setattr(wt, "remove", lambda *a, **k: remove_calls.append((a, k)))

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir)], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Removed 1 worktree(s)." in result.output
    assert len(remove_calls) == 1
    # Confirmed once at the coppice level, so `wt remove` is always told
    # `-y` too rather than relying on its own (non-functional, here) prompt.
    assert remove_calls[0][1]["yes"] is True


def test_remove_no_branch_falls_back_without_fzf(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch, which={"fzf": None})
    entries = [
        _entry("feature-a", tmp_path / "feature-a", commit_ts=0),
        _entry("feature-b", tmp_path / "feature-b", commit_ts=0, dirty=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)

    result = runner.invoke(app, ["remove", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "fzf isn't installed" in result.output
    assert "feature-a" in result.output
    assert "feature-b" in result.output and "dirty" in result.output
    assert "Re-run: coppice remove BRANCH" in result.output


def test_status_reports_wt_and_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    repo_dir = _init_repo(tmp_path / "repo")
    repo.register_repo(repo_dir)
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "wt v9.9.9\n", ""))
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [{"branch": "main"}])

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    flat_output = result.output.replace("\n", "")
    assert "wt v9.9.9" in flat_output
    assert cli._short_path(repo_dir) in flat_output
    assert "1 worktree(s)" in flat_output


def test_status_without_wt_still_lists_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    repo_dir = _init_repo(tmp_path / "repo")
    repo.register_repo(repo_dir)
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    flat_output = result.output.replace("\n", "")
    assert "not found" in flat_output
    assert cli._short_path(repo_dir) in flat_output


def test_status_prunes_missing_repos_from_registry(tmp_path, monkeypatch):
    """A registered repo whose directory is gone (e.g. a deleted scratch
    repo, or one a `wt` hook registered that later got cleaned up) shows as
    `missing` for this run, but `status` self-heals the registry so it
    doesn't show up on every subsequent run forever.
    """
    registry_path = tmp_path / "known-repos"
    monkeypatch.setattr(repo, "REGISTRY_PATH", registry_path)
    repo_dir = _init_repo(tmp_path / "repo")
    gone_dir = tmp_path / "gone"
    gone_dir.mkdir()
    repo.register_repo(repo_dir)
    repo.register_repo(gone_dir)
    gone_dir.rmdir()
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "wt v9.9.9\n", ""))
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [{"branch": "main"}])

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    flat_output = result.output.replace("\n", "")
    assert "missing" in flat_output
    assert "Pruned 1 missing repo(s)" in flat_output
    assert "1 repo(s)" in flat_output  # only the live repo counted, not the pruned one
    assert repo.known_repos() == [repo_dir]
