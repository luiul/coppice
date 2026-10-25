"""`wt.branch_exists`/`wt.remote_branch_exists` are plain git plumbing (no
`wt` binary involved), so these run against a real git repo rather than a
stubbed one.
"""

import io
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from coppice import wt


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    _run("config", "user.email", "test@example.com", cwd=path)
    _run("config", "user.name", "Test", cwd=path)
    _run("commit", "--allow-empty", "-q", "-m", "init", cwd=path)
    return path


def test_branch_exists_true_for_local_branch(tmp_path):
    repo_dir = _init_repo(tmp_path / "repo")
    _run("branch", "local-only", cwd=repo_dir)

    assert wt.branch_exists(repo_dir, "local-only") is True


def test_branch_exists_false_for_unknown_branch(tmp_path):
    repo_dir = _init_repo(tmp_path / "repo")

    assert wt.branch_exists(repo_dir, "does-not-exist") is False


def test_remote_branch_exists_false_with_no_remote(tmp_path):
    repo_dir = _init_repo(tmp_path / "repo")

    assert wt.remote_branch_exists(repo_dir, "anything") is False


def test_remote_branch_exists_true_for_a_remote_tracking_ref(tmp_path):
    """The case `branch_exists` alone misses: pushed by someone else, never
    checked out locally, so it has a `refs/remotes/origin/...` ref but no
    `refs/heads/...` one.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    repo_dir = _init_repo(tmp_path / "repo")
    _run("remote", "add", "origin", str(origin), cwd=repo_dir)
    _run("push", "-q", "origin", "main", cwd=repo_dir)
    _run("branch", "remote-only", cwd=repo_dir)
    _run("push", "-q", "origin", "remote-only", cwd=repo_dir)
    _run("branch", "-D", "remote-only", cwd=repo_dir)

    assert wt.branch_exists(repo_dir, "remote-only") is False
    assert wt.remote_branch_exists(repo_dir, "remote-only") is True


def test_remote_branch_exists_false_for_a_branch_not_on_the_remote(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    repo_dir = _init_repo(tmp_path / "repo")
    _run("remote", "add", "origin", str(origin), cwd=repo_dir)
    _run("push", "-q", "origin", "main", cwd=repo_dir)

    assert wt.remote_branch_exists(repo_dir, "never-pushed") is False


def _capture_wt_subprocess(monkeypatch) -> list[dict[str, Any]]:
    """Stub `wt` as installed and capture every `subprocess.run` call `wt.run`
    makes, without a real `wt` binary. NOTE: `wt.subprocess` is the global
    subprocess module object, so this patches subprocess.run for everyone
    until the test ends; only call it after any real subprocess setup is done.
    """
    monkeypatch.setattr(wt.shutil, "which", lambda _name: "/usr/bin/wt")
    calls: list[dict[str, Any]] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout='{"action": "created"}', stderr="")

    monkeypatch.setattr(wt.subprocess, "run", fake_run)
    return calls


def _capture_wt_popen(monkeypatch, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Stub `wt` as installed and capture every `subprocess.Popen` call the
    streaming path makes, without a real `wt` binary. NOTE: `wt.subprocess`
    is the global subprocess module object, so this patches subprocess.Popen
    for everyone until the test ends; only call it after any real subprocess
    setup is done.
    """
    monkeypatch.setattr(wt.shutil, "which", lambda _name: "/usr/bin/wt")
    calls: list[dict[str, Any]] = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append({"cmd": cmd, **kwargs})
            self.stdout = io.BytesIO(stdout)
            self.stderr = io.BytesIO(stderr)

        def wait(self):
            return returncode

    monkeypatch.setattr(wt.subprocess, "Popen", FakePopen)
    return calls


