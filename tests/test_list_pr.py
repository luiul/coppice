"""PR display stays separate from Worktrunk data and cleanup policy."""

import copy
import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from coppice import cli, gh, repo, wt

runner = CliRunner()


def _entry(branch, path, *, main=False, stale=False):
    return {
        "branch": branch,
        "path": str(path),
        "is_main": main,
        "commit": {"timestamp": 1},
        "working_tree": {"modified": False},
        "main_state": "ahead",
        "worktree": {"state": "prunable" if stale else "active"},
    }


@pytest.fixture
def listing(tmp_path, monkeypatch):
    # Other suites temporarily set Console.width. Restoring that property
    # leaves an explicit width, which would make COLUMNS ineffective here.
    monkeypatch.setattr(cli, "console", Console(highlight=False))
    monkeypatch.setattr(cli, "err", Console(stderr=True, highlight=False))
    root = tmp_path / "repo"
    entries = [
        _entry("main", root, main=True),
        _entry("feature", tmp_path / "feature"),
        _entry("waiting", tmp_path / "waiting"),
    ]
    monkeypatch.setattr(repo, "scope_repos", lambda path: [root])
    monkeypatch.setattr(wt, "require_wt", lambda: None)
    monkeypatch.setattr(wt, "list_worktrees_many", lambda roots: {root: entries})
    monkeypatch.setattr(cli.park_mod, "parked_at_many", lambda roots: {})
    lookup = Mock(
        return_value=gh.PullRequestLookup(
            "ok",
            {
                "feature": (gh.PullRequest(123, "https://github.com/owner/repo/pull/123"),),
            },
        )
    )
    monkeypatch.setattr(gh, "lookup_prs", lookup)
    return root, entries, lookup


@pytest.mark.parametrize("width", [80, 120, 200])
def test_list_default_pr_column(listing, width):
    root, entries, lookup = listing
    before = copy.deepcopy(entries)
    result = runner.invoke(cli.app, ["list", "--no-size"], env={"COLUMNS": str(width)})
    assert result.exit_code == 0, result.output
    assert "PR" in result.stdout
    assert "Open #123" in result.stdout
    assert "No PR" in result.stdout
    assert "feature" in result.stdout
    assert result.stderr == ""
    lookup.assert_called_once_with(root, {"feature", "waiting"})
    assert entries == before


