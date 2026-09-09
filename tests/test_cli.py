"""`wt` is coppice's hard prerequisite (see README's Install section and
`coppice.wt.require_wt`). Every command that shells out to it must fail with
a clear, actionable message when it's missing from PATH, never a raw
traceback, regardless of which `wt`-calling code path a command happens to
hit first.

Also covers `clean`/`remove`'s picker/`status`, all synthesized against
fake `wt.list_worktrees`/`gh.open_prs` results rather than a real `wt`/`gh`
install, so these pass in CI the same as locally.
"""

import subprocess
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from coppice import cli, gh, git, park, repo, vscode, wt
from coppice.cli import app

runner = CliRunner()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "--allow-empty", "-q", "-m", "init"], check=True)
    return path


def _hide_wt(monkeypatch):
    """Simulate `wt` not being installed, independent of the real PATH."""
    monkeypatch.setattr(wt.shutil, "which", lambda name: None)


def _stub_wt(monkeypatch, *, which: dict[str, str | None] | None = None):
    """Simulate `wt` being installed (require_wt succeeds) without shelling
    out to a real binary, and control any other `shutil.which` lookups
    (fzf/gh) a test cares about. `shutil` is the same module object from
    every import site, so this one patch covers wt.py's and cli.py's calls.
    """
    table = {"wt": "/usr/bin/wt", **(which or {})}
    monkeypatch.setattr(wt.shutil, "which", lambda name: table.get(name))


def _entry(
    branch: str,
    path: Path,
    *,
    is_main: bool = False,
    is_current: bool = False,
    commit_ts: float = 0,
    dirty: bool = False,
    main_state: str = "ahead",
    stale: bool = False,
    mismatch: bool = False,
) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    state = "prunable" if stale else ("branch_worktree_mismatch" if mismatch else "active")
    return {
        "branch": branch,
        "path": str(path),
        "is_main": is_main,
        "is_current": is_current,
        "commit": {"timestamp": commit_ts},
        "working_tree": {"modified": dirty},
        "main_state": main_state,
        "worktree": {"state": state},
    }


def test_new_without_wt_fails_clearly(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "some-branch"])

    assert result.exit_code != 0
    assert "wt" in result.output
    assert "worktrunk.dev" in result.output
    assert "Traceback" not in result.output


def _stub_switch(monkeypatch, *, branch_exists: bool, remote_branch_exists: bool = False):
    """Stub out every `wt` call `cmd_new` makes once the branch name is
    decided: the branch-exists checks and the switch/create call, so its
    confirmation-prompt logic can be tested without a real `wt` install or
    worktree creation. The `_print_existing_worktrees` preview's
    `list_worktrees` call deliberately gets no stub here: every test below
    passes `--branch` (which skips the preview), so a regression that
    reintroduces a `wt list` subprocess on that path fails loudly on a
    `wt`-less CI instead of going unnoticed on machines with a real `wt`.
    """
    switch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: branch_exists)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: remote_branch_exists)
    monkeypatch.setattr(
        wt,
        "switch",
        lambda repo, branch, **kwargs: (
            switch_calls.append({"repo": repo, "branch": branch, **kwargs}) or {"action": "created", "path": None}
        ),
    )
    return switch_calls


def test_new_prompts_before_switching_to_an_existing_branch(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    switch_calls = _stub_switch(monkeypatch, branch_exists=True)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "existing-branch"], input="n\n")

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert "Cancelled" in result.output
    assert switch_calls == []


def test_new_bare_enter_declines_the_prompt(tmp_path, monkeypatch):
    """The prompt defaults to no on a bare Enter: 'new' implies a fresh
    branch, so hitting an existing one is the surprising case, not one to
    wave through by default.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    switch_calls = _stub_switch(monkeypatch, branch_exists=True)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "existing-branch"], input="\n")

    assert result.exit_code != 0
    assert "Cancelled" in result.output
    assert switch_calls == []


def test_new_confirm_needs_no_enter(tmp_path, monkeypatch):
    """A single `y` keypress is the whole answer, no enter: the dashkit
    confirmation convention (CONVENTIONS.md) coppice's prompts follow. The
    input below carries no newline; a line-based confirm would hit EOF and
    abort instead of confirming, which is exactly the regression this
    guards."""
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    switch_calls = _stub_switch(monkeypatch, branch_exists=True)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "existing-branch"], input="y")

    assert result.exit_code == 0, result.output
    assert len(switch_calls) == 1
    assert switch_calls[0]["create"] is False


def test_new_prompts_for_a_remote_only_branch_too(tmp_path, monkeypatch):
    """A branch that only exists on the remote (pushed by someone else,
    never checked out locally) must not be treated as absent: that would
    silently fork a new local branch from --base instead of picking up
    the remote one, diverging under the same name.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    switch_calls = _stub_switch(monkeypatch, branch_exists=False, remote_branch_exists=True)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "remote-only-branch", "--yes"])

    assert result.exit_code == 0
    assert len(switch_calls) == 1
    assert switch_calls[0]["create"] is False


def test_new_yes_skips_the_confirmation_prompt(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    switch_calls = _stub_switch(monkeypatch, branch_exists=True)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "existing-branch", "--yes"])

    assert result.exit_code == 0
    assert "already exists" not in result.output
    assert len(switch_calls) == 1
    assert switch_calls[0]["create"] is False


def test_new_creating_a_fresh_branch_never_prompts(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    switch_calls = _stub_switch(monkeypatch, branch_exists=False)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "fresh-branch"])

    assert result.exit_code == 0
    assert "already exists" not in result.output
    assert len(switch_calls) == 1
    assert switch_calls[0]["create"] is True


def test_new_resolves_the_actual_default_branch_as_base_when_creating(tmp_path, monkeypatch):
    """Without an explicit `--base`, `cmd_new` must resolve the repo's
    *actual* default branch itself (repo.default_branch) rather than
    leaving the base unset and trusting `wt`'s own (cacheable, and so
    potentially stale) default-branch detection.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    switch_calls = _stub_switch(monkeypatch, branch_exists=False)
    monkeypatch.setattr(repo, "default_branch", lambda _repo: "master")

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "fresh-branch"])

    assert result.exit_code == 0, result.output
    assert switch_calls[0]["base"] == "master"


def test_new_explicit_base_wins_over_the_resolved_default_branch(tmp_path, monkeypatch):
    """An explicit `--base` must be passed through as-is, never overridden
    by the freshly resolved default branch.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    switch_calls = _stub_switch(monkeypatch, branch_exists=False)
    monkeypatch.setattr(repo, "default_branch", lambda _repo: "master")

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "fresh-branch", "--base", "develop"])

    assert result.exit_code == 0, result.output
    assert switch_calls[0]["base"] == "develop"


def test_new_skips_default_branch_resolution_when_reusing(tmp_path, monkeypatch):
    """`--base` is meaningless (and `wt` warns + ignores it) when switching
    to a branch that already exists, so `cmd_new` shouldn't even bother
    resolving one in that case.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    switch_calls = _stub_switch(monkeypatch, branch_exists=True)
    resolved: list[Path] = []
    monkeypatch.setattr(repo, "default_branch", lambda repo_root: resolved.append(repo_root) or "master")

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "existing-branch", "--yes"])

    assert result.exit_code == 0, result.output
    assert resolved == []
    assert switch_calls[0]["base"] is None


def test_new_reports_the_resolved_base_branch_it_forked_from(tmp_path, monkeypatch):
    """`wt switch --create`'s JSON reply carries `base_branch`; surface it
    in the confirmation message so what a new worktree actually forked
    from is visible at a glance, not something you have to dig into `git
    log` to double-check.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(
        wt,
        "switch",
        lambda repo, branch, **kwargs: {
            "action": "created",
            "path": str(tmp_path / "new-worktree"),
            "base_branch": "master",
        },
    )

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "fresh-branch"])

    assert result.exit_code == 0, result.output
    assert "from master" in result.output


def test_new_skips_the_worktree_preview_when_branch_is_given(tmp_path, monkeypatch):
    """The 'already in flight' preview exists to inform the interactive
    branch-description prompt. With --branch the name is already decided, so
    the preview's `wt list` subprocess is pure latency (scripted callers like
    jira-worktree pass --branch on every call) and must not run at all.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    _stub_switch(monkeypatch, branch_exists=False)
    list_calls: list[Path] = []
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: list_calls.append(_repo) or [])

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "fresh-branch"])

    assert result.exit_code == 0, result.output
    assert list_calls == []


def test_new_shows_the_worktree_preview_before_prompting_interactively(tmp_path, monkeypatch):
    """Without --branch, `cmd_new` prompts for a branch description, and the
    preview of what's already in flight must still run first, so the user can
    avoid accidentally starting a near-duplicate of existing work.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    _stub_switch(monkeypatch, branch_exists=False)
    list_calls: list[Path] = []
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: list_calls.append(_repo) or [])

    result = runner.invoke(app, ["new", str(repo_dir)], input="\n")

    assert result.exit_code == 0, result.output
    assert len(list_calls) == 1


