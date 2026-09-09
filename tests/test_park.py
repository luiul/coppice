"""The parked mark's git-config storage (park.py), against real repos in
tmp_path: the key round-trips, unrelated branch config is left alone, the
mark is readable from a linked worktree (shared config), and deleting the
branch drops the whole `branch.<name>` section, which is what makes
`cop remove`/`wt remove` clean up marks for free.
"""

import subprocess
from pathlib import Path

from coppice import park


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "commit", "--allow-empty", "-qm", "init")
    return path


def test_park_round_trip(tmp_path):
    repo_dir = _init_repo(tmp_path / "repo")

    ts = park.park(repo_dir, "feat-a", at=1_700_000_000.0)

    assert ts == 1_700_000_000.0
    assert park.parked_at(repo_dir) == {"feat-a": 1_700_000_000.0}
    # Stored as a plain unix timestamp in the repo's shared local config.
    assert _git(repo_dir, "config", "--local", "branch.feat-a.parked-at") == "1700000000"

    park.unpark(repo_dir, "feat-a")
    assert park.parked_at(repo_dir) == {}


def test_park_defaults_to_now(tmp_path):
    repo_dir = _init_repo(tmp_path / "repo")
    ts = park.park(repo_dir, "feat-a")
    assert abs(park.parked_at(repo_dir)["feat-a"] - ts) < 1


def test_repark_refreshes_the_timestamp(tmp_path):
    repo_dir = _init_repo(tmp_path / "repo")
    park.park(repo_dir, "feat-a", at=1_000_000_000.0)
    park.park(repo_dir, "feat-a", at=1_700_000_000.0)
    assert park.parked_at(repo_dir) == {"feat-a": 1_700_000_000.0}


def test_parked_at_ignores_unrelated_branch_config(tmp_path):
    repo_dir = _init_repo(tmp_path / "repo")
    _git(repo_dir, "branch", "feat-a")
    # The usual upstream-tracking keys share the `branch.<name>.` namespace
    # and must not read as marks; a non-integer value must not either.
    _git(repo_dir, "config", "branch.feat-a.remote", "origin")
    _git(repo_dir, "config", "branch.feat-b.parked-at", "not-a-number")
    park.park(repo_dir, "feat-c", at=1_700_000_000.0)

    assert park.parked_at(repo_dir) == {"feat-c": 1_700_000_000.0}


def test_unpark_without_a_mark_is_a_noop(tmp_path):
    repo_dir = _init_repo(tmp_path / "repo")
    park.unpark(repo_dir, "never-parked")  # git exits 5 for a missing key
    assert park.parked_at(repo_dir) == {}


def test_the_mark_dies_with_the_branch(tmp_path):
    """`git branch -D` drops the whole `branch.<name>` config section, so
    removing a worktree+branch cleans up its parked mark for free."""
    repo_dir = _init_repo(tmp_path / "repo")
    _git(repo_dir, "branch", "feat-a")
    park.park(repo_dir, "feat-a", at=1_700_000_000.0)
    assert park.parked_at(repo_dir) != {}

    _git(repo_dir, "branch", "-D", "feat-a")

    assert park.parked_at(repo_dir) == {}


def test_the_mark_reads_from_a_linked_worktree(tmp_path):
    """The config is shared, not per-worktree: a mark written against the
    main checkout is visible when read from a linked worktree's directory."""
    repo_dir = _init_repo(tmp_path / "repo")
    wt_dir = tmp_path / "wt"
    _git(repo_dir, "worktree", "add", "-q", "-b", "feat-a", str(wt_dir))

    park.park(repo_dir, "feat-a", at=1_700_000_000.0)

    assert park.parked_at(wt_dir) == {"feat-a": 1_700_000_000.0}


def test_parked_at_many_covers_every_repo(tmp_path):
    repo_a = _init_repo(tmp_path / "a")
    repo_b = _init_repo(tmp_path / "b")
    park.park(repo_a, "feat-a", at=1_700_000_000.0)

    marks = park.parked_at_many([repo_a, repo_b, repo_a])  # dupes collapse

    assert marks == {repo_a: {"feat-a": 1_700_000_000.0}, repo_b: {}}
