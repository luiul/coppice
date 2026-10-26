"""The explicit Orca workflow watches before creating any worktree."""

import shlex
import subprocess
from unittest.mock import Mock

import pytest

from coppice import orca


@pytest.fixture
def explicit(monkeypatch):
    monkeypatch.setattr(orca, "manual_invocation", lambda: True)
    monkeypatch.setattr(orca.shutil, "which", lambda _: "/fake/orca")


def test_watch_adds_missing_repo_then_shows_external_worktrees(monkeypatch, tmp_path, explicit):
    command = Mock(side_effect=[orca.OrcaError("repo_not_found", "missing"), {}, {}])
    monkeypatch.setattr(orca, "_orca", command)
    orca.watch(tmp_path)
    assert [call.args[0] for call in command.call_args_list] == [
        ["repo", "show", "--repo", f"path:{tmp_path}"],
        ["repo", "add", "--path", str(tmp_path)],
        ["repo", "set", "--repo", f"path:{tmp_path}", "--external-worktree-visibility", "show"],
    ]
    assert all(call.kwargs == {"cwd": tmp_path} for call in command.call_args_list)


def test_watch_reuses_registered_repo(monkeypatch, tmp_path, explicit):
    command = Mock(return_value={})
    monkeypatch.setattr(orca, "_orca", command)
    orca.watch(tmp_path)
    assert [call.args[0][:2] for call in command.call_args_list] == [["repo", "show"], ["repo", "set"]]


@pytest.mark.parametrize("code", ["transport_unknown", "access_denied", "runtime_not_running"])
def test_watch_does_not_hide_errors_or_try_registration(monkeypatch, tmp_path, explicit, code):
    command = Mock(side_effect=orca.OrcaError(code, "blocked"))
    monkeypatch.setattr(orca, "_orca", command)
    with pytest.raises(orca.OrcaError):
        orca.watch(tmp_path)
    command.assert_called_once()


@pytest.mark.parametrize("phase", ["add", "set"])
def test_watch_failure_stops_workflow(monkeypatch, tmp_path, explicit, phase):
    replies = [orca.OrcaError("repo_not_found", "missing")]
    if phase == "set":
        replies.append({})
    replies.append(orca.OrcaError("failed", "stopped"))
    monkeypatch.setattr(orca, "_orca", Mock(side_effect=replies))
    prepare = Mock()
    monkeypatch.setattr(orca, "prepare", prepare)
    with pytest.raises(orca.OrcaError):
        orca.prepare_workflow(tmp_path)
    prepare.assert_not_called()


def test_workflow_watches_before_setup_preflight(monkeypatch, tmp_path, explicit):
    events = []
    setup = orca.Setup(tmp_path, (), ())
    monkeypatch.setattr(orca, "watch", lambda root: events.append(("watch", root)))
    monkeypatch.setattr(orca, "prepare", lambda root: events.append(("prepare", root)) or setup)
    assert orca.prepare_workflow(tmp_path) == setup
    assert events == [("watch", tmp_path), ("prepare", tmp_path)]


def test_workflow_requires_manual_call_before_watch(monkeypatch, tmp_path):
    monkeypatch.setattr(orca, "manual_invocation", lambda: False)
    watch = Mock()
    monkeypatch.setattr(orca, "watch", watch)
    with pytest.raises(orca.HandoffError, match="interactive shell"):
        orca.prepare_workflow(tmp_path)
    watch.assert_not_called()


def test_checkout_detection_uses_git_branch_not_directory_name(monkeypatch, tmp_path):
    output = f"worktree {tmp_path}/unrelated-name\0HEAD deadbeef\0branch refs/heads/feature/existing\0\0"
    command = Mock(return_value=subprocess.CompletedProcess([], 0, output, ""))
    monkeypatch.setattr(orca, "_run", command)
    assert orca._branch_has_checkout(tmp_path, "feature/existing") is True
    assert orca._branch_has_checkout(tmp_path, "feature/missing") is False
    assert command.call_args.args[0] == ["git", "-C", str(tmp_path), "worktree", "list", "--porcelain", "-z"]


def test_checkout_detection_failure_blocks_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(orca, "_run", Mock(return_value=subprocess.CompletedProcess([], 1, "", "failed")))
    with pytest.raises(orca.HandoffError, match="inspect existing worktrees"):
        orca._branch_has_checkout(tmp_path, "feature/existing")


def test_retry_selects_exact_repo_and_branch(tmp_path):
    command = orca.retry_command(tmp_path / "space repo", "feature/space branch")
    assert shlex.split(command) == ["cop", "orca", str(tmp_path / "space repo"), "--branch", "feature/space branch", "--yes"]
