"""Remote integration, PR lifecycle, local changes, and local history stay separate."""

import copy
import datetime as dt
import json
import re
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
    # Identity evidence must agree across local and remote snapshots. Counts
    # stay unavailable unless a test supplies a measured collector result.
    facts = {
        root: cli.list_facts.RepoFacts(
            rows={
                row["path"]: cli.list_facts.WorktreeFacts(
                    path=row["path"],
                    head=("b" if row["branch"] == "waiting" else "a") * 40,
                    branch=row["branch"],
                    detached=False,
                    identity_verified=True,
                )
                for row in rows
            }
        ),
    }
    monkeypatch.setattr(cli.list_facts, "collect_many", Mock(return_value=facts))
    monkeypatch.setattr(cli.list_facts, "revalidate_many", Mock(side_effect=lambda entries, facts: facts))
    pr_lookup = Mock(
        return_value=gh.PullRequestLookup(
            "ok", {"feature": (gh.PullRequest(123, "https://github.com/owner/repo/pull/123", "merged"),)}
        )
    )
    monkeypatch.setattr(gh, "lookup_prs", pr_lookup)
    lookup = Mock(
        return_value=rd.RemoteDefaultLookup(
            {
                rows[1]["path"]: rd.HeadEvidence("reached", "a" * 40),
                rows[2]["path"]: rd.HeadEvidence("pending", "b" * 40),
            },
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
def test_default_column_and_independent_states(listing, width, monkeypatch):
    root, rows, lookup, pr_lookup, check = listing
    before = copy.deepcopy(rows)
    local = cli.list_facts.RepoFacts(
        rows={
            rows[1]["path"]: cli.list_facts.WorktreeFacts(
                path=rows[1]["path"],
                head="a" * 40,
                branch="feature",
                detached=False,
                identity_verified=True,
                staged=2,
                unstaged=3,
                untracked=4,
                unmerged=1,
                local_ahead=5,
                local_behind=6,
                local_contains=False,
            ),
            rows[2]["path"]: cli.list_facts.WorktreeFacts(
                path=rows[2]["path"],
                head="b" * 40,
                branch="waiting",
                detached=False,
                identity_verified=True,
            ),
        },
        target_ref="refs/heads/main",
        target_sha="b" * 40,
        target_source="cached config",
    )
    monkeypatch.setattr(cli.list_facts, "collect_many", Mock(return_value={root: local}))
    result = runner.invoke(cli.app, ["list", "--no-size"], env={"COLUMNS": str(width)})
    assert result.exit_code == 0, result.output
    assert "Reached" in result.stdout and "Pending" in result.stdout
    assert "HEAD integration" in result.stdout
    assert "PR lookup" in result.stdout and "Complete" in result.stdout
    assert "PR state" in result.stdout and re.search(r"\bMerged\b", result.stdout)
    assert "PR number" in result.stdout and re.search(r"\b123\b", result.stdout)
    assert "Merged #123" not in result.stdout
    assert all(
        label in result.stdout for label in ("Staged paths", "Unstaged paths", "Untracked paths", "Unmerged paths")
    )
    assert "Reported relation" in result.stdout and "Ahead" in result.stdout and "Behind" in result.stdout
    assert "feature" in result.stdout and "waiting" in result.stdout
    assert "Remote name: origin" in result.stdout
    assert "Remote default branch: main" in result.stdout
    assert "Observed remote commit: " + "a" * 12 in result.stdout
    assert (
        "Remote checked at: " + dt.datetime.fromtimestamp(1, dt.UTC).astimezone().isoformat(sep=" ", timespec="minutes")
        in result.stdout
    )
    assert "Local comparison branch: refs/heads/main" in result.stdout
    if width == 80:
        feature = result.stdout.split("Branch: feature", 1)[1].split("Branch: waiting", 1)[0]
        for label, value in (
            ("Staged paths", "2"),
            ("Unstaged paths", "3"),
            ("Untracked paths", "4"),
            ("Unmerged paths", "1"),
            ("Commits ahead", "5"),
            ("Commits behind", "6"),
            ("HEAD contained", "No"),
            ("Reported relation", "Ahead"),
            ("Content integrated", "No"),
            ("Predicted conflicts", "Unknown"),
            ("HEAD integration", "Reached"),
            ("PR state", "Merged"),
            ("PR number", "123"),
        ):
            assert f"{label}: {value}" in feature
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
    assert ("PR state" in result.stdout and re.search(r"\bMerged\b", result.stdout) is not None) == bool(pr_calls)
    assert ("Remote default branch" in result.stdout) == bool(remote_calls)
    assert ("HEAD integration" in result.stdout) == bool(remote_calls)
    assert ("PR lookup" in result.stdout) == bool(pr_calls)
    cli.list_facts.collect_many.assert_called_once()
    cli.list_facts.revalidate_many.assert_called_once()


@pytest.mark.parametrize("flags", [[], ["--remote-default"], ["--no-remote-default"]])
def test_json_unchanged_no_network(listing, flags):
    _, rows, lookup, pr_lookup, check = listing
    result = runner.invoke(cli.app, ["list", "--json", *flags])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data == [{**row, "repo": "repo", "last_modified": row.get("commit", {}).get("timestamp")} for row in rows]
    lookup.assert_not_called()
    pr_lookup.assert_not_called()
    check.assert_not_called()
    cli.list_facts.collect_many.assert_not_called()
    cli.list_facts.revalidate_many.assert_not_called()


@pytest.mark.parametrize("identity", ["missing-facts", "failed-snapshot", "head-moved", "remote-head-missing"])
def test_remote_evidence_requires_matching_local_snapshot(listing, monkeypatch, identity):
    root, rows, lookup, _, _ = listing
    if identity == "missing-facts":
        facts = {}
    else:
        snapshot = cli.list_facts.WorktreeFacts(
            path=rows[1]["path"],
            head=None if identity == "failed-snapshot" else ("c" if identity == "head-moved" else "a") * 40,
            branch=None if identity == "failed-snapshot" else "feature",
            detached=None if identity == "failed-snapshot" else False,
            identity_verified=identity != "failed-snapshot",
            reason="checkout identity changed" if identity == "failed-snapshot" else None,
        )
        facts = {root: cli.list_facts.RepoFacts(rows={rows[1]["path"]: snapshot})}
    monkeypatch.setattr(cli.list_facts, "collect_many", Mock(return_value=facts))
    if identity == "remote-head-missing":
        lookup.return_value = rd.RemoteDefaultLookup({rows[1]["path"]: rd.HeadEvidence("reached")})
    result = runner.invoke(cli.app, ["list", "--no-size"], env={"COLUMNS": "80"})

    assert result.exit_code == 0, result.output
    if identity == "failed-snapshot":
        feature = result.stdout.split("HEAD commit: Unknown", 1)[1].split("Branch: waiting", 1)[0]
        assert "Branch: Unknown" in result.stdout
        assert "PR lookup: Unknown" in feature
        assert "PR state: Unknown" in feature
    else:
        feature = result.stdout.split("Branch: feature", 1)[1].split("Branch: waiting", 1)[0]
        assert "PR state: Merged" in feature
    assert "HEAD integration: Unknown" in feature
    assert "HEAD integration: Reached" not in feature


def test_unknown_warns_and_preserves_listing(listing):
    _, rows, lookup, _, _ = listing
    lookup.return_value = rd.RemoteDefaultLookup({rows[1]["path"]: rd.HeadEvidence("unknown")}, reason="offline")
    result = runner.invoke(cli.app, ["list", "--no-size"], env={"COLUMNS": "160"})
    assert result.exit_code == 0 and "Unknown" in result.stdout
    assert "Remote default unavailable for repo: offline" in result.stderr
    assert "PR state" in result.stdout and re.search(r"\bMerged\b", result.stdout)
    assert "PR number" in result.stdout and re.search(r"\b123\b", result.stdout)


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
    assert all(
        label in result.stdout
        for label in (
            "Disk usage",
            "Path",
            "Local changes",
            "Reported relation",
            "Remote default integration",
            "PR state",
        )
    )
    assert "Reached" in result.stdout


def test_main_only_repo_is_displayed_without_remote_or_pr_collection(listing):
    _, rows, lookup, pr_lookup, check = listing
    rows[:] = rows[:1]
    result = runner.invoke(cli.app, ["list", "--no-size"])
    assert result.exit_code == 0, result.output
    assert "Worktree type: Main" in result.stdout
    assert "HEAD integration: Not checked" in result.stdout
    assert "PR lookup: Not checked" in result.stdout
    assert "Main worktrees: 1" in result.stdout
    assert "Linked worktrees: 0" in result.stdout
    lookup.assert_not_called()
    pr_lookup.assert_not_called()
    check.assert_not_called()