def _collision_error(branch: str, path: Path, occupying: str) -> wt.WtCommandError:
    """The WtCommandError a streamed `wt switch` raises when BRANCH's
    worktree path is occupied by a worktree on another branch: `wt`'s own
    two-line message, already shown live (streamed) and carried on the
    exception for inspection.

    The SGR color codes are deliberate: the streaming path forces wt's
    colors on a terminal, so its real message arrives wrapped in them and
    the recovery's message parsing must see through them.
    """
    stderr = (
        f"\x1b[31m✗\x1b[39m \x1b[31mCannot switch to \x1b[1m{branch}\x1b[22m — there's a worktree"
        f" at the expected path \x1b[1m{path}\x1b[22m on branch \x1b[1m{occupying}\x1b[22m\x1b[39m\n"
        f"\x1b[2m↳\x1b[22m \x1b[2mTo switch the worktree at \x1b[4m{path}\x1b[24m to \x1b[4m{branch}\x1b[24m,"
        f" run \x1b[4mcd {path} && git switch {branch}\x1b[24m\x1b[22m\n"
    )
    return wt.WtCommandError(["switch", branch], 1, stderr, streamed=True)


def _stub_occupied_switch(monkeypatch, occupier: dict[str, Any]) -> list[dict[str, Any]]:
    """A `wt.switch` stub that fails once with the occupied-path error for
    OCCUPIER's path, then succeeds (as it would after the in-place remedy)."""
    switch_calls: list[dict[str, Any]] = []

    def fake_switch(repo_root, branch, **kwargs):
        switch_calls.append({"repo": repo_root, "branch": branch, **kwargs})
        if len(switch_calls) == 1:
            raise _collision_error(branch, Path(occupier["path"]), occupier["branch"])
        return {"action": "existing", "branch": branch, "path": occupier["path"]}

    monkeypatch.setattr(wt, "switch", fake_switch)
    return switch_calls


def _stub_git_switch(monkeypatch) -> list[dict[str, Any]]:
    git_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        git,
        "switch_in_place",
        lambda path, branch, **kwargs: (
            git_calls.append({"path": path, "branch": branch, **kwargs}) or f"Switched to branch '{branch}'"
        ),
    )
    return git_calls


def test_new_recovers_an_occupied_worktree_path(tmp_path, monkeypatch):
    """When BRANCH's worktree path is occupied by a worktree on another
    branch (a directory created for BRANCH earlier, later `git switch`ed
    away by hand), `wt switch` refuses and names the remedy: switch that
    worktree in place. coppice offers to run it, then retries the switch."""
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    occupier = _entry("feature/other", tmp_path / "occupied" / "repo", dirty=True)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: True)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [occupier])
    switch_calls = _stub_occupied_switch(monkeypatch, occupier)
    git_calls = _stub_git_switch(monkeypatch)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "review-jamie", "--yes"], input="y")

    assert result.exit_code == 0, result.output
    assert git_calls == [{"path": Path(occupier["path"]), "branch": "review-jamie", "create": False, "base": None}]
    assert len(switch_calls) == 2
    assert switch_calls[1].get("create", False) is False
    assert "dirty" in result.output
    assert "Reused worktree" in result.output


def test_new_occupied_path_prompt_declined_cancels(tmp_path, monkeypatch):
    """Declining the remedy cancels the command: the occupying worktree is
    left on its branch, and no retry happens."""
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    occupier = _entry("feature/other", tmp_path / "occupied" / "repo")
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: True)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [occupier])
    switch_calls = _stub_occupied_switch(monkeypatch, occupier)
    git_calls = _stub_git_switch(monkeypatch)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "review-jamie", "--yes"], input="n")

    assert result.exit_code != 0
    assert "Cancelled" in result.output
    assert git_calls == []
    assert len(switch_calls) == 1


def test_new_occupied_path_recovery_creates_the_branch_when_creating(tmp_path, monkeypatch):
    """The same collision can hit a brand-new branch (its directory left
    behind, checked out on another branch, after the branch itself was
    deleted). The in-place remedy then has to fork the branch too:
    `git switch -c BRANCH BASE`."""
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    occupier = _entry("feature/other", tmp_path / "occupied" / "repo")
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [occupier])
    monkeypatch.setattr(repo, "default_branch", lambda _repo: "master")
    switch_calls = _stub_occupied_switch(monkeypatch, occupier)
    git_calls = _stub_git_switch(monkeypatch)
    # The in-place switch creates the branch, so once it ran, the branch
    # exists: the retry must not pass --create (wt refuses to create an
    # existing branch).
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: bool(git_calls))

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "review-jamie"], input="y")

    assert result.exit_code == 0, result.output
    assert git_calls == [{"path": Path(occupier["path"]), "branch": "review-jamie", "create": True, "base": "master"}]
    assert switch_calls[0]["create"] is True
    assert switch_calls[0]["base"] == "master"
    assert len(switch_calls) == 2
    assert switch_calls[1]["create"] is False


def _relocate_target(occupier_path: Path) -> Path:
    """The expected path a relocate would move OCCUPIER_PATH to: a sibling
    directory named after the occupier's own branch."""
    return occupier_path.parent.parent / "feature-other" / occupier_path.name


def _stub_relocate(monkeypatch, occupier: dict[str, Any], *, targets: list[dict[str, str]] | None = None):
    """Stub `wt`'s relocate pair for OCCUPIER: the dry-run preview returns
    TARGETS (by default one move to a fresh expected path), and the real
    run records its call."""
    occupier_path = Path(occupier["path"])
    if targets is None:
        targets = [{"from": str(occupier_path), "to": str(_relocate_target(occupier_path))}]
    monkeypatch.setattr(wt, "relocate_preview", lambda _repo, _branch: targets)
    relocate_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        wt,
        "relocate",
        lambda repo_root, branch, targets_: relocate_calls.append(
            {"repo": repo_root, "branch": branch, "targets": targets_}
        ),
    )
    return relocate_calls


def test_new_relocates_a_mismatched_occupier(tmp_path, monkeypatch):
    """When the occupier is itself at the wrong path (a
    branch_worktree_mismatch), the remedy that keeps both worktrees is
    relocating it to its own expected path, not evicting it: the occupier
    keeps a worktree, and BRANCH's freed path gets a fresh one on the
    retried switch."""
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    occupier = _entry("feature/other", tmp_path / "occupied" / "repo", mismatch=True)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: True)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [occupier])
    switch_calls = _stub_occupied_switch(monkeypatch, occupier)
    git_calls = _stub_git_switch(monkeypatch)
    relocate_calls = _stub_relocate(monkeypatch, occupier)

    result = runner.invoke(
        app, ["new", str(repo_dir), "--branch", "review-jamie", "--yes"], input="y", env={"COLUMNS": "160"}
    )

    assert result.exit_code == 0, result.output
    assert "Relocate 'feature/other'" in result.output
    assert len(relocate_calls) == 1
    assert relocate_calls[0]["branch"] == "feature/other"
    assert git_calls == []  # no in-place switch: the occupier moved away whole
    assert len(switch_calls) == 2
    assert switch_calls[1]["create"] is False
    assert "Reused worktree" in result.output


def test_new_relocate_declined_cancels(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    occupier = _entry("feature/other", tmp_path / "occupied" / "repo", mismatch=True)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: True)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [occupier])
    switch_calls = _stub_occupied_switch(monkeypatch, occupier)
    relocate_calls = _stub_relocate(monkeypatch, occupier)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "review-jamie", "--yes"], input="n")

    assert result.exit_code != 0
    assert "Cancelled" in result.output
    assert relocate_calls == []
    assert len(switch_calls) == 1


def test_new_relocate_retry_still_creates_a_brand_new_branch(tmp_path, monkeypatch):
    """A relocate frees the path but creates nothing, so when the occupied
    path blocked a brand-new branch, the retried switch still needs
    --create (and its resolved --base)."""
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    occupier = _entry("feature/other", tmp_path / "occupied" / "repo", mismatch=True)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [occupier])
    monkeypatch.setattr(repo, "default_branch", lambda _repo: "master")
    switch_calls = _stub_occupied_switch(monkeypatch, occupier)
    relocate_calls = _stub_relocate(monkeypatch, occupier)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "review-jamie"], input="y")

    assert result.exit_code == 0, result.output
    assert len(relocate_calls) == 1
    assert len(switch_calls) == 2
    assert switch_calls[1]["create"] is True
    assert switch_calls[1]["base"] == "master"


