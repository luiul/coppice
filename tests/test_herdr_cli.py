"""Explicit Herdr creation shares wt paths and shell behavior, without runtime calls."""

import shlex
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from coppice import cli, herdr, orca, repo, shell, workflow, wt

runner = CliRunner()


@pytest.fixture
def creation(monkeypatch, tmp_path):
    root = tmp_path / "repo with spaces"
    root.mkdir()
    checkout = tmp_path / "wt selected checkout"
    checkout.mkdir()
    monkeypatch.setattr(cli.console, "width", 1000)
    resolve = Mock(return_value=root)
    register = Mock()
    monkeypatch.setattr(repo, "resolve_repo_root", resolve)
    monkeypatch.setattr(repo, "register_repo", register)
    monkeypatch.setattr(repo, "default_branch", Mock(return_value="main"))
    monkeypatch.setattr(wt, "require_wt", Mock(return_value="wt"))
    monkeypatch.setattr(wt, "branch_exists", Mock(return_value=False))
    monkeypatch.setattr(wt, "remote_branch_exists", Mock(return_value=False))
    monkeypatch.setattr(cli, "_freshen_explicit_base", Mock())
    monkeypatch.setattr(workflow, "_run", Mock(side_effect=AssertionError("Unexpected runtime subprocess")))
    monkeypatch.setattr(herdr.Target, "call", Mock(side_effect=AssertionError("Unexpected Herdr runtime call")))
    monkeypatch.setattr(herdr, "STATE_ROOT", tmp_path / "herdr-state")
    monkeypatch.setattr(workflow, "STATE_ROOT", tmp_path / "setup-state")
    events = []

    def switch(actual_root, branch, **kwargs):
        events.append(("switch", actual_root, branch, kwargs))
        return {
            "action": "created" if kwargs["create"] else "existing",
            "path": str(checkout), "base_branch": kwargs["base"],
        }

    switch_mock = Mock(side_effect=switch)
    cd = Mock(side_effect=lambda path: events.append(("cd", path)))
    monkeypatch.setattr(cli, "_switch_with_recovery", switch_mock)
    monkeypatch.setattr(shell, "write_cd_file", cd)
    setup = workflow.Setup(root, ("post-start=[]",), ())
    target = herdr.Target("/tmp/named-herdr.sock", "/fake/herdr")
    prepare = Mock(side_effect=lambda *args: events.append(("prepare", *args)) or (setup, target))
    monkeypatch.setattr(herdr, "prepare_workflow", prepare)
    monkeypatch.setattr(orca, "prepare_workflow", Mock(return_value=setup))
    monkeypatch.setattr(orca, "handoff", Mock(return_value="Started Pi in Orca"))

    def switch_ready(actual_setup, branch, base, switch):
        events.append(("ready", actual_setup, branch, base))
        result = switch()
        events.append(("setup-complete",))
        return result

    ready = Mock(side_effect=switch_ready)
    monkeypatch.setattr(workflow, "switch_ready", ready)
    handoff = Mock(side_effect=lambda *args: events.append(("handoff", *args)) or "Started Pi in Herdr")
    monkeypatch.setattr(herdr, "handoff", handoff)
    return SimpleNamespace(root=root, checkout=checkout, resolve=resolve, register=register, switch=switch_mock,
                           cd=cd, setup=setup, target=target, prepare=prepare, ready=ready, handoff=handoff,
                           events=events)


def test_herdr_preflight_and_setup_precede_handoff_using_exact_wt_path(creation):
    c = creation
    result = runner.invoke(cli.app, ["herdr", str(c.root), "--branch", "probe"])

    assert result.exit_code == 0, result.output
    assert c.events == [
        ("prepare", c.root, None),
        ("ready", c.setup, "probe", "main"),
        ("switch", c.root, "probe", {"create": True, "base": "main", "config_overrides": c.setup.suppress}),
        ("setup-complete",), ("cd", c.checkout), ("handoff", c.target, c.root, c.checkout),
    ]
    c.prepare.assert_called_once_with(c.root, None)
    c.ready.assert_called_once()
    c.register.assert_called_once_with(c.root)
    c.cd.assert_called_once_with(c.checkout)
    c.handoff.assert_called_once_with(c.target, c.root, c.checkout)
    orca.prepare_workflow.assert_not_called()
    orca.handoff.assert_not_called()


