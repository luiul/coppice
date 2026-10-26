"""Herdr handoff contracts, with isolated state and no runtime calls."""

import hashlib
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from coppice import herdr, workflow


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(herdr, "STATE_ROOT", tmp_path / "herdr-state")
    monkeypatch.setattr(workflow, "STATE_ROOT", tmp_path / "setup-state")
    monkeypatch.setattr(workflow, "_run", Mock(side_effect=AssertionError("Unexpected subprocess")))
    monkeypatch.setattr(workflow, "prepare", Mock(return_value=workflow.Setup(tmp_path, ("post-start=[]",), ())))
    monkeypatch.setattr(workflow.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(herdr.shutil, "which", lambda name: "/fake/herdr" if name == "herdr" else None)
    monkeypatch.delenv("WORKTRUNK_AGENT", raising=False)
    for name in list(os.environ):
        if name.startswith("HERDR_"):
            monkeypatch.delenv(name)


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / "repo with spaces"
    checkout = tmp_path / "wt chosen path"
    root.mkdir()
    checkout.mkdir()
    return root.resolve(), checkout.resolve()


@pytest.fixture
def target():
    return herdr.Target("/tmp/selected-herdr.sock", "/fake/herdr")


def reply(value, returncode=0):
    return subprocess.CompletedProcess([], returncode, json.dumps(value), "private stderr")


def session(name="default", *, default=True, running=True, socket="/tmp/default.sock"):
    return {"name": name, "default": default, "running": running, "socket_path": socket}


def opened(root, checkout):
    return {
        "workspace": {
            "workspace_id": "workspace-1",
            "worktree": {"checkout_path": str(checkout), "repo_root": str(root)},
        },
        "root_pane": {"workspace_id": "workspace-1", "pane_id": "pane-1", "terminal_id": "terminal-1"},
    }


def ready(checkout, **changes):
    return {
        "workspace_id": "workspace-1", "agent": "pi", "pane_id": "pane-1", "terminal_id": "terminal-1",
        "launch_pending": False, "interactive_ready": True,
        "agent_session": {"source": "herdr:pi", "agent": "pi", "value": "pi-session-1"},
        "cwd": str(checkout), "foreground_cwd": str(checkout), "focused": False,
        **changes,
    }


def process_info(**changes):
    return {"process_info": {
        "pane_id": "pane-1", "shell_pid": 401, "foreground_processes": [{"pid": 401}], **changes,
    }}


def record_path(target, checkout):
    digest = hashlib.sha256((target.socket + "\0" + str(checkout)).encode()).hexdigest()
    return herdr.STATE_ROOT / f"{digest}.json"


def pending(target, checkout, **changes):
    state = {"path": str(checkout), "socket": target.socket, "pane": "pane-1", "terminal": "terminal-1", **changes}
    herdr.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    record_path(target, checkout).write_text(json.dumps(state))
    return state


def commands(monkeypatch, *values):
    mocked = Mock(side_effect=values if values else AssertionError("Unexpected Herdr command"))
    monkeypatch.setattr(herdr.Target, "call", mocked)
    return mocked


def open_call(root, checkout):
    return call(["worktree", "open", "--cwd", str(root), "--path", str(checkout), "--no-focus"], cwd=checkout)


def test_target_copies_environment_and_removes_other_herdr_variables(monkeypatch, target, paths):
    root, _ = paths
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/caller.sock")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SESSION", "caller")
    monkeypatch.setenv("HERDR_UNKNOWN", "inherited")
    monkeypatch.setenv("COP_TEST_KEEP", "ordinary")
    before = dict(os.environ)
    run = Mock(return_value=reply({"result": {"workspaces": []}}))
    monkeypatch.setattr(workflow, "_run", run)

    assert target.call(["workspace", "list"], cwd=root, timeout=7) == {"workspaces": []}

    env = {key: value for key, value in before.items() if not key.startswith("HERDR_")}
    env["HERDR_SOCKET_PATH"] = target.socket
    run.assert_called_once_with([target.binary, "workspace", "list"], cwd=root, timeout=7, env=env)
    assert dict(os.environ) == before


@pytest.mark.parametrize("payload,code", [
    (None, 0), ([], 0), ("string", 0), ({}, 0), ({"result": []}, 0), ({"result": None}, 0),
    ({"result": {}}, 1), ({"error": {"message": "server rejected"}}, 0), ({"error": "broken"}, 0),
])
def test_target_rejects_bad_payloads(monkeypatch, target, paths, payload, code):
    monkeypatch.setattr(workflow, "_run", Mock(return_value=reply(payload, code)))
    with pytest.raises(workflow.HandoffError):
        target.call(["workspace", "list"], cwd=paths[0])


