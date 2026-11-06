import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from coppice import gh


def _init_repo_with_remote(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote_url], check=True)
    return path


def test_repo_slug_ssh_remote(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "git@github.com:owner/repo.git")
    assert gh.repo_slug(repo_dir) == "owner/repo"


def test_repo_slug_https_remote(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "https://github.com/owner/repo.git")
    assert gh.repo_slug(repo_dir) == "owner/repo"


def test_repo_slug_https_remote_without_dot_git_suffix(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "https://github.com/owner/repo")
    assert gh.repo_slug(repo_dir) == "owner/repo"


def test_repo_slug_matches_repo_name_containing_a_dot(tmp_path):
    """Confirmed bug: the old regex's `[^/.]+?` repo-name group excluded
    dots, so any GitHub repo whose name contains a literal dot (a common,
    valid case, e.g. `my.project`) silently failed to match at all. Since
    `open_prs` returns `{}` when `repo_slug` is None, `cop clean`'s open-PR
    safety check would silently no-op for such repos.
    """
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "git@github.com:owner/my.project.git")
    assert gh.repo_slug(repo_dir) == "owner/my.project"


def test_repo_slug_none_for_non_github_remote(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "git@gitlab.com:owner/repo.git")
    assert gh.repo_slug(repo_dir) is None


def test_repo_slug_none_when_no_origin_remote(tmp_path):
    tmp_path_repo = tmp_path / "repo"
    tmp_path_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path_repo)], check=True)
    assert gh.repo_slug(tmp_path_repo) is None


def test_open_prs_returns_empty_without_wanted_branches(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "git@github.com:owner/repo.git")
    assert gh.open_prs(repo_dir, []) == {}


# Projection of a real gh api pulls response, checked against
# https://github.com/psf/requests/pull/7640. Keep only fields read here.
_PR = {
    "state": "open",
    "number": 7640,
    "html_url": "https://github.com/psf/requests/pull/7640",
    "head": {"ref": "dependabot/github_actions/actions-e115530b2f", "repo": {"full_name": "psf/requests"}},
    "draft": False,
    "merged_at": None,
    "updated_at": "2026-01-01T00:00:00Z",
    "base": {"repo": {"full_name": "psf/requests"}},
}


def _pr(number=7640, branch="feature", head_repo="psf/requests", *, draft=False):
    pr = copy.deepcopy(_PR)
    pr.update(number=number, html_url=f"https://github.com/psf/requests/pull/{number}", draft=draft)
    pr["head"] = {"ref": branch, "repo": {"full_name": head_repo} if head_repo else None}
    return pr


def _lookup_mock(monkeypatch, pages, *, remote="git@github.com:psf/requests.git"):
    monkeypatch.setattr(gh.shutil, "which", lambda name: "/usr/bin/gh")
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, remote + "\n"),
            subprocess.CompletedProcess([], 0, json.dumps(pages)),
        ]
    )
    monkeypatch.setattr(gh.subprocess, "run", run)
    return run