@pytest.mark.parametrize("status,label", [("unknown", "Unknown"), ("unsupported", "N/A")])
def test_list_lookup_outcomes(listing, status, label):
    root, _, lookup = listing
    lookup.return_value = gh.PullRequestLookup(status, reason="gh not installed" if status == "unknown" else None)
    result = runner.invoke(cli.app, ["list", "--no-size"], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    assert label in result.stdout
    assert "No PR" not in result.stdout
    if status == "unknown":
        assert f"PR lookup unavailable for {root.name}" in result.stderr
        assert "gh not installed" in result.stderr
    else:
        assert result.stderr == ""


def test_no_pr_omits_column_and_all_lookup(listing):
    _, _, lookup = listing
    result = runner.invoke(cli.app, ["list", "--no-size", "--no-pr"])
    assert result.exit_code == 0, result.output
    assert "PR" not in result.stdout
    lookup.assert_not_called()


@pytest.mark.parametrize("flags", [[], ["--no-pr"], ["--pr"]])
def test_json_has_no_pr_lookup_or_new_fields(listing, flags):
    _, entries, lookup = listing
    result = runner.invoke(cli.app, ["list", "--json", *flags])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data == [{**entry, "repo": "repo", "last_modified": 1.0} for entry in entries]
    assert result.stderr == ""
    lookup.assert_not_called()


def test_verbose_and_size_columns_align(listing, monkeypatch):
    _, entries, _ = listing
    monkeypatch.setattr(cli.sizes, "dir_sizes_kb", lambda paths, **kwargs: {Path(w["path"]): 10 for w in entries})
    result = runner.invoke(cli.app, ["list", "--verbose"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert all(header in result.stdout for header in ["Size", "Path", "Merge", "PR"])
    assert "Open #123" in result.stdout


def test_stale_branch_still_has_pr_and_detached_is_not_queried(listing):
    root, entries, lookup = listing
    entries[1]["worktree"]["state"] = "prunable"
    entries[2]["branch"] = None
    result = runner.invoke(cli.app, ["list", "--no-size"], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    assert "Open #123" in result.stdout
    assert "N/A" in result.stdout
    assert "stale" in result.stdout
    lookup.assert_called_once_with(root, {"feature"})


def test_empty_and_main_only_repos_skip_lookup(listing):
    _, entries, lookup = listing
    entries[:] = entries[:1]
    result = runner.invoke(cli.app, ["list", "--all", "--no-size"])
    assert result.exit_code == 0, result.output
    assert "no extra worktrees" in result.stdout
    assert "PR" not in result.stdout
    lookup.assert_not_called()
    entries.clear()
    result = runner.invoke(cli.app, ["list", "--no-size"])
    assert result.exit_code == 0, result.output
    lookup.assert_not_called()


def test_repo_scoped_matching_and_empty_section_alignment(listing, monkeypatch):
    root, entries, lookup = listing
    other, quiet = root.parent / "other", root.parent / "quiet"
    monkeypatch.setattr(repo, "scope_repos", lambda path: [root, other, quiet])
    monkeypatch.setattr(
        wt,
        "list_worktrees_many",
        lambda roots: {
            root: entries,
            other: [_entry("main", other, main=True), _entry("feature", other / "feature")],
            quiet: [_entry("main", quiet, main=True)],
        },
    )
    lookup.side_effect = lambda path, branches: gh.PullRequestLookup(
        "ok",
        {
            "feature": (gh.PullRequest(123 if path == root else 456, f"https://github.com/owner/{path.name}/pull/1"),),
        },
    )
    result = runner.invoke(cli.app, ["list", "--no-size", "--all"], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    assert "Open #123" in result.stdout and "Open #456" in result.stdout
    assert "no extra worktrees" in result.stdout
    assert lookup.call_count == 2
    assert all(call.args[0] != quiet for call in lookup.call_args_list)


def test_pr_cell_links_each_number_without_io(monkeypatch):
    lookup = gh.PullRequestLookup(
        "ok",
        {
            "feature": (
                gh.PullRequest(1, "https://github.com/owner/repo/pull/1"),
                gh.PullRequest(2, "https://github.com/owner/repo/pull/2"),
            )
        },
    )
    monkeypatch.setattr(gh, "lookup_prs", Mock(side_effect=AssertionError("rendering did IO")))
    cell = cli._pr_cell(_entry("feature", Path("/repo")), lookup)
    assert cell.plain == "Open #1 +1"
    assert [span.style for span in cell.spans] == [
        "cyan link https://github.com/owner/repo/pull/1",
        "dim",
    ]
    console = Console(force_terminal=True, legacy_windows=False)
    with console.capture() as capture:
        console.print(cell)
    assert "\x1b]8;" in capture.get()


@pytest.mark.parametrize(
    "state,expected_style",
    [
        ("draft", "dim cyan"),
        ("open", "cyan"),
        ("merged", "green"),
        ("closed", ""),
    ],
)
def test_pr_cell_state_and_color(state, expected_style):
    pr = gh.PullRequest(123, "https://github.com/owner/repo/pull/123", state)
    cell = cli._pr_cell(_entry("feature", Path("/repo")), gh.PullRequestLookup("ok", {"feature": (pr,)}))
    assert cell.plain == f"{state.title()} #123"
    assert cell.style == expected_style


def test_pr_detail_includes_each_url_and_state(listing):
    _, _, lookup = listing
    lookup.return_value = gh.PullRequestLookup(
        "ok",
        {
            "feature": (
                gh.PullRequest(7, "https://github.com/owner/repo/pull/7", "draft"),
                gh.PullRequest(6, "https://github.com/owner/repo/pull/6", "merged"),
            )
        },
    )
    result = runner.invoke(cli.app, ["list", "--no-size", "--pr-detail"], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    assert "Draft #7 +1" in result.stdout
    assert "repo / feature: Draft #7 https://github.com/owner/repo/pull/7" in result.stdout
    assert "repo / feature: Merged #6 https://github.com/owner/repo/pull/6" in result.stdout


@pytest.mark.parametrize("flags", [["--no-pr"], ["--json"]])
def test_pr_detail_rejects_incompatible_modes(listing, flags):
    _, _, lookup = listing
    result = runner.invoke(cli.app, ["list", "--pr-detail", *flags])
    assert result.exit_code == 1
    assert "--pr-detail cannot be combined" in result.stderr
    lookup.assert_not_called()


def test_pr_detail_preserves_long_urls_at_narrow_width(listing):
    _, _, lookup = listing
    url = "https://github.com/owner/" + "long-repo-name-" * 8 + "/pull/123"
    lookup.return_value = gh.PullRequestLookup("ok", {"feature": (gh.PullRequest(123, url),)})
    result = runner.invoke(cli.app, ["list", "--no-size", "--pr-detail"], env={"COLUMNS": "40"})
    assert result.exit_code == 0, result.output
    assert url in result.stdout


def test_link_pr_ignores_git_environment_overrides(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("GIT_DIR", "/wrong")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, str(tmp_path)),
            subprocess.CompletedProcess([], 0, "feature\n"),
        ]
    )
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(gh, "lookup_prs", lambda *args: gh.PullRequestLookup("ok"))
    result = runner.invoke(cli.app, ["link-pr", str(tmp_path)])
    assert result.exit_code == 0
    assert all(not any(key.startswith("GIT_") for key in call.kwargs["env"]) for call in run.call_args_list)


def test_new_preview_table_unchanged(listing, monkeypatch):
    _, entries, lookup = listing
    monkeypatch.setattr(wt, "list_worktrees", lambda root: entries)
    cli._print_existing_worktrees(listing[0])
    lookup.assert_not_called()
    table, _, _ = cli._worktrees_table(entries[1:], show_size=False, dirty_mtimes={})
    assert [column.header for column in table.columns] == [
        "Branch",
        "Created",
        "Modified",
        "Working tree",
        "Merge",
    ]


@pytest.mark.parametrize("width", [80, 120, 200])
@pytest.mark.parametrize("count", [1, 6])
def test_default_size_layout_multiple_long_pr_numbers(listing, monkeypatch, width, count):
    _, entries, lookup = listing
    entries[1]["branch"] = "feature/verify-open-pr-status"
    monkeypatch.setattr(cli.sizes, "dir_sizes_kb", lambda paths, **kwargs: {Path(w["path"]): 10 for w in entries})
    lookup.return_value = gh.PullRequestLookup(
        "ok",
        {
            entries[1]["branch"]: tuple(
                gh.PullRequest(1234567 + i, f"https://github.com/owner/repo/pull/{1234567 + i}") for i in range(count)
            ),
        },
    )
    result = runner.invoke(cli.app, ["list"], env={"COLUMNS": str(width)})
    assert result.exit_code == 0, result.output
    assert "#1234567" in result.stdout
    if count > 1:
        assert f"+{count - 1}" in result.stdout
    assert "No PR" in result.stdout
    assert "clean" in result.stdout
    assert "unmerged" in result.stdout
    assert "feature" in result.stdout
    assert "Size" in result.stdout


def test_link_pr_submits_primary_and_never_changes_orca_implicitly(tmp_path, monkeypatch):
    import subprocess

    checkout = tmp_path.resolve()
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, str(checkout)),
            subprocess.CompletedProcess([], 0, "feature\n"),
        ]
    )
    monkeypatch.setattr(cli.subprocess, "run", run)
    lookup = Mock(
        return_value=gh.PullRequestLookup(
            "ok",
            {
                "feature": (
                    gh.PullRequest(7, "https://github.com/owner/repo/pull/7", "open"),
                    gh.PullRequest(6, "https://github.com/owner/repo/pull/6", "merged"),
                )
            },
        )
    )
    monkeypatch.setattr(gh, "lookup_prs", lookup)
    attach = Mock(return_value=1)
    monkeypatch.setattr(cli.orca, "attach_pr_references", attach, raising=False)
    result = runner.invoke(cli.app, ["link-pr", str(checkout)])
    assert result.exit_code == 0, result.output
    lookup.assert_called_once_with(checkout, ["feature"])
    attach.assert_called_once_with(checkout, "feature", ["https://github.com/owner/repo/pull/7"])
    assert "Submitted https://github.com/owner/repo/pull/7" in result.stdout


@pytest.mark.parametrize("status", ["unknown", "unsupported"])
def test_link_pr_unavailable_never_writes(tmp_path, monkeypatch, status):
    import subprocess

    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, str(tmp_path)),
            subprocess.CompletedProcess([], 0, "feature\n"),
        ]
    )
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(gh, "lookup_prs", lambda *args: gh.PullRequestLookup(status))
    attach = Mock()
    monkeypatch.setattr(cli.orca, "attach_pr_references", attach, raising=False)
    result = runner.invoke(cli.app, ["link-pr", str(tmp_path)])
    assert result.exit_code == 1
    attach.assert_not_called()


