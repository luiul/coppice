from unittest.mock import Mock

import pytest

from coppice import orca, workflow


@pytest.fixture
def activation(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(orca, "manual_invocation", lambda: True)
    watch = Mock()
    monkeypatch.setattr(orca, "watch", watch)
    path = tmp_path.resolve()
    key = "wt2:local:instance"
    row = {
        "id": f"repo::{path}",
        "path": str(path),
        "repoId": "repo",
        "hostId": "local",
        "instanceId": "instance",
        "isMainWorktree": True,
        "identity": {"key": key, "executionHostId": "local", "instanceId": "instance"},
    }
    command = Mock(side_effect=[{"worktree": row}, {"worktreeId": row["id"]}])
    monkeypatch.setattr(orca, "_orca", command)
    return path, row, command, watch


def test_activate_uses_exact_local_workspace_not_pi_launch(activation):
    path, row, command, watch = activation
    assert "Opened the main worktree" in orca.activate_main(path)
    watch.assert_called_once_with(path, local=True)
    assert [c.args[0] for c in command.call_args_list] == [
        ["worktree", "show", "--environment", "local", "--worktree", f"path:{path}"],
        ["worktree", "activate", "--environment", "local", "--worktree", f"identity:{row['identity']['key']}"],
    ]


@pytest.mark.parametrize("changes", [{"isMainWorktree": False}, {"hostId": "remote"}, {"path": "/wrong"}])
def test_activate_rejects_wrong_workspace_without_fallback(activation, changes):
    path, row, command, _ = activation
    row.update(changes)
    with pytest.raises(orca.HandoffError):
        orca.activate_main(path)
    command.assert_called_once()


def test_does_not_claim_focus_if_app_confirms_another_workspace(activation):
    path, row, command, _ = activation
    command.side_effect = [{"worktree": row}, {"worktreeId": "repo::different"}]
    with pytest.raises(orca.HandoffError, match="did not confirm activation"):
        orca.activate_main(path)
    assert command.call_count == 2