@pytest.mark.parametrize("failure", [
    OSError("socket unavailable"), subprocess.TimeoutExpired("herdr", 10),
])
def test_target_transport_failure_is_bounded(monkeypatch, target, paths, failure):
    run = Mock(side_effect=failure)
    monkeypatch.setattr(workflow, "_run", run)
    with pytest.raises(workflow.HandoffError, match="did not complete"):
        target.call(["workspace", "list"], cwd=paths[0])
    assert run.call_args.kwargs["timeout"] == 10


def test_target_rejects_non_json(monkeypatch, target, paths):
    monkeypatch.setattr(workflow, "_run", Mock(return_value=subprocess.CompletedProcess([], 0, "not json", "")))
    with pytest.raises(workflow.HandoffError, match="did not complete"):
        target.call(["workspace", "list"], cwd=paths[0])


@pytest.mark.parametrize("selected", ["caller", "default", "named"])
def test_prepare_selects_caller_default_or_named_running_session_before_setup(monkeypatch, paths, selected):
    root, _ = paths
    records = [session("focused", default=False, socket="/tmp/focused.sock"), session(),
               session("named", default=False, socket="/tmp/named.sock")]
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/caller.sock" if selected != "default" else "")
    run = Mock(return_value=reply({"sessions": records}))
    monkeypatch.setattr(workflow, "_run", run)
    events = []
    setup = workflow.Setup(root, ("post-start=[]",), ())
    probe = Mock(side_effect=lambda args, **kw: events.append(("probe", args, kw)) or {"workspaces": []})
    monkeypatch.setattr(herdr.Target, "call", probe)
    prepare = Mock(side_effect=lambda path: events.append(("setup", path)) or setup)
    monkeypatch.setattr(workflow, "prepare", prepare)

    actual_setup, actual_target = herdr.prepare_workflow(root, "named" if selected == "named" else None)

    assert actual_setup is setup
    assert actual_target == herdr.Target(f"/tmp/{selected}.sock", "/fake/herdr")
    assert events == [("probe", ["workspace", "list"], {"cwd": root}), ("setup", root)]
    prepare.assert_called_once_with(root)
    if selected == "caller":
        run.assert_not_called()
    else:
        run.assert_called_once_with(["/fake/herdr", "session", "list", "--json"], cwd=root)


@pytest.mark.parametrize("selection,records", [
    (None, []), (None, [session(running=False)]), ("missing", [session()]),
    ("named", [session("named", running=False)]), (None, [session(), session("duplicate")]),
    ("named", [session("named"), session("named")]), (None, [session(default=False)]),
])
def test_prepare_missing_ambiguous_or_stopped_session_creates_nothing(monkeypatch, paths, selection, records):
    monkeypatch.setattr(workflow, "_run", Mock(return_value=reply({"sessions": records})))
    probe = commands(monkeypatch)
    with pytest.raises(workflow.HandoffError, match="Start the selected Herdr session first"):
        herdr.prepare_workflow(paths[0], selection)
    workflow.prepare.assert_not_called()
    probe.assert_not_called()
    assert not herdr.STATE_ROOT.exists()


@pytest.mark.parametrize("payload", [
    None, [], {}, {"sessions": None}, {"sessions": {}}, {"sessions": "sessions"},
    {"sessions": 7}, {"sessions": [None]}, {"sessions": ["session"]}, {"sessions": [7]},
    {"sessions": [session(), None]}, {"sessions": [session(socket=None)]},
])
def test_prepare_unreadable_session_selection_fails_closed(monkeypatch, paths, payload):
    monkeypatch.setattr(workflow, "_run", Mock(return_value=reply(payload)))
    probe = commands(monkeypatch)
    with pytest.raises(workflow.HandoffError):
        herdr.prepare_workflow(paths[0])
    workflow.prepare.assert_not_called()
    probe.assert_not_called()


@pytest.mark.parametrize("selection", [None, "named"])
@pytest.mark.parametrize("socket", [None, "", False, 7, [], {}, ["/tmp/session.sock"]])
def test_prepare_rejects_malformed_selected_socket_before_runtime_probe(monkeypatch, paths, selection, socket):
    records = [session("named", socket=socket)]
    monkeypatch.setattr(workflow, "_run", Mock(return_value=reply({"sessions": records})))
    probe = commands(monkeypatch)
    with pytest.raises(workflow.HandoffError, match="Start the selected Herdr session first"):
        herdr.prepare_workflow(paths[0], selection)
    workflow.prepare.assert_not_called()
    probe.assert_not_called()
    assert not herdr.STATE_ROOT.exists()