@pytest.mark.parametrize("gate", [None, "1", "0"])
@pytest.mark.parametrize("create,base", [(False, None), (True, "main"), (False, "develop")])
def test_switch_inherits_environment_and_passes_worktree_args(monkeypatch, tmp_path, gate, create, base):
    """Switch leaves hook policy to the caller's environment."""
    if gate is None:
        monkeypatch.delenv("WORKTRUNK_NO_OPEN", raising=False)
    else:
        monkeypatch.setenv("WORKTRUNK_NO_OPEN", gate)
    monkeypatch.setenv("WORKTRUNK_AGENT", "1")
    monkeypatch.setenv("COPPICE_TEST_INHERITED", "unchanged")
    monkeypatch.setattr(wt.sys, "stderr", io.StringIO())
    before = dict(os.environ)
    calls = _capture_wt_popen(monkeypatch, stdout=b'{"action": "created"}')

    result = wt.switch(tmp_path, "some-branch", create=create, base=base)

    expected = ["wt", "-C", str(tmp_path), "switch"]
    if create:
        expected.append("--create")
    if base is not None:
        expected += ["--base", base]
    expected += ["--no-cd", "--format", "json", "some-branch"]
    assert calls[0]["cmd"] == expected
    assert calls[0]["env"] is None  # Popen inherits the environment without a replacement dict
    assert dict(os.environ) == before
    assert result == {"action": "created"}


def test_switch_passes_no_environment_override_to_run(monkeypatch, tmp_path):
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout='{"action": "existing"}', stderr="")

    monkeypatch.setattr(wt, "run", fake_run)

    result = wt.switch(tmp_path, "some-branch")

    assert calls == [(["switch", "--no-cd", "--format", "json", "some-branch"], {"cwd": tmp_path, "stream": True})]
    assert result == {"action": "existing"}


@pytest.mark.parametrize("stream", [False, True])
def test_run_preserves_explicit_environment_without_a_terminal(monkeypatch, tmp_path, stream):
    monkeypatch.setattr(wt.sys, "stderr", io.StringIO())
    env = {"COPPICE_TEST_EXPLICIT": "only this", "CLICOLOR_FORCE": "0"}
    calls = _capture_wt_popen(monkeypatch) if stream else _capture_wt_subprocess(monkeypatch)

    wt.run(["list"], cwd=tmp_path, stream=stream, env=env)

    assert calls[0]["env"] is env
    assert env == {"COPPICE_TEST_EXPLICIT": "only this", "CLICOLOR_FORCE": "0"}


def test_streaming_forces_wt_colors_when_our_stderr_is_a_terminal(monkeypatch, tmp_path):
    """The tee pipes `wt`'s stderr, which hides the terminal from it and
    would strip its colors; CLICOLOR_FORCE keeps them when our own stderr
    is a TTY."""

    class FakeTty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setenv("WORKTRUNK_NO_OPEN", "0")
    monkeypatch.setenv("CLICOLOR_FORCE", "0")
    before = dict(os.environ)
    monkeypatch.setattr(wt.sys, "stderr", FakeTty())
    calls = _capture_wt_popen(monkeypatch, stdout=b'{"action": "created"}')

    wt.switch(tmp_path, "some-branch")

    assert calls[0]["env"] == {**before, "CLICOLOR_FORCE": "1"}
    assert dict(os.environ) == before


def test_streaming_layers_tty_colors_on_explicit_environment(monkeypatch, tmp_path):
    class FakeTty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(wt.sys, "stderr", FakeTty())
    env = {"COPPICE_TEST_EXPLICIT": "only this", "CLICOLOR_FORCE": "0"}
    calls = _capture_wt_popen(monkeypatch)

    wt.run(["list"], cwd=tmp_path, stream=True, env=env)

    assert calls[0]["env"] == {"COPPICE_TEST_EXPLICIT": "only this", "CLICOLOR_FORCE": "1"}
    assert env == {"COPPICE_TEST_EXPLICIT": "only this", "CLICOLOR_FORCE": "0"}


