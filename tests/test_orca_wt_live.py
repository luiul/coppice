"""Exercise setup handoff against real wt, without Orca or the user's config."""

import json
import shutil
import subprocess

import pytest

from coppice import orca, wt


@pytest.mark.skipif(shutil.which("wt") is None, reason="real wt not installed")
def test_real_wt_scoped_setup_order_and_default_background(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "test@example.com"],
                 ["config", "user.name", "Test"], ["commit", "--allow-empty", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, timeout=10)
    config = tmp_path / "wt.toml"
    target = tmp_path / "manual"
    config.write_text(
        f'worktree-path = {json.dumps(str(target))}\n'
        '[post-start]\n'
        "seed = \"printf ready > '{{ worktree_path }}/setup-ready'\"\n"
        f'[projects.{json.dumps(str(root.resolve()))}.post-start]\n'
        "dependent = \"test -f '{{ worktree_path }}/setup-ready' && printf done > '{{ worktree_path }}/setup-dependent'\"\n"
    )
    monkeypatch.setenv("WORKTRUNK_CONFIG_PATH", str(config))
    monkeypatch.setenv("WORKTRUNK_SYSTEM_CONFIG_PATH", str(tmp_path / "missing-system.toml"))
    monkeypatch.setenv("WORKTRUNK_PROJECT_CONFIG_PATH", str(tmp_path / "missing-project.toml"))
    monkeypatch.setattr(orca, "manual_invocation", lambda: True)
    monkeypatch.setattr(orca, "_orca", lambda *_args, **_kw: {"externalWorktreeVisibility": "show"})
    monkeypatch.setattr(orca, "STATE_ROOT", tmp_path / "state")
    setup = orca.prepare(root)
    assert setup
    result = wt.switch(root, "manual", create=True, base="main", config_overrides=setup.suppress)
    assert result["action"] == "created"
    assert not (target / "setup-ready").exists()
    orca.finish_setup(setup, target, "manual", "main")
    assert (target / "setup-ready").read_text() == "ready"
    assert (target / "setup-dependent").read_text() == "done"
    assert not orca._setup_path(target).exists()
    # Original config remains unchanged and direct wt still sees both setup hooks.
    hooks = wt.run(["hook", "show", "post-start", "--format", "json"], cwd=root)
    assert {row["name"] for row in json.loads(hooks.stdout)} == {"seed", "dependent"}
    (target / "setup-ready").unlink()
    (target / "setup-dependent").unlink()
    wt.run(["remove", "manual", "--yes"], cwd=root)
