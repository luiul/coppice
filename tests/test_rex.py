import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coppice import rex, workflow


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(rex, "STATE_ROOT", tmp_path / "rex")
    monkeypatch.setattr(workflow, "STATE_ROOT", tmp_path / "workflows")
    target = rex.Target("rex", "unix:///private/test.sock", "host", "instance", "session:selected")
    state = SimpleNamespace(
        windows=[],
        active="window:untouched",
        cwd=tmp_path,
        fail_create=False,
        creates=0,
        cannot_start=False,
        malformed_layout=False,
    )

    def call(self, args, cwd):
        assert self == target
        assert cwd == tmp_path
        command = args[:2]
        if command == ["whoami", "--json"]:
            return {"endpoint": target.endpoint, "host_id": target.host, "instance_id": target.instance}
        if command == ["session", "inspect"]:
            assert args[2] == target.session
            return {"session_id": target.session, "windows": state.windows, "active_window_id": state.active}
        if command == ["window", "new"]:
            assert args[2:7] == ["-s", target.session, "-c", str(tmp_path), "--json"]
            if state.cannot_start:
                raise rex.CliNotStarted("Could not start the Rex CLI.")
            state.creates += 1
            state.active = f"window:{state.creates}"
            block = f"block:{state.creates}"
            state.windows.append(
                {
                    "window_id": state.active,
                    "focused_block_id": block,
                    "label": args[-1],
                    "layers": [
                        {"blocks": [{"block_id": block, "host_id": target.host}]},
                    ],
                }
            )
            if state.malformed_layout:
                del state.windows[-1]["layers"]
            if state.fail_create:
                raise rex.HandoffError("lost reply")
            return {"window_id": state.active, "block_ids": [block]}
        if command == ["block", "call"]:
            assert args[2:4] == ["-s", target.session]
            return {"foreground": {"cwd": str(state.cwd)}}
        if args[0] == "focus":
            assert args[1:3] == ["-s", target.session]
            state.active = args[-1].replace("block:", "window:")
            return {}
        pytest.fail(str(args))

    monkeypatch.setattr(rex.Target, "call", call)
    return target, tmp_path, state


def test_opens_one_shell_tab_then_reuses_it(server):
    target, path, state = server
    assert "Opened" in rex.handoff(target, path, label="project: task")
    assert "Focused" in rex.handoff(target, path, label="project: task")
    assert state.active == "window:1"
    assert state.creates == 1


def test_lost_create_reply_recovers_existing_tab_without_creating_again(server):
    target, path, state = server
    state.fail_create = True
    with pytest.raises(rex.HandoffError, match="unconfirmed"):
        rex.handoff(target, path, label="project: task")
    assert "Focused" in rex.handoff(target, path, label="project: task")
    assert state.creates == 1


def test_pending_create_without_a_verifiable_tab_is_not_reissued(server):
    target, path, state = server
    state.fail_create = True
    with pytest.raises(rex.HandoffError, match="unconfirmed"):
        rex.handoff(target, path, label="project: task")
    state.windows.clear()
    with pytest.raises(rex.HandoffError, match="Rex tab creation is unconfirmed") as exc:
        rex.handoff(target, path, label="project: task")
    (record,) = rex.STATE_ROOT.glob("*.json")
    assert str(record) in str(exc.value)
    assert "session:selected" in str(exc.value)
    assert state.creates == 1


def test_cli_that_cannot_start_does_not_leave_pending_record(server):
    target, path, state = server
    state.cannot_start = True
    with pytest.raises(rex.CliNotStarted):
        rex.handoff(target, path, label="project: task")
    assert not list(rex.STATE_ROOT.glob("*.json"))
    state.cannot_start = False
    assert "Opened" in rex.handoff(target, path, label="project: task")
    assert state.creates == 1


def test_malformed_tab_layout_does_not_raise_key_error(monkeypatch, server):
    target, path, state = server
    state.malformed_layout = True
    clock = iter([0, 4])
    monkeypatch.setattr(rex, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(rex.HandoffError, match="unreadable tab layout"):
        rex.handoff(target, path, label="project: task")
    assert state.creates == 1


def test_does_not_focus_tab_after_its_cwd_changes(server):
    target, path, state = server
    rex.handoff(target, path, label="project: task")
    state.cwd = path / "different"
    state.active = "window:unrelated"
    with pytest.raises(rex.HandoffError, match="another directory"):
        rex.handoff(target, path, label="project: task")
    assert state.active == "window:unrelated"
    assert state.creates == 1


def test_closed_verified_tab_can_be_reopened(server):
    target, path, state = server
    rex.handoff(target, path, label="project: task")
    state.windows.clear()
    assert "Opened" in rex.handoff(target, path, label="project: task")
    assert state.creates == 2


@pytest.mark.parametrize("session", ["not-an-id", "session:"])
def test_explicit_session_requires_full_id(monkeypatch, tmp_path, session):
    monkeypatch.delenv("REX_SESSION", raising=False)
    monkeypatch.setattr(workflow, "manual_invocation", lambda: True)
    binary = Mock(side_effect=AssertionError("Unexpected executable lookup"))
    monkeypatch.setattr(rex, "_binary", binary)
    with pytest.raises(rex.HandoffError, match="full ID"):
        rex.prepare(tmp_path, session)
    binary.assert_not_called()


def test_transport_disables_autostart_and_ignores_rex_host_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("REX_SERVER", "remote")
    monkeypatch.setenv("REX_SESSION", "session:elsewhere")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"result": True})))
    monkeypatch.setattr(workflow, "_run", run)
    assert rex._call("rex", ["whoami", "--json"], cwd=tmp_path, endpoint="unix:///local.sock") == {"result": True}
    (args,) = run.call_args.args
    assert args == ["rex", "--autostart=false", "--timeout", "3s", "-S", "unix:///local.sock", "whoami", "--json"]
    assert not any(k.startswith("REX_") for k in run.call_args.kwargs["env"])
