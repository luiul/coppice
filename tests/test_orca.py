"""Orca handoff contracts, with isolated state and no live runtime calls."""

import hashlib
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coppice import orca

_ORCA = orca._orca
_FOCUS_PI = orca._focus_pi


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Unexpected commands fail rather than reaching a real Orca or wt."""
    monkeypatch.setattr(orca, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(orca, "_orca", Mock(side_effect=AssertionError("Unexpected Orca command")))
    monkeypatch.setattr(orca, "_focus_pi", Mock())
    monkeypatch.setattr(orca, "_run", Mock(side_effect=AssertionError("Unexpected subprocess")))
    monkeypatch.setattr(orca.shutil, "which", lambda name: "/fake/orca" if name == "orca" else None)
    monkeypatch.setattr(orca.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    for name in ("WORKTRUNK_AGENT", "HERDR_ENV", "HERDR_SOCKET_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(orca, "time", value)
    return value


@pytest.fixture
def checkout(tmp_path):
    path = tmp_path / "checkout with spaces"
    path.mkdir()
    return path.resolve()


def state_file(path):
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()
    return orca.STATE_ROOT / f"{digest}.json"


def write_state(path, value):
    orca.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    state_file(path).write_text(json.dumps(value))


def read_state(path):
    return json.loads(state_file(path).read_text())


def live(path, **changes):
    return {
        "worktreePath": str(path),
        "agentIdentity": "pi",
        "connected": True,
        "writable": True,
        "orphaned": False,
        "incarnationId": "incarnation-1",
        "ptyId": "pty-1",
        "handle": "terminal-1",
        **changes,
    }


def terminal_list(path):
    return ["terminal", "list", "--worktree", f"path:{path}"]


def launch_args(path):
    return ["agent", "launch", "--agent", "pi", "--worktree", f"path:{path}", "--surface", "terminal"]


def launch_result(handle="terminal-1"):
    return {"operationId": "operation-1", "outcome": {"kind": "terminal", "handle": handle}}


def write_binding(path, terminal=None, **changes):
    terminal = live(path) if terminal is None else terminal
    orca.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    binding = {
        "path": str(path), "handle": terminal["handle"],
        "incarnationId": terminal["incarnationId"], "ptyId": terminal["ptyId"],
        **changes,
    }
    orca._binding_path(path).write_text(json.dumps(binding))
    return binding


def focus_result(path, terminal_handle="terminal-1", **changes):
    return {"focus": {"handle": terminal_handle, "navigated": True, "tabId": "tab-1", "worktreeId": f"repo-1::{path}", **changes}}


def replies(monkeypatch, *values):
    mock = Mock(side_effect=values)
    monkeypatch.setattr(orca, "_orca", mock)
    return mock


def hook_listing():
    return subprocess.CompletedProcess([], 0, '[{"name": "configured-hook"}]', "")


def config_setup(monkeypatch, config, *, registration=None):
    query = replies(monkeypatch, registration or {"repo": {"externalWorktreeVisibility": "show"}})
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(config), ""))
    monkeypatch.setattr(orca, "_run", run)
    return query, run


@pytest.mark.parametrize(
    "name,value",
    [("WORKTRUNK_AGENT", "1"), ("HERDR_ENV", "1"), ("HERDR_SOCKET_PATH", "/tmp/herdr.sock")],
)
def test_prepare_skips_agent_and_herdr_invocations(monkeypatch, checkout, name, value):
    monkeypatch.setenv(name, value)
    assert orca.manual_invocation() is False
    assert orca.prepare(checkout) is None
    orca._orca.assert_not_called()
    orca._run.assert_not_called()


@pytest.mark.parametrize("name", ["WORKTRUNK_AGENT", "HERDR_ENV", "HERDR_SOCKET_PATH"])
def test_manual_invocation_allows_inactive_environment(monkeypatch, name):
    monkeypatch.setenv(name, "" if name == "HERDR_SOCKET_PATH" else "0")
    assert orca.manual_invocation() is True


def test_prepare_skips_non_tty(monkeypatch, checkout):
    monkeypatch.setattr(orca.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert orca.prepare(checkout) is None
    orca._orca.assert_not_called()


def test_prepare_skips_without_orca_installed(monkeypatch, checkout):
    monkeypatch.setattr(orca.shutil, "which", lambda _name: None)
    assert orca.prepare(checkout) is None
    orca._orca.assert_not_called()


@pytest.mark.parametrize("visibility", [None, "hide", "auto", "SHOW"])
def test_prepare_requires_watched_registration_show(monkeypatch, checkout, visibility):
    query = replies(monkeypatch, {"repo": {"externalWorktreeVisibility": visibility}})
    assert orca.prepare(checkout) is None
    query.assert_called_once_with(["repo", "show", "--repo", f"path:{checkout}"], cwd=checkout)
    orca._run.assert_not_called()


@pytest.mark.parametrize("code", ["selector_not_found", "transport_unknown", "command_failed"])
def test_prepare_registration_failure_is_optional(monkeypatch, checkout, code):
    replies(monkeypatch, orca.OrcaError(code, "Registration unavailable"))
    assert orca.prepare(checkout) is None
    orca._run.assert_not_called()


@pytest.mark.parametrize("wrapped", [False, True])
def test_prepare_suppresses_global_and_user_project_hooks(monkeypatch, checkout, wrapped):
    key = str(checkout)
    registration = {"externalWorktreeVisibility": "show"}
    if wrapped:
        registration = {"repo": registration}
    query, run = config_setup(
        monkeypatch,
        {
            "user": {
                "config": {
                    "post-start": {"global": "configured-global-command"},
                    "projects": {
                        key: {"post-start": {"setup": "configured-project-command"}},
                        'quoted"repo': {"post-start": ["another-hook"]},
                        "empty": {"post-start": []},
                        "other-setting": {"post-switch": ["untouched"]},
                        "scalar": "not-a-project-config",
                    },
                }
            }
        },
        registration=registration,
    )
    setup = orca.prepare(checkout)
    overrides = (f"projects.{json.dumps(key)}.post-start=[]", 'projects."quoted\\"repo".post-start=[]')
    assert setup == orca.Setup(checkout, ("post-start=[]", *overrides), overrides)
    query.assert_called_once_with(["repo", "show", "--repo", f"path:{checkout}"], cwd=checkout)
    run.assert_called_once_with(["wt", "-C", str(checkout), "config", "show", "--format", "json"], cwd=checkout)


def test_prepare_without_hooks_still_suppresses_global_start(monkeypatch, checkout):
    config_setup(monkeypatch, {"user": {"config": None}, "project": None, "system": None})
    assert orca.prepare(checkout) == orca.Setup(checkout, ("post-start=[]",), ())


@pytest.mark.parametrize("source", ["project", "system"])
def test_prepare_rejects_project_file_background_hooks(monkeypatch, checkout, source):
    config_setup(
        monkeypatch,
        {"user": {"config": {}}, source: {"config": {"post-start": {"background": "run-build"}}}},
    )
    with pytest.raises(orca.HandoffError, match=f"{source} post-start hooks need an explicit readiness barrier"):
        orca.prepare(checkout)


@pytest.mark.parametrize(
    "stdout,returncode",
    [("not json", 0), ("[]", 0), ("{}", 0), ('{"user": {"config": {}}}', 1)],
)
def test_prepare_invalid_config_fails_closed(monkeypatch, checkout, stdout, returncode):
    config_setup(monkeypatch, {})
    orca._run.return_value = subprocess.CompletedProcess([], returncode, stdout, "secret stderr")
    with pytest.raises(orca.HandoffError, match="could not read wt setup configuration"):
        orca.prepare(checkout)


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("wt", 10)])
def test_prepare_config_transport_failure(monkeypatch, checkout, error):
    config_setup(monkeypatch, {})
    orca._run.side_effect = error
    with pytest.raises(orca.HandoffError, match="could not read wt setup configuration"):
        orca.prepare(checkout)


@pytest.mark.parametrize("base", [None, "main"])
def test_finish_runs_configured_global_then_project_hooks_foreground(monkeypatch, checkout, clock, base):
    root = checkout.parent
    override = f"projects.{json.dumps(str(root))}.post-start=[]"
    setup = orca.Setup(root, ("post-start=[]", override), (override,))
    calls = []

    def run(args, **kwargs):
        # Setup changes the list in place after the read-only query returns.
        calls.append((args.copy(), kwargs))
        clock.now += 7
        if "show" in args:
            return hook_listing()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(orca, "_run", run)
    setup.finish(checkout, "feature/test", base)
    for index, overrides in enumerate(((override,), ("post-start=[]",))):
        prefix = ["wt", "-C", str(checkout)]
        for value in overrides:
            prefix += ["--config-set", value]
        listing = [*prefix, "hook", "show", "post-start", "--format", "json"]
        foreground = [
            *prefix, "hook", "post-start", "user:", "--foreground",
            f"--repo_path={root}", f"--worktree_path={checkout}", "--branch=feature/test",
            f"--base_worktree_path={root}",
        ]
        if base:
            foreground.append(f"--base={base}")
        assert calls[index * 2] == (listing, {"cwd": checkout, "timeout": orca.SETUP_TIMEOUT - 14 * index})
        assert calls[index * 2 + 1] == (
            foreground, {"cwd": checkout, "timeout": orca.SETUP_TIMEOUT - 14 * index - 7},
        )
    assert len(calls) == 4
    orca._orca.assert_not_called()


@pytest.mark.parametrize("global_hooks,project_hooks", [(False, False), (False, True), (True, False), (True, True)])
def test_finish_skips_only_empty_phases(monkeypatch, checkout, clock, global_hooks, project_hooks):
    calls = []
    override = f"projects.{json.dumps(str(checkout.parent))}.post-start=[]"
    listings = iter([global_hooks, project_hooks])

    def run(args, **kwargs):
        calls.append((args.copy(), kwargs))
        clock.now += 3
        if "show" in args:
            return hook_listing() if next(listings) else subprocess.CompletedProcess(
                args, 0, "[]", "No user post-start hooks configured",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(orca, "_run", run)
    orca.Setup(checkout.parent, (), (override,)).finish(checkout, "branch", None)
    expected = ["listing"]
    if global_hooks:
        expected.append("foreground")
    expected.append("listing")
    if project_hooks:
        expected.append("foreground")
    assert ["listing" if "show" in args else "foreground" for args, _kwargs in calls] == expected
    listings_seen = 0
    for index, (args, kwargs) in enumerate(calls):
        assert kwargs == {"cwd": checkout, "timeout": orca.SETUP_TIMEOUT - 3 * index}
        if "show" in args:
            listings_seen += 1
            assert args[-5:] == ["hook", "show", "post-start", "--format", "json"]
            assert "--foreground" not in args
        assert args[3:5] == ["--config-set", override if listings_seen == 1 else "post-start=[]"]
    orca._orca.assert_not_called()


@pytest.mark.parametrize("failed_step", [0, 1])
def test_finish_hook_failure_stops_and_hides_output(monkeypatch, checkout, clock, failed_step):
    success = subprocess.CompletedProcess([], 0, "", "")
    failure = subprocess.CompletedProcess([], 1, "secret stdout", "secret stderr")
    run = Mock(side_effect=[hook_listing(), success] * failed_step + [hook_listing(), failure])
    monkeypatch.setattr(orca, "_run", run)
    with pytest.raises(orca.HandoffError, match="A wt setup hook failed") as caught:
        orca.Setup(checkout, (), ()).finish(checkout, "branch", None)
    assert "secret" not in str(caught.value)
    assert run.call_count == 2 * (failed_step + 1)
    orca._orca.assert_not_called()


@pytest.mark.parametrize("failed_call", [0, 1, 2, 3])
@pytest.mark.parametrize("error", [OSError("missing wt"), subprocess.TimeoutExpired("wt", 240)])
def test_finish_transport_failure_prevents_launch(monkeypatch, checkout, clock, error, failed_call):
    results = [hook_listing(), subprocess.CompletedProcess([], 0, "", "")] * 2
    run = Mock(side_effect=[*results[:failed_call], error])
    monkeypatch.setattr(orca, "_run", run)
    with pytest.raises(orca.HandoffError, match="setup did not finish; Pi was not started"):
        orca.Setup(checkout, (), ()).finish(checkout, "branch", None)
    assert run.call_count == failed_call + 1
    orca._orca.assert_not_called()


@pytest.mark.parametrize("failed_phase", [0, 1])
@pytest.mark.parametrize("stdout,returncode", [
    ("", 0), ("not json", 0), ("{}", 0), ("null", 0), ('"hooks"', 0), ("1", 0),
    ("[]", 1), ('[{"name": "configured-hook"}]', 1),
])
def test_finish_invalid_hook_listing_fails_closed(monkeypatch, checkout, clock, failed_phase, stdout, returncode):
    success = subprocess.CompletedProcess([], 0, "", "")
    failure = subprocess.CompletedProcess([], returncode, stdout, "secret stderr")
    run = Mock(side_effect=[hook_listing(), success] * failed_phase + [failure])
    monkeypatch.setattr(orca, "_run", run)
    with pytest.raises(orca.HandoffError, match="Could not read configured wt setup hooks") as caught:
        orca.Setup(checkout, (), ()).finish(checkout, "branch", None)
    assert "secret" not in str(caught.value)
    assert run.call_count == 2 * failed_phase + 1
    orca._orca.assert_not_called()


@pytest.mark.parametrize("exhausted_call", [0, 1, 2])
def test_finish_uses_one_shared_deadline(monkeypatch, checkout, clock, exhausted_call):
    calls = []

    def run(args, **kwargs):
        calls.append((args.copy(), kwargs))
        clock.now += orca.SETUP_TIMEOUT - 5 * exhausted_call if len(calls) == exhausted_call + 1 else 5
        return hook_listing() if "show" in args else subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(orca, "_run", run)
    with pytest.raises(orca.HandoffError, match="240-second deadline"):
        orca.Setup(checkout, (), ()).finish(checkout, "branch", None)
    assert clock.now == orca.SETUP_TIMEOUT
    assert len(calls) == exhausted_call + 1
    assert [kwargs["timeout"] for _args, kwargs in calls] == [
        orca.SETUP_TIMEOUT - 5 * index for index in range(exhausted_call + 1)
    ]
    orca._orca.assert_not_called()


def test_finish_empty_phase_listing_uses_shared_deadline(monkeypatch, checkout, clock):
    def run(args, **_kwargs):
        clock.now += orca.SETUP_TIMEOUT
        return subprocess.CompletedProcess(args, 0, "[]", "")

    mock = Mock(side_effect=run)
    monkeypatch.setattr(orca, "_run", mock)
    with pytest.raises(orca.HandoffError, match="240-second deadline"):
        orca.Setup(checkout, (), ()).finish(checkout, "branch", None)
    mock.assert_called_once()
    orca._orca.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"agentIdentity": "claude", "title": "Pi"},
        {"agentIdentity": None, "title": "pi"},
        {"connected": False}, {"connected": 1},
        {"writable": False}, {"writable": 1},
        {"orphaned": True}, {"orphaned": None},
        {"incarnationId": ""}, {"incarnationId": None}, {"handle": ""},
        {"worktreePath": "/other/checkout"},
    ],
)
def test_live_pi_requires_exact_identity_and_live_pty(checkout, changes):
    assert orca._live_pi({"terminals": [live(checkout, **changes)]}, checkout) is None


@pytest.mark.parametrize("suffix", ["/nested", "-neighbor"])
def test_live_pi_rejects_similar_paths(checkout, suffix):
    terminal = live(checkout, worktreePath=f"{checkout}{suffix}")
    assert orca._live_pi({"terminals": [terminal]}, checkout) is None


@pytest.mark.parametrize("missing", ["connected", "writable", "orphaned", "incarnationId", "handle", "agentIdentity"])
def test_live_pi_requires_all_liveness_fields(checkout, missing):
    terminal = live(checkout)
    del terminal[missing]
    assert orca._live_pi({"terminals": [terminal]}, checkout) is None


@pytest.mark.parametrize("title", [None, "a misleading non-agent title"])
def test_handoff_reuses_live_identity_without_title(monkeypatch, checkout, clock, title):
    terminal = live(checkout, handle="verified-reused-terminal")
    if title is not None:
        terminal["title"] = title
    alias = checkout.parent / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    write_state(checkout, {"path": str(checkout), "pending": True})
    write_binding(checkout, terminal)
    query = replies(monkeypatch, {"terminals": [terminal]})

    def focus(handle, path):
        query.assert_called_once_with(terminal_list(checkout), cwd=checkout, timeout=orca.CALL_TIMEOUT)
        assert read_state(checkout) == {"path": str(checkout), "pending": True}

    orca._focus_pi.side_effect = focus
    assert orca.handoff(alias) == "Reused Pi and opened its worktree in Orca."
    orca._focus_pi.assert_called_once_with("verified-reused-terminal", checkout)
    query.assert_called_once_with(terminal_list(checkout), cwd=checkout, timeout=orca.CALL_TIMEOUT)
    assert not state_file(checkout).exists()


def test_handoff_discovery_retries_only_selector_not_found(monkeypatch, checkout, clock):
    write_binding(checkout)
    query = replies(
        monkeypatch,
        orca.OrcaError("selector_not_found", "Not discovered yet"),
        orca.OrcaError("selector_not_found", "Still not discovered"),
        {"terminals": [live(checkout)]},
    )
    assert orca.handoff(checkout) == "Reused Pi and opened its worktree in Orca."
    orca._focus_pi.assert_called_once_with("terminal-1", checkout)
    assert query.call_count == 3
    assert clock.sleeps == [0.5, 0.5]
    for call in query.call_args_list:
        assert call.args == (terminal_list(checkout),)
        assert call.kwargs == {"cwd": checkout, "timeout": orca.CALL_TIMEOUT}


def test_handoff_discovery_is_bounded(monkeypatch, checkout, clock):
    query = Mock(side_effect=orca.OrcaError("selector_not_found", "Not found"))
    monkeypatch.setattr(orca, "_orca", query)
    with pytest.raises(orca.HandoffError, match="within 45 seconds; Pi was not started"):
        orca.handoff(checkout)
    assert clock.now == orca.DISCOVERY_TIMEOUT
    assert query.call_count == 90
    assert all(call.args == (terminal_list(checkout),) for call in query.call_args_list)
    assert max(call.kwargs["timeout"] for call in query.call_args_list) <= orca.CALL_TIMEOUT
    assert query.call_args.kwargs["timeout"] == 0.5
    orca._focus_pi.assert_not_called()
    assert not state_file(checkout).exists()


@pytest.mark.parametrize("code", ["transport_unknown", "command_failed", "access_denied"])
def test_handoff_does_not_retry_other_discovery_failures(monkeypatch, checkout, clock, code):
    error = orca.OrcaError(code, "Unavailable")
    query = replies(monkeypatch, error)
    with pytest.raises(orca.OrcaError) as caught:
        orca.handoff(checkout)
    assert caught.value is error
    assert query.call_count == 1
    orca._focus_pi.assert_not_called()
    assert not clock.sleeps
    assert not state_file(checkout).exists()


def test_handoff_launch_exact_resolved_path_terminal_surface_without_focus_or_prompt(monkeypatch, checkout, clock):
    alias = checkout.parent / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    replies_seen = []

    def command(args, **kwargs):
        replies_seen.append((args, kwargs))
        if args[:2] == ["agent", "launch"]:
            assert read_state(checkout) == {"path": str(checkout), "pending": True}
            return launch_result("verified-new-terminal")
        return {"terminals": [] if len(replies_seen) == 1 else [live(checkout, handle="verified-new-terminal")]}

    def focus(handle, path):
        assert len(replies_seen) == 3
        assert read_state(checkout) == {
            "path": str(checkout), "operationId": "operation-1", "handle": "verified-new-terminal", "pending": True,
        }

    monkeypatch.setattr(orca, "_orca", command)
    orca._focus_pi.side_effect = focus
    message = orca.handoff(alias)
    assert message == "Started Pi and opened its worktree in Orca."
    orca._focus_pi.assert_called_once_with("verified-new-terminal", checkout)
    assert replies_seen == [
        (terminal_list(checkout), {"cwd": checkout, "timeout": orca.CALL_TIMEOUT}),
        (launch_args(checkout), {"cwd": checkout, "timeout": orca.LAUNCH_TIMEOUT}),
        (terminal_list(checkout), {"cwd": checkout, "timeout": orca.CALL_TIMEOUT}),
    ]
    assert not state_file(checkout).exists()


@pytest.mark.parametrize("pending", [False, True])
def test_handoff_title_inferred_pi_without_binding_never_launches_or_focuses(monkeypatch, checkout, clock, pending):
    terminal = live(checkout, title="review - pi")
    if pending:
        write_state(checkout, {"path": str(checkout), "pending": True})
    query = replies(monkeypatch, {"terminals": [terminal]})
    with pytest.raises(orca.HandoffError, match="unrecorded Pi terminal"):
        orca.handoff(checkout)
    query.assert_called_once_with(terminal_list(checkout), cwd=checkout, timeout=orca.CALL_TIMEOUT)
    orca._focus_pi.assert_not_called()
    assert not orca._binding_path(checkout).exists()
    assert state_file(checkout).exists() is pending


@pytest.mark.parametrize("changes", [
    {"orphaned": True}, {"orphaned": None}, {"connected": False}, {"writable": False},
    {"incarnationId": None}, {"handle": None},
])
@pytest.mark.parametrize("bound", [False, True])
def test_handoff_uncertain_pi_does_not_authorize_launch(monkeypatch, checkout, clock, changes, bound):
    if bound:
        write_binding(checkout)
    query = replies(monkeypatch, {"terminals": [live(checkout, **changes)]})
    with pytest.raises(orca.HandoffError, match="uncertain liveness"):
        orca.handoff(checkout)
    query.assert_called_once()
    orca._focus_pi.assert_not_called()
    assert not state_file(checkout).exists()


@pytest.mark.parametrize("missing", ["connected", "writable", "orphaned", "incarnationId", "handle"])
def test_handoff_missing_pi_liveness_field_never_launches(monkeypatch, checkout, clock, missing):
    terminal = live(checkout)
    del terminal[missing]
    query = replies(monkeypatch, {"terminals": [terminal]})
    with pytest.raises(orca.HandoffError, match="uncertain liveness"):
        orca.handoff(checkout)
    query.assert_called_once()
    orca._focus_pi.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"handle": "another-terminal"}, {"incarnationId": "new-incarnation"}, {"ptyId": "another-pty"},
])
def test_handoff_changed_binding_never_launches_or_focuses(monkeypatch, checkout, clock, changes):
    binding = write_binding(checkout, **changes)
    query = replies(monkeypatch, {"terminals": [live(checkout)]})
    with pytest.raises(orca.HandoffError, match="saved Pi terminal has changed"):
        orca.handoff(checkout)
    query.assert_called_once()
    orca._focus_pi.assert_not_called()
    assert json.loads(orca._binding_path(checkout).read_text()) == binding
    assert not state_file(checkout).exists()


@pytest.mark.parametrize("raw", ["not json", "null", "[]", "{}", '{"path": "/other/checkout"}'])
def test_handoff_unreadable_binding_fails_closed(monkeypatch, checkout, clock, raw):
    write_binding(checkout)
    orca._binding_path(checkout).write_text(raw)
    query = replies(monkeypatch, {"terminals": [live(checkout)]})
    with pytest.raises(orca.HandoffError, match="Cannot verify the saved Pi terminal"):
        orca.handoff(checkout)
    query.assert_called_once()
    orca._focus_pi.assert_not_called()
    assert orca._binding_path(checkout).read_text() == raw


def test_handoff_postlaunch_waits_for_exact_accepted_handle(monkeypatch, checkout, clock):
    unrelated = live(checkout, handle="unrelated-pi", incarnationId="other-incarnation", ptyId="other-pty")
    accepted = live(checkout)
    query = replies(
        monkeypatch, {"terminals": []}, launch_result(),
        {"terminals": [unrelated]}, {"terminals": [unrelated, accepted]},
    )
    assert orca.handoff(checkout).startswith("Started")
    assert clock.sleeps == [0.25]
    assert query.call_count == 4
    orca._focus_pi.assert_called_once_with("terminal-1", checkout)
    assert json.loads(orca._binding_path(checkout).read_text()) == {
        "path": str(checkout), "handle": "terminal-1", "incarnationId": "incarnation-1", "ptyId": "pty-1",
    }


def test_handoff_postlaunch_never_substitutes_another_live_pi(monkeypatch, checkout, clock):
    def command(args, **_kwargs):
        if args[:2] == ["agent", "launch"]:
            return launch_result()
        if orca.STATE_ROOT.exists() and state_file(checkout).exists():
            return {"terminals": [live(checkout, handle="unrelated-pi")]}
        return {"terminals": []}

    query = Mock(side_effect=command)
    monkeypatch.setattr(orca, "_orca", query)
    with pytest.raises(orca.HandoffError, match="not yet confirmed live"):
        orca.handoff(checkout)
    orca._focus_pi.assert_not_called()
    assert not orca._binding_path(checkout).exists()
    assert read_state(checkout) == {
        "path": str(checkout), "operationId": "operation-1", "handle": "terminal-1", "pending": True,
    }
    assert sum(call.args[0][:2] == ["agent", "launch"] for call in query.call_args_list) == 1


def test_handoff_title_alone_does_not_reuse_a_terminal(monkeypatch, checkout, clock):
    query = replies(
        monkeypatch,
        {"terminals": [live(checkout, agentIdentity="shell", title="pi")]},
        launch_result(),
        {"terminals": [live(checkout)]},
    )
    assert orca.handoff(checkout).startswith("Started")
    assert query.call_args_list[1].args == (launch_args(checkout),)


@pytest.mark.parametrize("code", ["transport_unknown", "command_failed", "unrecognized_failure"])
def test_handoff_unknown_launch_persists_and_blocks_fresh_replay(monkeypatch, checkout, clock, code):
    query = replies(monkeypatch, {"terminals": []}, orca.OrcaError(code, "Launch uncertain"), {"terminals": []})
    with pytest.raises(orca.OrcaError, match="Launch uncertain"):
        orca.handoff(checkout)
    assert read_state(checkout) == {"path": str(checkout), "pending": True}
    with pytest.raises(orca.HandoffError, match="earlier launch result is unknown"):
        orca.handoff(checkout)
    assert [call.args[0][:2] for call in query.call_args_list] == [
        ["terminal", "list"], ["agent", "launch"], ["terminal", "list"],
    ]
    assert read_state(checkout)["pending"] is True
    orca._focus_pi.assert_not_called()


@pytest.mark.parametrize("launch_failure", ["not json", "[]", '{"ok": true}', "timeout"])
def test_handoff_malformed_reply_or_timeout_preserves_pending(monkeypatch, checkout, clock, launch_failure):
    monkeypatch.setattr(orca, "_orca", _ORCA)
    empty = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {"terminals": []}}), "")
    failure = (
        subprocess.TimeoutExpired("orca", orca.LAUNCH_TIMEOUT)
        if launch_failure == "timeout"
        else subprocess.CompletedProcess([], 0, launch_failure, "")
    )
    run = Mock(side_effect=[empty, failure, empty])
    monkeypatch.setattr(orca, "_run", run)
    with pytest.raises(orca.OrcaError) as caught:
        orca.handoff(checkout)
    assert caught.value.code == "transport_unknown"
    assert read_state(checkout) == {"path": str(checkout), "pending": True}
    with pytest.raises(orca.HandoffError, match="earlier launch result is unknown"):
        orca.handoff(checkout)
    assert run.call_count == 3
    orca._focus_pi.assert_not_called()


def test_handoff_retries_saved_operation_id_instead_of_fresh_launch(monkeypatch, checkout, clock):
    error = orca.OrcaError("launch_failed", "Retryable launch", {"details": {"launch": {"operationId": "operation-1"}}})
    query = replies(
        monkeypatch, {"terminals": []}, error, {"terminals": []},
        {"launch": {"operationId": "operation-1", "outcome": {"kind": "terminal", "handle": "terminal-1"}}},
        {"terminals": [live(checkout)]},
    )
    with pytest.raises(orca.OrcaError, match="Retryable launch"):
        orca.handoff(checkout)
    assert read_state(checkout) == {"path": str(checkout), "operationId": "operation-1", "pending": True}
    assert orca.handoff(checkout) == "Started Pi and opened its worktree in Orca."
    orca._focus_pi.assert_called_once_with("terminal-1", checkout)
    assert query.call_args_list[1].args == (launch_args(checkout),)
    assert query.call_args_list[3].args == ([*launch_args(checkout), "--retry-launch", "operation-1"],)
    assert not state_file(checkout).exists()


@pytest.mark.parametrize("code", ["selector_not_found", "invalid_argument", "incompatible_runtime"])
def test_handoff_known_prelaunch_refusal_clears_only_fresh_record(monkeypatch, checkout, clock, code):
    replies(monkeypatch, {"terminals": []}, orca.OrcaError(code, "Verified refusal"))
    with pytest.raises(orca.OrcaError):
        orca.handoff(checkout)
    assert not state_file(checkout).exists()
    write_state(checkout, {"path": str(checkout), "operationId": "earlier-operation", "pending": True})
    query = replies(monkeypatch, {"terminals": []}, orca.OrcaError(code, "Retry refused"))
    with pytest.raises(orca.OrcaError):
        orca.handoff(checkout)
    assert read_state(checkout) == {"path": str(checkout), "operationId": "earlier-operation", "pending": True}
    assert query.call_args_list[1].args == ([*launch_args(checkout), "--retry-launch", "earlier-operation"],)


@pytest.mark.parametrize(
    "result",
    [{}, {"outcome": {"kind": "browser", "handle": "tab"}}, {"outcome": {"kind": "terminal"}}],
)
def test_handoff_unconfirmed_launch_outcome_keeps_record(monkeypatch, checkout, clock, result):
    query = replies(monkeypatch, {"terminals": []}, result)
    with pytest.raises(orca.HandoffError, match="did not confirm a Pi terminal launch"):
        orca.handoff(checkout)
    assert read_state(checkout) == {"path": str(checkout), "pending": True}
    assert query.call_count == 2
    orca._focus_pi.assert_not_called()


def test_handoff_malformed_outcome_preserves_nested_operation_id(monkeypatch, checkout, clock):
    replies(monkeypatch, {"terminals": []}, {"launch": {"operationId": "saved-operation"}})
    with pytest.raises(orca.HandoffError, match="did not confirm"):
        orca.handoff(checkout)
    assert read_state(checkout) == {"path": str(checkout), "operationId": "saved-operation", "pending": True}


def test_handoff_waits_for_real_liveness_after_launch(monkeypatch, checkout, clock):
    query = replies(
        monkeypatch, {"terminals": []}, launch_result("verified-live-terminal"),
        {"terminals": []}, {"terminals": [live(checkout, handle="verified-live-terminal", connected=False)]},
        {"terminals": [live(checkout, handle="verified-live-terminal")]},
    )

    def focus(handle, path):
        assert query.call_count == 5
        assert read_state(checkout) == {
            "path": str(checkout), "operationId": "operation-1", "handle": "verified-live-terminal", "pending": True,
        }

    orca._focus_pi.side_effect = focus
    assert orca.handoff(checkout) == "Started Pi and opened its worktree in Orca."
    orca._focus_pi.assert_called_once_with("verified-live-terminal", checkout)
    assert clock.sleeps == [0.25, 0.25]
    assert query.call_count == 5
    assert query.call_args.kwargs["timeout"] == orca.CALL_TIMEOUT - 0.5
    assert not state_file(checkout).exists()


def test_handoff_liveness_deadline_preserves_operation(monkeypatch, checkout, clock):
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ["agent", "launch"]:
            return launch_result()
        return {"terminals": [] if len(calls) == 1 else [live(checkout, writable=False)]}

    monkeypatch.setattr(orca, "_orca", command)
    with pytest.raises(orca.HandoffError, match="not yet confirmed live"):
        orca.handoff(checkout)
    assert clock.now == orca.CALL_TIMEOUT
    assert len(calls) == 42
    assert calls[-1][1]["timeout"] == 0.25
    orca._focus_pi.assert_not_called()
    assert read_state(checkout) == {
        "path": str(checkout), "operationId": "operation-1", "handle": "terminal-1", "pending": True,
    }


def test_handoff_liveness_transport_failure_keeps_operation(monkeypatch, checkout, clock):
    replies(monkeypatch, {"terminals": []}, launch_result(), orca.OrcaError("transport_unknown", "Lost connection"))
    with pytest.raises(orca.OrcaError, match="Lost connection"):
        orca.handoff(checkout)
    assert read_state(checkout)["operationId"] == "operation-1"
    orca._focus_pi.assert_not_called()


@pytest.mark.parametrize("after_launch", [False, True])
def test_handoff_truncated_list_blocks_duplicate_launch(monkeypatch, checkout, clock, after_launch):
    values = [{"terminals": []}, launch_result()] if after_launch else []
    query = replies(monkeypatch, *values, {"truncated": True, "terminals": [live(checkout)]})
    with pytest.raises(orca.HandoffError, match="terminal list is incomplete"):
        orca.handoff(checkout)
    assert query.call_count == (3 if after_launch else 1)
    orca._focus_pi.assert_not_called()
    assert state_file(checkout).exists() is after_launch


@pytest.mark.parametrize("raw", ["not json", "[]", "null", '{}', '{"path": "/wrong/checkout"}'])
def test_handoff_corrupted_persistent_state_fails_closed(checkout, clock, raw):
    orca.STATE_ROOT.mkdir()
    state_file(checkout).write_text(raw)
    with pytest.raises(orca.HandoffError, match="Cannot read the earlier launch record"):
        orca.handoff(checkout)
    assert state_file(checkout).read_text() == raw
    orca._orca.assert_not_called()


def test_handoff_pending_without_replay_id_blocks_fresh_launch(monkeypatch, checkout, clock):
    write_state(checkout, {"path": str(checkout), "pending": True})
    query = replies(monkeypatch, {"terminals": []})
    with pytest.raises(orca.HandoffError, match="Inspect this checkout in Orca"):
        orca.handoff(checkout)
    query.assert_called_once()
    assert read_state(checkout)["pending"] is True


def test_handoff_lock_contention_has_bounded_wait(monkeypatch, checkout, clock):
    lock = Mock(side_effect=BlockingIOError)
    monkeypatch.setattr(orca.fcntl, "flock", lock)
    with pytest.raises(orca.HandoffError, match="Another Orca handoff is still running"):
        orca.handoff(checkout)
    assert clock.now == orca.DISCOVERY_TIMEOUT
    assert lock.call_count == 181
    assert len(clock.sleeps) == 180
    assert all(call.args[1] == orca.fcntl.LOCK_EX | orca.fcntl.LOCK_NB for call in lock.call_args_list)
    orca._orca.assert_not_called()
    assert not state_file(checkout).exists()


def test_handoff_acquires_lock_after_contention(monkeypatch, checkout, clock):
    write_binding(checkout)
    monkeypatch.setattr(orca.fcntl, "flock", Mock(side_effect=[BlockingIOError, None]))
    query = replies(monkeypatch, {"terminals": [live(checkout)]})
    assert orca.handoff(checkout).startswith("Reused")
    assert clock.sleeps == [0.25]
    query.assert_called_once()


@pytest.mark.parametrize("payload", [{}, {"terminals": None}, {"terminals": {}}, {"terminals": "unknown"}])
@pytest.mark.parametrize("after_launch", [False, True])
def test_handoff_missing_or_malformed_terminal_list_fails_closed(monkeypatch, checkout, clock, payload, after_launch):
    values = [{"terminals": []}, launch_result()] if after_launch else []
    query = replies(monkeypatch, *values, payload)
    with pytest.raises(orca.HandoffError, match="terminal list is incomplete"):
        orca.handoff(checkout)
    assert query.call_count == (3 if after_launch else 1)
    assert state_file(checkout).exists() is after_launch
    if after_launch:
        assert read_state(checkout)["operationId"] == "operation-1"


def test_setup_path_uses_resolved_checkout_identity(checkout):
    alias = checkout.parent / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    expected = state_file(checkout).with_suffix(".setup.json")
    assert orca._setup_path(checkout) == expected
    assert orca._setup_path(alias) == expected


@pytest.mark.parametrize("base", [None, "main"])
def test_finish_setup_records_readiness_before_hooks_and_clears_on_success(monkeypatch, checkout, base):
    setup = orca.Setup(checkout.parent, (), ())
    calls = []

    def finish(self, path, branch, hook_base):
        calls.append((self, path, branch, hook_base))
        assert json.loads(orca._setup_path(path).read_text()) == {
            "path": str(checkout), "branch": "feature/test", "base": base,
        }
        with pytest.raises(orca.HandoffError, match="setup is still pending"):
            orca.handoff(checkout)
        orca._orca.assert_not_called()

    monkeypatch.setattr(orca.Setup, "finish", finish)
    orca.finish_setup(setup, checkout, "feature/test", base)
    assert calls == [(setup, checkout, "feature/test", base)]
    assert not orca._setup_path(checkout).exists()
    assert not list(orca.STATE_ROOT.glob("*.tmp"))


def test_finish_setup_failure_persists_and_blocks_handoff(monkeypatch, checkout, clock):
    run = Mock(side_effect=[hook_listing(), subprocess.CompletedProcess([], 1, "secret stdout", "secret stderr")])
    monkeypatch.setattr(orca, "_run", run)
    with pytest.raises(orca.HandoffError, match="setup hook failed"):
        orca.finish_setup(orca.Setup(checkout.parent, (), ()), checkout, "feature/test", "main")
    assert json.loads(orca._setup_path(checkout).read_text()) == {
        "path": str(checkout), "branch": "feature/test", "base": "main",
    }
    with pytest.raises(orca.HandoffError, match="setup is still pending"):
        orca.handoff(checkout)
    orca._orca.assert_not_called()
    assert not state_file(checkout).exists()


def test_resume_setup_without_record_is_noop(monkeypatch, checkout):
    finish = Mock()
    monkeypatch.setattr(orca.Setup, "finish", finish)
    orca.resume_setup(orca.Setup(checkout.parent, (), ()), checkout)
    finish.assert_not_called()
    assert not orca.STATE_ROOT.exists()


@pytest.mark.parametrize("base", [None, "main"])
def test_resume_setup_retries_saved_branch_and_base_before_launch(monkeypatch, checkout, clock, base):
    setup = orca.Setup(checkout.parent, (), ())
    run = Mock(side_effect=[hook_listing(), subprocess.CompletedProcess([], 1, "", "")])
    monkeypatch.setattr(orca, "_run", run)
    with pytest.raises(orca.HandoffError, match="setup hook failed"):
        orca.finish_setup(setup, checkout, "saved-branch", base)
    calls = []

    def ready_run(args, **kwargs):
        calls.append((args.copy(), kwargs))
        assert orca._setup_path(checkout).exists()
        with pytest.raises(orca.HandoffError, match="setup is still pending"):
            orca.handoff(checkout)
        clock.now += 2
        return hook_listing() if "show" in args else subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(orca, "_run", ready_run)
    orca.resume_setup(setup, checkout)
    assert len(calls) == 4
    assert ["show" in args for args, _kwargs in calls] == [True, False, True, False]
    for index, (args, kwargs) in enumerate(calls):
        assert kwargs == {"cwd": checkout, "timeout": orca.SETUP_TIMEOUT - 2 * index}
        if "show" not in args:
            assert "--branch=saved-branch" in args
            assert ("--base=main" in args) is (base is not None)
    orca._orca.assert_not_called()
    assert not orca._setup_path(checkout).exists()
    replies(monkeypatch, {"terminals": []}, launch_result(), {"terminals": [live(checkout)]})
    assert orca.handoff(checkout).startswith("Started")


def test_resume_setup_failed_retry_keeps_launch_blocked(monkeypatch, checkout, clock):
    setup = orca.Setup(checkout.parent, (), ())
    run = Mock(side_effect=[hook_listing(), subprocess.CompletedProcess([], 1, "", "")] * 2)
    monkeypatch.setattr(orca, "_run", run)
    with pytest.raises(orca.HandoffError):
        orca.finish_setup(setup, checkout, "saved-branch", "main")
    before = orca._setup_path(checkout).read_text()
    with pytest.raises(orca.HandoffError, match="setup hook failed"):
        orca.resume_setup(setup, checkout)
    assert orca._setup_path(checkout).read_text() == before
    with pytest.raises(orca.HandoffError, match="setup is still pending"):
        orca.handoff(checkout)
    orca._orca.assert_not_called()


@pytest.mark.parametrize("raw", [
    "not json", "null", "[]", '{}', '{"path": "/wrong", "branch": "branch"}',
    '{"branch": "branch"}', '{"path": "$CHECKOUT"}', '{"path": "$CHECKOUT", "branch": 1}',
])
def test_resume_setup_corrupted_record_fails_closed(monkeypatch, checkout, raw):
    orca.STATE_ROOT.mkdir()
    raw = raw.replace("$CHECKOUT", str(checkout))
    record = orca._setup_path(checkout)
    record.write_text(raw)
    finish = Mock()
    monkeypatch.setattr(orca.Setup, "finish", finish)
    with pytest.raises(orca.HandoffError, match="Cannot read the pending setup record"):
        orca.resume_setup(orca.Setup(checkout.parent, (), ()), checkout)
    finish.assert_not_called()
    assert record.read_text() == raw
    with pytest.raises(orca.HandoffError, match="setup is still pending"):
        orca.handoff(checkout)
    orca._orca.assert_not_called()


@pytest.mark.parametrize("raw", ["not json", "{}"])
def test_handoff_blocks_any_pending_setup_even_with_live_pi(monkeypatch, checkout, raw):
    orca.STATE_ROOT.mkdir()
    orca._setup_path(checkout).write_text(raw)
    query = replies(monkeypatch, {"terminals": [live(checkout)]})
    with pytest.raises(orca.HandoffError, match="setup is still pending"):
        orca.handoff(checkout)
    query.assert_not_called()
    orca._focus_pi.assert_not_called()
    assert orca._setup_path(checkout).read_text() == raw


def test_handoff_requires_existing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        orca.handoff(tmp_path / "missing")
    orca._orca.assert_not_called()
    assert not orca.STATE_ROOT.exists()


def test_focus_pi_switches_verified_terminal_in_exact_checkout(monkeypatch, checkout):
    query = replies(monkeypatch, focus_result(checkout, "verified-terminal"))
    assert _FOCUS_PI("verified-terminal", checkout) is None
    query.assert_called_once_with(["terminal", "switch", "--terminal", "verified-terminal"], cwd=checkout)
    orca._run.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"navigated": False}, {"navigated": 1}, {"handle": "other-terminal"},
    {"tabId": ""}, {"tabId": None}, {"tabId": 1},
    {"worktreeId": "repo-1::/other/checkout"}, {"worktreeId": None}, {"worktreeId": 1},
])
def test_focus_pi_requires_exact_confirmed_navigation(monkeypatch, checkout, changes):
    query = replies(monkeypatch, focus_result(checkout, "verified-terminal", **changes))
    with pytest.raises(orca.HandoffError, match="did not confirm opening"):
        _FOCUS_PI("verified-terminal", checkout)
    query.assert_called_once_with(["terminal", "switch", "--terminal", "verified-terminal"], cwd=checkout)


@pytest.mark.parametrize("result", [{}, {"focus": None}, {"focus": []}, {"focus": "unknown"}])
def test_focus_pi_malformed_confirmation_fails_closed(monkeypatch, checkout, result):
    replies(monkeypatch, result)
    with pytest.raises(orca.HandoffError, match="did not confirm opening"):
        _FOCUS_PI("verified-terminal", checkout)


def test_orca_passes_json_and_timeout_and_unwraps_result(monkeypatch, checkout):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok": true, "result": {"handle": "terminal-1"}}', ""))
    monkeypatch.setattr(orca, "_run", run)
    assert _ORCA(["terminal", "list"], cwd=checkout, timeout=3.5) == {"handle": "terminal-1"}
    run.assert_called_once_with(["orca", "terminal", "list", "--json"], cwd=checkout, timeout=3.5)


@pytest.mark.parametrize("stdout", ["", "garbage", "[]", "null", "42", '{"ok": true}', '{"ok": true, "result": []}'])
def test_orca_malformed_reply_is_transport_unknown(monkeypatch, checkout, stdout):
    monkeypatch.setattr(orca, "_run", Mock(return_value=subprocess.CompletedProcess([], 0, stdout, "")))
    with pytest.raises(orca.OrcaError) as caught:
        _ORCA(["terminal", "list"], cwd=checkout)
    assert caught.value.code == "transport_unknown"


@pytest.mark.parametrize("returncode,ok", [(0, False), (1, True), (1, False)])
def test_orca_nonzero_or_not_ok_preserves_structured_error(monkeypatch, checkout, returncode, ok):
    error = {"code": "launch_failed", "message": "Could not launch", "details": {"operationId": "operation-1"}}
    reply = {"ok": ok, "result": {"should": "not succeed"}, "error": error}
    monkeypatch.setattr(orca, "_run", Mock(return_value=subprocess.CompletedProcess([], returncode, json.dumps(reply), "")))
    with pytest.raises(orca.OrcaError, match="Could not launch") as caught:
        _ORCA(["agent", "launch"], cwd=checkout)
    assert caught.value.code == "launch_failed"
    assert caught.value.data == error


@pytest.mark.parametrize("reply,returncode", [({}, 0), ({"ok": 1}, 0), ({"ok": True, "result": {}}, 1)])
def test_orca_failure_without_error_uses_command_failed(monkeypatch, checkout, reply, returncode):
    monkeypatch.setattr(orca, "_run", Mock(return_value=subprocess.CompletedProcess([], returncode, json.dumps(reply), "")))
    with pytest.raises(orca.OrcaError, match="Orca command failed") as caught:
        _ORCA(["terminal", "list"], cwd=checkout)
    assert caught.value.code == "command_failed"
    assert caught.value.data == {}


@pytest.mark.parametrize("error", [OSError("missing orca"), subprocess.TimeoutExpired("orca", 10)])
def test_orca_transport_failure_becomes_unknown(monkeypatch, checkout, error):
    monkeypatch.setattr(orca, "_run", Mock(side_effect=error))
    with pytest.raises(orca.OrcaError, match="Orca command did not complete") as caught:
        _ORCA(["terminal", "list"], cwd=checkout)
    assert caught.value.code == "transport_unknown"
    assert caught.value.__cause__ is error
