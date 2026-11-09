"""Remote evidence is fresh, commit-specific, bounded, and side-effect free."""

import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from coppice import remote_default as rd

A, B, C = "a" * 40, "b" * 40, "c" * 40
SLUG = "owner/repo"


def entry(path, branch="feature", head=A):
    return {"path": str(path), "branch": branch, "commit": {"sha": head}, "worktree": {"state": "active"}}


def pr(*, head=A, base="main", merge=C, number=1):
    return {
        "number": number,
        "html_url": f"https://github.com/{SLUG}/pull/{number}",
        "state": "closed",
        "merged_at": "2026-11-06T12:00:00Z",
        "head": {"sha": head, "repo": {"full_name": SLUG}},
        "base": {"ref": base, "repo": {"full_name": SLUG}},
        "merge_commit_sha": merge,
    }


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    row = entry(tmp_path / "feature")
    inventory = {str(tmp_path / "feature"): ("feature", A)}
    calls = []
    data = {
        f"repos/{SLUG}": {"full_name": SLUG, "default_branch": "main"},
        f"repos/{SLUG}/commits/main": {"sha": B},
        f"repos/{SLUG}/commits/{A}/pulls?per_page=100": [[]],
    }
    ancestors = {(A, B): False, (C, B): True}
    monkeypatch.setattr(rd.shutil, "which", lambda name: "/bin/gh")
    monkeypatch.setattr(rd._Reader, "inventory", lambda self: copy.deepcopy(inventory))

    def run(self, args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "https://github.com/owner/repo.git\n", "")

    def api(self, endpoint, **kwargs):
        calls.append(endpoint)
        value = data[endpoint]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    monkeypatch.setattr(rd._Reader, "run", run)
    monkeypatch.setattr(rd._Reader, "api", api)
    monkeypatch.setattr(rd._Reader, "ancestor", lambda self, slug, source, target: ancestors[source, target])
    return tmp_path, row, inventory, data, ancestors, calls


def state(evidence):
    root, row, *_ = evidence
    result = rd.lookup(root, [row])
    return result.cells[row["path"]], result


def test_direct_ancestry_without_pr(evidence):
    evidence[4][A, B] = True
    cell, result = state(evidence)
    assert cell.state == "reached" and cell.head == A
    assert result.branch == "main" and result.remote_sha == B and result.checked_at
    assert not any(isinstance(call, str) and "/pulls" in call for call in evidence[5])


def test_pending_with_complete_commit_pr_response(evidence):
    cell, result = state(evidence)
    assert cell.state == "pending" and result.reason is None


@pytest.mark.parametrize("branch", [None, 123, {}, ""])
def test_invalid_default_metadata_does_not_escape_to_renderer(evidence, branch):
    evidence[3][f"repos/{SLUG}"]["default_branch"] = branch
    cell, result = state(evidence)
    assert cell.state == "unknown" and result.branch is None


def test_squash_or_rebase_exact_head_and_merge_ancestry(evidence):
    evidence[3][f"repos/{SLUG}/commits/{A}/pulls?per_page=100"] = [[pr()]]
    assert state(evidence)[0].state == "reached"


def test_renamed_branch_uses_commit_not_name(evidence):
    row, inventory = evidence[1:3]
    row["branch"] = "renamed"
    inventory[row["path"]] = ("renamed", A)
    evidence[3][f"repos/{SLUG}/commits/{A}/pulls?per_page=100"] = [[pr()]]
    assert state(evidence)[0].state == "reached"


@pytest.mark.parametrize("candidate", [pr(head=C), pr(base="release")])
def test_historical_head_or_wrong_target_not_proof(evidence, candidate):
    evidence[3][f"repos/{SLUG}/commits/{A}/pulls?per_page=100"] = [[candidate]]
    assert state(evidence)[0].state == "pending"


def test_merge_not_in_current_default(evidence):
    evidence[3][f"repos/{SLUG}/commits/{A}/pulls?per_page=100"] = [[pr()]]
    evidence[4][C, B] = False
    assert state(evidence)[0].state == "pending"