def test_switch_streams_wt_stderr_but_pipes_stdout_for_json(monkeypatch, tmp_path, capsys):
    """stderr is `wt`'s human channel (hook progress, status lines, config
    warnings): the mutating commands tee it to the terminal live AND keep a
    copy for the caller to inspect. stdout stays piped, it carries the JSON
    `switch` parses."""
    calls = _capture_wt_popen(monkeypatch, stdout=b'{"action": "created"}', stderr=b"hook progress\n")

    proc = wt.run(["switch", "--no-cd", "--format", "json", "some-branch"], cwd=tmp_path, stream=True)

    assert calls[0]["stderr"] is subprocess.PIPE
    assert calls[0]["stdout"] is subprocess.PIPE
    assert capsys.readouterr().err == "hook progress\n"
    assert proc.stderr == "hook progress\n"
    assert proc.stdout == '{"action": "created"}'


def test_remove_streams_wt_stderr_too(monkeypatch, tmp_path, capsys):
    calls = _capture_wt_popen(monkeypatch, stderr=b"removing\n")

    wt.remove(tmp_path, "some-branch")

    assert calls[0]["stderr"] is subprocess.PIPE
    assert calls[0]["stdout"] is subprocess.PIPE
    assert capsys.readouterr().err == "removing\n"


def test_list_worktrees_keeps_both_streams_captured(monkeypatch, tmp_path):
    """The machine-parsed, parallelized list path never streams: concurrent
    `wt` children would interleave on the shared terminal."""
    calls = _capture_wt_subprocess(monkeypatch)

    wt.list_worktrees(tmp_path)

    assert calls[0]["capture_output"] is True
    assert calls[0]["env"] is None


def test_streamed_failure_raises_the_short_error_form(monkeypatch, tmp_path):
    """A streamed failure's stderr already went to the screen live, so the
    exception message stays the short "exited N" form instead of re-printing
    it, but the tee'd text still rides along for callers to inspect."""
    _capture_wt_popen(monkeypatch, stderr=b"boom\n", returncode=1)

    with pytest.raises(wt.WtCommandError) as excinfo:
        wt.switch(tmp_path, "some-branch")

    assert excinfo.value.stderr == "boom\n"
    assert excinfo.value.streamed is True
    assert str(excinfo.value) == "wt switch --no-cd --format json some-branch exited 1"


def test_relocate_preview_parses_the_dry_run_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(wt.shutil, "which", lambda _name: "/usr/bin/wt")
    payload = (
        '{"dry_run": true, "entries": [{"branch": "feature/other",'
        ' "from": "/worktrees/review-jamie/repo", "to": "/worktrees/feature-other/repo"}], "skipped": []}'
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    monkeypatch.setattr(wt.subprocess, "run", fake_run)

    entries = wt.relocate_preview(tmp_path, "feature/other")

    assert entries == [
        {"branch": "feature/other", "from": "/worktrees/review-jamie/repo", "to": "/worktrees/feature-other/repo"}
    ]


def test_relocate_precreates_target_parents(monkeypatch, tmp_path):
    """`git worktree move` (what `wt step relocate` shells out to) refuses a
    target whose parent directory doesn't exist yet, so the real run must
    create each previewed target's parent first."""
    target = tmp_path / "worktrees" / "feature-other" / "repo"
    calls = _capture_wt_popen(monkeypatch)

    wt.relocate(tmp_path, "feature/other", [{"from": str(tmp_path / "old"), "to": str(target)}])

    assert target.parent.is_dir()
    assert calls[0]["cmd"][0] == "wt"
    assert "relocate" in calls[0]["cmd"]
    assert "--yes" in calls[0]["cmd"]
    assert "feature/other" in calls[0]["cmd"]


def test_captured_failure_still_carries_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(wt.shutil, "which", lambda _name: "/usr/bin/wt")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(wt.subprocess, "run", fake_run)

    with pytest.raises(wt.WtCommandError) as excinfo:
        wt.run(["list"], cwd=tmp_path)

    assert excinfo.value.stderr == "boom"
    assert excinfo.value.streamed is False
    assert str(excinfo.value) == "boom"
