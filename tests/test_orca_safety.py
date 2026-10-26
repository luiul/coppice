"""Safety regressions with mocks and bounded, locally owned subprocesses."""

import contextlib
import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from unittest.mock import Mock

import pytest

from coppice import orca, workflow

_RUN = workflow._run
_FOCUS_PI = orca._focus_pi


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(orca, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(workflow, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(workflow, "_run", Mock(side_effect=AssertionError("Unexpected setup subprocess")))
    monkeypatch.setattr(orca, "_orca", Mock(side_effect=AssertionError("Unexpected Orca command")))
    monkeypatch.setattr(orca, "_focus_pi", Mock())
    monkeypatch.setattr(orca, "_run", Mock(side_effect=AssertionError("Unexpected subprocess")))
    monkeypatch.setattr(workflow, "_branch_has_checkout", lambda *_args: False)
    monkeypatch.setattr(orca, "manual_invocation", lambda: True)
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "/fake/orca")


@pytest.fixture
def checkout(tmp_path):
    path = tmp_path / "checkout with spaces"
    path.mkdir()
    return path.resolve()


def _live_terminal(path):
    return {
        "terminals": [
            {
                "worktreePath": str(path),
                "agentIdentity": "pi",
                "connected": True,
                "writable": True,
                "orphaned": False,
                "incarnationId": "incarnation-1",
                "ptyId": "pty-1",
                "handle": "terminal-1",
            }
        ],
    }


def _seed_binding(path):
    orca.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    orca._remember_pi(path, _live_terminal(path)["terminals"][0])


def _launch_record(path):
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()
    return orca.STATE_ROOT / f"{digest}.json"


def _lock_worker(state_root, checkout, action, entered, release, completed, results):
    """Spawned workers use real flock, but can never reach Orca or wt."""
    orca.STATE_ROOT = Path(state_root)
    workflow.STATE_ROOT = Path(state_root)
    orca.DISCOVERY_TIMEOUT = 8
    workflow.DISCOVERY_TIMEOUT = 8
    path = Path(checkout)
    orca._run = Mock(side_effect=AssertionError("Unexpected subprocess"))
    workflow._run = Mock(side_effect=AssertionError("Unexpected setup subprocess"))

    def focus(handle, target):
        assert (handle, target) == ("terminal-1", path)
        assert release.is_set(), "Focus overlapped setup"
        assert not orca._setup_path(path).exists(), "Focus preceded readiness"

    orca._focus_pi = Mock(side_effect=focus)

    def finish(_self, target, _branch, _base):
        assert target == path
        if action == "hold_setup":
            entered.set()
            assert release.wait(8), "Setup release deadline exceeded"
        else:
            assert release.is_set(), "Setup overlapped another setup"

    def terminal_list(args, **_kwargs):
        assert args[:2] == ["terminal", "list"], "Unexpected launch"
        assert not orca._setup_path(path).exists(), "Launch preceded readiness"
        assert release.is_set(), "Launch overlapped setup"
        return _live_terminal(path)

    orca.Setup.finish = finish
    orca._orca = terminal_list
    setup = orca.Setup(path.parent, (), ())
    try:
        if action == "hold_setup":
            orca.finish_setup(setup, path, "branch", "main")
        else:
            entered.set()
            if action == "handoff":
                assert orca.handoff(path) == "Reused Pi and opened its worktree in Orca."
                orca._focus_pi.assert_called_once_with("terminal-1", path)
            elif action == "resume_setup":
                orca.resume_setup(setup, path)
            else:
                orca.finish_setup(setup, path, "branch", "main")
        results.put(None)
    except BaseException:
        results.put(traceback.format_exc())
    finally:
        completed.set()


@pytest.mark.parametrize("action", ["handoff", "resume_setup", "finish_setup"])
def test_setup_serializes_same_checkout_across_real_processes(checkout, action):
    _seed_binding(checkout)
    ctx = multiprocessing.get_context("spawn")
    release = ctx.Event()
    first_entered, second_entered = ctx.Event(), ctx.Event()
    first_done, second_done = ctx.Event(), ctx.Event()
    results = ctx.Queue()
    common = (str(orca.STATE_ROOT), str(checkout))
    first = ctx.Process(target=_lock_worker, args=(*common, "hold_setup", first_entered, release, first_done, results))
    second = ctx.Process(target=_lock_worker, args=(*common, action, second_entered, release, second_done, results))
    processes = []
    try:
        first.start()
        processes.append(first)
        assert first_entered.wait(8), "First setup did not enter its hook"
        assert orca._setup_path(checkout).exists()
        second.start()
        processes.append(second)
        assert second_entered.wait(8), "Second worker did not attempt its operation"
        assert not second_done.wait(0.5), "Same-checkout operation bypassed the setup lock"
        release.set()
        for process in processes:
            process.join(10)
            assert not process.is_alive(), "Worker exceeded its deadline"
            assert process.exitcode == 0
        assert first_done.is_set() and second_done.is_set()
        assert [results.get(timeout=2) for _ in processes] == [None, None]
        assert not orca._setup_path(checkout).exists()
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            assert not process.is_alive(), "Owned worker survived cleanup"
            process.close()
        results.close()
        results.join_thread()


def test_interrupted_creation_intent_finishes_setup_on_reused_path(monkeypatch, checkout):
    setup = orca.Setup(checkout.parent, (), ())

    def interrupted_switch():
        intents = list(orca.STATE_ROOT.glob("*.intent.json"))
        assert len(intents) == 1
        assert json.loads(intents[0].read_text()) == {
            "root": str(setup.root),
            "branch": "feature/test",
            "base": "main",
        }
        assert not orca._setup_path(checkout).exists()
        raise KeyboardInterrupt("Creation succeeded, caller interrupted before setup")

    switch = Mock(side_effect=interrupted_switch)
    with pytest.raises(KeyboardInterrupt):
        orca.switch_ready(setup, "feature/test", "main", switch)
    switch.assert_called_once_with()
    assert len(list(orca.STATE_ROOT.glob("*.intent.json"))) == 1

    def finish(_self, target, branch, base):
        assert (target, branch, base) == (checkout, "feature/test", "main")
        assert orca._setup_path(target).exists()
        assert len(list(orca.STATE_ROOT.glob("*.intent.json"))) == 1

    finish_mock = Mock(side_effect=finish)
    monkeypatch.setattr(orca.Setup, "finish", lambda self, *args: finish_mock(self, *args))
    reused = {"action": "existing", "path": str(checkout), "base_branch": "main"}
    switch = Mock(return_value=reused)
    assert orca.switch_ready(setup, "feature/test", "main", switch) == reused
    switch.assert_called_once_with()
    finish_mock.assert_called_once_with(setup, checkout, "feature/test", "main")
    assert not list(orca.STATE_ROOT.glob("*.intent.json"))
    assert not orca._setup_path(checkout).exists()
    _seed_binding(checkout)
    query = Mock(return_value=_live_terminal(checkout))
    monkeypatch.setattr(orca, "_orca", query)
    assert orca.handoff(checkout).startswith("Reused")
    query.assert_called_once()


def test_failed_setup_retry_keeps_intent_until_success(monkeypatch, checkout):
    setup = orca.Setup(checkout.parent, (), ())
    finish = Mock(side_effect=[orca.HandoffError("Hook failed"), None])
    monkeypatch.setattr(orca.Setup, "finish", finish)
    first = orca.switch_ready(
        setup,
        "feature/test",
        "main",
        Mock(return_value={"action": "created", "path": str(checkout)}),
    )
    assert first["setup_error"] == "Hook failed"
    assert orca._setup_path(checkout).exists()
    assert len(list(orca.STATE_ROOT.glob("*.intent.json"))) == 1
    with pytest.raises(orca.HandoffError, match="setup is still pending"):
        orca.handoff(checkout)
    orca._orca.assert_not_called()
    second = orca.switch_ready(
        setup,
        "feature/test",
        "main",
        Mock(return_value={"action": "existing", "path": str(checkout)}),
    )
    assert "setup_error" not in second
    assert finish.call_count == 2
    assert not orca._setup_path(checkout).exists()
    assert not list(orca.STATE_ROOT.glob("*.intent.json"))


def test_existing_tracked_path_without_intent_skips_setup_and_reuses_pi(monkeypatch, checkout):
    setup = orca.Setup(checkout.parent, (), ())
    finish = Mock(side_effect=AssertionError("Existing setup must not be repeated"))
    monkeypatch.setattr(orca.Setup, "finish", finish)
    existing = {"action": "existing", "path": str(checkout)}
    assert orca.switch_ready(setup, "feature/test", None, Mock(return_value=existing)) == existing
    finish.assert_not_called()
    assert not orca._setup_path(checkout).exists()
    assert not list(orca.STATE_ROOT.glob("*.intent.json"))
    _seed_binding(checkout)
    query = Mock(return_value=_live_terminal(checkout))
    monkeypatch.setattr(orca, "_orca", query)
    assert orca.handoff(checkout).startswith("Reused")
    query.assert_called_once()
    assert query.call_args.args[0][:2] == ["terminal", "list"]


@pytest.mark.parametrize("source", ["project", "system", "user"])
@pytest.mark.parametrize("value", ["wrong type", ["wrong type"], 1, True])
@pytest.mark.parametrize("nested", [False, True])
def test_malformed_nested_config_raises_handoff_error(monkeypatch, checkout, source, value, nested):
    config = {"user": {"config": {}}}
    config[source] = {"config": value} if nested else value
    monkeypatch.setattr(orca, "_orca", Mock(return_value={"repo": {"externalWorktreeVisibility": "show"}}))
    monkeypatch.setattr(
        workflow,
        "_run",
        Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(config), "")),
    )
    with pytest.raises(orca.HandoffError, match="could not read wt setup configuration"):
        orca.prepare(checkout)