@pytest.mark.parametrize("caller", [False, True])
def test_prepare_checks_selected_runtime_is_running(monkeypatch, paths, caller):
    if caller:
        monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/caller.sock")
    monkeypatch.setattr(workflow, "_run", Mock(return_value=reply({"sessions": [session()]})))
    probe = commands(monkeypatch, workflow.HandoffError("selected socket unavailable"))
    with pytest.raises(workflow.HandoffError, match="selected socket unavailable"):
        herdr.prepare_workflow(paths[0])
    probe.assert_called_once_with(["workspace", "list"], cwd=paths[0])
    workflow.prepare.assert_not_called()


@pytest.mark.parametrize("failure", [OSError("session list unavailable"), subprocess.TimeoutExpired("herdr", 10)])
def test_session_discovery_transport_failure_prevents_setup(monkeypatch, paths, failure):
    monkeypatch.setattr(workflow, "_run", Mock(side_effect=failure))
    probe = commands(monkeypatch)
    with pytest.raises(workflow.HandoffError, match="Start the selected Herdr session first"):
        herdr.prepare_workflow(paths[0])
    workflow.prepare.assert_not_called()
    probe.assert_not_called()


@pytest.mark.parametrize("blocked", ["agent", "non-tty", "missing-binary"])
def test_prepare_rejects_invalid_invocations_without_commands(monkeypatch, paths, blocked):
    if blocked == "agent":
        monkeypatch.setenv("WORKTRUNK_AGENT", "1")
    elif blocked == "non-tty":
        monkeypatch.setattr(workflow.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    else:
        monkeypatch.setattr(herdr.shutil, "which", lambda _: None)
    with pytest.raises(workflow.HandoffError):
        herdr.prepare_workflow(paths[0])
    workflow._run.assert_not_called()
    workflow.prepare.assert_not_called()


def test_handoff_opens_existing_checkout_without_focus_then_launches_verified_pi(monkeypatch, paths, target):
    root, checkout = paths
    agent = ready(checkout)
    responses = iter([opened(root, checkout), {"agents": []}, process_info(), {"agent": agent},
                      {"agent": {**agent, "focused": True}}])

    def respond(args, **_kwargs):
        if args[:2] == ["agent", "start"]:
            assert json.loads(record_path(target, checkout).read_text()) == {
                "path": str(checkout), "socket": target.socket, "pane": "pane-1", "terminal": "terminal-1",
            }
        return next(responses)

    mocked = Mock(side_effect=respond)
    monkeypatch.setattr(herdr.Target, "call", mocked)
    assert herdr.handoff(target, root, checkout) == "Started Pi and opened its worktree in Herdr."
    name = "cop-pi-" + record_path(target, checkout).stem[:12]
    assert mocked.call_args_list == [
        open_call(root, checkout), call(["agent", "list"], cwd=checkout),
        call(["pane", "process-info", "--pane", "pane-1"], cwd=checkout),
        call(["agent", "start", name, "--kind", "pi", "--pane", "pane-1", "--timeout", "30000"],
             cwd=checkout, timeout=35),
        call(["agent", "focus", "pane-1"], cwd=checkout),
    ]
    assert not record_path(target, checkout).exists()
    workflow._run.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("workspace", None), ("root_pane", None), ("worktree", None),
    ("checkout_path", "/different/checkout"), ("repo_root", "/different/repo"),
    ("checkout_path", None), ("checkout_path", ""), ("checkout_path", 7),
    ("checkout_path", []), ("checkout_path", {}), ("checkout_path", ["/tmp/checkout"]),
    ("repo_root", None), ("repo_root", ""), ("repo_root", 7),
    ("repo_root", []), ("repo_root", {}), ("repo_root", ["/tmp/repo"]),
    ("workspace_id", None), ("workspace_id", 7), ("pane_workspace", "different-workspace"),
])
def test_handoff_rejects_wrong_path_provenance_or_root_identity(monkeypatch, paths, target, field, value):
    root, checkout = paths
    payload = opened(root, checkout)
    if field in {"workspace", "root_pane"}:
        payload[field] = value
    elif field in {"worktree", "workspace_id"}:
        payload["workspace"][field] = value
    elif field == "pane_workspace":
        payload["root_pane"]["workspace_id"] = value
    else:
        payload["workspace"]["worktree"][field] = value
    mocked = commands(monkeypatch, payload)
    with pytest.raises(workflow.HandoffError):
        herdr.handoff(target, root, checkout)
    assert mocked.call_args_list == [open_call(root, checkout)]
    assert not record_path(target, checkout).exists()