def test_new_falls_back_to_switch_in_place_when_relocate_cant_move_it(tmp_path, monkeypatch):
    """A mismatched occupier `wt` can't relocate (locked, detached, blocked
    target: the dry-run preview comes back empty) still gets the in-place
    switch offer, the only remedy left."""
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    occupier = _entry("feature/other", tmp_path / "occupied" / "repo", mismatch=True)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: True)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [occupier])
    switch_calls = _stub_occupied_switch(monkeypatch, occupier)
    git_calls = _stub_git_switch(monkeypatch)
    relocate_calls = _stub_relocate(monkeypatch, occupier, targets=[])

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "review-jamie", "--yes"], input="y")

    assert result.exit_code == 0, result.output
    assert relocate_calls == []
    assert "Switch the worktree at" in result.output.replace("\n", "")
    assert len(git_calls) == 1
    assert len(switch_calls) == 2


def test_new_occupied_path_without_a_matching_worktree_falls_back(tmp_path, monkeypatch):
    """The message parse is cross-checked against `wt list`'s structured
    data: if no registered worktree sits at the parsed path there is no
    safe remedy to offer, and the failure falls back to a plain exit."""
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    occupier = _entry("feature/other", tmp_path / "occupied" / "repo")
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: True)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [])
    switch_calls = _stub_occupied_switch(monkeypatch, occupier)
    git_calls = _stub_git_switch(monkeypatch)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "review-jamie", "--yes"], input="")

    assert result.exit_code != 0
    assert "Cancelled" not in result.output
    assert git_calls == []
    assert len(switch_calls) == 1


def test_new_other_streamed_switch_failures_exit_quietly(tmp_path, monkeypatch):
    """A streamed `wt switch` failure already put `wt`'s own message on the
    terminal live; coppice exits 1 without piling a redundant 'Error: wt
    switch ... exited 1' line (or a re-print of the message) on top of it."""
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: True)
    monkeypatch.setattr(wt, "remote_branch_exists", lambda _repo, _branch: False)

    def fake_switch(repo_root, branch, **kwargs):
        raise wt.WtCommandError(["switch", branch], 1, "some other wt error\n", streamed=True)

    monkeypatch.setattr(wt, "switch", fake_switch)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "review-jamie", "--yes"], input="")

    assert result.exit_code != 0
    assert "Error:" not in result.output
    assert "some other wt error" not in result.output


def test_list_without_wt_fails_clearly(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["list", str(repo_dir)])

    assert result.exit_code != 0
    assert "wt" in result.output
    assert "worktrunk.dev" in result.output
    assert "Traceback" not in result.output


def test_list_flags_stale_worktrees(tmp_path, monkeypatch):
    """A prunable (dangling) worktree reference must stand out in `list`,
    not blend in as if it were just another clean, healthy worktree: its
    'Working tree' cell should read '-' (there's no directory left to be
    clean or dirty), and the run's summary should call out the stale count
    by name so it's obvious 'coppice clean' has something to do.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("stale-branch", tmp_path / "gone", stale=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["list", str(repo_dir), "--no-size"])

    assert result.exit_code == 0, result.output
    assert "stale-branch" in result.output
    assert "stale" in result.output
    flat_output = result.output.replace("\n", "")
    assert "1 stale (dangling) reference(s)" in flat_output
    assert "remove" in flat_output
    # no misleading 'clean' working-tree label for a gone directory (the
    # only legitimate mention of 'clean' on the page is the "run 'coppice
    # clean'" tip)
    assert "clean" not in flat_output.replace("copclean", "").replace("cop clean", "")


def test_list_flags_branch_path_mismatch(tmp_path, monkeypatch):
    """A worktree sitting at another branch's path (created for that
    branch, later `git switch`ed by hand) is keyed by its checked-out
    branch, so the path's own branch looks like it has no worktree at all.
    The row must name the path's branch segment, or there is no way to see
    where that branch's worktree path went."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("feature/other", tmp_path / "review-jamie" / "repo", mismatch=True),
        _entry("plain", tmp_path / "plain" / "repo"),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["list", str(repo_dir), "--no-size"], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")
    assert "feature/other" in flat
    assert "@ review-jamie/" in flat
    # a worktree at its rightful path gets no such flag
    assert "@ plain/" not in flat


def test_list_json_emits_valid_json(tmp_path, monkeypatch):
    """`list --json` pipes into jq & co., so stdout must be parseable JSON
    even when a field is longer than the console width: Rich soft-wraps at
    80 columns when stdout isn't a tty, which used to corrupt string values
    with literal newlines.
    """
    import json

    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(repo, "scope_repos", lambda _path: [repo_dir])
    entries = [_entry("main", repo_dir, is_main=True), _entry("feature", tmp_path / "feature")]
    for e in entries:
        e["commit"] = {"sha": "abc", "message": "x" * 200, "timestamp": 1}
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert {e["repo"] for e in data} == {"repo"}
    assert {e["branch"] for e in data} == {"main", "feature"}


def test_list_hides_repos_without_extra_worktrees_by_default(tmp_path, monkeypatch):
    """With several repos in scope, ones with no extra worktrees are noise
    next to ones that do: hidden by default (rolled up into a closing
    line), shown with --all.
    """
    busy = _init_repo(tmp_path / "busy")
    quiet = _init_repo(tmp_path / "quiet")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(repo, "scope_repos", lambda _path: [busy, quiet])
    entries = {
        busy: [_entry("main", busy, is_main=True), _entry("feature", tmp_path / "feature")],
        quiet: [_entry("main", quiet, is_main=True)],
    }
    monkeypatch.setattr(wt, "list_worktrees", lambda r: entries[r])

    result = runner.invoke(app, ["list", "--no-size"], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "busy" in result.output
    assert "feature" in result.output
    assert "quiet" not in result.output
    assert "1 more repo with no extra worktrees" in result.output
    assert "--all" in result.output

    result_all = runner.invoke(app, ["list", "--no-size", "--all"], env={"COLUMNS": "160"})
    assert result_all.exit_code == 0, result_all.output
    assert "quiet" in result_all.output
    assert "no extra worktrees" in result_all.output


def test_list_summary_pluralizes_singular(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("main", repo_dir, is_main=True), _entry("feature", tmp_path / "feature")]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["list", str(repo_dir), "--no-size"])

    assert result.exit_code == 0, result.output
    assert "1 worktree in 1 repo." in result.output
    assert "worktree(s)" not in result.output


def test_list_empty_state_when_no_worktrees_anywhere(tmp_path, monkeypatch):
    """Nothing anywhere (and repos hidden by default) should read as a
    friendly empty state, not an empty table."""
    repo_a = _init_repo(tmp_path / "repo-a")
    repo_b = _init_repo(tmp_path / "repo-b")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(repo, "scope_repos", lambda _path: [repo_a, repo_b])
    monkeypatch.setattr(wt, "list_worktrees", lambda r: [_entry("main", r, is_main=True)])

    result = runner.invoke(app, ["list", "--no-size"])

    assert result.exit_code == 0, result.output
    assert "No worktrees across 2 repos." in result.output
    assert "cop new" in result.output