@pytest.mark.parametrize("field", ["launch", "outcome"])
@pytest.mark.parametrize("value", ["wrong type", ["wrong type"], 1, True])
def test_malformed_nested_launch_raises_handoff_error_and_preserves_pending(monkeypatch, checkout, field, value):
    query = Mock(side_effect=[{"terminals": []}, {"operationId": "operation-1", field: value}])
    monkeypatch.setattr(orca, "_orca", query)
    with pytest.raises(orca.HandoffError, match=r"pending.*preserved"):
        orca.handoff(checkout)
    assert query.call_count == 2
    records = list(orca.STATE_ROOT.glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text()) == {
        "path": str(checkout),
        "operationId": "operation-1",
        "pending": True,
    }


@pytest.mark.parametrize("operation_id", [None, "earlier-operation"])
@pytest.mark.parametrize("code", ["selector_not_found", "transport_unknown", "command_failed"])
def test_reused_pi_focus_failure_preserves_state_and_never_launches(monkeypatch, checkout, operation_id, code):
    digest = hashlib.sha256(os.fsencode(checkout)).hexdigest()
    record = orca.STATE_ROOT / f"{digest}.json"
    orca.STATE_ROOT.mkdir()
    state = {"path": str(checkout), "pending": True}
    if operation_id:
        state["operationId"] = operation_id
    record.write_text(json.dumps(state))
    _seed_binding(checkout)
    before = record.read_text()
    query = Mock(return_value=_live_terminal(checkout))
    monkeypatch.setattr(orca, "_orca", query)
    error = orca.OrcaError(code, "Focus failed")
    orca._focus_pi.side_effect = error

    for _ in range(2):
        with pytest.raises(orca.OrcaError) as caught:
            orca.handoff(checkout)
        assert caught.value is error
        assert record.read_text() == before
    assert query.call_count == 2
    assert orca._focus_pi.call_count == 2
    for call in query.call_args_list:
        assert call.args == (["terminal", "list", "--worktree", f"path:{checkout}"],)
    for call in orca._focus_pi.call_args_list:
        assert call.args == ("terminal-1", checkout)


