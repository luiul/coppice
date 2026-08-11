import subprocess
from pathlib import Path

from coppice import gh


def _init_repo_with_remote(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote_url], check=True)
    return path


def test_repo_slug_ssh_remote(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "git@github.com:owner/repo.git")
    assert gh.repo_slug(repo_dir) == "owner/repo"


def test_repo_slug_https_remote(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "https://github.com/owner/repo.git")
    assert gh.repo_slug(repo_dir) == "owner/repo"


def test_repo_slug_https_remote_without_dot_git_suffix(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "https://github.com/owner/repo")
    assert gh.repo_slug(repo_dir) == "owner/repo"


def test_repo_slug_matches_repo_name_containing_a_dot(tmp_path):
    """Confirmed bug: the old regex's `[^/.]+?` repo-name group excluded
    dots, so any GitHub repo whose name contains a literal dot (a common,
    valid case, e.g. `my.project`) silently failed to match at all. Since
    `open_prs` returns `{}` when `repo_slug` is None, `cop clean`'s open-PR
    safety check would silently no-op for such repos.
    """
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "git@github.com:owner/my.project.git")
    assert gh.repo_slug(repo_dir) == "owner/my.project"


def test_repo_slug_none_for_non_github_remote(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "git@gitlab.com:owner/repo.git")
    assert gh.repo_slug(repo_dir) is None


def test_repo_slug_none_when_no_origin_remote(tmp_path):
    tmp_path_repo = tmp_path / "repo"
    tmp_path_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path_repo)], check=True)
    assert gh.repo_slug(tmp_path_repo) is None


def test_open_prs_returns_empty_without_wanted_branches(tmp_path):
    repo_dir = _init_repo_with_remote(tmp_path / "repo", "git@github.com:owner/repo.git")
    assert gh.open_prs(repo_dir, []) == {}