def test_list_verbose_shows_paths(tmp_path, monkeypatch):
    """--verbose answers 'where is it': a Path column per worktree, and the
    repo's own path back in the section heading."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("main", repo_dir, is_main=True), _entry("feature", tmp_path / "feature")]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["list", str(repo_dir), "--no-size", "--verbose"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "Path" in result.output
    flat_output = result.output.replace("\n", "")
    assert cli._short_path(tmp_path / "feature", max_len=40) in flat_output
    assert cli._short_path(repo_dir, max_len=40) in flat_output


def test_remove_without_wt_fails_clearly(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["remove", "some-branch", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "wt" in result.output
    assert "worktrunk.dev" in result.output
    assert "Traceback" not in result.output


def test_clean_without_wt_fails_clearly(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "wt" in result.output
    assert "worktrunk.dev" in result.output
    assert "Traceback" not in result.output


def test_clean_dry_run_categorizes_candidates(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch, which={"gh": "/usr/bin/gh"})
    # Force the commit-timestamp fallback everywhere instead of filesystem
    # birth time, so age is deterministic across OSes (tmp_path dirs are
    # freshly created "now", which would otherwise always read as 0d old).
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)

    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)

    old_seconds = 30 * 86400  # older than the 14-day default threshold
    young_seconds = 3 * 86400
    entries = [
        _entry("young-branch", tmp_path / "young", commit_ts=now - young_seconds),
        _entry("dirty-branch", tmp_path / "dirty", commit_ts=now - old_seconds, dirty=True),
        _entry("pr-branch", tmp_path / "pr", commit_ts=now - old_seconds),
        _entry("mergeable-branch", tmp_path / "mergeable", commit_ts=now - old_seconds, main_state="integrated"),
        _entry("unmerged-branch", tmp_path / "unmerged", commit_ts=now - old_seconds, main_state="ahead"),
        _entry("conflict-branch", tmp_path / "conflict", commit_ts=now - old_seconds, main_state="would_conflict"),
        _entry("stale-branch", tmp_path / "stale", stale=True),
        _entry("main", tmp_path / "repo", is_main=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(
        gh, "open_prs", lambda _repo, branches: {"pr-branch": "#42 Some PR"} if "pr-branch" in branches else {}
    )

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--dry-run", "--verbose"])

    assert result.exit_code == 0, result.output
    assert "young-branch" in result.output and "younger than 14d" in result.output
    assert "dirty-branch" in result.output and "uncommitted changes" in result.output
    assert "pr-branch" in result.output and "open PR #42 Some PR" in result.output
    assert "mergeable-branch" in result.output and "merged" in result.output
    assert "unmerged-branch" in result.output and "unmerged" in result.output
    # a would_conflict branch is still removable by age, but the preview
    # must say so in wt's vocabulary, not "merge status unknown"
    assert "conflict-branch" in result.output and "would conflict" in result.output
    assert "stale-branch" in result.output and "stale" in result.output
    assert "main" not in result.output.replace("Scanned", "").replace("remain", "")
    assert "Dry run, nothing removed." in result.output
    # 4 removable: mergeable-branch, unmerged-branch, conflict-branch, stale-branch.
    assert "4 removable" in result.output


def test_clean_marks_candidates_with_an_open_vscode_window(tmp_path, monkeypatch):
    """`clean` deletes worktree directories in batch, so its listing gets
    the same marker + note as `remove` when a window has a candidate open."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)
    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)
    entries = [
        _entry("mergeable-branch", tmp_path / "mergeable", commit_ts=now - 30 * 86400, main_state="integrated"),
        _entry("main", tmp_path / "repo", is_main=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(vscode, "registry_window_folders", lambda: [[str(tmp_path / "mergeable")]])

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "mergeable-branch" in result.output and "(VS Code window open)" in result.output
    assert "Close the marked windows first" in result.output


def test_clean_nothing_to_clean(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)
    monkeypatch.setattr(cli.time, "time", lambda: 2_000_000_000.0)
    monkeypatch.setattr(
        wt, "list_worktrees", lambda _repo: [_entry("young", tmp_path / "young", commit_ts=2_000_000_000.0)]
    )

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Nothing to clean." in result.output


def test_clean_removes_branchless_stale_worktrees(tmp_path, monkeypatch):
    """A stale (prunable) entry can be detached, i.e. have no branch at all
    (`wt list` reports `"branch": null` and `list` shows it as '?'). It must
    still be an unconditional `clean` candidate: it used to be filtered out
    for lacking a branch before the stale check ever ran, so `clean`
    reported 'Nothing to clean' while `list` kept nudging to run it. With
    no branch to hand to `wt remove` (which also refuses worktrees whose
    directory is already gone), the dangling reference is pruned by path
    with git instead.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry(None, tmp_path / "gone", stale=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    remove_calls: list[Any] = []
    prune_calls: list[Any] = []
    monkeypatch.setattr(wt, "remove", lambda *a, **k: remove_calls.append((a, k)))
    monkeypatch.setattr(wt, "prune_stale", lambda *a, **k: prune_calls.append((a, k)))

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--yes"])

    assert result.exit_code == 0, result.output
    assert "1 stale (dangling) reference(s)" in result.output
    assert "Removed 1 worktree." in result.output
    assert remove_calls == []
    assert prune_calls == [((repo_dir, str(tmp_path / "gone")), {})]


def test_clean_merged_ignores_age(tmp_path, monkeypatch):
    """--merged sweeps up every merged worktree regardless of DAYS, but still
    leaves young-but-unmerged branches alone.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)

    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)
    young_seconds = 1 * 86400

    entries = [
        _entry("young-merged", tmp_path / "young-merged", commit_ts=now - young_seconds, main_state="integrated"),
        _entry("young-unmerged", tmp_path / "young-unmerged", commit_ts=now - young_seconds, main_state="ahead"),
        _entry("main", tmp_path / "repo", is_main=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--merged", "--dry-run", "--verbose"])

    assert result.exit_code == 0, result.output
    assert "young-merged" in result.output and "merged, branch will be deleted" in result.output
    assert "young-unmerged" in result.output and "not merged" in result.output
    assert "1 removable" in result.output


def test_clean_merged_removable_set(tmp_path, monkeypatch):
    """--merged's removable set is the whole merged bucket: `behind` loses
    no committed work, and a clean `same_commit` equals `empty`. `diverged`
    and `would_conflict` always stay kept, and a dirty `same_commit` is
    still protected by the dirty skip, not by the merge filter.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)

    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)
    old_seconds = 30 * 86400

    entries = [
        _entry("behind-main", tmp_path / "behind", commit_ts=now - old_seconds, main_state="behind"),
        _entry("at-main", tmp_path / "same", commit_ts=now - old_seconds, main_state="same_commit"),
        _entry(
            "dirty-at-main", tmp_path / "dirty-same", commit_ts=now - old_seconds, main_state="same_commit", dirty=True
        ),
        _entry("diverged-branch", tmp_path / "diverged", commit_ts=now - old_seconds, main_state="diverged"),
        _entry("conflict-branch", tmp_path / "conflict", commit_ts=now - old_seconds, main_state="would_conflict"),
        _entry("main", tmp_path / "repo", is_main=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--merged", "--dry-run", "--verbose"])

    assert result.exit_code == 0, result.output
    assert "behind-main" in result.output and "merged, branch will be deleted" in result.output
    assert "at-main" in result.output
    assert "dirty-at-main" in result.output and "uncommitted changes" in result.output
    assert "diverged-branch" in result.output and "not merged" in result.output
    assert "conflict-branch" in result.output and "not merged" in result.output
    # 2 removable: behind-main, at-main.
    assert "2 removable" in result.output


def test_merge_status_surfaces_full_main_state_vocabulary(tmp_path):
    """The list table's Merge column maps wt's whole `main_state`
    vocabulary onto four buckets, instead of dropping six of nine states
    into a fallback 'unknown'."""
    cases = {
        "empty": ("merged", "green"),
        "integrated": ("merged", "green"),
        "same_commit": ("merged", "green"),
        "behind": ("merged", "green"),
        "ahead": ("unmerged", "cyan"),
        "diverged": ("unmerged", "cyan"),
        "would_conflict": ("conflict", "red"),
        "orphan": ("unknown", "dim"),
        "some_future_state": ("unknown", "dim"),
    }
    for main_state, expected in cases.items():
        entry = _entry("branch", tmp_path / main_state, main_state=main_state)
        assert cli._merge_status(entry) == expected, f"main_state={main_state}"

    # absent main_state (an unremarkable up-to-date branch) is unknown too
    entry = _entry("branch", tmp_path / "absent")
    del entry["main_state"]
    assert cli._merge_status(entry) == ("unknown", "dim")


def test_merge_label_speaks_the_same_vocabulary(tmp_path):
    """`clean`'s removal preview uses the same buckets as the list table:
    a conflict reads as 'unmerged (would conflict)', an invitation to merge
    or rebase, never today's 'merge status unknown'."""

    def label(main_state: str, *, force_delete: bool = False) -> str:
        entry = _entry("branch", tmp_path / f"{main_state}-{force_delete}", main_state=main_state)
        return cli._merge_label(entry, force_delete=force_delete)

    assert label("integrated") == "merged, branch will be deleted"
    assert label("behind") == "merged, branch will be deleted"
    assert label("ahead") == "unmerged, branch will be kept"
    assert label("diverged") == "unmerged, branch will be kept"
    assert label("would_conflict") == "unmerged (would conflict), branch will be kept"
    assert label("orphan") == "merge status unknown, branch will be kept"
    assert label("would_conflict", force_delete=True) == "unmerged (would conflict), -D will delete the branch too"
    assert label("ahead", force_delete=True) == "unmerged, -D will delete the branch too"


def test_remove_no_branch_no_candidates(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [])

    result = runner.invoke(app, ["remove", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "no removable worktrees in scope" in result.output


def test_remove_partial_failure_reports_both_counts(tmp_path, monkeypatch):
    """On a partial failure, the summary must say how many succeeded, not
    just list what failed (previously it only reported the failures).
    """
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(wt, "remove", lambda *a, **k: None)

    result = runner.invoke(app, ["remove", "good-branch", "missing-branch", "--repo", str(repo_dir), "--yes"])

    assert result.exit_code != 0
    assert "Removed 1 worktree, 1 failed" in result.output
    assert "missing-branch" in result.output


def test_remove_without_yes_prompts_and_cancels_on_no(tmp_path, monkeypatch):
    """Without --yes, `remove` must ask for confirmation itself rather than
    relying on `wt remove`'s own prompt: `wt.run` captures stdout/stderr, so
    `wt` treats the call as non-interactive and never actually prompts,
    which would otherwise remove worktrees with no confirmation at all.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    remove_calls: list[Any] = []
    monkeypatch.setattr(wt, "remove", lambda *a, **k: remove_calls.append((a, k)))

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir)], input="n\n")

    assert result.exit_code != 0
    assert "Remove the 1 worktree listed above?" in result.output
    assert "Branches survive unless already merged." in result.output
    assert "Cancelled." in result.output
    assert remove_calls == []


def test_remove_without_yes_prompts_and_proceeds_on_yes(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    remove_calls: list[Any] = []
    monkeypatch.setattr(wt, "remove", lambda *a, **k: remove_calls.append((a, k)))

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir)], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Removed 1 worktree." in result.output
    assert len(remove_calls) == 1
    # Confirmed once at the coppice level, so `wt remove` is always told
    # `-y` too rather than relying on its own (non-functional, here) prompt.
    assert remove_calls[0][1]["yes"] is True


def test_remove_marks_a_target_with_an_open_vscode_window(tmp_path, monkeypatch):
    """A worktree a VS Code window has open gets marked in remove's
    confirmation listing, with the close-it-first note: deleting the
    directory out from under the window strands it. The window registry
    is the source: a fresh entry whose folder is the worktree (or inside
    it) marks the line."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(wt, "remove", lambda *a, **k: None)
    monkeypatch.setattr(vscode, "registry_window_folders", lambda: [[str(tmp_path / "good")]])

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir)], input="n\n")

    assert "(VS Code window open)" in result.output
    assert "Close the marked windows first" in result.output


def test_remove_registry_match_respects_path_element_boundaries(tmp_path, monkeypatch):
    """A window open on /w/good-old is not a window on /w/good: the
    registry match is path containment, not a string prefix."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(wt, "remove", lambda *a, **k: None)
    monkeypatch.setattr(vscode, "registry_window_folders", lambda: [[str(tmp_path / "good-old")]])

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir)], input="n\n")

    assert "VS Code window open" not in result.output


def test_remove_stays_silent_when_windows_cannot_be_listed(tmp_path, monkeypatch):
    """An unreadable registry (extension not installed) is 'can't tell':
    no marker, no note, rather than a claim of 'not open'."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(wt, "remove", lambda *a, **k: None)
    monkeypatch.setattr(vscode, "registry_window_folders", lambda: None)

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir)], input="n\n")

    assert "VS Code window open" not in result.output