@pytest.mark.parametrize("operation_id", [None, "operation-1"])
def test_new_pi_focus_failure_preserves_state_and_reuses_on_retry(monkeypatch, checkout, operation_id):
    result = {"outcome": {"kind": "terminal", "handle": "terminal-1"}}
    if operation_id:
        result["operationId"] = operation_id
    query = Mock(side_effect=[{"terminals": []}, result, _live_terminal(checkout), _live_terminal(checkout)])
    monkeypatch.setattr(orca, "_orca", query)
    error = orca.OrcaError("selector_not_found", "Focus failed")
    orca._focus_pi.side_effect = [error, None]

    with pytest.raises(orca.OrcaError) as caught:
        orca.handoff(checkout)
    assert caught.value is error
    record = _launch_record(checkout)
    expected = {"path": str(checkout), "pending": True, "operationId": operation_id, "handle": "terminal-1"}
    assert json.loads(record.read_text()) == expected
    assert json.loads(orca._binding_path(checkout).read_text()) == {
        "path": str(checkout), "handle": "terminal-1", "incarnationId": "incarnation-1", "ptyId": "pty-1",
    }
    assert query.call_count == 3
    orca._focus_pi.assert_called_once_with("terminal-1", checkout)

    assert orca.handoff(checkout) == "Reused Pi and opened its worktree in Orca."
    assert not record.exists()
    assert orca._binding_path(checkout).exists()
    assert query.call_count == 4
    assert [call.args[0][:2] for call in query.call_args_list] == [
        ["terminal", "list"], ["agent", "launch"], ["terminal", "list"], ["terminal", "list"],
    ]
    assert orca._focus_pi.call_count == 2


