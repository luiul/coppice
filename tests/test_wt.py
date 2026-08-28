"""`wt.branch_exists`/`wt.remote_branch_exists` are plain git plumbing (no
`wt` binary involved), so these run against a real git repo rather than a
stubbed one.
"""

import os
import subprocess
from pathlib import Path
from typing import Any

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


def test_switch_hands_extra_env_to_the_wt_subprocess(monkeypatch, tmp_path):
    """`cop new --prompt` rides on this: COP_PROMPT must reach `wt`'s
    subprocess environment (and through it, `wt`'s hooks) intact, quotes,
    dollar signs and all, merged over the inherited environment rather than
    replacing it.
    """
    calls = _capture_wt_subprocess(monkeypatch)

    result = wt.switch(tmp_path, "some-branch", create=True, extra_env={"COP_PROMPT": 'hello "world" $foo'})

    assert result == {"action": "created"}
    env = calls[0]["env"]
    assert env["COP_PROMPT"] == 'hello "world" $foo'
    assert env["PATH"] == os.environ["PATH"]


def test_switch_without_extra_env_inherits_the_environment_as_is(monkeypatch, tmp_path):
    """No EXTRA_ENV means env=None: the `wt` child inherits this process's
    environment untouched, so a plain `cop new` can never leak a COP_PROMPT
    into a hook.
    """
    calls = _capture_wt_subprocess(monkeypatch)

    wt.switch(tmp_path, "some-branch")

    assert calls[0]["env"] is None