def test_remove_confirm_needs_no_enter(tmp_path, monkeypatch):
    """Same single-keypress convention as `new`'s prompt (see
    test_new_confirm_needs_no_enter), here on a destructive one."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    remove_calls: list[Any] = []
    monkeypatch.setattr(wt, "remove", lambda *a, **k: remove_calls.append((a, k)))

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir)], input="y")

    assert result.exit_code == 0, result.output
    assert len(remove_calls) == 1


def test_remove_confirm_swallows_other_keys(tmp_path, monkeypatch):
    """Keys that mean nothing change nothing: the prompt waits them out,
    and the EOF they leave behind cancels rather than confirming."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    remove_calls: list[Any] = []
    monkeypatch.setattr(wt, "remove", lambda *a, **k: remove_calls.append((a, k)))

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir)], input="xq")

    assert result.exit_code != 0
    assert "Cancelled." in result.output
    assert remove_calls == []


def test_remove_force_delete_prompt_warns_about_branch_deletion(tmp_path, monkeypatch):
    """The prompt's consequence sentence names whatever the flags in play
    make unrecoverable: with -D, unmerged branches die with the worktree."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("good-branch", tmp_path / "good", commit_ts=0)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(wt, "remove", lambda *a, **k: None)

    result = runner.invoke(app, ["remove", "good-branch", "--repo", str(repo_dir), "-D"], input="n")

    assert result.exit_code != 0
    assert "Unmerged branches are deleted too." in result.output
    assert "Cancelled." in result.output


def test_clean_without_yes_prompts_and_cancels(tmp_path, monkeypatch):
    """`clean`'s bulk prompt follows the same template and key semantics as
    `remove`'s: one keypress, enter/n/esc cancel, consequence sentence
    included."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)
    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)
    entries = [_entry("old-branch", tmp_path / "old", commit_ts=now - 30 * 86400)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    remove_calls: list[Any] = []
    monkeypatch.setattr(wt, "remove", lambda *a, **k: remove_calls.append((a, k)))

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir)], input="n")

    assert result.exit_code != 0
    assert "Remove the 1 worktree listed above?" in result.output
    assert "Cancelled." in result.output
    assert remove_calls == []


