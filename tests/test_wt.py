"""`wt.branch_exists`/`wt.remote_branch_exists` are plain git plumbing (no
`wt` binary involved), so these run against a real git repo rather than a
stubbed one.
"""

import subprocess
from pathlib import Path

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
