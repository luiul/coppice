"""Both layouts preserve the same atomic facts and contain no collection IO."""

import copy
from pathlib import Path

import pytest
from rich.console import Console

from coppice import cli, gh, list_facts, list_view, remote_default, repo, wt


def sample():
    root = Path("/repo")
    rows = [
        {
            "path": "/repo",
            "branch": "topic-not-default",
            "is_main": True,
            "is_current": False,
            "main_state": "is_main",
            "worktree": {"detached": False},
        },
        {
            "path": "/feature",
            "branch": "feature/very-long-atomic-name",
            "is_main": False,
            "is_current": True,
            "main_state": "ahead",
            "worktree": {"detached": False},
            "parked_at": 1000,
        },
    ]
    facts = {
        row["path"]: list_facts.WorktreeFacts(
            path=row["path"],
            head="a" * 40,
            branch=row["branch"],
            detached=False,
            identity_verified=True,
            directory_created=1000,
            commit_time=2000,
            file_changed=3000,
            staged=1,
            unstaged=2,
            untracked=3,
            unmerged=4,
            local_ahead=2,
            local_behind=0,
            local_contains=False,
            reason=None,
        )
        for row in rows
    }
    repo_facts = list_facts.RepoFacts(
        rows=facts, target_ref="refs/heads/main", target_sha="b" * 40, target_source="Cached configuration", reason=None
    )
    pr = gh.PullRequestLookup(
        "ok",
        {
            rows[1]["branch"]: (
                gh.PullRequest(123, "https://github.com/owner/repo/pull/123", "merged"),
                gh.PullRequest(124, "https://github.com/owner/repo/pull/124", "closed"),
            )
        },
    )
    remote = remote_default.RemoteDefaultLookup(
        {"/feature": remote_default.HeadEvidence("reached", "a" * 40)}, "main", "c" * 40, 1000
    )
    return root, rows, repo_facts, pr, remote


def output(width=120, *, verbose=False, show_size=False, pr_enabled=True, remote_enabled=True):
    root, rows, facts, pr, remote = sample()
    console = Console(width=width, highlight=False, force_terminal=False)
    with console.capture() as capture:
        list_view.render(
            console,
            [root],
            {root: rows},
            {root: facts},
            show_all=False,
            show_size=show_size,
            verbose=verbose,
            size_cache={Path("/repo"): 1, Path("/feature"): 2},
            pr_lookups={root: pr} if pr_enabled else None,
            remote_lookups={root: remote} if remote_enabled else None,
        )
    return capture.get()


@pytest.mark.parametrize("width", [80, 120, 200])
def test_same_atomic_values_in_each_layout(width):
    text = output(width)
    assert "main: main" not in text
    assert "topic-not-default" in text
    assert "feature/very-long-atomic-name" in text
    assert "Main worktrees: 1" in text and "Linked worktrees: 1" in text
    assert "Worktree type" in text and "Current" in text and "HEAD state" in text
    for label in (
        "Staged paths",
        "Unstaged paths",
        "Untracked paths",
        "Unmerged paths",
        "Commits ahead",
        "Commits behind",
        "HEAD contained",
        "PR state",
        "PR number",
        "Additional PRs",
    ):
        assert label in text
    assert "Reached" in text and "Merged" in text and "123" in text
    assert "Merged #123" not in text and "+1" not in text
    assert "Directory created" in text and "HEAD committed" in text and "Local file changed" in text
    assert max(len(line) for line in text.splitlines()) <= width


def test_disabled_fields_absent():
    text = output(pr_enabled=False, remote_enabled=False)
    assert "Pull requests" not in text and "HEAD integration" not in text
    assert "Disk usage" not in text and "Path:" not in text


def test_complete_long_path_wraps_not_ellipsized():
    root, rows, facts, _, _ = sample()
    rows[1]["path"] = "/path/" + "long-directory/" * 12 + "distinct-tail"
    console = Console(width=80, highlight=False)
    with console.capture() as capture:
        list_view.render(
            console,
            [root],
            {root: rows},
            {root: facts},
            show_all=False,
            show_size=False,
            verbose=True,
            size_cache=None,
            pr_lookups=None,
            remote_lookups=None,
        )
    text = capture.get()
    compact = "".join(text.split())
    assert rows[1]["path"] in compact
    assert "distinct-tail" in text and "…" not in text


def test_missing_facts_not_clean_zero_or_time_fallback():
    row = {"path": "/missing", "branch": "feature"}
    values = list_view._fields(row, None, None, None, None)
    assert all(
        values[label].text == "Unknown"
        for label in ("Staged paths", "Unstaged paths", "Unmerged paths", "Directory created", "Local file changed")
    )
    assert values["PR state"].text == "Not checked"


def test_timestamp_no_commit_fallback():
    _, rows, facts, pr, remote = sample()
    old = facts.rows["/feature"]
    from dataclasses import replace

    values = list_view._fields(rows[1], replace(old, directory_created=None, file_changed=None), pr, remote, None)
    assert values["Directory created"].text == "Unknown"
    assert values["Local file changed"].text == "Unknown"
    assert values["HEAD committed"].text != "Unknown"


