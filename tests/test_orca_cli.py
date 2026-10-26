"""Explicit Orca creation shares wt and shell behavior with worktree-only creation."""

import shlex
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from coppice import cli, orca, repo, shell, wt

runner = CliRunner()


@pytest.fixture
def creation(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.console, "width", 1000)
    monkeypatch.setattr(repo, "resolve_repo_root", lambda _path: tmp_path)
    monkeypatch.setattr(repo, "register_repo", lambda _path: None)
    monkeypatch.setattr(repo, "default_branch", lambda _path: "main")
    monkeypatch.setattr(wt, "require_wt", lambda: "wt")
    monkeypatch.setattr(wt, "branch_exists", lambda *_: False)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda *_: False)
    calls = []

    def switch(*args, **kwargs):
        calls.append((args, kwargs))
        action = "created" if kwargs["create"] else "existing"
        return {"action": action, "path": str(tmp_path / "checkout"), "base_branch": kwargs["base"]}

    monkeypatch.setattr(cli, "_switch_with_recovery", switch)
    cd = []
    monkeypatch.setattr(shell, "write_cd_file", cd.append)
    monkeypatch.setattr(orca, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        orca, "_branch_has_checkout",
        lambda root, branch: wt.branch_exists(root, branch) or wt.remote_branch_exists(root, branch),
    )
    return tmp_path, calls, cd


@pytest.fixture
def workflow(monkeypatch, creation):
    root, _, _ = creation
    setup = SimpleNamespace(root=root, suppress=("post-start=[]",))
    prepare = Mock(return_value=setup)
    monkeypatch.setattr(orca, "prepare_workflow", prepare)
    monkeypatch.setattr(orca, "finish_setup", Mock())
    monkeypatch.setattr(orca, "handoff", Mock(return_value="Started Pi"))
    return setup, prepare


def test_manual_orca_finishes_setup_before_handoff(monkeypatch, creation, workflow):
    root, calls, cd = creation
    setup, prepare = workflow
    events = []
    monkeypatch.setattr(orca, "finish_setup", lambda *args: events.append(("ready", args)))
    monkeypatch.setattr(orca, "handoff", lambda path: events.append(("launch", path)) or "Started Pi")

    result = runner.invoke(cli.app, ["orca", str(root), "--branch", "probe"])

    assert result.exit_code == 0, result.output
    prepare.assert_called_once_with(root)
    assert calls == [((root, "probe"), {"create": True, "base": "main", "config_overrides": setup.suppress})]
    assert events == [("ready", (setup, root / "checkout", "probe", "main")), ("launch", root / "checkout")]
    assert cd == [root / "checkout"]


