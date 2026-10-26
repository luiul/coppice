"""Manual creation keeps Orca optional and preserves the shell handoff."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from coppice import cli, orca, repo, shell, wt

runner = CliRunner()


@pytest.fixture
def creation(monkeypatch, tmp_path):
    monkeypatch.setattr(repo, "resolve_repo_root", lambda _path: tmp_path)
    monkeypatch.setattr(repo, "register_repo", lambda _path: None)
    monkeypatch.setattr(repo, "default_branch", lambda _path: "main")
    monkeypatch.setattr(wt, "require_wt", lambda: "wt")
    monkeypatch.setattr(wt, "branch_exists", lambda *_: False)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda *_: False)
    calls = []

    def switch(*_args, **kwargs):
        calls.append(kwargs)
        return {"action": "created", "path": str(tmp_path / "checkout"), "base_branch": "main"}

    monkeypatch.setattr(cli, "_switch_with_recovery", switch)
    cd = []
    monkeypatch.setattr(shell, "write_cd_file", cd.append)
    monkeypatch.setattr(orca, "STATE_ROOT", tmp_path / "state")
    return tmp_path, calls, cd


def test_manual_new_finishes_setup_before_handoff(monkeypatch, creation):
    root, calls, cd = creation
    events = []
    setup = SimpleNamespace(root=root, suppress=("post-start=[]",))
    monkeypatch.setattr(orca, "prepare", lambda _: setup)
    monkeypatch.setattr(orca, "finish_setup", lambda *args: events.append(("ready", args)))
    monkeypatch.setattr(orca, "handoff", lambda path: events.append(("launch", path)) or "Started Pi")
    result = runner.invoke(cli.app, ["new", "--branch", "probe"])
    assert result.exit_code == 0, result.output
    assert calls[0]["config_overrides"] == setup.suppress
    assert [event[0] for event in events] == ["ready", "launch"]
    assert cd == [root / "checkout"]


def test_optional_new_keeps_original_switch_call(monkeypatch, creation):
    _, calls, _ = creation
    monkeypatch.setattr(orca, "prepare", lambda _: None)
    monkeypatch.setattr(orca, "handoff", lambda _: pytest.fail("unexpected launch"))
    result = runner.invoke(cli.app, ["new", "--branch", "probe"])
    assert result.exit_code == 0
    assert "config_overrides" not in calls[0]


def test_setup_failure_keeps_worktree_and_cd(monkeypatch, creation):
    root, _, cd = creation
    monkeypatch.setattr(orca, "prepare", lambda _: SimpleNamespace(root=root, suppress=("post-start=[]",)))

    def fail(*_args):
        raise orca.HandoffError("setup failed")

    monkeypatch.setattr(orca, "finish_setup", fail)
    monkeypatch.setattr(orca, "handoff", lambda _: pytest.fail("setup not ready"))
    result = runner.invoke(cli.app, ["new", "--branch", "probe"])
    assert result.exit_code == 0
    assert cd == [root / "checkout"]
    assert "Retry: cop orca" in result.output


def test_reuse_does_not_repeat_creation_setup(monkeypatch, creation):
    root, _, _ = creation
    monkeypatch.setattr(orca, "prepare", lambda _: SimpleNamespace(root=root, suppress=("post-start=[]",)))
    monkeypatch.setattr(cli, "_switch_with_recovery", lambda *_args, **_kw: {"action": "existing", "path": str(root)})
    monkeypatch.setattr(orca, "finish_setup", lambda *_args: pytest.fail("unexpected setup"))
    monkeypatch.setattr(orca, "resume_setup", lambda *_args: None)
    monkeypatch.setattr(orca, "handoff", lambda _: "Reused live Pi")
    result = runner.invoke(cli.app, ["new", "--branch", "probe"])
    assert result.exit_code == 0
    assert "Reused live Pi" in result.output


def test_retry_runs_pending_setup_without_switch(monkeypatch, tmp_path):
    import subprocess

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True, timeout=10)
    monkeypatch.setattr(repo, "resolve_repo_root", lambda _: tmp_path)
    setup = SimpleNamespace()
    monkeypatch.setattr(orca, "prepare", lambda _: setup)
    events = []
    monkeypatch.setattr(orca, "resume_setup", lambda *args: events.append(("setup", args)))
    monkeypatch.setattr(orca, "handoff", lambda path: events.append(("launch", path)) or "Started Pi")
    monkeypatch.setattr(wt, "switch", lambda *_args, **_kw: pytest.fail("retry creates no checkout"))
    result = runner.invoke(cli.app, ["orca", str(tmp_path)])
    assert result.exit_code == 0
    assert [event[0] for event in events] == ["setup", "launch"]


def test_scoped_overrides_survive_occupied_path_retry(monkeypatch, tmp_path):
    calls = []

    def switch(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise wt.WtCommandError([], 1, "occupied")
        return {"action": "created", "path": str(tmp_path)}

    monkeypatch.setattr(wt, "switch", switch)
    monkeypatch.setattr(wt, "branch_exists", lambda *_: False)
    monkeypatch.setattr(cli, "_recover_occupied_path", lambda *_args, **_kw: True)
    overrides = ("post-start=[]",)
    cli._switch_with_recovery(tmp_path, "probe", create=True, base="main", config_overrides=overrides)
    assert len(calls) == 2
    assert all(call["config_overrides"] == overrides for call in calls)


def test_wt_override_only_changes_one_invocation(monkeypatch, tmp_path):
    calls = []

    def run(args, **kwargs):
        import subprocess
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{"action":"created"}', "")

    monkeypatch.setattr(wt, "run", run)
    wt.switch(tmp_path, "probe", create=True, config_overrides=("post-start=[]",))
    assert calls[0][0][:3] == ["--config-set", "post-start=[]", "switch"]
    assert calls[0][1] == {"cwd": tmp_path, "stream": True}
