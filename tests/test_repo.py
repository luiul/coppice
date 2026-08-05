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


def test_prune_missing_repos_drops_deleted_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")

    alive = tmp_path / "alive"
    alive.mkdir()
    gone = tmp_path / "gone"
    gone.mkdir()
    repo.register_repo(alive)
    repo.register_repo(gone)
    gone.rmdir()

    pruned = repo.prune_missing_repos()

    assert pruned == [gone]
    assert repo.known_repos() == [alive]


def test_prune_missing_repos_removes_registry_file_when_all_gone(tmp_path, monkeypatch):
    registry_path = tmp_path / "known-repos"
    monkeypatch.setattr(repo, "REGISTRY_PATH", registry_path)

    gone = tmp_path / "gone"
    gone.mkdir()
    repo.register_repo(gone)
    gone.rmdir()

    assert repo.prune_missing_repos() == [gone]
    assert not registry_path.exists()


def test_scope_repos_self_heals_missing_entries(tmp_path, monkeypatch):
    """`scope_repos` (used by list/remove/clean) drops dead registry
    entries as a side effect, same as `coppice status` does explicitly, so
    a repo deleted outside `coppice` doesn't keep costing a wasted `wt`
    subprocess call (or, for `status`, a permanent `missing` row) forever.
    """
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")

    checkout = _init_repo(tmp_path / "repo")
    gone = tmp_path / "gone"
    gone.mkdir()
    repo.register_repo(checkout)
    repo.register_repo(gone)
    gone.rmdir()

    result = repo.scope_repos(None)

    assert gone not in result
    assert checkout in result
    assert repo.known_repos() == [checkout]