@pytest.mark.parametrize("pages", [[], {}, [None], [[{}]], [[{**pr(), "merge_commit_sha": None}]]])
def test_incomplete_or_malformed_merge_evidence_unknown(evidence, pages):
    evidence[3][f"repos/{SLUG}/commits/{A}/pulls?per_page=100"] = pages
    assert state(evidence)[0].state == "unknown"


def test_valid_positive_survives_other_bad_candidate(evidence):
    evidence[3][f"repos/{SLUG}/commits/{A}/pulls?per_page=100"] = [[{}], [pr()]]
    assert state(evidence)[0].state == "reached"


@pytest.mark.parametrize("field", ["head", "base"])
def test_wrong_repository_unknown(evidence, field):
    candidate = pr()
    candidate[field]["repo"]["full_name"] = "fork/repo"
    evidence[3][f"repos/{SLUG}/commits/{A}/pulls?per_page=100"] = [[candidate]]
    assert state(evidence)[0].state == "unknown"


def test_unknown_not_pending_on_api_failure(evidence):
    evidence[3][f"repos/{SLUG}/commits/{A}/pulls?per_page=100"] = rd._Unavailable("offline")
    assert state(evidence)[0].state == "unknown"


def test_dirty_edits_independent(evidence):
    evidence[1]["working_tree"] = {"modified": True}
    evidence[4][A, B] = True
    assert state(evidence)[0].state == "reached"


def test_empty_detached_stale_main_skip_all_io(monkeypatch):
    fail = Mock(side_effect=AssertionError("unexpected command"))
    monkeypatch.setattr(rd._Reader, "run", fail)
    for rows in [
        [],
        [{"branch": None}],
        [{**entry("/gone"), "worktree": {"state": "prunable"}}],
        [{**entry("/main"), "is_main": True}],
    ]:
        assert rd.lookup(Path("/repo"), rows).cells == {}
    fail.assert_not_called()


def test_moved_head_or_branch_unknown(evidence, monkeypatch):
    root, row, inventory = evidence[:3]
    initial = copy.deepcopy(inventory)
    final = {row["path"]: ("other", A)}
    monkeypatch.setattr(rd._Reader, "inventory", Mock(side_effect=[initial, final]))
    evidence[4][A, B] = True
    assert rd.lookup(root, [row]).cells[row["path"]].state == "unknown"


def test_listed_head_mismatch_unknown(evidence):
    evidence[1]["commit"]["sha"] = C
    assert state(evidence)[0].state == "unknown"


def test_recheck_after_size_collection(evidence):
    root, row, inventory = evidence[:3]
    evidence[4][A, B] = True
    result = rd.lookup(root, [row])
    inventory[row["path"]] = ("feature", C)
    checked = rd.revalidate(root, [row], result)
    assert checked.cells[row["path"]].state == "unknown"


def test_equal_heads_share_proof(evidence):
    root, row, inventory = evidence[:3]
    other = entry(root / "other", branch="other")
    inventory[other["path"]] = ("other", A)
    evidence[3][f"repos/{SLUG}/commits/{A}/pulls?per_page=100"] = [[pr()]]
    result = rd.lookup(root, [row, other])
    assert all(cell.state == "reached" for cell in result.cells.values())
    assert evidence[5].count(f"repos/{SLUG}/commits/{A}/pulls?per_page=100") == 1


def test_inventory_nul_paths_and_detached(tmp_path, monkeypatch):
    reader = rd._Reader(tmp_path)
    path = tmp_path / 'with\n"quotes'
    output = f"worktree {path}\0HEAD {A}\0branch refs/heads/feature\0\0worktree /detached\0HEAD {B}\0detached\0\0"
    monkeypatch.setattr(reader, "git", lambda *args: subprocess.CompletedProcess(args, 0, output, ""))
    assert reader.inventory() == {str(path): ("feature", A), "/detached": (None, B)}


