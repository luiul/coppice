"""Unit tests for the plain-git helpers behind `coppice sync` (git.py), run
against real repos in tmp_path: a local bare repo plays 'origin', so no
network is involved. The helpers are exercised end-to-end through the
`sync` command tests in test_cli.py too; these pin their individual
contracts (exit-code mapping, ancestor direction, abort tolerance).
"""

import subprocess
from pathlib import Path

import pytest

from coppice import git


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _init_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A repo cloned from a local bare 'origin' with one commit on main.
    Returns (repo, origin)."""
    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    (seed / "f.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "init"], check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    repo_dir = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    return repo_dir, origin


def _advance_origin(origin: Path, tmp_path: Path, *, filename: str = "f.txt", content: str = "day2") -> None:
    """Push one new commit to origin's main from a scratch clone, simulating
    the base branch moving while worktrees are being worked on."""
    other = tmp_path / "other"
    if not other.exists():
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.name", "Test"], check=True)
    with open(other / filename, "a") as f:
        f.write(content + "\n")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", f"advance {filename}")
    _git(other, "push", "-q")


def test_fetch_base_updates_the_remote_tracking_ref(tmp_path):
    repo_dir, origin = _init_with_origin(tmp_path)
    _advance_origin(origin, tmp_path)

    # The clone's origin/main is stale (still the initial commit) until fetched.
    assert git.is_ancestor(repo_dir, "origin/main", "main")
    git.fetch_base(repo_dir, "main")
    assert not git.is_ancestor(repo_dir, "origin/main", "main")
    assert git.is_ancestor(repo_dir, "main", "origin/main")
    assert git.commits_between(repo_dir, "main", "origin/main") == 1


def test_is_ancestor_raises_on_unknown_ref(tmp_path):
    repo_dir, _origin = _init_with_origin(tmp_path)
    with pytest.raises(git.GitError):
        git.is_ancestor(repo_dir, "origin/nope", "main")


def test_merge_would_conflict_without_touching_anything(tmp_path):
    repo_dir, origin = _init_with_origin(tmp_path)
    # feat adds c.txt one way...
    _git(repo_dir, "switch", "-qC", "feat")
    (repo_dir / "c.txt").write_text("ours\n")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-qm", "ours")
    _git(repo_dir, "switch", "-q", "main")
    # ...origin/main adds the same file another way (add/add conflict)
    _advance_origin(origin, tmp_path, filename="c.txt", content="theirs")
    git.fetch_base(repo_dir, "main")

    assert git.merge_would_conflict(repo_dir, "feat", "origin/main")
    assert not git.merge_would_conflict(repo_dir, "main", "origin/main")
    # Pure plumbing: no merge state, no index or worktree changes.
    assert _git(repo_dir, "status", "--porcelain") == ""
    assert subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "-q", "--verify", "MERGE_HEAD"]).returncode != 0


def test_merge_then_abort_tolerates_no_merge_in_progress(tmp_path):
    repo_dir, origin = _init_with_origin(tmp_path)
    feat = tmp_path / "feat"
    _git(repo_dir, "worktree", "add", "-q", "-b", "feat", str(feat))
    (feat / "feat.txt").write_text("feat work\n")
    _git(feat, "add", ".")
    _git(feat, "commit", "-qm", "feat work")
    _advance_origin(origin, tmp_path)
    git.fetch_base(repo_dir, "main")

    git.merge(feat, "origin/main")
    assert _git(feat, "rev-list", "--count", "--merges", "HEAD") == "1"
    assert git.is_ancestor(feat, "origin/main", "HEAD")
    # merge_abort is the error path's cleanup; it must not blow up when the
    # merge actually succeeded (nothing to abort).
    git.merge_abort(feat)
    assert _git(feat, "status", "--porcelain") == ""


def test_ff_only_fast_forwards_and_refuses_divergence(tmp_path):
    repo_dir, origin = _init_with_origin(tmp_path)
    _advance_origin(origin, tmp_path)
    git.fetch_base(repo_dir, "main")

    git.ff_only(repo_dir, "origin/main")
    assert (repo_dir / "f.txt").read_text() == "base\nday2\n"

    # A local commit on main makes the next ff impossible.
    (repo_dir / "local.txt").write_text("local\n")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-qm", "local work")
    _advance_origin(origin, tmp_path, content="day3")
    git.fetch_base(repo_dir, "main")
    with pytest.raises(git.GitError):
        git.ff_only(repo_dir, "origin/main")
