"""Regression tests for durable setup recovery through the actual CLI."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from coppice import cli, orca, repo, shell, wt

runner = CliRunner()
_RUN = orca._run
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
    )
    monkeypatch.setattr(orca, "STATE_ROOT", tmp_path / "isolated-orca-state")
    monkeypatch.setattr(orca, "prepare", lambda actual_root: setup if actual_root == root else pytest.fail("Wrong repo root"))
    monkeypatch.setattr(wt, "require_wt", lambda: "wt")
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
        assert args == ["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"]
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
    monkeypatch.setattr(orca, "_run", run)
    monkeypatch.setattr(orca, "_orca", command)
    return state


def _interrupt_creation(state):
    def interrupted_switch():
        assert orca._intent_path(state.root, BRANCH).exists()
        assert not orca._setup_path(state.root).exists()
        raise KeyboardInterrupt("Checkout exists, but its creation reply was interrupted")

    with pytest.raises(KeyboardInterrupt):
        orca.switch_ready(state.setup, BRANCH, BASE, interrupted_switch)
    assert json.loads(orca._intent_path(state.root, BRANCH).read_text())["base"] == BASE


def _reuse_through_new(monkeypatch, state):
    switch = Mock(return_value={"action": "existing", "path": str(state.root)})
    monkeypatch.setattr(cli, "_switch_with_recovery", switch)
    result = runner.invoke(cli.app, ["new", str(state.root), "--branch", BRANCH, "--yes"])
    assert result.exit_code == 0, result.output
    switch.assert_called_once_with(
        state.root, BRANCH, create=False, base=None, config_overrides=state.setup.suppress,
    )
    assert state.cd == [state.root]
    return result


@pytest.mark.parametrize("location", ["checkout", "nested", "symlink"])
def test_interrupted_creation_cli_retry_finishes_setup_before_pi(monkeypatch, recovery, tmp_path, location):
    state = recovery
    _interrupt_creation(state)
    target = state.root
    if location == "nested":
        target = state.root / "nested directory"
        target.mkdir()
    elif location == "symlink":
        target = tmp_path / "checkout alias"
        target.symlink_to(state.root, target_is_directory=True)
    monkeypatch.setattr(wt, "switch", Mock(side_effect=AssertionError("Retry must not create a checkout")))

    result = runner.invoke(cli.app, ["orca", str(target)])

    assert result.exit_code == 0, result.output
    assert "Started Pi" in result.output
    assert state.attempts == [(state.root, BRANCH, BASE)]
    assert state.events == [
        "setup", ("terminal", "list"), ("agent", "launch"),
        ("terminal", "list"), ("terminal", "switch"),
    ]
    assert not orca._setup_path(state.root).exists()
    assert not orca._intent_path(state.root, BRANCH).exists()
    wt.switch.assert_not_called()


def test_failed_setup_cli_retry_clears_intent_and_later_reuse_skips_hooks(monkeypatch, recovery):
    state = recovery
    state.fail_next = True
    failed = orca.switch_ready(
        state.setup, BRANCH, BASE,
        Mock(return_value={"action": "created", "path": str(state.root), "base_branch": BASE}),
    )
    assert failed["orca_setup_error"] == "Setup failed once"
    assert orca._setup_path(state.root).exists()
    assert orca._intent_path(state.root, BRANCH).exists()
    assert state.events == ["setup"]

    retried = runner.invoke(cli.app, ["orca", str(state.root)])

    assert retried.exit_code == 0, retried.output
    assert state.attempts == [(state.root, BRANCH, BASE)] * 2
    assert not orca._setup_path(state.root).exists()
    assert not orca._intent_path(state.root, BRANCH).exists()
    reused = _reuse_through_new(monkeypatch, state)
    assert "Reused Pi" in reused.output
    assert state.attempts == [(state.root, BRANCH, BASE)] * 2
    assert state.events.count(("agent", "launch")) == 1
    assert not orca._setup_path(state.root).exists()
    assert not orca._intent_path(state.root, BRANCH).exists()


@pytest.mark.parametrize("pending_setup_record", [False, True])
def test_new_retry_keeps_saved_base_when_reusing_existing_branch(monkeypatch, recovery, pending_setup_record):
    state = recovery
    if pending_setup_record:
        state.fail_next = True
        failed = orca.switch_ready(
            state.setup, BRANCH, BASE,
            Mock(return_value={"action": "created", "path": str(state.root), "base_branch": BASE}),
        )
        assert failed["orca_setup_error"] == "Setup failed once"
    else:
        _interrupt_creation(state)
    assert orca._setup_path(state.root).exists() is pending_setup_record
    expected_attempts = 2 if pending_setup_record else 1

    result = _reuse_through_new(monkeypatch, state)

    assert "Started Pi" in result.output
    assert state.attempts == [(state.root, BRANCH, BASE)] * expected_attempts
    assert not orca._setup_path(state.root).exists()
    assert not orca._intent_path(state.root, BRANCH).exists()
