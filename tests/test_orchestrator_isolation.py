"""Plain commands never contact orchestrators; explicit commands use only their surface."""

import copy
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from coppice import cli, git, herdr, orca, park, repo, shell, sizes, vscode, workflow, wt

runner = CliRunner()


def _guards(monkeypatch, *surfaces):
    """Guard entry points and transports without patching shared subprocess."""
    guards = []
    names = {
        orca: ("prepare_workflow", "prepare", "watch", "handoff", "_orca", "_run",
               "switch_ready", "finish_setup", "resume_setup"),
        herdr: ("prepare_workflow", "handoff", "_focus"),
    }
    for surface in surfaces:
        for name in names[surface]:
            guard = Mock(side_effect=AssertionError(f"Unexpected {surface.__name__}.{name}"))
            monkeypatch.setattr(surface, name, guard)
            guards.append(guard)
        if surface is herdr:
            guard = Mock(side_effect=AssertionError("Unexpected Herdr transport"))
            monkeypatch.setattr(herdr.Target, "call", guard)
            guards.append(guard)
    return guards


def _assert_unused(guards):
    # Also detect forbidden calls if production code catches their exceptions.
    for guard in guards:
        guard.assert_not_called()


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Use real Git metadata, temporary state, and stubbed wt boundaries."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "missing-gitconfig"))
    for name in ("WORKTRUNK_AGENT", "HERDR_ENV", "HERDR_SOCKET_PATH", shell.CD_FILE_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    root = (tmp_path / "repo with spaces").resolve()
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["config", "core.hooksPath", str(tmp_path / "missing-hooks")],
        ["commit", "--allow-empty", "-q", "-m", "init"],
        ["branch", "probe"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, timeout=10)
    checkout = tmp_path / "wt selected checkout"
    checkout.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "registry" / "known-repos")
    repo.register_repo(root)
    monkeypatch.setattr(orca, "STATE_ROOT", tmp_path / "orca-state")
    monkeypatch.setattr(herdr, "STATE_ROOT", tmp_path / "herdr-state")
    monkeypatch.setattr(workflow, "STATE_ROOT", tmp_path / "setup-state")
    monkeypatch.setattr(cli.console, "width", 1000)
    monkeypatch.setattr(cli.err, "width", 1000)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/fake/{name}" if name in {"wt", "orca", "herdr"} else None)
    monkeypatch.setattr(wt, "require_wt", Mock(return_value="/fake/wt"))
    monkeypatch.setattr(repo, "default_branch", Mock(return_value="main"))
    monkeypatch.setattr(wt, "branch_exists", Mock(return_value=False))
    monkeypatch.setattr(wt, "remote_branch_exists", Mock(return_value=False))
    monkeypatch.setattr(wt, "run", Mock(side_effect=AssertionError("Unexpected wt subprocess")))
    monkeypatch.setattr(workflow, "_run", Mock(side_effect=AssertionError("Unexpected workflow subprocess")))
    monkeypatch.setattr(vscode, "window_paths", Mock(return_value=[]))
    monkeypatch.setattr(vscode, "_run_osascript", Mock(side_effect=AssertionError("Unexpected desktop access")))
    monkeypatch.setattr(sizes, "dir_sizes_kb", lambda paths, **_kwargs: dict.fromkeys(paths, 0))
    monkeypatch.setattr(git, "fetch_base", Mock())
    monkeypatch.setattr(git, "is_ancestor", Mock(return_value=False))
    monkeypatch.setattr(git, "merge_would_conflict", Mock(return_value=False))
    monkeypatch.setattr(git, "commits_between", Mock(return_value=1))
    mutations = []
    for module, names in ((git, ("ff_only", "merge", "merge_abort")),
                          (wt, ("remove", "relocate"))):
        for name in names:
            guard = Mock(side_effect=AssertionError(f"Unexpected mutation: {name}"))
            monkeypatch.setattr(module, name, guard)
            mutations.append(guard)
    cd = Mock()
    monkeypatch.setattr(shell, "write_cd_file", cd)

    def switch(actual_root, branch, **kwargs):
        assert actual_root == root
        assert branch == "probe"
        return {"action": "created" if kwargs["create"] else "existing",
                "path": str(checkout), "base_branch": kwargs.get("base")}

    switch_mock = Mock(side_effect=switch)
    monkeypatch.setattr(wt, "switch", switch_mock)
    entries = [
        {"branch": "main", "path": str(root), "is_main": True, "is_current": True,
         "working_tree": {}, "worktree": {"state": "active"}},
        {"branch": "probe", "path": str(checkout), "is_main": False, "is_current": False,
         "working_tree": {}, "worktree": {"state": "branch_worktree_mismatch"},
         "main_state": "integrated", "commit": {"timestamp": 1}},
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _root: copy.deepcopy(entries))
    monkeypatch.setattr(wt, "list_worktrees_many", lambda roots: {r: copy.deepcopy(entries) for r in roots})
    # Park uses Git porcelain, not wt. Keep its config reads and writes real.
    monkeypatch.setattr(git, "worktree_entries", lambda _root: copy.deepcopy(entries))
    monkeypatch.setattr(git, "worktree_entries_many", lambda roots: {r: copy.deepcopy(entries) for r in roots})
    monkeypatch.setattr(wt, "relocate_preview", Mock(return_value=[
        {"from": str(checkout), "to": str(tmp_path / "expected checkout")},
    ]))
    # Replace only cli's binding. Patching subprocess.run would also patch Git.
    real_run = subprocess.run

    def cli_run(args, **kwargs):
        if args == ["wt", "--version"]:
            return subprocess.CompletedProcess(args, 0, "wt v0.test", "")
        assert args[0] == "git", f"Unexpected CLI subprocess: {args}"
        return real_run(args, **kwargs)

    monkeypatch.setattr(cli, "subprocess", SimpleNamespace(run=cli_run))
    return SimpleNamespace(root=root, checkout=checkout, switch=switch_mock, cd=cd, mutations=mutations)