@pytest.mark.parametrize("path_kind", ["default", "root", "nested"])
@pytest.mark.parametrize("session", [None, "review session"])
def test_herdr_resolves_default_root_and_nested_paths_before_selecting_session(creation, path_kind, session):
    c = creation
    args = ["herdr"]
    supplied_path = {"default": ".", "root": str(c.root), "nested": str(c.root / "src" / "deep")}[path_kind]
    if path_kind != "default":
        args += [supplied_path]
    args += ["--branch", "feature/probe"]
    if session:
        args += ["--session", session]

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    c.resolve.assert_called_once_with(supplied_path)
    c.prepare.assert_called_once_with(c.root, session)
    c.handoff.assert_called_once_with(c.target, c.root, c.checkout)
    c.cd.assert_called_once_with(c.checkout)


@pytest.mark.parametrize("command", ["new", "orca", "herdr"])
@pytest.mark.parametrize("flags", [("--branch", "--base", "--yes"), ("-b", "-B", "-y")])
def test_creation_commands_have_matching_branch_base_and_yes_flags(creation, command, flags):
    c = creation
    branch_flag, base_flag, yes_flag = flags
    result = runner.invoke(cli.app, [command, str(c.root), branch_flag, "feature/probe",
                                     base_flag, "develop", yes_flag])

    assert result.exit_code == 0, result.output
    expected = {"create": True, "base": "develop"}
    if command != "new":
        expected["config_overrides"] = c.setup.suppress
        c.ready.assert_called_once()
    else:
        c.ready.assert_not_called()
    c.switch.assert_called_once_with(c.root, "feature/probe", **expected)
    cli._freshen_explicit_base.assert_called_once_with(c.root, "develop")
    c.cd.assert_called_once_with(c.checkout)
    if command == "herdr":
        c.prepare.assert_called_once_with(c.root, None)
        c.handoff.assert_called_once_with(c.target, c.root, c.checkout)
        orca.prepare_workflow.assert_not_called()
    else:
        c.prepare.assert_not_called()
        c.handoff.assert_not_called()


@pytest.mark.parametrize("description,branch", [("Fix thing / one", "fix-thing-one"), ("", "wip-fixed-timestamp")])
def test_herdr_uses_shared_description_prompt(monkeypatch, creation, description, branch):
    c = creation
    listing = Mock()
    monkeypatch.setattr(cli, "_print_existing_worktrees", listing)
    monkeypatch.setattr(cli.branch_mod, "timestamp_branch", lambda: "wip-fixed-timestamp")
    result = runner.invoke(cli.app, ["herdr", str(c.root)], input=f"{description}\n")

    assert result.exit_code == 0, result.output
    assert "Short branch description" in result.output
    listing.assert_called_once_with(c.root)
    c.switch.assert_called_once_with(c.root, branch, create=True, base="main", config_overrides=c.setup.suppress)
    c.cd.assert_called_once_with(c.checkout)


@pytest.mark.parametrize("location", ["local", "remote"])
@pytest.mark.parametrize("accept", [True, False])
def test_herdr_confirms_existing_branch_before_preflight(monkeypatch, creation, location, accept):
    c = creation
    monkeypatch.setattr(wt, "branch_exists", Mock(return_value=location == "local"))
    monkeypatch.setattr(wt, "remote_branch_exists", Mock(return_value=location == "remote"))
    ask = Mock(return_value=accept)
    monkeypatch.setattr(cli.confirm, "ask", ask)
    result = runner.invoke(cli.app, ["herdr", str(c.root), "--branch", "probe"])

    ask.assert_called_once_with("Switch to the 'probe' worktree? The branch already exists.")
    repo.default_branch.assert_not_called()
    if accept:
        assert result.exit_code == 0, result.output
        assert "Reused worktree" in result.output
        c.switch.assert_called_once_with(c.root, "probe", create=False, base=None, config_overrides=c.setup.suppress)
        c.handoff.assert_called_once_with(c.target, c.root, c.checkout)
        c.cd.assert_called_once_with(c.checkout)
    else:
        assert result.exit_code == 1, result.output
        assert "Cancelled." in result.output
        c.prepare.assert_not_called()
        c.ready.assert_not_called()
        c.switch.assert_not_called()
        c.handoff.assert_not_called()
        c.cd.assert_not_called()


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_herdr_existing_branch_yes_skips_confirmation(monkeypatch, creation, flag):
    c = creation
    monkeypatch.setattr(wt, "branch_exists", Mock(return_value=True))
    monkeypatch.setattr(cli.confirm, "ask", Mock(side_effect=AssertionError("--yes skips confirmation")))
    result = runner.invoke(cli.app, ["herdr", str(c.root), "--branch", "probe", flag])

    assert result.exit_code == 0, result.output
    c.switch.assert_called_once_with(c.root, "probe", create=False, base=None, config_overrides=c.setup.suppress)
    c.cd.assert_called_once_with(c.checkout)