def test_lookup_prs_pages_drafts_forks_and_multiple_matches(monkeypatch):
    run = _lookup_mock(
        monkeypatch,
        [
            [_PR, _pr(9, draft=True), _pr(8, head_repo="someone/requests")],
            [_pr(3), _pr(2, branch="other"), _pr(1, head_repo=None)],
        ],
    )
    result = gh.lookup_prs(Path("/repo"), ["feature", _PR["head"]["ref"], "missing"])
    assert result.status == "ok"
    assert result.reason is None
    assert [pr.number for pr in result.prs["feature"]] == [9, 3]
    assert result.prs["feature"][0].state == "draft"
    assert result.prs[_PR["head"]["ref"]] == (
        gh.PullRequest(7640, _PR["html_url"], "open", "2026-01-01T00:00:00+00:00"),
    )
    assert "missing" not in result.prs
    assert run.call_count == 2
    remote_call = run.call_args_list[0]
    assert remote_call.args == (["git", "-C", "/repo", "remote", "get-url", "origin"],)
    assert remote_call.kwargs["capture_output"] is True
    assert remote_call.kwargs["text"] is True
    assert remote_call.kwargs["timeout"] == 5
    assert not any(key.startswith("GIT_") for key in remote_call.kwargs["env"])
    run.assert_any_call(
        [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            "repos/psf/requests/pulls?state=all&per_page=100",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_lookup_prs_repo_case_insensitive_branch_case_sensitive(monkeypatch):
    pr = _pr(head_repo="PSF/REQUESTS")
    pr["html_url"] = "https://github.com/PSF/REQUESTS/pull/7640"
    _lookup_mock(monkeypatch, [[pr, _pr(1, branch="FEATURE")]])
    result = gh.lookup_prs(Path("/repo"), ["feature"])
    assert result.status == "ok"
    assert result.prs == {"feature": (gh.PullRequest(7640, pr["html_url"], "open", "2026-01-01T00:00:00+00:00"),)}


def test_lookup_prs_successful_empty(monkeypatch):
    _lookup_mock(monkeypatch, [[]])
    assert gh.lookup_prs(Path("/repo"), ["feature"]) == gh.PullRequestLookup("ok")


@pytest.mark.parametrize(
    "remote",
    [
        "git@gitlab.com:psf/requests.git",
        "https://notgithub.com/psf/requests.git",
        "https://github.com.evil.test/psf/requests.git",
        "https://github.com/psf/requests/extra",
    ],
)
def test_lookup_prs_unsupported_remote(monkeypatch, remote):
    run = _lookup_mock(monkeypatch, [[]], remote=remote)
    assert gh.lookup_prs(Path("/repo"), ["feature"]).status == "unsupported"
    assert run.call_count == 1


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:psf/requests.git",
        "https://github.com/psf/requests.git",
        "https://github.com/psf/requests",
        "ssh://git@github.com/psf/requests.git",
        "ssh://git@github.com:22/psf/requests.git",
        "https://GITHUB.COM/psf/requests.git",
        "git@GITHUB.COM:psf/requests.git",
    ],
)
def test_lookup_prs_supported_remote(monkeypatch, remote):
    _lookup_mock(monkeypatch, [[]], remote=remote)
    assert gh.lookup_prs(Path("/repo"), ["feature"]).status == "ok"


def test_lookup_prs_missing_origin(tmp_path):
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    assert gh.lookup_prs(path, ["feature"]).status == "unsupported"


def test_lookup_prs_empty_branches_skip_all_io(monkeypatch):
    run = Mock(side_effect=AssertionError("unexpected IO"))
    monkeypatch.setattr(gh.subprocess, "run", run)
    assert gh.lookup_prs(Path("/repo"), []).status == "unsupported"
    run.assert_not_called()


def test_lookup_prs_missing_gh(monkeypatch):
    run = _lookup_mock(monkeypatch, [[]])
    monkeypatch.setattr(gh.shutil, "which", lambda name: None)
    result = gh.lookup_prs(Path("/repo"), ["feature"])
    assert result.status == "unknown"
    assert result.reason == "gh not installed"
    assert run.call_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CompletedProcess([], 1, "", "authentication failed"),
        subprocess.TimeoutExpired("gh", 15),
        OSError("not executable"),
    ],
)
def test_lookup_prs_gh_failures(monkeypatch, failure):
    run = _lookup_mock(monkeypatch, [[]])
    run.side_effect = [subprocess.CompletedProcess([], 0, "git@github.com:psf/requests.git"), failure]
    result = gh.lookup_prs(Path("/repo"), ["feature"])
    assert result.status == "unknown"
    assert result.reason
    assert result.prs == {}


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CompletedProcess([], 128, "", "not a repo"),
        subprocess.TimeoutExpired("git", 5),
        OSError("not executable"),
    ],
)
def test_lookup_prs_git_failures(monkeypatch, failure):
    run = _lookup_mock(monkeypatch, [[]])
    run.side_effect = [failure]
    result = gh.lookup_prs(Path("/repo"), ["feature"])
    assert result.status == "unknown"
    assert result.reason == "origin lookup failed"
    assert run.call_count == 1


@pytest.mark.parametrize("payload", ["", "bad json", "[]", "{}", "[null]", '[{"number": 1}]'])
def test_lookup_prs_invalid_pages(monkeypatch, payload):
    run = _lookup_mock(monkeypatch, [[]])
    run.side_effect = [
        subprocess.CompletedProcess([], 0, "git@github.com:psf/requests.git"),
        subprocess.CompletedProcess([], 0, payload),
    ]
    result = gh.lookup_prs(Path("/repo"), ["feature"])
    assert result == gh.PullRequestLookup("unknown", reason="invalid GitHub response")