@pytest.mark.parametrize("environment", ["manual", "herdr", "agent"])
@pytest.mark.parametrize("case", [
    "list", "status", "sync", "clean", "relocate", "park", "unpark", "remove",
    "new-create", "new-reuse", "bare-create", "bare-reuse",
])
def test_plain_commands_have_zero_orchestrator_calls(monkeypatch, isolated, case, environment):
    c = isolated
    if environment == "herdr":
        monkeypatch.setenv("HERDR_ENV", "1")
        monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/test-only-caller.sock")
    elif environment == "agent":
        monkeypatch.setenv("WORKTRUNK_AGENT", "1")
    # Installed and watched Orca must not make a plain command opt in.
    monkeypatch.setattr(orca, "manual_invocation", Mock(return_value=True))
    guards = _guards(monkeypatch, orca, herdr)
    shared_guards = []
    for name in ("prepare", "switch_ready", "finish_setup", "resume_setup"):
        guard = Mock(side_effect=AssertionError(f"Unexpected shared workflow: {name}"))
        monkeypatch.setattr(workflow, name, guard)
        shared_guards.append(guard)
    args = {
        "list": ["list", "--all", "--no-size"],
        "status": ["status", "--no-size"],
        "sync": ["sync", "--dry-run"],
        "clean": ["clean", "--merged", "--dry-run"],
        "relocate": ["relocate", "--dry-run"],
        "park": ["park", "probe", "--yes"],
        "unpark": ["unpark", "probe"],
        "remove": ["remove", "missing-branch", "--yes"],
    }
    if case == "unpark":
        park.park(c.root, "probe", at=1)
    if case.startswith(("new-", "bare-")):
        monkeypatch.setattr(wt, "branch_exists", Mock(return_value=case.endswith("reuse")))
        command = ["new", str(c.root)] if case.startswith("new-") else [str(c.root)]
        args[case] = [*command, "--branch", "probe", "--yes"]

    result = runner.invoke(cli.app, args[case])

    _assert_unused([*guards, *shared_guards, *c.mutations])
    workflow._run.assert_not_called()
    wt.run.assert_not_called()
    assert result.exit_code == (1 if case == "remove" else 0), result.output
    expected_output = {
        "list": "probe", "status": c.root.name, "sync": "Dry run, nothing changed",
        "clean": "Dry run", "relocate": "Dry run, nothing moved", "park": "Parked 'probe'",
        "unpark": "Unparked 'probe'", "remove": "no worktree for branch 'missing-branch'",
    }
    if case in expected_output:
        assert expected_output[case] in result.output
    else:
        c.switch.assert_called_once_with(c.root, "probe", create=case.endswith("create"),
                                         base="main" if case.endswith("create") else None)
        c.cd.assert_called_once_with(c.checkout)
    if case == "park":
        assert "probe" in park.parked_at(c.root)
    elif case == "unpark":
        assert park.parked_at(c.root) == {}
    assert c.checkout.is_dir()
    assert not orca.STATE_ROOT.exists()
    assert not herdr.STATE_ROOT.exists()
    assert not workflow.STATE_ROOT.exists()


