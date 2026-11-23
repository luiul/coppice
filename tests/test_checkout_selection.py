import os
import subprocess
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from coppice import cli, git, orca, repo, rex, shell, workflow, wt

runner = CliRunner()


def run_git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        check=True,
        text=True,
        env={**git.clean_git_environment(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    ).stdout


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "main worktree"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "local-anchor")
    tracked = root / "tracked"
    tracked.write_text("initial\n")
    run_git(root, "add", "tracked")
    run_git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "initial")
    linked = tmp_path / "linked worktree"
    run_git(root, "worktree", "add", "-qb", "linked-task", str(linked))
    run_git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/default")
    monkeypatch.setattr(cli.console, "width", 1000)
    monkeypatch.setattr(cli.err, "width", 1000)
    monkeypatch.setattr(cli, "_print_existing_worktrees", Mock())
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "registry")
    monkeypatch.setattr(wt, "require_wt", Mock(return_value="wt"))
    monkeypatch.setattr(wt, "switch", Mock(side_effect=AssertionError("Unexpected switch")))
    monkeypatch.setattr(repo, "default_branch", Mock(side_effect=AssertionError("Unexpected remote lookup")))
    monkeypatch.setattr(workflow, "prepare", Mock(side_effect=AssertionError("Unexpected setup")))
    monkeypatch.delenv(shell.CD_FILE_ENV_VAR, raising=False)
    cd = Mock()
    monkeypatch.setattr(shell, "write_cd_file", cd)
    return root, linked, cd


@pytest.mark.parametrize("option,answer", [([], "\n"), ([], "   \n"), (["--main-worktree"], None)])
@pytest.mark.parametrize("command", ["new", "orca", "rex"])
def test_existing_main_selection_preserves_dirty_index_and_branch(checkout, monkeypatch, command, option, answer):
    root, linked, cd = checkout
    tracked = root / "tracked"
    tracked.write_text("staged\n")
    run_git(root, "add", "tracked")
    tracked.write_text("unstaged\n")
    before = run_git(root, "status", "--porcelain")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: answer is not None)
    activate = Mock(return_value="Opened Orca")
    target = Mock()
    prepare = Mock(return_value=target)
    open_rex = Mock(return_value="Opened Rex")
    monkeypatch.setattr(orca, "activate_main", activate)
    monkeypatch.setattr(orca, "handoff", Mock(side_effect=AssertionError("Unexpected Pi launch")))
    monkeypatch.setattr(rex, "prepare", prepare)
    monkeypatch.setattr(rex, "handoff", open_rex)
    result = runner.invoke(cli.app, [command, str(linked), *option], input=answer)
    assert result.exit_code == 0, result.output
    assert "Branch: local-anchor" in result.output
    assert "Cached default branch: default" in result.output
    cd.assert_called_once_with(root.resolve())
    assert run_git(root, "branch", "--show-current").strip() == "local-anchor"
    assert run_git(root, "status", "--porcelain") == before == "MM tracked\n"
    assert run_git(root, "show", ":tracked") == "staged\n"
    assert tracked.read_text() == "unstaged\n"
    wt.switch.assert_not_called()
    workflow.prepare.assert_not_called()
    if command == "orca":
        activate.assert_called_once_with(root.resolve())
    else:
        activate.assert_not_called()
    if command == "rex":
        prepare.assert_called_once_with(root.resolve(), None)
        open_rex.assert_called_once_with(target, root.resolve(), label="main worktree: local-anchor")
    else:
        prepare.assert_not_called()
        open_rex.assert_not_called()


@pytest.mark.parametrize(
    "flags,answer,tty,expected",
    [
        (["--main-worktree", "--base", "default"], None, False, "cannot be combined"),
        (["--main-worktree", "--branch", "foo"], None, True, "cannot be combined"),
        (["--base", "default"], "\n", True, "needs a task branch"),
        ([], "!!!\n", True, "letters or numbers"),
        ([], "\n", False, "stdin is not a terminal"),
        ([], "", True, "Aborted"),
    ],
)
def test_invalid_or_cancelled_selection_does_not_mutate(checkout, monkeypatch, flags, answer, tty, expected):
    _, linked, cd = checkout
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: tty)
    result = runner.invoke(cli.app, ["new", str(linked), *flags], input=answer)
    assert result.exit_code != 0
    assert expected in result.output
    wt.switch.assert_not_called()
    cd.assert_not_called()


def test_detached_main_remains_detached(checkout):
    root, linked, cd = checkout
    run_git(root, "switch", "--detach", "-q")
    result = runner.invoke(cli.app, ["new", str(linked), "--main-worktree"])
    assert result.exit_code == 0, result.output
    assert "Detached HEAD" in result.output
    assert not run_git(root, "branch", "--show-current")
    cd.assert_called_once_with(root.resolve())


def test_bare_has_no_main_worktree(tmp_path):
    run_git(tmp_path, "init", "--bare", "-q")
    result = runner.invoke(cli.app, ["new", str(tmp_path), "--main-worktree"])
    assert result.exit_code == 1
    assert "no available main worktree" in result.output


def test_orca_main_activation_failure_retries_main_not_branch(checkout, monkeypatch):
    root, linked, cd = checkout
    monkeypatch.setattr(orca, "activate_main", Mock(side_effect=orca.HandoffError("Activation failed")))
    result = runner.invoke(cli.app, ["orca", str(linked), "--main-worktree"])
    assert result.exit_code == 1
    assert "Activation failed" in result.output
    assert "--main-worktree" in result.output and "--branch" not in result.output
    cd.assert_called_once_with(root.resolve())


def test_retired_command_is_not_available():
    result = runner.invoke(cli.app, ["herdr"])
    assert result.exit_code == 2
    assert "No such command" in result.output