@pytest.mark.parametrize("agents", [None, {}, [None], ["pi"]])
def test_unreadable_agent_list_never_launches(monkeypatch, paths, target, agents):
    root, checkout = paths
    mocked = commands(monkeypatch, opened(root, checkout), {"agents": agents})
    with pytest.raises(workflow.HandoffError, match="Cannot inspect Herdr agents"):
        herdr.handoff(target, root, checkout)
    assert mocked.call_count == 2
    assert not record_path(target, checkout).exists()


def test_exact_ready_agent_reuse_focuses_once_and_does_not_start(monkeypatch, paths, target):
    root, checkout = paths
    agent = ready(checkout)
    mocked = commands(monkeypatch, opened(root, checkout), {"agents": [agent]},
                      {"agent": {**agent, "focused": True}})
    assert herdr.handoff(target, root, checkout) == "Reused Pi and opened its worktree in Herdr."
    assert mocked.call_args_list == [open_call(root, checkout), call(["agent", "list"], cwd=checkout),
                                     call(["agent", "focus", "pane-1"], cwd=checkout)]
    assert not record_path(target, checkout).exists()


@pytest.mark.parametrize("changes", [
    {"agent_session": None}, {"agent_session": {}},
    {"agent_session": {"source": "other:pi", "agent": "pi", "value": "session"}},
    {"agent_session": {"source": "herdr:pi", "agent": "claude", "value": "session"}},
    {"agent_session": {"source": "herdr:pi", "agent": "pi", "value": ""}},
    {"interactive_ready": False}, {"launch_pending": True}, {"cwd": "/different"},
    {"foreground_cwd": "/different"}, {"pane_id": None}, {"terminal_id": None}, {"agent": "claude"},
])
def test_missing_session_or_readiness_proof_blocks_duplicate(monkeypatch, paths, target, changes):
    root, checkout = paths
    mocked = commands(monkeypatch, opened(root, checkout), {"agents": [ready(checkout, **changes)]})
    with pytest.raises(workflow.HandoffError, match="uncertain or busy agent"):
        herdr.handoff(target, root, checkout)
    assert mocked.call_count == 2
    assert not record_path(target, checkout).exists()


@pytest.mark.parametrize("field", ["cwd", "foreground_cwd"])
@pytest.mark.parametrize("value", [None, "", False, 7, [], {}, ["/tmp/checkout"]])
@pytest.mark.parametrize("stage", ["reuse", "start", "focus"])
def test_malformed_agent_cwd_fails_closed_at_every_verification_stage(monkeypatch, paths, target, field, value, stage):
    root, checkout = paths
    agent = ready(checkout)
    malformed = ready(checkout, **{field: value})
    if stage == "reuse":
        values = [opened(root, checkout), {"agents": [malformed]}]
        message = "uncertain or busy agent"
    elif stage == "start":
        values = [opened(root, checkout), {"agents": []}, process_info(), {"agent": malformed}]
        message = "did not confirm Pi readiness"
    else:
        pending(target, checkout)
        values = [opened(root, checkout), {"agents": [agent]}, {"agent": {**malformed, "focused": True}}]
        message = "did not confirm focus"
    mocked = commands(monkeypatch, *values)
    with pytest.raises(workflow.HandoffError, match=message):
        herdr.handoff(target, root, checkout)
    assert mocked.call_count == len(values)
    assert record_path(target, checkout).exists() is (stage != "reuse")


def test_lost_start_response_preserves_launch_record_and_prevents_fresh_start(monkeypatch, paths, target):
    root, checkout = paths
    mocked = commands(monkeypatch, opened(root, checkout), {"agents": []}, process_info(),
                      workflow.HandoffError("response deadline exceeded"))
    with pytest.raises(workflow.HandoffError, match="deadline exceeded"):
        herdr.handoff(target, root, checkout)
    before = record_path(target, checkout).read_text()
    assert json.loads(before)["terminal"] == "terminal-1"
    assert mocked.call_args.kwargs["timeout"] == 35
    retry = commands(monkeypatch, opened(root, checkout), {"agents": []})
    with pytest.raises(workflow.HandoffError, match="uncertain or busy agent"):
        herdr.handoff(target, root, checkout)
    assert retry.call_count == 2
    assert record_path(target, checkout).read_text() == before


def test_pending_launch_recovers_only_its_matching_ready_agent(monkeypatch, paths, target):
    root, checkout = paths
    pending(target, checkout)
    agent = ready(checkout)
    mocked = commands(monkeypatch, opened(root, checkout), {"agents": [agent]},
                      {"agent": {**agent, "focused": True}})
    assert "Reused Pi" in herdr.handoff(target, root, checkout)
    assert mocked.call_args_list == [open_call(root, checkout), call(["agent", "list"], cwd=checkout),
                                     call(["agent", "focus", "pane-1"], cwd=checkout)]
    assert not record_path(target, checkout).exists()


