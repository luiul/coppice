"""PR reference handoff never creates or changes a workspace except for adding links."""

import copy
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from coppice import orca

URLS = ["https://github.com/example/repo/pull/1", "https://github.com/example/repo/pull/2"]
KEY = "wt2:local:instance-1"
SELECTOR = f"identity:{KEY}"
_ORCA = orca._orca


def workspace(path, *, key=KEY, host="local", instance="instance-1"):
    return {
        "id": f"repo-1::{path}",
        "repoId": "repo-1",
        "path": str(path),
        "hostId": host,
        "instanceId": instance,
        "identity": {"key": key, "executionHostId": host, "instanceId": instance},
    }


def inventory(*rows, host_ids=None, omitted=None, truncated=False):
    return {
        "worktrees": list(rows),
        "totalCount": len(rows),
        "truncated": truncated,
        "hostScope": {"hostIds": host_ids or ["local"], "omittedHostIds": omitted or []},
    }


@pytest.fixture
def harness(monkeypatch, tmp_path):
    path = tmp_path / "checkout with spaces"
    path.mkdir()
    row = workspace(path)
    state = {"inventory": inventory(row), "show": {"worktree": copy.deepcopy(row)}}
    git = Mock(return_value=subprocess.CompletedProcess([], 0, "feature\n", ""))
    monkeypatch.setattr(orca, "_run", git)

    def invoke(args, *, cwd, timeout=orca.CALL_TIMEOUT):
        assert cwd == path
        assert timeout > 0
        if args == ["worktree", "list", "--limit", "10000"]:
            return state["inventory"]
        if args == ["worktree", "show", "--worktree", SELECTOR]:
            return state["show"]
        if args == ["reference", "add", *URLS, "--worktree", SELECTOR]:
            if "add_error" in state:
                raise state["add_error"]
            return {}
        raise AssertionError(f"Forbidden or unexpected Orca command: {args}")

    command = Mock(side_effect=invoke)
    monkeypatch.setattr(orca, "_orca", command)
    yield path, state, git, command
    # Whitelist all calls, including failure cases. No creation, registration,
    # launch, status, comment, removal, or unrelated subprocess may run.
    for call in command.call_args_list:
        args = call.args[0]
        assert args in (
            ["worktree", "list", "--limit", "10000"],
            ["worktree", "show", "--worktree", SELECTOR],
            ["reference", "add", *URLS, "--worktree", SELECTOR],
        )
    for call in git.call_args_list:
        args = call.args[0]
        if args[0] == "git":
            assert args == ["git", "symbolic-ref", "--quiet", "--short", "HEAD"]
            assert not any(name.startswith("GIT_") for name in call.kwargs["env"])
        else:
            assert args in (
                ["orca", "worktree", "list", "--limit", "10000", "--json"],
                ["orca", "worktree", "show", "--worktree", SELECTOR, "--json"],
                ["orca", "reference", "add", *URLS, "--worktree", SELECTOR, "--json"],
            )
        assert call.kwargs["cwd"] == path
        assert call.kwargs["timeout"] == orca.CALL_TIMEOUT


def assert_no_add(command):
    assert not any(call.args[0][:2] == ["reference", "add"] for call in command.call_args_list)


def test_success_submits_all_urls_once_and_returns_submitted_count(harness, monkeypatch):
    path, _, git, command = harness
    monkeypatch.setenv("GIT_DIR", "/wrong")
    assert orca.attach_pr_references(path, "feature", URLS) == 2
    assert git.call_count == 2
    assert [call.args[0] for call in command.call_args_list] == [
        ["worktree", "list", "--limit", "10000"],
        ["worktree", "show", "--worktree", SELECTOR],
        ["reference", "add", *URLS, "--worktree", SELECTOR],
    ]


def test_symlink_checkout_resolves_to_existing_canonical_workspace(harness, tmp_path):
    path, _, _, command = harness
    alias = tmp_path / "alias"
    alias.symlink_to(path, target_is_directory=True)
    assert orca.attach_pr_references(alias, "feature", URLS) == len(URLS)
    assert command.call_count == 3


