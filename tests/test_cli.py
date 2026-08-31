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

from coppice import cli, gh, repo, wt
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
) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    return {
        "branch": branch,
        "path": str(path),
        "is_main": is_main,
        "is_current": is_current,
        "commit": {"timestamp": commit_ts},
        "working_tree": {"modified": dirty},
        "main_state": main_state,
        "worktree": {"state": "prunable" if stale else "active"},
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


def _stub_prompt_preflight(monkeypatch, tmp_path, *, pi: bool = True, automatic_tasks: bool = True) -> None:
    """Control every input `new --prompt`'s preflight reads: `pi` on PATH (via
    `_stub_wt`'s which table) and the VS Code user settings file (repointed at
    a throwaway path so the real one on this machine can't leak into tests).
    """
    _stub_wt(monkeypatch, which={"pi": "/usr/bin/pi" if pi else None})
    settings = tmp_path / "vscode-settings.json"
    settings.write_text('{"task.allowAutomaticTasks": "on"}' if automatic_tasks else "{}")
    monkeypatch.setattr(cli, "_VSCODE_SETTINGS_PATH", settings)


def test_new_prompt_sets_cop_prompt_in_the_wt_environment(tmp_path, monkeypatch):
    """The whole feature in one assertion: `--prompt` must reach the `wt`
    subprocess as COP_PROMPT, where the user's post-switch hook picks it up.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_prompt_preflight(monkeypatch, tmp_path)
    switch_calls = _stub_switch(monkeypatch, branch_exists=False)

    result = runner.invoke(
        app, ["new", str(repo_dir), "--branch", "fresh-branch", "--prompt", "fix the flaky login test"]
    )

    assert result.exit_code == 0, result.output
    assert switch_calls[0]["extra_env"] == {"COP_PROMPT": "fix the flaky login test"}
    assert "note:" not in result.output


def test_new_without_prompt_leaves_the_wt_environment_alone(tmp_path, monkeypatch):
    """Regression guard: a plain `cop new` must pass no extra env at all, so
    the prompt hook stays off and everything behaves exactly as before.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_prompt_preflight(monkeypatch, tmp_path, pi=False, automatic_tasks=False)
    switch_calls = _stub_switch(monkeypatch, branch_exists=False)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "fresh-branch"])

    assert result.exit_code == 0, result.output
    assert switch_calls[0]["extra_env"] is None
    # no --prompt, no preflight: the missing pieces above must not be reported
    assert "note:" not in result.output


def test_new_prompt_preflight_warns_when_pi_is_missing(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_prompt_preflight(monkeypatch, tmp_path, pi=False)
    _stub_switch(monkeypatch, branch_exists=False)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "fresh-branch", "-p", "do a thing"])

    assert result.exit_code == 0, result.output
    assert "`pi` is not on PATH" in result.output
    assert "task.allowAutomaticTasks" not in result.output


def test_new_prompt_preflight_warns_when_automatic_tasks_are_off(tmp_path, monkeypatch):
    repo_dir = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_prompt_preflight(monkeypatch, tmp_path, automatic_tasks=False)
    _stub_switch(monkeypatch, branch_exists=False)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "fresh-branch", "-p", "do a thing"])

    assert result.exit_code == 0, result.output
    assert "task.allowAutomaticTasks" in result.output
    assert "`pi` is not on PATH" not in result.output


def test_new_prompt_preflight_warns_about_an_existing_tasks_json(tmp_path, monkeypatch):
    """A repo with its own .vscode/tasks.json gets the merge path in the hook
    instead of the fresh-write one; worth a heads-up since the merge edits a
    file the repo already owns.
    """
    repo_dir = _init_repo(tmp_path / "repo")
    (repo_dir / ".vscode").mkdir()
    (repo_dir / ".vscode" / "tasks.json").write_text('{"version": "2.0.0", "tasks": []}')
    monkeypatch.setattr(repo, "REGISTRY_PATH", tmp_path / "known-repos")
    _stub_prompt_preflight(monkeypatch, tmp_path)
    _stub_switch(monkeypatch, branch_exists=False)

    result = runner.invoke(app, ["new", str(repo_dir), "--branch", "fresh-branch", "-p", "do a thing"])

    assert result.exit_code == 0, result.output
    assert "tasks.json" in result.output
    assert "`pi` is not on PATH" not in result.output
    assert "task.allowAutomaticTasks" not in result.output


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
