"""Final review regressions for conservative Pi reuse and confirmed focus."""

from unittest.mock import Mock

import pytest

from coppice import orca


@pytest.mark.parametrize("changes", [
    {"orphaned": True}, {"connected": False}, {"writable": False},
    {"handle": None}, {"incarnationId": None},
])
def test_uncertain_pi_blocks_new_launch(monkeypatch, tmp_path, changes):
    monkeypatch.setattr(orca, "STATE_ROOT", tmp_path / "state")
    terminal = {
        "worktreePath": str(tmp_path), "agentIdentity": "pi", "connected": True,
        "writable": True, "orphaned": False, "incarnationId": "live-1", "handle": "pi-1", **changes,
    }
    command = Mock(return_value={"terminals": [terminal]})
    monkeypatch.setattr(orca, "_orca", command)
    with pytest.raises(orca.HandoffError, match="uncertain liveness"):
        orca.handoff(tmp_path)
    command.assert_called_once()
    assert command.call_args.args[0][:2] == ["terminal", "list"]


def test_malformed_entry_after_live_pi_is_rejected(monkeypatch, tmp_path):
    terminal = {
        "worktreePath": str(tmp_path), "agentIdentity": "pi", "connected": True,
        "writable": True, "orphaned": False, "incarnationId": "live-1", "handle": "pi-1",
    }
    with pytest.raises(orca.HandoffError, match="terminal list is unreadable"):
        orca._live_pi({"terminals": [terminal, "unknown"]}, tmp_path)


@pytest.mark.parametrize("result", [
    {}, {"focus": None}, {"focus": []}, {"focus": {"handle": "pi-1", "navigated": False}},
    {"focus": {"handle": "other", "navigated": True}}, {"focus": {"handle": "pi-1"}},
])
def test_focus_requires_confirmed_navigation(monkeypatch, tmp_path, result):
    monkeypatch.setattr(orca, "_orca", Mock(return_value=result))
    with pytest.raises(orca.HandoffError, match="did not confirm opening"):
        orca._focus_pi("pi-1", tmp_path)
