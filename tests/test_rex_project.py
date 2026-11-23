import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from coppice import repo, rex, workflow


@pytest.fixture
def project_server(monkeypatch, tmp_path):
    monkeypatch.setattr(rex, "STATE_ROOT", tmp_path / "rex")
    monkeypatch.setattr(workflow, "STATE_ROOT", tmp_path / "workflow")
    root = tmp_path / "project"
    root.mkdir()
    common = root / ".git"
    monkeypatch.setattr(repo, "project_identity", lambda _: (root.name, common))
    server = rex.ProjectTarget("rex", "unix:///local.sock", "host", "instance", root.name, common)
    state = SimpleNamespace(sessions={}, directories={}, creates=0, uncertain=False, tabs=0)

    def tab(session, cwd, label):
        state.tabs += 1
        window, block = f"window:{state.tabs}", f"block:{state.tabs}"
        state.directories[block] = cwd
        entry = state.sessions[session]
        entry["windows"].append(
            {
                "window_id": window,
                "label": label,
                "focused_block_id": block,
                "layers": [{"blocks": [{"host_id": "host", "block_id": block}]}],
            }
        )
        entry["active_window_id"] = window
        return {"window_id": window, "block_ids": [block]}

    def call(self, args, cwd):
        match args:
            case ["whoami", "--json"]:
                return {"endpoint": self.endpoint, "host_id": self.host, "instance_id": self.instance}
            case ["session", "ls", "--json"]:
                return {"sessions": list(state.sessions.values())}
            case ["session", "inspect", session, "--json"]:
                return state.sessions[session]
            case ["new", name, "-c", directory, "--window", title, "--json"]:
                assert directory == str(cwd)
                state.creates += 1
                session = f"session:{state.creates}"
                state.sessions[session] = {"session_id": session, "label": name, "windows": []}
                first = tab(session, directory, title)
                if state.uncertain:
                    raise rex.HandoffError("Lost reply")
                return {"session_id": session, "initial_windows": [first]}
            case ["window", "new", "-s", session, "-c", directory, "--json", title]:
                return tab(session, directory, title)
            case ["block", "call", "-s", _, "-b", block, "process"]:
                return {"foreground": {"cwd": state.directories[block]}}
            case ["focus", "-s", session, block]:
                state.sessions[session]["active_window_id"] = block.replace("block:", "window:")
                return {}
            case _:
                pytest.fail(str(args))

    monkeypatch.setattr(rex.Server, "call", call)
    return server, root, state


def test_named_project_session_starts_with_requested_checkout_then_reuses_tabs(project_server, tmp_path):
    server, root, state = project_server
    linked = tmp_path / "linked checkout"
    linked.mkdir()
    assert "new Rex session 'project'" in rex.handoff(server, linked, label="first")
    assert "Opened" in rex.handoff(server, root, label="main")
    assert "Focused" in rex.handoff(server, linked, label="first")
    assert state.creates == 1
    session = state.sessions["session:1"]
    assert session["label"] == "project"
    assert [state.directories[w["focused_block_id"]] for w in session["windows"]] == [str(linked), str(root)]
    assert session["active_window_id"] == "window:1"


def test_project_session_creation_stops_if_setup_is_pending(project_server):
    server, root, state = project_server
    workflow.STATE_ROOT.mkdir()
    workflow._setup_path(root).touch()
    with pytest.raises(rex.HandoffError, match="setup is still pending"):
        rex.handoff(server, root, label="main")
    assert state.creates == 0


def test_same_named_clone_is_not_attached_to_previous_project_session(project_server, monkeypatch):
    server, root, state = project_server
    rex.handoff(server, root, label="main")
    other_common = root / "elsewhere" / ".git"
    other = rex.ProjectTarget(server.binary, server.endpoint, server.host, server.instance, server.name, other_common)
    monkeypatch.setattr(repo, "project_identity", lambda _: (server.name, other_common))
    assert server.record != other.record
    with pytest.raises(rex.HandoffError, match=r"already in use.*session:1"):
        rex.handoff(other, root, label="other")
    assert state.creates == state.tabs == 1


def test_lost_session_receipt_recovers_first_tab_without_duplicate(project_server, tmp_path):
    server, root, state = project_server
    linked = tmp_path / "second"
    linked.mkdir()
    state.uncertain = True
    with pytest.raises(rex.HandoffError, match="project session creation is unconfirmed"):
        rex.handoff(server, root, label="main")
    assert "Opened" in rex.handoff(server, linked, label="second")
    assert "Focused" in rex.handoff(server, root, label="main")
    assert state.creates == 1 and state.tabs == 2


def test_pending_session_never_adopts_same_name_with_unverified_tab(project_server):
    server, root, state = project_server
    state.uncertain = True
    with pytest.raises(rex.HandoffError, match="creation is unconfirmed"):
        rex.handoff(server, root, label="main")
    state.sessions["session:1"]["windows"][0]["label"] = "Different tab"
    with pytest.raises(rex.HandoffError, match="expected session and first tab"):
        rex.handoff(server, root, label="main")
    state.sessions.clear()
    with pytest.raises(rex.HandoffError, match="No unique session"):
        rex.handoff(server, root, label="main")
    assert state.creates == 1 and state.tabs == 1


@pytest.mark.parametrize("override", [None, "session:explicit"])
def test_preflight_prefers_project_over_caller_but_honors_override(monkeypatch, tmp_path, override):
    endpoint = "unix:///local.sock"
    monkeypatch.setenv("REX_SESSION", "session:unrelated-caller")
    monkeypatch.setattr(workflow, "manual_invocation", lambda: True)
    monkeypatch.setattr(rex, "_binary", lambda: "rex")
    monkeypatch.setattr(Path, "is_socket", lambda _: True)
    monkeypatch.setattr(repo, "project_identity", Mock(return_value=("project", tmp_path / ".git")))
    monkeypatch.setattr(rex, "STATE_ROOT", tmp_path / "state")
    calls = Mock(
        side_effect=[
            {"endpoint": endpoint, "host_id": "host", "instance_id": "instance"},
            {"sessions": []} if override is None else {"session_id": override, "windows": []},
        ]
    )
    monkeypatch.setattr(rex, "_call", calls)
    target = rex.prepare(tmp_path, override)
    if override is None:
        assert isinstance(target, rex.ProjectTarget) and target.name == "project"
        assert calls.call_args.args[1] == ["session", "ls", "--json"]
    else:
        assert isinstance(target, rex.Target) and target.session == override
        assert calls.call_args.args[1] == ["session", "inspect", override, "--json"]
        repo.project_identity.assert_not_called()
    assert not rex.STATE_ROOT.exists()


def test_git_project_identity_uses_main_checkout_name_and_common_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    main = tmp_path / "Main project"
    main.mkdir()
    linked = tmp_path / "other-name"

    def git(*args):
        return subprocess.run(["git", "-C", str(main), *args], check=True, capture_output=True, timeout=5)

    git("init", "-q", "-b", "main")
    git("-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "--allow-empty", "-qm", "init")
    git("worktree", "add", "-qb", "task", str(linked))
    assert repo.project_identity(main) == repo.project_identity(linked) == (main.name, (main / ".git").resolve())
