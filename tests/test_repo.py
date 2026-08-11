import subprocess
import threading
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


def _init_bare_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
    return path


def test_resolve_repo_root_from_bare_repo(tmp_path):
    """`git-common-dir` for a bare repo *is* the repo root, not a `.git`
    subdirectory inside it, so `.parent` must not be taken unconditionally.
    """
    bare = _init_bare_repo(tmp_path / "bare.git")
    assert repo.resolve_repo_root(bare) == bare


def test_resolve_repo_root_from_bare_repo_linked_worktree(tmp_path):
    """Confirmed live: from a linked worktree of a bare repo,
    `--is-bare-repository` run against the worktree itself reports false
    even though `--git-common-dir` already points at the bare repo's own
    root. Bareness must be checked against the resolved common dir, not
    against the path the caller passed in.
    """
    bare = _init_bare_repo(tmp_path / "bare.git")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "--allow-empty", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(clone), "push", "-q", str(bare), "main"], check=True)

    worktree = tmp_path / "wt1"
    subprocess.run(["git", "-C", str(bare), "worktree", "add", str(worktree), "main"], check=True)

    assert repo.resolve_repo_root(worktree) == bare


def test_registry_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")

    assert repo.known_repos() == []

    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo.register_repo(repo_a)
    repo.register_repo(repo_b)
    repo.register_repo(repo_a)  # dedup, no-op

    assert set(repo.known_repos()) == {repo_a, repo_b}


def test_register_repo_survives_concurrent_writers(tmp_path, monkeypatch):
    """Without a lock around the read-modify-write, concurrent registrations
    silently clobber each other (confirmed: 30 concurrent writers, only a
    handful survived). The `_locked_registry` exclusive lock must serialize
    every writer so all of them land in the final registry.
    """
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")

    n = 30
    repos = [tmp_path / f"repo-{i}" for i in range(n)]
    threads = [threading.Thread(target=repo.register_repo, args=(r,)) for r in repos]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(repo.known_repos()) == set(repos)


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