@pytest.mark.parametrize(
    "behind,ahead,status,common,expected",
    [
        (0, 2, "ahead", A, True),
        (2, 0, "behind", C, False),
        (2, 3, "diverged", C, False),
        (False, 2, "ahead", A, None),
        (0, 0, "identical", A, None),
        (0, 2, "ahead", C, None),
        (2, 0, "ahead", C, None),
        (-1, 0, "behind", C, None),
    ],
)
def test_remote_compare_direction_and_validation(tmp_path, monkeypatch, behind, ahead, status, common, expected):
    reader = rd._Reader(tmp_path)
    monkeypatch.setattr(
        reader,
        "git",
        lambda *args: subprocess.CompletedProcess(
            args,
            1 if args[0] == "merge-base" else 0,
            "true" if args[0] == "rev-parse" and args[1] == "--is-shallow-repository" else str(tmp_path / "grafts"),
            "",
        ),
    )
    monkeypatch.setattr(
        reader,
        "api",
        lambda endpoint: {
            "url": f"https://api.github.com/repos/{SLUG}/compare/{A}...{B}",
            "base_commit": {"sha": A},
            "merge_base_commit": {"sha": common},
            "behind_by": behind,
            "ahead_by": ahead,
            "status": status,
        },
    )
    if expected is None:
        with pytest.raises(rd._Unavailable):
            reader.ancestor(SLUG, A, B)
    else:
        assert reader.ancestor(SLUG, A, B) is expected


def test_run_bounded_and_disables_replacements_lazy_fetch(tmp_path, monkeypatch):
    call = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(rd.subprocess, "run", call)
    reader = rd._Reader(tmp_path)
    reader.git("status")
    options = call.call_args.kwargs
    assert 0 < options["timeout"] <= 10
    assert options["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert options["env"]["GIT_NO_LAZY_FETCH"] == "1"
    reader.deadline = 0
    with pytest.raises(rd._Unavailable):
        reader.git("status")
    assert call.call_count == 1


def test_api_paginates_and_has_no_mutating_command(tmp_path, monkeypatch):
    reader = rd._Reader(tmp_path)
    call = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([[]]), ""))
    monkeypatch.setattr(reader, "run", call)
    assert reader.api("repos/owner/repo/commits/test/pulls", paginate=True) == [[]]
    args = call.call_args.args[0]
    assert "--paginate" in args and "--slurp" in args and "GET" in args
    assert not {"fetch", "merge", "push", "checkout", "update-ref"}.intersection(args)


def test_revalidation_contains_decoding_failure(evidence, monkeypatch):
    root, row = evidence[:2]
    evidence[4][A, B] = True
    result = rd.lookup(root, [row])
    monkeypatch.setattr(rd._Reader, "inventory", Mock(side_effect=UnicodeDecodeError("utf8", b"\xff", 0, 1, "bad")))
    assert rd.revalidate(root, [row], result).cells[row["path"]].state == "unknown"


@pytest.mark.parametrize("url", [None, f"https://api.github.com/repos/{SLUG}/compare/{A}...{C}"])
def test_compare_missing_or_wrong_target_identity(tmp_path, monkeypatch, url):
    reader = rd._Reader(tmp_path)
    monkeypatch.setattr(
        reader,
        "git",
        lambda *args: subprocess.CompletedProcess(
            args, 128 if args[0] == "merge-base" else 0, str(tmp_path / "grafts"), ""
        ),
    )
    monkeypatch.setattr(
        reader,
        "api",
        lambda endpoint: {
            "url": url,
            "base_commit": {"sha": A},
            "merge_base_commit": {"sha": A},
            "ahead_by": 1,
            "behind_by": 0,
            "status": "ahead",
        },
    )
    with pytest.raises(rd._Unavailable, match="target identity"):
        reader.ancestor(SLUG, A, B)


def test_real_local_git_identity_and_ancestry_without_ref_changes(tmp_path):
    def run(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("commit", "--allow-empty", "-q", "-m", "one")
    first = run("rev-parse", "HEAD")
    run("commit", "--allow-empty", "-q", "-m", "two")
    second = run("rev-parse", "HEAD")
    before = run("show-ref")
    reader = rd._Reader(tmp_path)
    assert reader.inventory() == {str(tmp_path.resolve()): ("main", second)}
    assert reader.ancestor(SLUG, first, second)
    assert not reader.ancestor(SLUG, second, first)
    assert run("show-ref") == before