@pytest.mark.parametrize("operation_id", [None, "operation-1"])
def test_focus_failure_without_live_pi_blocks_fresh_launch_retry(monkeypatch, checkout, operation_id):
    launch = ["agent", "launch", "--agent", "pi", "--worktree", f"path:{checkout}", "--surface", "terminal"]
    result = {"outcome": {"kind": "terminal", "handle": "terminal-1"}}
    if operation_id:
        result["operationId"] = operation_id
    values = [{"terminals": []}, result, _live_terminal(checkout), {"terminals": []}]
    if operation_id:
        values.append(orca.OrcaError("transport_unknown", "Retry uncertain"))
    query = Mock(side_effect=values)
    monkeypatch.setattr(orca, "_orca", query)
    error = orca.OrcaError("transport_unknown", "Focus failed")
    orca._focus_pi.side_effect = error

    with pytest.raises(orca.OrcaError, match="Focus failed"):
        orca.handoff(checkout)
    record = _launch_record(checkout)
    before = json.loads(record.read_text())
    expected = {"path": str(checkout), "pending": True, "operationId": operation_id, "handle": "terminal-1"}
    assert before == expected
    assert orca._binding_path(checkout).exists()

    with pytest.raises(orca.HandoffError, match="Retry uncertain" if operation_id else "earlier launch result is unknown"):
        orca.handoff(checkout)
    assert json.loads(record.read_text()) == before
    assert query.call_args_list[1].args == (launch,)
    if operation_id:
        assert query.call_count == 5
        assert query.call_args_list[4].args == ([*launch, "--retry-launch", operation_id],)
    else:
        assert query.call_count == 4
    orca._focus_pi.assert_called_once_with("terminal-1", checkout)


@pytest.mark.parametrize("operation_id", [None, "operation-1"])
def test_interrupted_postlaunch_verification_recovers_only_accepted_handle(monkeypatch, checkout, operation_id):
    result = {"outcome": {"kind": "terminal", "handle": "terminal-1"}}
    if operation_id:
        result["operationId"] = operation_id
    query = Mock(side_effect=[
        {"terminals": []}, result, orca.OrcaError("transport_unknown", "Verification interrupted"),
        _live_terminal(checkout),
    ])
    monkeypatch.setattr(orca, "_orca", query)
    with pytest.raises(orca.OrcaError, match="Verification interrupted"):
        orca.handoff(checkout)
    assert not orca._binding_path(checkout).exists()
    assert json.loads(_launch_record(checkout).read_text()) == {
        "path": str(checkout), "operationId": operation_id, "handle": "terminal-1", "pending": True,
    }
    assert orca.handoff(checkout).startswith("Reused")
    orca._focus_pi.assert_called_once_with("terminal-1", checkout)
    assert orca._binding_path(checkout).exists()
    assert not _launch_record(checkout).exists()
    assert [call.args[0][:2] for call in query.call_args_list] == [
        ["terminal", "list"], ["agent", "launch"], ["terminal", "list"], ["terminal", "list"],
    ]