@pytest.mark.parametrize("surface", ["orca", "herdr"])
@pytest.mark.parametrize("case", ["create", "reuse", "cancel", "preflight-fail", "setup-fail", "handoff-fail"])
def test_explicit_commands_contact_only_the_matching_surface(monkeypatch, isolated, surface, case):
    c = isolated
    matching = orca if surface == "orca" else herdr
    opposite = herdr if surface == "orca" else orca
    guards = _guards(monkeypatch, opposite)
    monkeypatch.setattr(workflow, "manual_invocation", Mock(return_value=True))
    monkeypatch.setattr(workflow, "_branch_has_checkout", Mock(return_value=case in {"reuse", "cancel"}))
    monkeypatch.setattr(wt, "branch_exists", Mock(return_value=case in {"reuse", "cancel"}))
    monkeypatch.setattr(cli.confirm, "ask", Mock(return_value=case != "cancel"))
    ready = {"worktreePath": str(c.checkout), "agentIdentity": "pi", "connected": True,
             "writable": True, "orphaned": False, "incarnationId": "incarnation", "handle": "terminal"}
    herdr_ready = {"workspace_id": "workspace", "agent": "pi", "launch_pending": False,
                   "interactive_ready": True, "pane_id": "pane", "terminal_id": "terminal",
                   "agent_session": {"source": "herdr:pi", "agent": "pi", "value": "session"},
                   "cwd": str(c.checkout), "foreground_cwd": str(c.checkout)}
    pi_live = case == "reuse"
    if surface == "orca" and pi_live:
        orca.STATE_ROOT.mkdir()
        orca._remember_pi(c.checkout, ready)

    def run(args, *, cwd, **_kwargs):
        assert cwd in {c.root, c.checkout}
        if args == ["/fake/herdr", "session", "list", "--json"]:
            assert surface == "herdr"
            payload = {"sessions": [{"name": "default", "default": True, "running": True,
                                     "socket_path": "/tmp/test-only-default.sock"}]}
        elif args == ["wt", "-C", str(c.root), "config", "show", "--format", "json"]:
            payload = {"user": {"config": {}}}
        elif "hook" in args and "show" in args:
            payload = [{"name": "failing-setup"}] if case == "setup-fail" else []
        elif "hook" in args and "--foreground" in args:
            assert case == "setup-fail"
            return subprocess.CompletedProcess(args, 1, "", "")
        else:
            pytest.fail(f"Unexpected shared command: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    run_mock = Mock(side_effect=run)
    monkeypatch.setattr(workflow, "_run", run_mock)

    def orca_call(args, *, cwd, **_kwargs):
        nonlocal pi_live
        assert cwd in {c.root, c.checkout}
        if args[:2] == ["repo", "show"]:
            if case == "preflight-fail":
                raise orca.OrcaError("command_failed", "preflight failed")
            return {"repo": {"externalWorktreeVisibility": "show"}}
        if args[:2] == ["repo", "set"]:
            return {}
        if case == "handoff-fail":
            raise orca.OrcaError("command_failed", "handoff failed")
        if args[:2] == ["terminal", "list"]:
            return {"terminals": [ready] if pi_live else []}
        if args[:2] == ["agent", "launch"]:
            pi_live = True
            return {"operationId": "operation", "outcome": {"kind": "terminal", "handle": "terminal"}}
        assert args == ["terminal", "switch", "--terminal", "terminal"]
        return {"focus": {"handle": "terminal", "navigated": True, "tabId": "tab",
                          "worktreeId": f"repo::{c.checkout}"}}

    def herdr_call(args, *, cwd, **_kwargs):
        assert cwd in {c.root, c.checkout}
        if args == ["workspace", "list"]:
            if case == "preflight-fail":
                raise workflow.HandoffError("preflight failed")
            return {"workspaces": []}
        if case == "handoff-fail":
            raise workflow.HandoffError("handoff failed")
        if args[:2] == ["worktree", "open"]:
            return {"workspace": {"workspace_id": "workspace", "worktree": {
                "checkout_path": str(c.checkout), "repo_root": str(c.root)}},
                "root_pane": {"workspace_id": "workspace", "pane_id": "pane", "terminal_id": "terminal"}}
        if args == ["agent", "list"]:
            return {"agents": [herdr_ready] if pi_live else []}
        if args[:2] == ["pane", "process-info"]:
            return {"process_info": {"pane_id": "pane", "shell_pid": 1, "foreground_processes": [{"pid": 1}]}}
        if args[:2] == ["agent", "start"]:
            return {"agent": herdr_ready}
        assert args == ["agent", "focus", "pane"]
        return {"agent": {**herdr_ready, "focused": True}}

    transport = Mock(side_effect=orca_call if surface == "orca" else herdr_call)
    if surface == "orca":
        monkeypatch.setattr(orca, "_orca", transport)
        unused_runner = Mock(side_effect=AssertionError("Unexpected real Orca subprocess"))
        monkeypatch.setattr(orca, "_run", unused_runner)
        guards.append(unused_runner)
    else:
        monkeypatch.setattr(herdr.Target, "call", transport)
    result = runner.invoke(cli.app, [surface, str(c.root), "--branch", "probe"])

    _assert_unused([*guards, *c.mutations])
    wt.run.assert_not_called()
    assert result.exit_code == (0 if case in {"create", "reuse"} else 1), result.output
    assert not opposite.STATE_ROOT.exists()
    if case == "cancel":
        transport.assert_not_called()
        run_mock.assert_not_called()
    else:
        assert transport.call_count > 0
    if case in {"cancel", "preflight-fail"}:
        c.switch.assert_not_called()
        c.cd.assert_not_called()
        assert not workflow.STATE_ROOT.exists()
    else:
        c.switch.assert_called_once_with(c.root, "probe", create=case != "reuse",
                                         base=None if case == "reuse" else "main", config_overrides=("post-start=[]",))
        c.cd.assert_called_once_with(c.checkout)
    if case in {"create", "reuse"}:
        assert f"{'Started' if case == 'create' else 'Reused'} Pi" in result.output
        assert matching.STATE_ROOT.exists()
    elif case in {"setup-fail", "handoff-fail"}:
        assert f"Retry: cop {surface}" in result.output
        message = "A wt setup hook failed" if case == "setup-fail" else "handoff failed"
        assert message in result.output


def test_shared_prepare_contacts_neither_orchestrator(monkeypatch, isolated):
    guards = _guards(monkeypatch, orca, herdr)
    monkeypatch.setenv("WORKTRUNK_AGENT", "1")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/test-only-caller.sock")
    gate = Mock(side_effect=AssertionError("Shared config reads must not apply a surface gate"))
    monkeypatch.setattr(workflow, "manual_invocation", gate)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '{"user": {"config": {}}}', ""))
    monkeypatch.setattr(workflow, "_run", run)

    assert workflow.prepare(isolated.root) == workflow.Setup(isolated.root, ("post-start=[]",), ())

    run.assert_called_once_with(["wt", "-C", str(isolated.root), "config", "show", "--format", "json"],
                                cwd=isolated.root)
    _assert_unused([*guards, gate])
    assert not orca.STATE_ROOT.exists()
    assert not herdr.STATE_ROOT.exists()
    assert not workflow.STATE_ROOT.exists()


def test_fresh_cli_import_starts_no_subprocess(monkeypatch, isolated, tmp_path):
    """Guard subprocess creation only in the child, leaving Git fixtures alone."""
    monkeypatch.setenv("HOME", str(tmp_path))
    code = """
from unittest.mock import Mock, patch
blocked = Mock(side_effect=AssertionError("Import started a subprocess"))
with patch("subprocess.Popen", blocked), patch("subprocess.run", blocked), patch("os.system", blocked):
    from coppice import cli, herdr, orca
blocked.assert_not_called()
print("imported without subprocesses")
"""
    result = subprocess.run(["uv", "run", "--no-sync", sys.executable, "-c", code],
                            cwd=isolated.root, check=True, capture_output=True, text=True, timeout=20)
    assert result.stdout.strip() == "imported without subprocesses"