def test_link_pr_no_match_never_writes(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        cli.subprocess,
        "run",
        Mock(
            side_effect=[
                subprocess.CompletedProcess([], 0, str(tmp_path)),
                subprocess.CompletedProcess([], 0, "feature\n"),
            ]
        ),
    )
    monkeypatch.setattr(gh, "lookup_prs", lambda *args: gh.PullRequestLookup("ok"))
    attach = Mock()
    monkeypatch.setattr(cli.orca, "attach_pr_references", attach, raising=False)
    result = runner.invoke(cli.app, ["link-pr", str(tmp_path)])
    assert result.exit_code == 0
    assert "No matching PR" in result.stdout
    attach.assert_not_called()


def test_link_pr_rejects_detached_and_missing_worktree(tmp_path, monkeypatch):
    import subprocess

    lookup = Mock()
    monkeypatch.setattr(gh, "lookup_prs", lookup)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        Mock(
            side_effect=[
                subprocess.CompletedProcess([], 0, str(tmp_path)),
                subprocess.CompletedProcess([], 1, ""),
            ]
        ),
    )
    result = runner.invoke(cli.app, ["link-pr", str(tmp_path)])
    assert result.exit_code == 1
    assert "no checked-out branch" in result.stderr
    lookup.assert_not_called()