@pytest.mark.parametrize("pending", [False, True])
def test_unconfirmed_focus_preserves_binding_and_retries_only_focus(monkeypatch, checkout, pending):
    _seed_binding(checkout)
    binding_before = orca._binding_path(checkout).read_text()
    record = _launch_record(checkout)
    if pending:
        record.write_text(json.dumps({"path": str(checkout), "handle": "terminal-1", "pending": True}))
    record_before = record.read_text() if pending else None
    success = {"focus": {
        "handle": "terminal-1", "navigated": True, "tabId": "tab-1", "worktreeId": f"repo-1::{checkout}",
    }}
    query = Mock(side_effect=[
        _live_terminal(checkout), {"focus": {**success["focus"], "navigated": False}},
        _live_terminal(checkout), success,
    ])
    monkeypatch.setattr(orca, "_orca", query)
    monkeypatch.setattr(orca, "_focus_pi", _FOCUS_PI)
    with pytest.raises(orca.HandoffError, match="did not confirm opening"):
        orca.handoff(checkout)
    assert orca._binding_path(checkout).read_text() == binding_before
    assert record.exists() is pending
    if pending:
        assert record.read_text() == record_before
    assert orca.handoff(checkout).startswith("Reused")
    assert not record.exists()
    assert orca._binding_path(checkout).read_text() == binding_before
    assert [call.args[0][:2] for call in query.call_args_list] == [
        ["terminal", "list"], ["terminal", "switch"], ["terminal", "list"], ["terminal", "switch"],
    ]


def test_shared_run_passes_explicit_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("COPPICE_WORKFLOW_PARENT_ONLY", "must not leak")
    code = (
        "import json, os; "
        "print(json.dumps([os.getenv('COPPICE_WORKFLOW_VALUE'), os.getenv('COPPICE_WORKFLOW_PARENT_ONLY')]))"
    )
    result = _RUN(
        [sys.executable, "-c", code], cwd=tmp_path, timeout=10,
        env={"COPPICE_WORKFLOW_VALUE": "explicit environment"},
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == ["explicit environment", None]
    assert result.stderr == ""


def test_shared_run_inherits_environment_when_omitted(monkeypatch, tmp_path):
    monkeypatch.setenv("COPPICE_WORKFLOW_VALUE", "inherited environment")
    result = _RUN(
        [sys.executable, "-c", "import os; print(os.getenv('COPPICE_WORKFLOW_VALUE'))"],
        cwd=tmp_path, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "inherited environment"
    assert result.stderr == ""


def test_run_timeout_bounds_drain_of_escaped_owned_pipe_holder(tmp_path):
    """The escaped helper has a hard deadline and is explicitly killed in finally."""
    pid_file = tmp_path / "escaped-pid"
    root_file = tmp_path / "root-pid"
    root_code = (
        "import os, pathlib, subprocess, sys, time\n"
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(12)'], "
        "start_new_session=True)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(12)\n"
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _RUN([sys.executable, "-c", root_code, str(pid_file), str(root_file)], cwd=tmp_path, timeout=2)
        elapsed = time.monotonic() - started
        assert pid_file.exists(), "Escaped pipe holder never started"
        assert elapsed < 6, f"Timeout drain took {elapsed:.2f} seconds"
        root_pid = int(root_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(root_pid, 0)
        # Its own session survived the original group kill and still holds pipes.
        child_pid = int(pid_file.read_text())
        assert os.getpgid(child_pid) == child_pid
    finally:
        # Both groups belong only to this test. Also clean up if an assertion fails.
        for marker in (pid_file, root_file):
            if marker.exists():
                pid = int(marker.read_text())
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail(f"Owned subprocess {pid} survived cleanup")