def test_empty_does_no_io(harness, monkeypatch):
    _, _, git, command = harness
    monkeypatch.setattr(Path, "resolve", Mock(side_effect=AssertionError("resolve called")))
    assert orca.attach_pr_references(Path("/missing"), "feature", []) == 0
    git.assert_not_called()
    command.assert_not_called()


def test_strict_path_resolution_failure_precedes_all_commands(harness):
    path, _, git, command = harness
    with pytest.raises(orca.OrcaError, match="resolve") as err:
        orca.attach_pr_references(path / "missing", "feature", URLS)
    assert err.value.code == "path_unknown"
    git.assert_not_called()
    command.assert_not_called()


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 0, "other\n", ""),
        subprocess.CompletedProcess([], 1, "", ""),
        subprocess.CompletedProcess([], 0, "", ""),
    ],
)
def test_missing_detached_or_wrong_branch_rejected(harness, result):
    path, _, git, command = harness
    git.return_value = result
    with pytest.raises(orca.OrcaError, match="branch") as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "branch_mismatch"
    command.assert_not_called()


@pytest.mark.parametrize("error", [OSError("missing git"), subprocess.TimeoutExpired("git", 10)])
def test_git_error_is_refusal(harness, error):
    path, _, git, command = harness
    git.side_effect = error
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "branch_unknown"
    command.assert_not_called()


def test_branch_is_checked_again_after_show(harness):
    path, _, git, command = harness
    git.side_effect = [
        subprocess.CompletedProcess([], 0, "feature\n", ""),
        subprocess.CompletedProcess([], 0, "changed\n", ""),
    ]
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "branch_mismatch"
    assert command.call_count == 2
    assert_no_add(command)


def test_missing_workspace_is_refused(harness):
    path, state, _, command = harness
    state["inventory"] = inventory()
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "workspace_not_found"
    assert_no_add(command)


def test_duplicate_matching_workspaces_are_ambiguous(harness):
    path, state, _, command = harness
    second = workspace(path, key="wt2:local:instance-2", instance="instance-2")
    state["inventory"] = inventory(workspace(path), second)
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "workspace_ambiguous"
    assert_no_add(command)


@pytest.mark.parametrize(
    "change",
    [
        lambda inv: inv.update(truncated=True),
        lambda inv: inv.pop("truncated"),
        lambda inv: inv.update(totalCount=2),
        lambda inv: inv.update(totalCount=True),
        lambda inv: inv.pop("totalCount"),
        lambda inv: inv.update(worktrees=None),
        lambda inv: inv.update(hostScope=None),
        lambda inv: inv["hostScope"].update(omittedHostIds=["remote"]),
        lambda inv: inv["hostScope"].pop("omittedHostIds"),
        lambda inv: inv["hostScope"].update(hostIds=["remote"]),
        lambda inv: inv["hostScope"].update(hostIds=["local", "local"]),
    ],
)
def test_incomplete_inventory_refused(harness, change):
    path, state, _, command = harness
    change(state["inventory"])
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "inventory_incomplete"
    assert_no_add(command)


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row.pop("identity"),
        lambda row: row["identity"].update(key=""),
        lambda row: row["identity"].update(executionHostId="remote"),
        lambda row: row.update(hostId="remote"),
        lambda row: row.update(instanceId="changed"),
        lambda row: row.update(id="repo-1::/wrong"),
        lambda row: row.update(path="relative"),
        lambda row: row.update(repoId=None),
    ],
)
def test_malformed_inventory_refused(harness, change):
    path, state, _, command = harness
    change(state["inventory"]["worktrees"][0])
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "inventory_invalid"
    assert_no_add(command)


def test_duplicate_identity_refused_even_on_different_paths(harness):
    path, state, _, command = harness
    state["inventory"] = inventory(workspace(path), workspace(path / "another"))
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "inventory_invalid"
    assert_no_add(command)


def test_remote_path_match_is_not_a_local_workspace(harness):
    path, state, _, command = harness
    row = workspace(path, key="wt2:remote:instance-1", host="remote")
    state["inventory"] = inventory(row, host_ids=["local", "remote"])
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "workspace_ambiguous"
    assert_no_add(command)