@pytest.mark.parametrize("existing", [False, True])
def test_new_never_calls_orca_even_when_watched(monkeypatch, creation, existing):
    root, calls, cd = creation
    monkeypatch.setattr(wt, "branch_exists", lambda *_: existing)
    monkeypatch.setattr(orca, "manual_invocation", lambda: True)
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "orca")
    watched = Mock(return_value={"externalWorktreeVisibility": "show"})
    monkeypatch.setattr(orca, "_orca", watched)
    for name in (
        "prepare_workflow", "prepare", "watch", "_branch_has_checkout",
        "switch_ready", "finish_setup", "resume_setup", "handoff",
    ):
        monkeypatch.setattr(orca, name, Mock(side_effect=AssertionError(f"cop new must not call {name}")))

    result = runner.invoke(cli.app, ["new", str(root), "--branch", "probe", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == [((root, "probe"), {"create": not existing, "base": None if existing else "main"})]
    assert cd == [root / "checkout"]
    watched.assert_not_called()
    assert "Pi" not in result.output
    assert not orca.STATE_ROOT.exists()


@pytest.mark.parametrize("failure", [orca.HandoffError("preflight failed"), OSError("preflight failed")])
def test_orca_preflight_failure_creates_nothing_and_writes_no_cd(monkeypatch, creation, failure):
    root, calls, cd = creation
    prepare = Mock(side_effect=failure)
    register = Mock()
    handoff = Mock(side_effect=AssertionError("preflight must precede handoff"))
    monkeypatch.setattr(orca, "prepare_workflow", prepare)
    monkeypatch.setattr(repo, "register_repo", register)
    monkeypatch.setattr(orca, "handoff", handoff)

    result = runner.invoke(cli.app, ["orca", str(root), "--branch", "probe"])

    assert result.exit_code == 1, result.output
    assert "preflight failed" in result.output
    prepare.assert_called_once_with(root)
    assert calls == []
    assert cd == []
    register.assert_not_called()
    handoff.assert_not_called()
    assert not orca.STATE_ROOT.exists()


def test_orca_watches_before_creation_and_hands_off_exact_wt_path(monkeypatch, creation):
    root, _, cd = creation
    setup = SimpleNamespace(root=root, suppress=("post-start=[]",))
    checkout = root / "wt-chosen path"
    events = []
    monkeypatch.setattr(orca, "manual_invocation", lambda: True)
    monkeypatch.setattr(orca, "watch", lambda path: events.append(("watch", path)))
    monkeypatch.setattr(orca, "prepare", lambda path: events.append(("prepare", path)) or setup)

    def switch(actual_root, branch, **kwargs):
        events.append(("create", (actual_root, branch, kwargs)))
        return {"action": "created", "path": str(checkout), "base_branch": "main"}

    monkeypatch.setattr(cli, "_switch_with_recovery", switch)
    monkeypatch.setattr(orca, "finish_setup", lambda *args: events.append(("ready", args)))
    monkeypatch.setattr(orca, "handoff", lambda path: events.append(("launch", path)) or "Started Pi")

    result = runner.invoke(cli.app, ["orca", str(root), "--branch", "probe"])

    assert result.exit_code == 0, result.output
    assert events == [
        ("watch", root),
        ("prepare", root),
        ("create", (root, "probe", {"create": True, "base": "main", "config_overrides": setup.suppress})),
        ("ready", (setup, checkout, "probe", "main")),
        ("launch", checkout),
    ]
    assert cd == [checkout]


@pytest.mark.parametrize("command", ["new", "orca"])
@pytest.mark.parametrize("flags", [("--branch", "--base", "--yes"), ("-b", "-B", "-y")])
def test_creation_commands_accept_matching_flags(monkeypatch, creation, workflow, command, flags):
    root, calls, cd = creation
    setup, prepare = workflow
    freshen = Mock()
    monkeypatch.setattr(cli, "_freshen_explicit_base", freshen)
    branch_flag, base_flag, yes_flag = flags

    result = runner.invoke(
        cli.app, [command, str(root), branch_flag, "feature/probe", base_flag, "develop", yes_flag],
    )

    assert result.exit_code == 0, result.output
    expected = {"create": True, "base": "develop"}
    if command == "orca":
        expected["config_overrides"] = setup.suppress
        prepare.assert_called_once_with(root)
        orca.handoff.assert_called_once_with(root / "checkout")
    else:
        prepare.assert_not_called()
        orca.handoff.assert_not_called()
    assert calls == [((root, "feature/probe"), expected)]
    freshen.assert_called_once_with(root, "develop")
    assert cd == [root / "checkout"]


@pytest.mark.parametrize("command", ["new", "orca"])
@pytest.mark.parametrize("description,branch", [("Fix thing / one", "fix-thing-one"), ("", "wip-fixed-timestamp")])
def test_creation_commands_share_description_prompt(monkeypatch, creation, workflow, command, description, branch):
    root, calls, cd = creation
    _, prepare = workflow
    existing = Mock()
    monkeypatch.setattr(cli, "_print_existing_worktrees", existing)
    monkeypatch.setattr(cli.branch_mod, "timestamp_branch", lambda: "wip-fixed-timestamp")

    result = runner.invoke(cli.app, [command, str(root)], input=f"{description}\n")

    assert result.exit_code == 0, result.output
    assert "Short branch description" in result.output
    existing.assert_called_once_with(root)
    assert calls[0][0] == (root, branch)
    assert calls[0][1]["create"] is True
    assert calls[0][1]["base"] == "main"
    assert cd == [root / "checkout"]
    if command == "orca":
        prepare.assert_called_once_with(root)
    else:
        prepare.assert_not_called()


@pytest.mark.parametrize("command", ["new", "orca"])
@pytest.mark.parametrize("location", ["local", "remote"])
@pytest.mark.parametrize("accept", [False, True])
def test_creation_commands_confirm_existing_branch(monkeypatch, creation, workflow, command, location, accept):
    root, calls, cd = creation
    setup, prepare = workflow
    monkeypatch.setattr(wt, "branch_exists", lambda *_: location == "local")
    monkeypatch.setattr(wt, "remote_branch_exists", lambda *_: location == "remote")
    ask = Mock(return_value=accept)
    default_branch = Mock(side_effect=AssertionError("Reuse must not resolve a base"))
    monkeypatch.setattr(cli.confirm, "ask", ask)
    monkeypatch.setattr(repo, "default_branch", default_branch)

    result = runner.invoke(cli.app, [command, str(root), "--branch", "probe"])

    ask.assert_called_once_with("Switch to the 'probe' worktree? The branch already exists.")
    default_branch.assert_not_called()
    if accept:
        assert result.exit_code == 0, result.output
        expected = {"create": False, "base": None}
        if command == "orca":
            expected["config_overrides"] = setup.suppress
            prepare.assert_called_once_with(root)
        else:
            prepare.assert_not_called()
        assert calls == [((root, "probe"), expected)]
        assert cd == [root / "checkout"]
        assert "Reused worktree" in result.output
        orca.finish_setup.assert_not_called()
    else:
        assert result.exit_code == 1, result.output
        assert "Cancelled." in result.output
        assert calls == []
        assert cd == []
        prepare.assert_not_called()
        orca.handoff.assert_not_called()


@pytest.mark.parametrize("command", ["new", "orca"])
@pytest.mark.parametrize("yes_flag", ["--yes", "-y"])
def test_creation_commands_skip_reuse_confirmation_with_yes(monkeypatch, creation, workflow, command, yes_flag):
    root, calls, cd = creation
    monkeypatch.setattr(wt, "branch_exists", lambda *_: True)
    monkeypatch.setattr(cli.confirm, "ask", Mock(side_effect=AssertionError("--yes must skip confirmation")))

    result = runner.invoke(cli.app, [command, str(root), "--branch", "probe", yes_flag])

    assert result.exit_code == 0, result.output
    assert calls[0][1]["create"] is False
    assert calls[0][1]["base"] is None
    assert cd == [root / "checkout"]


def test_setup_failure_keeps_worktree_and_cd(monkeypatch, creation, workflow):
    root, _, cd = creation
    _, prepare = workflow
    monkeypatch.setattr(orca, "finish_setup", Mock(side_effect=orca.HandoffError("setup failed")))

    result = runner.invoke(cli.app, ["orca", str(root), "--branch", "probe"])

    assert result.exit_code == 1, result.output
    prepare.assert_called_once_with(root)
    assert cd == [root / "checkout"]
    orca.handoff.assert_not_called()
    assert "setup failed" in result.output
    assert shlex.split(result.output.split("Retry: ", 1)[1]) == [
        "cop", "orca", str(root), "--branch", "probe", "--yes",
    ]


def test_handoff_failure_keeps_exact_checkout_and_retry_branch(monkeypatch, creation, workflow):
    root, _, cd = creation
    monkeypatch.setattr(orca, "handoff", Mock(side_effect=orca.HandoffError("launch failed")))

    result = runner.invoke(cli.app, ["orca", str(root), "--branch", "feature/probe"])

    assert result.exit_code == 1, result.output
    assert cd == [root / "checkout"]
    orca.handoff.assert_called_once_with(root / "checkout")
    assert shlex.split(result.output.split("Retry: ", 1)[1]) == [
        "cop", "orca", str(root), "--branch", "feature/probe", "--yes",
    ]


def test_reuse_does_not_repeat_creation_setup(monkeypatch, creation, workflow):
    root, calls, cd = creation
    monkeypatch.setattr(wt, "branch_exists", lambda *_: True)
    monkeypatch.setattr(orca, "resume_setup", Mock(side_effect=AssertionError("No setup is pending")))
    monkeypatch.setattr(orca, "handoff", Mock(return_value="Reused live Pi"))

    result = runner.invoke(cli.app, ["orca", str(root), "--branch", "probe", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls[0][1]["create"] is False
    orca.finish_setup.assert_not_called()
    orca.resume_setup.assert_not_called()
    orca.handoff.assert_called_once_with(root / "checkout")
    assert cd == [root / "checkout"]
    assert "Reused live Pi" in result.output


def test_retry_runs_pending_setup_by_switching_to_existing_checkout(monkeypatch, creation, workflow):
    root, calls, cd = creation
    setup, _ = workflow
    checkout = root / "existing-checkout"
    orca.STATE_ROOT.mkdir()
    orca._setup_path(checkout).write_text("{}")
    monkeypatch.setattr(wt, "branch_exists", lambda *_: True)
    switch = Mock(return_value={"action": "existing", "path": str(checkout)})
    monkeypatch.setattr(cli, "_switch_with_recovery", switch)
    events = []
    monkeypatch.setattr(orca, "resume_setup", lambda *args: events.append(("setup", args)))
    monkeypatch.setattr(orca, "handoff", lambda path: events.append(("launch", path)) or "Started Pi")

    result = runner.invoke(cli.app, ["orca", str(root), "--branch", "probe", "--yes"])

    assert result.exit_code == 0, result.output
    switch.assert_called_once_with(root, "probe", create=False, base=None, config_overrides=setup.suppress)
    assert calls == []
    assert events == [("setup", (setup, checkout)), ("launch", checkout)]
    orca.finish_setup.assert_not_called()
    assert cd == [checkout]


def test_retry_command_quotes_repo_and_branch(tmp_path):
    root = tmp_path / "repo with spaces"
    assert shlex.split(orca.retry_command(root, "feature/quoted'branch")) == [
        "cop", "orca", str(root), "--branch", "feature/quoted'branch", "--yes",
    ]


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
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{"action":"created"}', "")

    monkeypatch.setattr(wt, "run", run)
    wt.switch(tmp_path, "probe", create=True, config_overrides=("post-start=[]",))
    assert calls[0][0][:3] == ["--config-set", "post-start=[]", "switch"]
    assert calls[0][1] == {"cwd": tmp_path, "stream": True}