@pytest.mark.parametrize("field,value", [
    ("path", "/other/checkout"), ("socket", "/tmp/other.sock"),
    ("pane", "other-pane"), ("terminal", "other-terminal"),
])
def test_pending_launch_rejects_unrelated_ready_agent(monkeypatch, paths, target, field, value):
    root, checkout = paths
    state = pending(target, checkout, **{field: value})
    mocked = commands(monkeypatch, opened(root, checkout), {"agents": [ready(checkout)]})
    with pytest.raises(workflow.HandoffError, match="Cannot verify the earlier Herdr launch"):
        herdr.handoff(target, root, checkout)
    assert mocked.call_count == 2
    assert json.loads(record_path(target, checkout).read_text()) == state


@pytest.mark.parametrize("payload", ["not json", "null", "[]"])
def test_unreadable_pending_record_blocks_recovery(monkeypatch, paths, target, payload):
    root, checkout = paths
    pending(target, checkout)
    record_path(target, checkout).write_text(payload)
    mocked = commands(monkeypatch, opened(root, checkout), {"agents": [ready(checkout)]})
    with pytest.raises(workflow.HandoffError, match="Cannot verify the earlier Herdr launch"):
        herdr.handoff(target, root, checkout)
    assert mocked.call_count == 2
    assert record_path(target, checkout).read_text() == payload


@pytest.mark.parametrize("changes", [{"agent": "pi"}, {"pane_id": None}, {"terminal_id": None}])
def test_root_pane_must_be_unused_before_process_inspection(monkeypatch, paths, target, changes):
    root, checkout = paths
    payload = opened(root, checkout)
    payload["root_pane"].update(changes)
    mocked = commands(monkeypatch, payload, {"agents": []})
    with pytest.raises(workflow.HandoffError, match="not an unused shell"):
        herdr.handoff(target, root, checkout)
    assert mocked.call_count == 2
    assert not record_path(target, checkout).exists()


@pytest.mark.parametrize("process", [
    {}, {"process_info": None}, process_info(pane_id="other-pane"),
    process_info(shell_pid=None), process_info(foreground_processes=None),
    process_info(foreground_processes=[]), process_info(foreground_processes=[None]),
    process_info(foreground_processes=[{"pid": 999}]),
    process_info(foreground_processes=[{"pid": 401}, {"pid": 999}]),
])
def test_busy_or_unproved_shell_never_records_or_launches(monkeypatch, paths, target, process):
    root, checkout = paths
    mocked = commands(monkeypatch, opened(root, checkout), {"agents": []}, process)
    with pytest.raises(workflow.HandoffError):
        herdr.handoff(target, root, checkout)
    assert mocked.call_count == 3
    assert not record_path(target, checkout).exists()


@pytest.mark.parametrize("changes", [{"interactive_ready": False}, {"pane_id": "other-pane"},
                                     {"terminal_id": "other-terminal"}, {"agent_session": None}])
def test_unproved_start_preserves_pending_state(monkeypatch, paths, target, changes):
    root, checkout = paths
    mocked = commands(monkeypatch, opened(root, checkout), {"agents": []}, process_info(),
                      {"agent": ready(checkout, **changes)})
    with pytest.raises(workflow.HandoffError, match="did not confirm Pi readiness"):
        herdr.handoff(target, root, checkout)
    assert mocked.call_count == 4
    assert record_path(target, checkout).exists()


@pytest.mark.parametrize("changes", [
    {"focused": False}, {"interactive_ready": False}, {"terminal_id": "other-terminal"},
    {"pane_id": "other-pane"}, {"workspace_id": "other-workspace"}, {"agent_session": None},
])
@pytest.mark.parametrize("reuse", [False, True])
def test_final_focus_failure_preserves_pending_state(monkeypatch, paths, target, changes, reuse):
    root, checkout = paths
    agent = ready(checkout)
    if reuse:
        pending(target, checkout)
        values = [opened(root, checkout), {"agents": [agent]}]
    else:
        values = [opened(root, checkout), {"agents": []}, process_info(), {"agent": agent}]
    mocked = commands(monkeypatch, *values, {"agent": ready(checkout, focused=True) | changes})
    with pytest.raises(workflow.HandoffError, match="did not confirm focus"):
        herdr.handoff(target, root, checkout)
    assert mocked.call_count == (3 if reuse else 5)
    assert record_path(target, checkout).exists()
