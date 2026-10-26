"""Regression tests for durable setup recovery through the actual CLI."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from coppice import cli, orca, repo, shell, workflow, wt

runner = CliRunner()
_RUN = workflow._run
BRANCH = "feature/final-review"
BASE = "develop"


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    """Use real Git checkout identity, isolated records, and no live Orca or wt."""
    root = (tmp_path / "repo with spaces").resolve()
    root.mkdir()
    commands = (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["commit", "--allow-empty", "-q", "-m", "init"],
        ["checkout", "-q", "-b", BRANCH],
    )
    for args in commands:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, timeout=10)
    setup = orca.Setup(root, ("post-start=[]",), ())
    state = SimpleNamespace(
        root=root,
        setup=setup,
        events=[],
        attempts=[],
        cd=[],
        ready=False,
        fail_next=False,
        pi_live=False,
        switch=Mock(return_value={"action": "existing", "path": str(root)}),
    )
    monkeypatch.setattr(orca, "STATE_ROOT", tmp_path / "isolated-orca-state")
    monkeypatch.setattr(workflow, "STATE_ROOT", tmp_path / "isolated-orca-state")
    monkeypatch.setattr(
        orca, "prepare_workflow", lambda actual_root: setup if actual_root == root else pytest.fail("Wrong repo root"),
    )
    monkeypatch.setattr(wt, "require_wt", lambda: "wt")
    monkeypatch.setattr(wt, "switch", state.switch)
    monkeypatch.setattr(repo, "register_repo", lambda _root: None)
    monkeypatch.setattr(shell, "write_cd_file", state.cd.append)

    def finish(actual_setup, path, branch, base):
        assert actual_setup == setup
        assert path == root
        assert orca._setup_path(root).exists(), "Readiness must be recorded before setup"
        state.attempts.append((path, branch, base))
        state.events.append("setup")
        if state.fail_next:
            state.fail_next = False
            raise orca.HandoffError("Setup failed once")
        state.ready = True

    def run(args, *, cwd, timeout=orca.CALL_TIMEOUT):
        assert args in (
            ["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"],
            ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
        )
        return _RUN(args, cwd=cwd, timeout=timeout)

    def command(args, *, cwd, timeout=orca.CALL_TIMEOUT):
        assert cwd == root
        assert state.ready, "Orca handoff preceded setup completion"
        assert not orca._setup_path(root).exists()
        assert not orca._intent_path(root, BRANCH).exists()
        state.events.append(tuple(args[:2]))
        if args[:2] == ["agent", "launch"]:
            assert not state.pi_live
            state.pi_live = True
            return {"operationId": "operation-1", "outcome": {"kind": "terminal", "handle": "terminal-1"}}
        if args[:2] == ["terminal", "list"]:
            terminals = []
            if state.pi_live:
                terminals.append({
                    "worktreePath": str(root),
                    "agentIdentity": "pi",
                    "connected": True,
                    "writable": True,
                    "orphaned": False,
                    "incarnationId": "incarnation-1",
                    "handle": "terminal-1",
                })
            return {"terminals": terminals}
        assert args == ["terminal", "switch", "--terminal", "terminal-1"]
        return {"focus": {
            "handle": "terminal-1", "navigated": True,
            "tabId": "tab-1", "worktreeId": f"repo-1::{root}",
        }}

    monkeypatch.setattr(orca.Setup, "finish", finish)
    monkeypatch.setattr(workflow, "_run", run)
    monkeypatch.setattr(orca, "_orca", command)
    return state


def _interrupt_creation(state):
    def interrupted_switch():
        assert orca._intent_path(state.root, BRANCH).exists()
        assert not orca._setup_path(state.root).exists()
        raise KeyboardInterrupt("Checkout exists, but its creation reply was interrupted")

    with patch.object(workflow, "_branch_has_checkout", return_value=False), pytest.raises(KeyboardInterrupt):
        orca.switch_ready(state.setup, BRANCH, BASE, interrupted_switch)
    assert orca._branch_has_checkout(state.root, BRANCH)
    assert json.loads(orca._intent_path(state.root, BRANCH).read_text())["base"] == BASE


def _reuse_through_orca(state, target=None):
    state.switch.reset_mock()
    previous_cd = list(state.cd)
    result = runner.invoke(cli.app, ["orca", str(target or state.root), "--branch", BRANCH, "--yes"])
    assert result.exit_code == 0, result.output
    state.switch.assert_called_once_with(
        state.root, BRANCH, create=False, base=None, config_overrides=state.setup.suppress,
    )
    assert state.switch.return_value == {"action": "existing", "path": str(state.root)}
    assert state.cd == [*previous_cd, state.root]
    return result


def test_interrupted_existing_reuse_writes_no_intent_or_repeats_setup(monkeypatch, recovery):
    state = recovery
    state.ready = True
    started = _reuse_through_orca(state)
    assert "Started Pi" in started.output
    binding = orca._binding_path(state.root).read_text()
    assert state.attempts == []
    state.events.clear()
    state.cd.clear()
    state.switch.reset_mock()
    finish = Mock(side_effect=AssertionError("Existing checkout must not repeat setup"))
    resume = Mock(side_effect=AssertionError("Existing checkout has no pending setup"))
    monkeypatch.setattr(workflow, "finish_setup", finish)
    monkeypatch.setattr(workflow, "resume_setup", resume)

    def interrupted_switch(*_args, **_kwargs):
        assert orca._branch_has_checkout(state.root, BRANCH)
        assert not orca._intent_path(state.root, BRANCH).exists()
        assert not orca._setup_path(state.root).exists()
        raise KeyboardInterrupt("Existing checkout switch was interrupted")

    state.switch.side_effect = interrupted_switch
    interrupted = runner.invoke(cli.app, ["orca", str(state.root), "--branch", BRANCH, "--yes"])

    assert interrupted.exit_code == 130, interrupted.output
    state.switch.assert_called_once_with(
        state.root, BRANCH, create=False, base=None, config_overrides=state.setup.suppress,
    )
    assert state.events == []
    assert state.cd == []
    assert not orca._intent_path(state.root, BRANCH).exists()
    assert not orca._setup_path(state.root).exists()
    assert orca._binding_path(state.root).read_text() == binding

    state.switch.side_effect = None
    retried = _reuse_through_orca(state)

    assert "Reused Pi" in retried.output
    assert state.events == [("terminal", "list"), ("terminal", "switch")]
    assert state.attempts == []
    finish.assert_not_called()
    resume.assert_not_called()
    assert not orca._intent_path(state.root, BRANCH).exists()
    assert not orca._setup_path(state.root).exists()
    assert orca._binding_path(state.root).read_text() == binding


@pytest.mark.parametrize("location", ["checkout", "nested", "symlink"])
def test_interrupted_creation_cli_retry_finishes_setup_before_pi(recovery, tmp_path, location):
    state = recovery
    _interrupt_creation(state)
    target = state.root
    if location == "nested":
        target = state.root / "nested directory"
        target.mkdir()
    elif location == "symlink":
        target = tmp_path / "checkout alias"
        target.symlink_to(state.root, target_is_directory=True)

    result = _reuse_through_orca(state, target)
    assert "Started Pi" in result.output
    assert state.attempts == [(state.root, BRANCH, BASE)]
    assert state.events == [
        "setup", ("terminal", "list"), ("agent", "launch"),
        ("terminal", "list"), ("terminal", "switch"),
    ]
    assert not orca._setup_path(state.root).exists()
    assert not orca._intent_path(state.root, BRANCH).exists()


def test_failed_setup_cli_retry_clears_intent_and_later_reuse_skips_hooks(recovery):
    state = recovery
    state.fail_next = True
    with patch.object(workflow, "_branch_has_checkout", return_value=False):
        failed = workflow.switch_ready(
            state.setup, BRANCH, BASE,
            Mock(return_value={"action": "created", "path": str(state.root), "base_branch": BASE}),
        )
    assert failed["setup_error"] == "Setup failed once"
    assert orca._setup_path(state.root).exists()
    assert orca._intent_path(state.root, BRANCH).exists()
    assert state.events == ["setup"]

    retried = _reuse_through_orca(state)

    assert "Started Pi" in retried.output
    assert state.attempts == [(state.root, BRANCH, BASE)] * 2
    assert not orca._setup_path(state.root).exists()
    assert not orca._intent_path(state.root, BRANCH).exists()
    reused = _reuse_through_orca(state)
    assert "Reused Pi" in reused.output
    assert state.attempts == [(state.root, BRANCH, BASE)] * 2
    assert state.events.count(("agent", "launch")) == 1
    assert not orca._setup_path(state.root).exists()
    assert not orca._intent_path(state.root, BRANCH).exists()


@pytest.mark.parametrize("pending_setup_record", [False, True])
def test_orca_retry_keeps_saved_base_when_reusing_existing_branch(recovery, pending_setup_record):
    state = recovery
    if pending_setup_record:
        state.fail_next = True
        with patch.object(workflow, "_branch_has_checkout", return_value=False):
            failed = workflow.switch_ready(
                state.setup, BRANCH, BASE,
                Mock(return_value={"action": "created", "path": str(state.root), "base_branch": BASE}),
            )
        assert failed["setup_error"] == "Setup failed once"
    else:
        _interrupt_creation(state)
    assert orca._setup_path(state.root).exists() is pending_setup_record
    expected_attempts = 2 if pending_setup_record else 1

    result = _reuse_through_orca(state)

    assert "Started Pi" in result.output
    assert state.attempts == [(state.root, BRANCH, BASE)] * expected_attempts
    assert not orca._setup_path(state.root).exists()
    assert not orca._intent_path(state.root, BRANCH).exists()