@pytest.mark.parametrize("existing", [False, True])
def test_new_has_zero_orchestrator_calls(monkeypatch, creation, existing):
    c = creation
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/caller.sock")
    monkeypatch.setattr(wt, "branch_exists", Mock(return_value=existing))
    guards = []
    for module, names in (
        (herdr, ("prepare_workflow", "handoff")),
        (orca, ("prepare_workflow", "handoff")),
        (workflow, ("prepare", "switch_ready", "finish_setup", "resume_setup")),
    ):
        for name in names:
            guard = Mock(side_effect=AssertionError(f"cop new must not call {name}"))
            guards.append(guard)
            monkeypatch.setattr(module, name, guard)
    result = runner.invoke(cli.app, ["new", str(c.root), "--branch", "probe", "--yes"])

    assert result.exit_code == 0, result.output
    c.switch.assert_called_once_with(c.root, "probe", create=not existing, base=None if existing else "main")
    c.cd.assert_called_once_with(c.checkout)
    for guard in guards:
        guard.assert_not_called()
    herdr.Target.call.assert_not_called()
    workflow._run.assert_not_called()
    assert not herdr.STATE_ROOT.exists()
    assert not workflow.STATE_ROOT.exists()
    assert "Pi" not in result.output


@pytest.mark.parametrize("failure", [workflow.HandoffError("selected session stopped"), OSError("preflight failed")])
def test_herdr_preflight_failure_creates_nothing_and_writes_no_cd(creation, failure):
    c = creation
    c.prepare.side_effect = failure
    result = runner.invoke(cli.app, ["herdr", str(c.root), "--branch", "probe", "--session", "review"])

    assert result.exit_code == 1, result.output
    assert str(failure) in result.output
    c.prepare.assert_called_once_with(c.root, "review")
    c.ready.assert_not_called()
    c.switch.assert_not_called()
    c.register.assert_not_called()
    c.handoff.assert_not_called()
    c.cd.assert_not_called()


@pytest.mark.parametrize("failure", [workflow.HandoffError("creation lock busy"), OSError("checkout failed")])
def test_shared_switch_failure_is_nonzero_without_handoff_or_cd(creation, failure):
    c = creation
    c.ready.side_effect = failure
    result = runner.invoke(cli.app, ["herdr", str(c.root), "--branch", "probe"])

    assert result.exit_code == 1, result.output
    assert str(failure) in result.output
    c.switch.assert_not_called()
    c.register.assert_not_called()
    c.handoff.assert_not_called()
    c.cd.assert_not_called()


@pytest.mark.parametrize("session", [None, "review session'quoted"])
@pytest.mark.parametrize("failure_kind", ["setup", "handoff"])
def test_failed_setup_or_handoff_preserves_cd_and_exact_branch_retry(creation, session, failure_kind):
    c = creation
    branch = "feature/quoted'branch"
    message = "setup failed" if failure_kind == "setup" else "focus failed"
    if failure_kind == "setup":
        def incomplete(actual_setup, actual_branch, base, switch):
            result = switch()
            result["setup_error"] = message
            return result
        c.ready.side_effect = incomplete
    else:
        c.handoff.side_effect = workflow.HandoffError(message)
    args = ["herdr", str(c.root), "--branch", branch]
    if session:
        args += ["--session", session]
    result = runner.invoke(cli.app, args)

    assert result.exit_code == 1, result.output
    assert message in result.output
    c.cd.assert_called_once_with(c.checkout)
    c.register.assert_called_once_with(c.root)
    c.switch.assert_called_once_with(c.root, branch, create=True, base="main", config_overrides=c.setup.suppress)
    expected = ["cop", "herdr", str(c.root), "--branch", branch, "--yes"]
    if session:
        expected += ["--session", session]
    assert shlex.split(result.output.split("Retry: ", 1)[1]) == expected
    if failure_kind == "setup":
        c.handoff.assert_not_called()
    else:
        c.handoff.assert_called_once_with(c.target, c.root, c.checkout)


def test_herdr_handoff_oserror_keeps_shell_cd_and_returns_nonzero(creation):
    c = creation
    c.handoff.side_effect = OSError("state store unavailable")
    result = runner.invoke(cli.app, ["herdr", str(c.root), "--branch", "probe", "--session", "review"])

    assert result.exit_code == 1, result.output
    assert "state store unavailable" in result.output
    c.cd.assert_called_once_with(c.checkout)
    assert shlex.split(result.output.split("Retry: ", 1)[1]) == [
        "cop", "herdr", str(c.root), "--branch", "probe", "--yes", "--session", "review",
    ]
