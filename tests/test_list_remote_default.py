"""Remote-default cells remain independent of PR, dirtiness, and local Merge."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from coppice import cli, gh, repo, wt
from coppice import remote_default as rd

runner = CliRunner()


@pytest.fixture
def listing(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    rows = [
        {"path": str(root), "branch": "main", "is_main": True},
        {
            "path": str(tmp_path / "feature"),
            "branch": "feature",
            "main_state": "ahead",
            "commit": {"timestamp": 1},
            "working_tree": {"modified": True},
        },
        {"path": str(tmp_path / "waiting"), "branch": "waiting", "main_state": "behind", "commit": {"timestamp": 1}},
    ]
    monkeypatch.setattr(cli, "console", Console(highlight=False))
    monkeypatch.setattr(cli, "err", Console(stderr=True, highlight=False))
    monkeypatch.setattr(repo, "scope_repos", lambda path: [root])
    monkeypatch.setattr(wt, "require_wt", lambda: None)
    monkeypatch.setattr(wt, "list_worktrees_many", lambda roots: {root: rows})
    monkeypatch.setattr(cli.park_mod, "parked_at_many", lambda roots: {})
    monkeypatch.setattr(cli, "_collect_dirty_mtimes", lambda rows: {str(row["path"]): None for row in rows})
    pr_lookup = Mock(
        return_value=gh.PullRequestLookup(
            "ok", {"feature": (gh.PullRequest(123, "https://github.com/owner/repo/pull/123", "merged"),)}
        )
    )
    monkeypatch.setattr(gh, "lookup_prs", pr_lookup)
    lookup = Mock(
        return_value=rd.RemoteDefaultLookup(
            {rows[1]["path"]: rd.HeadEvidence("reached"), rows[2]["path"]: rd.HeadEvidence("pending")},
            "main",
            "a" * 40,
            1,
        )
    )
    monkeypatch.setattr(rd, "lookup", lookup)
    check = Mock(side_effect=lambda root, rows, data: data)
    monkeypatch.setattr(rd, "revalidate", check)
    return root, rows, lookup, pr_lookup, check


@pytest.mark.parametrize("width", [80, 120, 200])
def test_default_column_and_independent_states(listing, width):
    root, rows, lookup, pr_lookup, check = listing
    before = copy.deepcopy(rows)
    result = runner.invoke(cli.app, ["list", "--no-size"], env={"COLUMNS": str(width)})
    assert result.exit_code == 0, result.output
    assert "Reached" in result.stdout and "Pending" in result.stdout
    assert "Merged #123" in result.stdout
    assert "dirty" in result.stdout and "unmerged" in result.stdout
    assert "feature" in result.stdout and "waiting" in result.stdout
    assert "origin/main" in result.stdout
    assert result.stderr == ""
    assert rows == before
    lookup.assert_called_once_with(root, rows)
    assert pr_lookup.call_count == 1 and check.call_count == 1


@pytest.mark.parametrize(
    "flags,remote_calls,pr_calls",
    [
        (["--no-pr"], 1, 0),
        (["--no-remote-default"], 0, 1),
        (["--no-pr", "--no-remote-default"], 0, 0),
    ],
)
def test_independent_opt_outs(listing, flags, remote_calls, pr_calls):
    _, _, lookup, pr_lookup, check = listing
    result = runner.invoke(cli.app, ["list", "--no-size", *flags], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    assert lookup.call_count == remote_calls and pr_lookup.call_count == pr_calls
    assert check.call_count == remote_calls
    assert ("Reached" in result.stdout) == bool(remote_calls)
    assert ("Merged #123" in result.stdout) == bool(pr_calls)


@pytest.mark.parametrize("flags", [[], ["--remote-default"], ["--no-remote-default"]])
def test_json_unchanged_no_network(listing, flags):
    _, rows, lookup, pr_lookup, check = listing
    result = runner.invoke(cli.app, ["list", "--json", *flags])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert len(data) == len(rows)
    assert all("remote_default" not in row and "pr" not in row for row in data)
    lookup.assert_not_called()
    pr_lookup.assert_not_called()
    check.assert_not_called()


def test_unknown_warns_and_preserves_listing(listing):
    _, rows, lookup, _, _ = listing
    lookup.return_value = rd.RemoteDefaultLookup({rows[1]["path"]: rd.HeadEvidence("unknown")}, reason="offline")
    result = runner.invoke(cli.app, ["list", "--no-size"], env={"COLUMNS": "160"})
    assert result.exit_code == 0 and "Unknown" in result.stdout
    assert "Remote default unavailable for repo: offline" in result.stderr
    assert "Merged #123" in result.stdout


@pytest.mark.parametrize(
    "state,label,style",
    [
        ("reached", "Reached", "green"),
        ("pending", "Pending", "cyan"),
        ("unknown", "Unknown", "yellow"),
        ("unsupported", "N/A", "dim"),
    ],
)
def test_cell_styles_pure_and_plain(state, label, style, monkeypatch):
    monkeypatch.setattr(rd, "lookup", Mock(side_effect=AssertionError("render IO")))
    row = {"path": "/feature", "branch": "feature"}
    cell = cli._remote_default_cell(row, rd.RemoteDefaultLookup({"/feature": rd.HeadEvidence(state)}))
    assert cell.plain == label and cell.style == style


def test_stale_and_detached_na_without_lookup():
    assert cli._remote_default_cell({"branch": None}, None).plain == "N/A"
    assert cli._remote_default_cell({"branch": "x", "worktree": {"state": "prunable"}}, None).plain == "N/A"


def test_size_verbose_and_section_alignment(listing, monkeypatch):
    _, rows, _, _, _ = listing
    monkeypatch.setattr(cli.sizes, "dir_sizes_kb", lambda paths, **kwargs: {Path(w["path"]): 10 for w in rows})
    result = runner.invoke(cli.app, ["list", "--verbose"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert all(label in result.stdout for label in ("Size", "Path", "Working tree", "Merge", "Remote", "default", "PR"))
    assert "Reached" in result.stdout


def test_no_rows_skip_collection(listing):
    _, rows, lookup, pr_lookup, check = listing
    rows[:] = rows[:1]
    result = runner.invoke(cli.app, ["list", "--no-size"])
    assert result.exit_code == 0 and "no extra worktrees" in result.stdout
    lookup.assert_not_called()
    pr_lookup.assert_not_called()
    check.assert_not_called()