def test_remote_and_local_same_path_is_ambiguous(harness):
    path, state, _, command = harness
    remote = workspace(path, key="wt2:remote:instance-2", host="remote", instance="instance-2")
    state["inventory"] = inventory(workspace(path), remote, host_ids=["local", "remote"])
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "workspace_ambiguous"
    assert_no_add(command)


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row["identity"].update(key="wt2:local:changed"),
        lambda row: row.update(path="/different", id="repo-1::/different"),
        lambda row: row.update(hostId="remote"),
        lambda row: row.update(instanceId="different"),
        lambda row: (row.update(instanceId="different"), row["identity"].update(instanceId="different")),
        lambda row: row.update(repoId="different", id=f"different::{row['path']}"),
        lambda row: row.pop("identity"),
    ],
)
def test_reread_changed_identity_path_or_host_refused(harness, change):
    path, state, _, command = harness
    change(state["show"]["worktree"])
    with pytest.raises(orca.OrcaError):
        orca.attach_pr_references(path, "feature", URLS)
    assert_no_add(command)


def test_retargeted_symlink_is_refused(harness, tmp_path):
    path, state, _, command = harness
    alias = tmp_path / "alias"
    alias.symlink_to(path, target_is_directory=True)
    other = tmp_path / "other"
    other.mkdir()

    def show_and_retarget(args, **kwargs):
        if args[:2] == ["worktree", "show"]:
            alias.unlink()
            alias.symlink_to(other, target_is_directory=True)
            return state["show"]
        return state["inventory"]

    command.side_effect = show_and_retarget
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(alias, "feature", URLS)
    assert err.value.code == "workspace_changed"
    assert_no_add(command)


@pytest.mark.parametrize("phase", ["list", "show"])
def test_orca_read_error_propagates_without_add(harness, phase):
    path, _, _, command = harness
    default = command.side_effect

    def invoke(args, **kwargs):
        if args[:2] == ["worktree", phase]:
            raise orca.OrcaError("transport_unknown", "unavailable")
        return default(args, **kwargs)

    command.side_effect = invoke
    with pytest.raises(orca.OrcaError) as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "transport_unknown"
    assert_no_add(command)


@pytest.mark.parametrize("code", ["transport_unknown", "command_failed", "invalid_argument"])
def test_add_error_is_explicitly_indeterminate_and_never_retried(harness, code):
    path, state, _, command = harness
    state["add_error"] = orca.OrcaError(code, "lost reply")
    with pytest.raises(orca.OrcaError, match="may have succeeded") as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "add_outcome_unknown"
    assert err.value.data == {"cause": code}
    assert sum(call.args[0][:2] == ["reference", "add"] for call in command.call_args_list) == 1


@pytest.mark.parametrize("failure", ["timeout", "unreadable", "unstructured"])
def test_real_orca_parser_treats_lost_add_reply_as_unknown(harness, monkeypatch, failure):
    path, state, git, _ = harness
    monkeypatch.setattr(orca, "_orca", _ORCA)
    calls = []

    def run(args, *, cwd, timeout, env=None):
        calls.append(args)
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, "feature\n", "")
        assert args[0] == "orca" and args[-1] == "--json"
        if args[1:3] == ["worktree", "list"]:
            payload = state["inventory"]
        elif args[1:3] == ["worktree", "show"]:
            payload = state["show"]
        elif args[1:3] == ["reference", "add"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, timeout)
            if failure == "unreadable":
                return subprocess.CompletedProcess(args, 0, "not json", "")
            payload = None
        else:
            raise AssertionError(f"Unexpected subprocess: {args}")
        import json

        return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True, "result": payload}), "")

    git.side_effect = run
    with pytest.raises(orca.OrcaError, match="may have succeeded") as err:
        orca.attach_pr_references(path, "feature", URLS)
    assert err.value.code == "add_outcome_unknown"
    assert err.value.data == {"cause": "transport_unknown"}
    assert [args[1:3] for args in calls if args[0] == "orca"] == [
        ["worktree", "list"],
        ["worktree", "show"],
        ["reference", "add"],
    ]