def test_link_pr_orca_failure_is_visible(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        cli.subprocess,
        "run",
        Mock(
            side_effect=[
                subprocess.CompletedProcess([], 0, str(tmp_path)),
                subprocess.CompletedProcess([], 0, "feature\n"),
            ]
        ),
    )
    monkeypatch.setattr(
        gh,
        "lookup_prs",
        lambda *args: gh.PullRequestLookup(
            "ok", {"feature": (gh.PullRequest(7, "https://github.com/owner/repo/pull/7"),)}
        ),
    )
    monkeypatch.setattr(
        cli.orca,
        "attach_pr_references",
        Mock(side_effect=cli.workflow.HandoffError("ambiguous workspace")),
        raising=False,
    )
    result = runner.invoke(cli.app, ["link-pr", str(tmp_path)])
    assert result.exit_code == 1
    assert "ambiguous workspace" in result.stderr


def test_collection_runs_repos_concurrently_with_worker_cap(monkeypatch):
    barrier = threading.Barrier(8)
    lock = threading.Lock()
    active = peak = 0
    calls = []

    def lookup(root, branches):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append((root, branches))
        try:
            barrier.wait(timeout=5)
            return gh.PullRequestLookup("ok")
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(gh, "lookup_prs", lookup)
    roots = [Path(f"/repo/{i}") for i in range(16)]
    result = cli._collect_list_prs({root: [_entry("feature", root)] for root in roots})
    assert set(result) == set(roots)
    assert len(calls) == 16
    assert peak == 8
    assert active == 0