def test_stale_has_path_but_no_fabricated_local_facts():
    row = {"path": "/gone", "branch": "old", "worktree": {"state": "prunable"}}
    values = list_view._fields(row, None, gh.PullRequestLookup("ok"), None, None)
    assert values["Prunable"].text == "Yes"
    assert values["Staged paths"].text == "N/A" and values["HEAD integration"].text == "N/A"
    assert values["Path"].text == "/gone"


def test_render_preserves_inputs_and_never_collects(monkeypatch):
    root, rows, facts, pr, remote = sample()
    before = copy.deepcopy(rows)
    monkeypatch.setattr(list_facts, "collect_many", lambda *args: (_ for _ in ()).throw(AssertionError("render IO")))
    console = Console(width=120)
    with console.capture():
        list_view.render(
            console,
            [root],
            {root: rows},
            {root: facts},
            show_all=False,
            show_size=False,
            verbose=False,
            size_cache=None,
            pr_lookups={root: pr},
            remote_lookups={root: remote},
        )
    assert rows == before


def test_current_is_original_command_directory_not_query_root():
    _, rows, facts, pr, remote = sample()
    rows[0]["is_current"] = True
    rows[1]["is_current"] = False
    main = list_view._fields(rows[0], facts.rows["/repo"], pr, remote, None, "/feature")
    feature = list_view._fields(rows[1], facts.rows["/feature"], pr, remote, None, "/feature")
    assert main["Current"].text == "No" and feature["Current"].text == "Yes"


def test_ancestor_integration_not_called_different_history():
    row = {"branch": "old", "path": "/old", "main_state": "integrated", "integration_reason": "ancestor"}
    values = list_view._fields(row, None, None, None, None)
    assert values["Reported relation"].text == "Ancestor contained"
    assert values["Integration method"].text == "Strict ancestor"


def test_failed_revalidation_never_restores_registration_identity():
    row = {"branch": "old", "path": "/old", "commit": {"sha": "a" * 40}, "worktree": {"detached": False}}
    facts = list_facts.WorktreeFacts("/old", reason="checkout changed")
    remote = remote_default.RemoteDefaultLookup({"/old": remote_default.HeadEvidence("reached", "a" * 40)})
    values = list_view._fields(row, facts, gh.PullRequestLookup("ok"), remote, None)
    assert values["Branch"].text == "Unknown" and values["HEAD commit"].text == "Unknown"
    assert values["PR lookup"].text == "Unknown" and values["HEAD integration"].text == "Unknown"


def test_remote_evidence_requires_matching_head():
    _, rows, facts, pr, remote = sample()
    remote.cells["/feature"] = remote_default.HeadEvidence("reached", "b" * 40)
    values = list_view._fields(rows[1], facts.rows["/feature"], pr, remote, None)
    assert values["HEAD integration"].text == "Unknown"


def test_unverified_registered_head_not_remote_proof():
    row = {"branch": "old", "path": "/old"}
    facts = list_facts.WorktreeFacts("/old", head="a" * 40, branch="old", detached=False, reason="read failed")
    remote = remote_default.RemoteDefaultLookup({"/old": remote_default.HeadEvidence("reached", "a" * 40)})
    values = list_view._fields(row, facts, None, remote, None)
    assert values["HEAD commit"].text == "Unknown" and values["HEAD integration"].text == "Unknown"


def test_wide_repeated_branch_never_revives_invalid_identity():
    root, rows, facts, pr, remote = sample()
    facts.rows["/feature"] = list_facts.WorktreeFacts("/feature", reason="changed checkout")
    console = Console(width=200, highlight=False)
    with console.capture() as capture:
        list_view.render(
            console,
            [root],
            {root: rows},
            {root: facts},
            show_all=False,
            show_size=False,
            verbose=False,
            size_cache=None,
            pr_lookups={root: pr},
            remote_lookups={root: remote},
        )
    assert "feature/very-long-atomic-name" not in capture.get()


def test_explain_skips_repo_wt_and_network(monkeypatch):
    from typer.testing import CliRunner

    def forbidden(*args, **kwargs):
        raise AssertionError("explanation must not scan")

    for module, symbol in (
        (repo, "scope_repos"),
        (wt, "require_wt"),
        (list_facts, "collect_many"),
        (gh, "lookup_prs"),
        (remote_default, "lookup"),
    ):
        monkeypatch.setattr(module, symbol, forbidden)
    result = CliRunner().invoke(cli.app, ["list", "--explain"])
    assert result.exit_code == 0, result.output
    assert "Staged paths" in result.stdout and "committer" in result.stdout


def test_explanation_uses_registry_without_io():
    console = Console(width=80)
    with console.capture() as capture:
        list_view.explain(console)
    text = capture.get()
    assert "index" in text and "Detached" in text and "creation" in text
    assert all(field.label in text for _, fields in list_view.GROUPS for field in fields)