def test_remove_no_branch_falls_back_without_fzf(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch, which={"fzf": None})
    entries = [
        _entry("feature-a", tmp_path / "feature-a", commit_ts=0),
        _entry("feature-b", tmp_path / "feature-b", commit_ts=0, dirty=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)

    result = runner.invoke(app, ["remove", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "fzf isn't installed" in result.output
    assert "feature-a" in result.output
    assert "feature-b" in result.output and "dirty" in result.output
    assert "Re-run: coppice remove BRANCH" in result.output


# --- park/unpark ------------------------------------------------------------
#
# The parked mark itself is real (git config in the tmp repo, see
# tests/test_park.py for its own contracts); only `wt list` is stubbed, via
# the usual `wt.list_worktrees` monkeypatch.


def test_park_bare_inside_a_worktree_parks_its_branch(tmp_path, monkeypatch):
    """The common case: the task just finished and you're standing in the
    worktree, so bare `park` marks its branch with no arguments. The current
    worktree is detected by path (`git rev-parse --show-toplevel`), not `wt
    list`'s `is_current`: `wt` always runs with `-C repo_root`, so from its
    cwd the main worktree is the current one."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat")
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("feat", feat),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.chdir(feat)

    result = runner.invoke(app, ["park", "--repo", str(repo_dir)])

    assert result.exit_code == 0, result.output
    assert "Parked 'feat' @ repo." in result.output
    assert set(park.parked_at(repo_dir)) == {"feat"}


def test_park_bare_outside_a_worktree_falls_back_without_fzf(tmp_path, monkeypatch):
    """Standing in the main checkout (or anywhere with no current managed
    worktree), bare `park` opens the picker, same as `remove`, here with
    fzf missing, so the candidates print instead. An already-parked
    worktree is not a candidate."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch, which={"fzf": None})
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("active-branch", tmp_path / "active"),
        _entry("parked-branch", tmp_path / "parked"),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.chdir(repo_dir)  # standing in the main checkout: no current managed worktree
    park.park(repo_dir, "parked-branch", at=1_700_000_000.0)

    result = runner.invoke(app, ["park", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "fzf isn't installed" in result.output
    assert "active-branch" in result.output
    assert "parked-branch" not in result.output
    assert "Re-run: coppice park BRANCH" in result.output


def test_park_without_candidates_errors(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [_entry("main", repo_dir, is_main=True)])

    result = runner.invoke(app, ["park", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "no parkable worktrees in scope" in result.output


def test_parking_a_dirty_worktree_asks_first(tmp_path, monkeypatch):
    """Dirty and complete contradict each other, so parking a dirty
    worktree confirms first; declining parks nothing."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat")
    entries = [_entry("feat", feat, dirty=True)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.chdir(feat)

    result = runner.invoke(app, ["park", "--repo", str(repo_dir)], input="n\n")

    assert result.exit_code != 0
    assert "Park the 1 worktree listed above?" in result.output
    assert "Cancelled." in result.output
    assert park.parked_at(repo_dir) == {}


def test_parking_a_dirty_worktree_proceeds_on_yes(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat")
    entries = [_entry("feat", feat, dirty=True)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.chdir(feat)

    result = runner.invoke(app, ["park", "--repo", str(repo_dir)], input="y\n")

    assert result.exit_code == 0, result.output
    assert set(park.parked_at(repo_dir)) == {"feat"}


def test_park_yes_skips_the_dirty_prompt(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat")
    entries = [_entry("feat", feat, dirty=True)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.chdir(feat)

    result = runner.invoke(app, ["park", "--repo", str(repo_dir), "--yes"])

    assert result.exit_code == 0, result.output
    assert "Park the" not in result.output
    assert set(park.parked_at(repo_dir)) == {"feat"}


def test_park_explicit_branch_and_repark_refreshes(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("feat", tmp_path / "feat")]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    first = runner.invoke(app, ["park", "feat", "--repo", str(repo_dir)])
    assert first.exit_code == 0, first.output
    assert "Parked 'feat' @ repo." in first.output
    parked_ts = park.parked_at(repo_dir)["feat"]

    second = runner.invoke(app, ["park", "feat", "--repo", str(repo_dir)])
    assert second.exit_code == 0, second.output
    assert "Re-parked 'feat' @ repo (was parked" in second.output
    assert park.parked_at(repo_dir)["feat"] >= parked_ts


def test_park_unknown_branch_fails(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("feat", tmp_path / "feat")]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["park", "nope", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "no parkable worktree for branch 'nope'" in result.output


def test_park_stale_entry_is_not_parkable(tmp_path, monkeypatch):
    """A stale (dangling) reference's directory is already gone, there's
    nothing left to keep for follow-up, so there's nothing to park."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("gone", tmp_path / "gone", stale=True)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["park", "gone", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "no parkable worktree for branch 'gone'" in result.output


def test_unpark_removes_the_mark(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("feat", tmp_path / "feat")]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    park.park(repo_dir, "feat", at=1_000_000_000.0)

    result = runner.invoke(app, ["unpark", "feat", "--repo", str(repo_dir)])

    assert result.exit_code == 0, result.output
    assert "Unparked 'feat' @ repo (was parked" in result.output
    assert park.parked_at(repo_dir) == {}


def test_unpark_bare_inside_a_parked_worktree(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat")
    entries = [_entry("feat", feat)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.chdir(feat)
    park.park(repo_dir, "feat", at=1_000_000_000.0)

    result = runner.invoke(app, ["unpark", "--repo", str(repo_dir)])

    assert result.exit_code == 0, result.output
    assert "Unparked 'feat' @ repo" in result.output
    assert park.parked_at(repo_dir) == {}


def test_unpark_bare_in_an_unparked_worktree_opens_the_picker(tmp_path, monkeypatch):
    """The current worktree is only the unpark target when it's actually
    parked; otherwise the picker offers the parked ones."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch, which={"fzf": None})
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat")
    entries = [
        _entry("feat", feat),
        _entry("other", tmp_path / "other"),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.chdir(feat)
    park.park(repo_dir, "other", at=1_700_000_000.0)

    result = runner.invoke(app, ["unpark", "--repo", str(repo_dir)])

    assert result.exit_code != 0
    assert "fzf isn't installed" in result.output
    assert "other" in result.output
    assert "feat" not in result.output
    assert "Re-run: coppice unpark BRANCH" in result.output


def test_unpark_not_parked_is_a_note_not_an_error(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("feat", tmp_path / "feat")]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["unpark", "feat", "--repo", str(repo_dir)])

    assert result.exit_code == 0, result.output
    assert "'feat' @ repo is not parked." in result.output


def test_list_parked_rows_sort_last_and_show_the_mark(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)
    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("parked-branch", tmp_path / "parked", commit_ts=now - 20 * 86400),
        _entry("active-branch", tmp_path / "active", commit_ts=now - 5 * 86400),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    park.park(repo_dir, "parked-branch", at=now - 6 * 86400)

    result = runner.invoke(app, ["list", str(repo_dir), "--no-size"], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")
    assert "parked 6d" in flat
    # unparked first, parked after, within the repo section
    assert flat.index("active-branch") < flat.index("parked-branch")


def test_list_marks_follow_up_when_the_head_moved_since_parking(tmp_path, monkeypatch):
    """A branch head newer than the parked mark means follow-up already
    happened: the worktree reads as active again (no dimming, no 'parked'
    label) with a 'follow-up' note, and no writes, the mark is left alone."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)
    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("comeback", tmp_path / "comeback", commit_ts=now - 1 * 86400),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    park.park(repo_dir, "comeback", at=now - 10 * 86400)

    result = runner.invoke(app, ["list", str(repo_dir), "--no-size"], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")
    assert "follow-up" in flat
    assert "· parked" not in flat
    # read-time only: the mark itself is untouched
    assert park.parked_at(repo_dir) == {"comeback": now - 10 * 86400}


def test_list_json_includes_parked_at(tmp_path, monkeypatch):
    import json

    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(repo, "scope_repos", lambda _path: [repo_dir])
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat", tmp_path / "feat")]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    park.park(repo_dir, "feat", at=1_700_000_000.0)

    result = runner.invoke(app, ["list", "--json"])

    assert result.exit_code == 0, result.output
    by_branch = {e["branch"]: e for e in json.loads(result.stdout)}
    assert by_branch["feat"]["parked_at"] == 1_700_000_000.0
    assert "parked_at" not in by_branch["main"]


def test_clean_parked_sweeps_old_marks(tmp_path, monkeypatch):
    """--parked reinterprets DAYS as parked age (default 7): old-enough
    marks are removable and the preview shows the parked age per row, while
    recent marks, never-parked worktrees, and ones with follow-up since the
    mark (active again) stay kept, and the dirty/open-PR safety rails still
    apply."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch, which={"gh": "/usr/bin/gh"})
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)
    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)

    entries = [
        _entry("parked-old", tmp_path / "old", commit_ts=now - 30 * 86400),
        _entry("parked-recent", tmp_path / "recent", commit_ts=now - 30 * 86400),
        _entry("never-parked", tmp_path / "active", commit_ts=now - 30 * 86400),
        _entry("follow-up", tmp_path / "followup", commit_ts=now - 1 * 86400),
        _entry("parked-dirty", tmp_path / "dirty", commit_ts=now - 30 * 86400, dirty=True),
        _entry("parked-pr", tmp_path / "pr", commit_ts=now - 30 * 86400),
        _entry("main", repo_dir, is_main=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    for branch, age_days in [
        ("parked-old", 10),
        ("parked-recent", 3),
        ("follow-up", 10),
        ("parked-dirty", 10),
        ("parked-pr", 10),
    ]:
        park.park(repo_dir, branch, at=now - age_days * 86400)
    monkeypatch.setattr(
        gh, "open_prs", lambda _repo, branches: {"parked-pr": "#42 Some PR"} if "parked-pr" in branches else {}
    )

    result = runner.invoke(
        app, ["clean", "--repo", str(repo_dir), "--parked", "--dry-run", "--verbose"], env={"COLUMNS": "160"}
    )

    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")
    assert "parked at least 7d ago" in flat
    # the preview shows the parked age (10d, '1w'), not the worktree age (30d)
    assert "1w  parked-old" in flat
    assert "parked-recent" in flat and "parked less than 7d ago" in flat
    assert "never-parked" in flat and "not parked" in flat
    assert "follow-up" in flat and "follow-up since it was parked" in flat
    assert "parked-dirty" in flat and "uncommitted changes" in flat
    assert "parked-pr" in flat and "open PR #42 Some PR" in flat
    assert "1 removable" in flat
    assert "1 parked under 7d" in flat and "1 not parked" in flat and "1 with follow-up" in flat
    assert "Dry run, nothing removed." in flat


def test_clean_parked_explicit_days(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)
    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)
    entries = [_entry("parked-branch", tmp_path / "parked", commit_ts=now - 30 * 86400)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    park.park(repo_dir, "parked-branch", at=now - 5 * 86400)

    result = runner.invoke(app, ["clean", "3", "--repo", str(repo_dir), "--parked", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "parked at least 3d ago" in result.output
    assert "1 removable" in result.output


def test_clean_merged_and_parked_are_mutually_exclusive():
    result = runner.invoke(app, ["clean", "--merged", "--parked"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_remove_drops_the_parked_mark_when_the_branch_went_with_it(tmp_path, monkeypatch):
    """`wt remove` deletes the ref directly (update-ref -d), which, unlike
    `git branch -D`, leaves the branch's config section behind, so coppice
    drops the parked mark itself when the branch went with the worktree."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("feat", tmp_path / "feat")]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(wt, "remove", lambda *a, **k: None)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: False)
    park.park(repo_dir, "feat", at=1_700_000_000.0)

    result = runner.invoke(app, ["remove", "feat", "--repo", str(repo_dir), "--yes"])

    assert result.exit_code == 0, result.output
    assert park.parked_at(repo_dir) == {}


def test_remove_keeps_the_parked_mark_when_the_branch_survives(tmp_path, monkeypatch):
    """A kept branch keeps its mark: recreating its worktree later revives
    the parked state, which is exactly what the mark means."""
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    entries = [_entry("feat", tmp_path / "feat")]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(wt, "remove", lambda *a, **k: None)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: True)
    park.park(repo_dir, "feat", at=1_700_000_000.0)

    result = runner.invoke(app, ["remove", "feat", "--repo", str(repo_dir), "--yes"])

    assert result.exit_code == 0, result.output
    assert park.parked_at(repo_dir) == {"feat": 1_700_000_000.0}


def test_clean_drops_the_parked_mark_for_removed_branches(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli, "_creation_ts", lambda _path: None)
    now = 2_000_000_000.0
    monkeypatch.setattr(cli.time, "time", lambda: now)
    entries = [_entry("feat", tmp_path / "feat", commit_ts=now - 30 * 86400)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    monkeypatch.setattr(wt, "remove", lambda *a, **k: None)
    monkeypatch.setattr(wt, "branch_exists", lambda _repo, _branch: False)
    park.park(repo_dir, "feat", at=now - 10 * 86400)

    result = runner.invoke(app, ["clean", "--repo", str(repo_dir), "--parked", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Removed 1 worktree." in result.output
    assert park.parked_at(repo_dir) == {}


def test_status_reports_wt_and_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    repo_dir = _init_repo(tmp_path / "repo")
    repo.register_repo(repo_dir)
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "wt v9.9.9\n", ""))
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [{"branch": "main"}])

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    flat_output = result.output.replace("\n", "")
    assert "v9.9.9" in flat_output
    assert cli._short_path(repo_dir) in flat_output
    assert "1 worktree across 1 repo" in flat_output


def test_status_reports_stale_worktrees(tmp_path, monkeypatch):
    """A registered repo with a dangling worktree reference should be
    flagged in `status`'s Status column and rolled up into the final
    summary, not silently reported as 'ok' alongside genuinely healthy
    repos.
    """
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    repo_dir = _init_repo(tmp_path / "repo")
    repo.register_repo(repo_dir)
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "wt v9.9.9\n", ""))
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("stale-branch", tmp_path / "gone", stale=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    flat_output = result.output.replace("\n", "")
    assert "1 stale" in flat_output
    assert "1 stale (dangling) reference(s)" in flat_output
    assert "cop clean" in flat_output


def test_status_without_wt_still_lists_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    repo_dir = _init_repo(tmp_path / "repo")
    repo.register_repo(repo_dir)
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    flat_output = result.output.replace("\n", "")
    assert "not found" in flat_output
    assert cli._short_path(repo_dir) in flat_output


def test_status_prunes_missing_repos_from_registry(tmp_path, monkeypatch):
    """A registered repo whose directory is gone (e.g. a deleted scratch
    repo, or one a `wt` hook registered that later got cleaned up) shows as
    `missing` for this run, but `status` self-heals the registry so it
    doesn't show up on every subsequent run forever.
    """
    registry_path = tmp_path / "known-repos"
    monkeypatch.setattr(repo, "REGISTRY_PATH", registry_path)
    repo_dir = _init_repo(tmp_path / "repo")
    gone_dir = tmp_path / "gone"
    gone_dir.mkdir()
    repo.register_repo(repo_dir)
    repo.register_repo(gone_dir)
    gone_dir.rmdir()
    _stub_wt(monkeypatch)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "wt v9.9.9\n", ""))
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [{"branch": "main"}])

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    flat_output = result.output.replace("\n", "")
    assert "missing" in flat_output
    assert "Pruned 1 missing repo" in flat_output
    assert "across 1 repo" in flat_output  # only the live repo counted, not the pruned one
    assert repo.known_repos() == [repo_dir]


# --- sync -------------------------------------------------------------------
#
# Unlike the other commands' tests, sync's git layer (fetch, merge-tree,
# merge) runs for real against repos in tmp_path, with a local bare repo
# playing 'origin' (no network). Only `wt list` is stubbed, via the usual
# `wt.list_worktrees` monkeypatch, with entries pointing at real worktree
# directories created with plain `git worktree add`.


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _init_repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A repo cloned from a local bare 'origin', so sync's fetch and default
    branch resolution run for real. Returns (repo, origin)."""
    seed = _init_repo(tmp_path / "seed")
    (seed / "f.txt").write_text("base\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "base file")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    repo_dir = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo_dir)], check=True)
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")
    return repo_dir, origin


def _advance_origin(origin: Path, tmp_path: Path, *, filename: str = "f.txt", content: str = "day2") -> None:
    """Push one new commit to origin's main from a scratch clone, simulating
    the base branch moving while worktrees are being worked on."""
    other = tmp_path / "other"
    if not other.exists():
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
        _git(other, "config", "user.email", "test@example.com")
        _git(other, "config", "user.name", "Test")
    with open(other / filename, "a") as f:
        f.write(content + "\n")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", f"advance {filename}")
    _git(other, "push", "-q")


def _add_worktree(repo_dir: Path, path: Path, branch: str, *, with_commit: bool = True) -> Path:
    """A real worktree on a new branch, optionally with one commit of its own
    (a branch with no unique commits reads as already-integrated to git, which
    is not the state these tests exercise)."""
    subprocess.run(["git", "-C", str(repo_dir), "worktree", "add", "-q", "-b", branch, str(path)], check=True)
    if with_commit:
        (path / f"{branch}.txt").write_text(f"{branch} work\n")
        _git(path, "add", ".")
        _git(path, "commit", "-qm", f"{branch} work")
    return path


def _merge_count(path: Path) -> str:
    return _git(path, "rev-list", "--count", "--merges", "HEAD")


def _attention_part(output: str) -> str:
    """The output from the 'Needs attention:' heading on, the closing extract
    sync prints after the counts line, so assertions can tell 'listed as
    needing attention' apart from 'shown once in the report table above'."""
    assert "Needs attention:" in output
    return output.split("Needs attention:", 1)[1]


def test_sync_without_wt_fails_clearly(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    _hide_wt(monkeypatch)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    assert result.exit_code != 0
    assert "wt" in result.output
    assert "worktrunk.dev" in result.output
    assert "Traceback" not in result.output


def test_sync_merges_base_into_behind_worktree(tmp_path, monkeypatch):
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat-behind")
    _advance_origin(origin, tmp_path)
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat-behind", feat)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "synced" in result.output and "feat-behind" in result.output
    assert "1 commit from origin/main" in result.output
    # Exactly one merge commit, and origin/main is now an ancestor.
    assert _merge_count(feat) == "1"
    assert (
        subprocess.run(["git", "-C", str(feat), "merge-base", "--is-ancestor", "origin/main", "HEAD"]).returncode == 0
    )
    # The main worktree was fast-forwarded too (rolled up in the summary).
    assert "fast-forwarded 1 main checkout" in result.output
    # A fully successful run has no closing extract.
    assert "Needs attention" not in result.output
    # ...and the report is a list-style sectioned table.
    assert "Worktree" in result.output and "Result" in result.output and "Detail" in result.output
    assert "day2" in (repo_dir / "f.txt").read_text()


def test_sync_up_to_date_worktree_is_left_alone(tmp_path, monkeypatch):
    repo_dir, _origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat-current")
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat-current", feat)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "Everything is already up to date." in result.output
    assert "Needs attention" not in result.output
    assert _merge_count(feat) == "0"


def test_sync_is_idempotent_across_runs(tmp_path, monkeypatch):
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat-behind")
    _advance_origin(origin, tmp_path)
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat-behind", feat)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    first = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})
    assert first.exit_code == 0, first.output
    assert "synced" in first.output

    second = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})
    assert second.exit_code == 0, second.output
    assert "Everything is already up to date." in second.output
    assert _merge_count(feat) == "1"  # no second merge commit appeared


def test_sync_skips_dirty_worktrees(tmp_path, monkeypatch):
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat-dirty")
    _advance_origin(origin, tmp_path)
    # Dirtiness comes from wt's data, so the stub decides (the real worktree
    # staying clean is what lets the merge assertions below mean anything).
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat-dirty", feat, dirty=True)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "uncommitted changes" in result.output
    assert _merge_count(feat) == "0"


def test_sync_predicts_and_skips_conflicts(tmp_path, monkeypatch):
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat-conflict", with_commit=False)
    (feat / "c.txt").write_text("ours\n")
    _git(feat, "add", ".")
    _git(feat, "commit", "-qm", "feat c")
    # origin/main adds the same file with different content (add/add conflict).
    _advance_origin(origin, tmp_path, filename="c.txt", content="theirs")
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat-conflict", feat)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    # A conflict is a reported outcome, not a command failure.
    assert result.exit_code == 0, result.output
    assert "conflict" in result.output and "left untouched" in result.output
    assert "1 conflict" in result.output
    # The worktree is exactly as it was: clean, no merge in progress.
    assert _git(feat, "status", "--porcelain") == ""
    assert subprocess.run(["git", "-C", str(feat), "rev-parse", "-q", "--verify", "MERGE_HEAD"]).returncode != 0
    assert (feat / "c.txt").read_text() == "ours\n"


def test_sync_dry_run_changes_nothing(tmp_path, monkeypatch):
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat-behind")
    _advance_origin(origin, tmp_path)
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat-behind", feat)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir), "--dry-run"], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "would sync" in result.output
    assert "would fast-forward 1 main checkout" in result.output
    assert "Dry run, nothing changed." in result.output
    assert _merge_count(feat) == "0"
    assert "day2" not in (repo_dir / "f.txt").read_text()


def test_sync_no_main_leaves_the_main_worktree_alone(tmp_path, monkeypatch):
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat-behind")
    _advance_origin(origin, tmp_path)
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat-behind", feat)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir), "--no-main"], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "main worktree" not in result.output
    assert "day2" not in (repo_dir / "f.txt").read_text()
    # ...while the managed worktree still synced.
    assert "synced" in result.output
    assert _merge_count(feat) == "1"


def test_sync_skips_stale_and_detached_entries(tmp_path, monkeypatch):
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    _advance_origin(origin, tmp_path)
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("stale-branch", tmp_path / "gone", stale=True),
        _entry(None, tmp_path / "detached"),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "stale" in result.output
    assert "detached HEAD" in result.output
    assert "cop clean" in result.output
    # Both fold into the closing 'Needs attention' extract; there is no
    # separate stale summary line anymore.
    attention = _attention_part(result.output)
    assert "stale-branch" in attention and "detached HEAD" in attention
    assert "stale (dangling) reference(s)" not in result.output


def test_sync_needs_attention_lists_only_actionable_rows(tmp_path, monkeypatch):
    """The closing extract repeats exactly the rows that did not sync
    correctly (here: a predicted conflict and a dirty skip), and none of
    the fine ones (the synced worktree, the fast-forwarded main checkout)."""
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    behind = _add_worktree(repo_dir, tmp_path / "behind", "feat-behind")
    conflict = _add_worktree(repo_dir, tmp_path / "conflict", "feat-conflict", with_commit=False)
    (conflict / "c.txt").write_text("ours\n")
    _git(conflict, "add", ".")
    _git(conflict, "commit", "-qm", "feat c")
    dirty = _add_worktree(repo_dir, tmp_path / "dirty", "feat-dirty")
    # origin/main adds c.txt with other content: an add/add conflict for
    # feat-conflict, a clean new file for everyone else.
    _advance_origin(origin, tmp_path, filename="c.txt", content="theirs")
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("feat-behind", behind),
        _entry("feat-conflict", conflict),
        _entry("feat-dirty", dirty, dirty=True),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    attention = _attention_part(result.output)
    assert "feat-conflict" in attention and "left untouched" in attention
    assert "feat-dirty" in attention and "uncommitted changes" in attention
    assert "feat-behind" not in attention
    assert "main worktree" not in attention


def test_sync_dry_run_still_lists_conflicts_as_needing_attention(tmp_path, monkeypatch):
    """--dry-run changes nothing, but the classification is real: a predicted
    conflict still lands in the closing extract."""
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat-conflict", with_commit=False)
    (feat / "c.txt").write_text("ours\n")
    _git(feat, "add", ".")
    _git(feat, "commit", "-qm", "feat c")
    _advance_origin(origin, tmp_path, filename="c.txt", content="theirs")
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat-conflict", feat)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir), "--dry-run"], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "Dry run, nothing changed." in result.output
    assert "feat-conflict" in _attention_part(result.output)


def test_sync_unknown_branch_filter_fails(tmp_path, monkeypatch):
    repo_dir, _origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: [_entry("main", repo_dir, is_main=True)])

    result = runner.invoke(app, ["sync", "nope", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    assert result.exit_code == 1
    assert "no worktree found for: nope" in result.output


def test_sync_branch_filter_syncs_only_the_named_worktree(tmp_path, monkeypatch):
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    feat_a = _add_worktree(repo_dir, tmp_path / "a", "feat-a")
    feat_b = _add_worktree(repo_dir, tmp_path / "b", "feat-b")
    _advance_origin(origin, tmp_path)
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("feat-a", feat_a),
        _entry("feat-b", feat_b),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(app, ["sync", "feat-a", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "feat-a" in result.output
    assert "feat-b" not in result.output
    assert _merge_count(feat_a) == "1"
    assert _merge_count(feat_b) == "0"


def test_sync_repo_without_origin_is_skipped_not_fatal(tmp_path, monkeypatch):
    """A repo with no origin remote has nothing to sync from; that's a
    reported per-repo skip (exit 0), never a traceback or an error, and it
    must not take the other repos in scope down with it."""
    good_dir, origin = _init_repo_with_origin(tmp_path / "good")
    bad_dir = _init_repo(tmp_path / "bad" / "repo")
    _stub_wt(monkeypatch)
    feat = _add_worktree(good_dir, tmp_path / "good" / "feat", "feat-behind")
    _advance_origin(origin, tmp_path / "good")
    entries = {
        good_dir: [_entry("main", good_dir, is_main=True), _entry("feat-behind", feat)],
        bad_dir: [_entry("main", bad_dir, is_main=True)],
    }
    monkeypatch.setattr(repo, "scope_repos", lambda _path: [good_dir, bad_dir])
    monkeypatch.setattr(wt, "list_worktrees", lambda r: entries[r])

    result = runner.invoke(app, ["sync"], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "no origin remote" in result.output
    assert "synced" in result.output
    assert _merge_count(feat) == "1"


def test_sync_repo_with_unreachable_origin_is_an_error(tmp_path, monkeypatch):
    """An origin remote that exists but can't be reached (with no cached
    origin/HEAD to fall back on) leaves the base branch unresolvable: a
    reported per-repo error (exit 1), while the other repos in scope still
    sync."""
    good_dir, origin = _init_repo_with_origin(tmp_path / "good")
    bad_dir = _init_repo(tmp_path / "bad" / "repo")
    _git(bad_dir, "remote", "add", "origin", str(tmp_path / "nonexistent"))
    _stub_wt(monkeypatch)
    feat = _add_worktree(good_dir, tmp_path / "good" / "feat", "feat-behind")
    _advance_origin(origin, tmp_path / "good")
    entries = {
        good_dir: [_entry("main", good_dir, is_main=True), _entry("feat-behind", feat)],
        bad_dir: [_entry("main", bad_dir, is_main=True)],
    }
    monkeypatch.setattr(repo, "scope_repos", lambda _path: [good_dir, bad_dir])
    monkeypatch.setattr(wt, "list_worktrees", lambda r: entries[r])

    result = runner.invoke(app, ["sync"], env={"COLUMNS": "160"})

    assert result.exit_code == 1
    assert "could not resolve the default branch" in result.output
    assert "synced" in result.output
    assert _merge_count(feat) == "1"
    # The failing repo lands in the closing extract; the synced one doesn't.
    attention = _attention_part(result.output)
    assert "could not resolve the default branch" in attention
    assert "feat-behind" not in attention


def test_sync_base_override_merges_that_branch_instead(tmp_path, monkeypatch):
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    # A 'develop' branch on origin, ahead of main.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "switch", "-qC", "develop")
    (other / "dev.txt").write_text("develop work\n")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", "develop work")
    _git(other, "push", "-q", "-u", "origin", "develop")
    feat = _add_worktree(repo_dir, tmp_path / "feat", "feat-behind")
    entries = [_entry("main", repo_dir, is_main=True), _entry("feat-behind", feat)]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)

    result = runner.invoke(
        app, ["sync", "--repo", str(repo_dir), "--base", "develop", "--no-main"], env={"COLUMNS": "160"}
    )

    assert result.exit_code == 0, result.output
    assert "origin/develop" in result.output
    assert (feat / "dev.txt").read_text() == "develop work\n"


def test_sync_skips_parked_worktrees(tmp_path, monkeypatch):
    """A parked worktree is task-complete: nothing to merge into it, and
    skipping it cuts the conflict surface of a sync run. The skip is a dim
    expected-state row (not 'Needs attention'); follow-up means `cop
    unpark` first, then sync."""
    repo_dir, origin = _init_repo_with_origin(tmp_path)
    _stub_wt(monkeypatch)
    parked_wt = _add_worktree(repo_dir, tmp_path / "parked", "feat-parked")
    live_wt = _add_worktree(repo_dir, tmp_path / "live", "feat-live")
    _advance_origin(origin, tmp_path)
    entries = [
        _entry("main", repo_dir, is_main=True),
        _entry("feat-parked", parked_wt),
        _entry("feat-live", live_wt),
    ]
    monkeypatch.setattr(wt, "list_worktrees", lambda _repo: entries)
    park.park(repo_dir, "feat-parked")

    result = runner.invoke(app, ["sync", "--repo", str(repo_dir)], env={"COLUMNS": "160"})

    assert result.exit_code == 0, result.output
    assert "parked; 'cop unpark' to sync it again" in result.output
    # The parked worktree was left alone; the live one got the merge.
    assert _merge_count(parked_wt) == "0"
    assert _merge_count(live_wt) == "1"
    # A dim skip is an expected state, so no closing extract at all.
    assert "Needs attention" not in result.output
