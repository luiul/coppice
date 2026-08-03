import subprocess
from pathlib import Path

import pytest

from coppice import repo


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    # CI runners have no global git identity configured; set one locally so
    # `git commit` doesn't fail with "empty ident name".
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "--allow-empty", "-q", "-m", "init"], check=True)
    return path


def test_resolve_repo_root_from_main_checkout(tmp_path):
    checkout = _init_repo(tmp_path / "repo")
    assert repo.resolve_repo_root(checkout) == checkout


def test_resolve_repo_root_rejects_non_repo(tmp_path):
    with pytest.raises(repo.RepoResolutionError):
        repo.resolve_repo_root(tmp_path / "not-a-repo")


def test_registry_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")

    assert repo.known_repos() == []

    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo.register_repo(repo_a)
    repo.register_repo(repo_b)
    repo.register_repo(repo_a)  # dedup, no-op

    assert set(repo.known_repos()) == {repo_a, repo_b}


def test_scope_repos_with_explicit_path(tmp_path):
    checkout = _init_repo(tmp_path / "repo")
    assert repo.scope_repos(str(checkout)) == [checkout]