@pytest.mark.parametrize(
    "field,value",
    [
        ("base", {}),
        ("base", {"repo": {"full_name": "invalid"}}),
        ("base", {"repo": {"full_name": "other/repo"}}),
        ("state", "invalid"),
        ("number", True),
        ("number", 0),
        ("number", "7640"),
        ("html_url", "https://evil.test/psf/requests/pull/7640"),
        ("html_url", "https://github.com/other/repo/pull/7640"),
        ("html_url", "https://github.com/psf/requests/pull/1"),
        ("head", {}),
        ("head", {"ref": "feature"}),
        ("head", {"ref": "feature", "repo": {}}),
        ("head", {"ref": 1, "repo": {"full_name": "psf/requests"}}),
    ],
)
def test_lookup_prs_invalid_record_discards_partial_results(monkeypatch, field, value):
    invalid = _pr()
    invalid[field] = value
    _lookup_mock(monkeypatch, [[_pr(1)], [invalid]])
    assert gh.lookup_prs(Path("/repo"), ["feature"]) == gh.PullRequestLookup(
        "unknown", reason="invalid GitHub response"
    )


def test_lookup_prs_renamed_origin_uses_canonical_base(monkeypatch):
    _lookup_mock(
        monkeypatch, [[_pr(), _pr(2, head_repo="someone/requests")]], remote="git@github.com:old-owner/old-name.git"
    )
    result = gh.lookup_prs(Path("/repo"), ["feature"])
    assert result.status == "ok"
    assert result.prs == {
        "feature": (
            gh.PullRequest(7640, "https://github.com/psf/requests/pull/7640", "open", "2026-01-01T00:00:00+00:00"),
        )
    }


def test_lookup_prs_rejects_inconsistent_canonical_repos(monkeypatch):
    invalid = _pr(2)
    invalid["base"]["repo"]["full_name"] = "other/repo"
    invalid["html_url"] = "https://github.com/other/repo/pull/2"
    _lookup_mock(monkeypatch, [[_pr()], [invalid]])
    assert gh.lookup_prs(Path("/repo"), ["feature"]).status == "unknown"


def test_lookup_prs_lifecycle_priority_across_pages(monkeypatch):
    closed = _pr(10)
    closed.update(state="closed", merged_at=None, updated_at="2026-05-01T00:00:00Z")
    merged = _pr(11)
    merged.update(state="closed", merged_at="2026-04-01T00:00:00Z", updated_at="2026-04-01T00:00:00Z")
    draft = _pr(12, draft=True)
    draft.update(updated_at="2026-03-01T00:00:00Z")
    opened = _pr(13)
    opened.update(updated_at="2026-02-01T00:00:00Z")
    _lookup_mock(monkeypatch, [[merged, opened], [closed, draft]])
    result = gh.lookup_prs(Path("/repo"), ["feature"])
    assert result.status == "ok"
    assert [(pr.number, pr.state) for pr in result.prs["feature"]] == [
        (12, "draft"),
        (13, "open"),
        (10, "closed"),
        (11, "merged"),
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"draft": None},
        {"updated_at": None},
        {"updated_at": "not-a-date"},
        {"updated_at": "2026-01-01T00:00:00"},
        {"merged_at": 1},
        {"state": "open", "merged_at": "2026-01-01T00:00:00Z"},
        {"state": "closed", "merged_at": "not-a-date"},
    ],
)
def test_lookup_prs_invalid_lifecycle_fails_closed(monkeypatch, fields):
    invalid = _pr(2)
    invalid.update(fields)
    _lookup_mock(monkeypatch, [[_pr(1)], [invalid]])
    assert gh.lookup_prs(Path("/repo"), ["feature"]).status == "unknown"


def test_lookup_prs_missing_merged_field_is_unknown(monkeypatch):
    invalid = _pr(2)
    del invalid["merged_at"]
    _lookup_mock(monkeypatch, [[invalid]])
    assert gh.lookup_prs(Path("/repo"), ["feature"]).status == "unknown"


def test_legacy_open_prs_contract_unchanged(monkeypatch):
    monkeypatch.setattr(gh.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(gh, "repo_slug", lambda repo: "owner/repo")
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    {"number": 123, "title": "Keep cleanup behavior", "headRefName": "feature"},
                ]
            ),
        )
    )
    monkeypatch.setattr(gh.subprocess, "run", run)
    assert gh.open_prs(Path("/repo"), ["feature", "missing"]) == {"feature": "#123 Keep cleanup behavior"}
    run.assert_called_once_with(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "owner/repo",
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
