"""Herdr delegation is gated, bounded, and passes exact wt paths."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from coppice import herdr, workflow


@pytest.fixture
def helper(monkeypatch, tmp_path):
    root = tmp_path / "repo with spaces"
    root.mkdir()
    checkout = tmp_path / "wt selected checkout"
    checkout.mkdir()
    monkeypatch.setattr(workflow, "manual_invocation", Mock(return_value=True))
    monkeypatch.setattr(herdr.shutil, "which", Mock(return_value="/fake/wt-herdr"))
    monkeypatch.setattr(workflow, "prepare", Mock(return_value=workflow.Setup(root, (), ())))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '{"message": "Ready"}', ""))
    monkeypatch.setattr(workflow, "_run", run)
    return root, checkout, run


def test_missing_helper_stops_before_setup(monkeypatch, helper):
    root, _, run = helper
    herdr.shutil.which.return_value = None

    with pytest.raises(workflow.HandoffError, match="wt-herdr is not installed"):
        herdr.prepare_workflow(root)

    herdr.shutil.which.assert_called_once_with("wt-herdr")
    workflow.prepare.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("agent,piped", [(True, False), (False, True), (True, True)])
def test_manual_gate_precedes_helper_lookup(monkeypatch, helper, agent, piped):
    root, _, run = helper
    monkeypatch.setattr(workflow, "manual_invocation", _MANUAL)
    monkeypatch.setattr(workflow.sys.stdin, "isatty", lambda: not piped)
    if agent:
        monkeypatch.setenv("WORKTRUNK_AGENT", "1")
    else:
        monkeypatch.delenv("WORKTRUNK_AGENT", raising=False)

    with pytest.raises(workflow.HandoffError, match="interactive, non-agent shell"):
        herdr.prepare_workflow(root)

    herdr.shutil.which.assert_not_called()
    workflow.prepare.assert_not_called()
    run.assert_not_called()


_MANUAL = workflow.manual_invocation


def test_check_precedes_setup_and_uses_exact_repo(helper):
    root, _, run = helper
    events = []
    run.side_effect = lambda *args, **kwargs: (
        events.append("check") or subprocess.CompletedProcess([], 0, '{"message": "Ready"}', "")
    )
    workflow.prepare.side_effect = lambda path: events.append("setup") or workflow.Setup(path, (), ())

    setup, target = herdr.prepare_workflow(root)

    assert events == ["check", "setup"]
    assert setup.root == root
    assert target == herdr.Target("/fake/wt-herdr")
    run.assert_called_once_with(["/fake/wt-herdr", "check", "--repo", str(root)], cwd=root, timeout=10)
    workflow.prepare.assert_called_once_with(root)


def test_handoff_uses_exact_paths_and_returns_message(helper):
    root, checkout, run = helper
    message = "Reused Pi and focused its terminal."
    run.return_value.stdout = json.dumps({"message": message})

    assert herdr.handoff(herdr.Target("/fake/wt-herdr"), root, checkout) == message

    run.assert_called_once_with(
        ["/fake/wt-herdr", "open", "--repo", str(root), "--path", str(checkout)], cwd=checkout, timeout=90
    )
    workflow.prepare.assert_not_called()


@pytest.mark.parametrize("operation", ["check", "open"])
@pytest.mark.parametrize(
    "failure,expected",
    [
        (OSError("private internal detail"), "wt-herdr command did not complete."),
        (subprocess.TimeoutExpired("private command", 90), "wt-herdr command did not complete."),
        (subprocess.CompletedProcess([], 1, "not JSON", "Safe helper failure.\n"), "Safe helper failure."),
        (subprocess.CompletedProcess([], 1, '{"message": "Do not trust stdout"}', ""), "wt-herdr command failed."),
        *[
            (subprocess.CompletedProcess([], 0, output, ""), "wt-herdr returned an unreadable result.")
            for output in ("not JSON", "null", "[]", "{}", '{"message": null}', '{"message": 1}', '{"message": []}')
        ],
    ],
)
def test_failed_or_malformed_call_stops_without_setup(helper, operation, failure, expected):
    root, checkout, run = helper
    if isinstance(failure, Exception):
        run.side_effect = failure
    else:
        run.return_value = failure

    with pytest.raises(workflow.HandoffError) as caught:
        if operation == "check":
            herdr.prepare_workflow(root)
        else:
            herdr.handoff(herdr.Target("/fake/wt-herdr"), root, checkout)

    assert str(caught.value) == expected
    assert run.call_count == 1
    assert run.call_args.kwargs["timeout"] == (10 if operation == "check" else 90)
    workflow.prepare.assert_not_called()
