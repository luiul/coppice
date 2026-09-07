"""VS Code window detection for the `remove`/`clean` confirmation warnings.

The primary source is the window registry (~/.local/state/vscode-windows/,
written by dashkit's vscode-window-registry extension), tested here
against files in a tmp_path directory. The osascript listing is the
fallback and is stubbed (no real Code instance on CI); the title matching
is pure and tested directly against the ecosystem's `window.title`
convention (`${rootName} — ${branch} — ${editor}`), including the
same-named-folder case the branch component exists to disambiguate: a
worktree's window and its repo's main-checkout window share a root name.
"""

import os
import subprocess
import time
from pathlib import Path

from coppice import vscode


def _completed(returncode: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["osascript"], returncode=returncode, stdout=stdout, stderr="")


def test_open_window_titles_is_none_without_osascript(monkeypatch):
    monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
    assert vscode.open_window_titles() is None


def test_open_window_titles_is_none_when_the_listing_fails(monkeypatch):
    """E.g. the macOS Automation permission is denied: 'can't tell', so
    callers stay silent rather than claiming 'not open'."""
    monkeypatch.setattr(vscode.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(vscode.subprocess, "run", lambda *a, **k: _completed(1, ""))
    assert vscode.open_window_titles() is None


def test_open_window_titles_is_empty_when_code_is_not_running(monkeypatch):
    """Not running is a real answer (no windows), distinct from 'can't tell'."""
    monkeypatch.setattr(vscode.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(vscode.subprocess, "run", lambda *a, **k: _completed(0, ""))
    assert vscode.open_window_titles() == []


def test_open_window_titles_splits_the_comma_separated_listing(monkeypatch):
    monkeypatch.setattr(vscode.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(
        vscode.subprocess,
        "run",
        lambda *a, **k: _completed(0, "understory — main — x.go, tardis-community — feat-x — a.py\n"),
    )
    assert vscode.open_window_titles() == ["understory — main — x.go", "tardis-community — feat-x — a.py"]


def test_title_matches_worktree_on_root_and_branch():
    assert vscode.title_matches_worktree("tardis-community — feat-x — a.py", Path("/w/tardis-community"), "feat-x")


def test_title_matches_worktree_rejects_the_same_named_main_checkout():
    """The case the branch component exists for: a window on the main
    checkout ("repo — master — ...") must not count as open on a worktree
    of the same repo."""
    assert not vscode.title_matches_worktree("tardis-community — master — a.py", Path("/w/tardis-community"), "feat-x")


def test_title_matches_worktree_drops_the_branchless_weak_fallback():
    """The difference from mycelium's open-or-focus match this module
    exists for (see the module docstring): a bare "repo" title carries no
    branch information, and matching it would make every removal of that
    repo's worktrees warn while any bare-titled window is around (e.g. a
    main checkout whose SCM branch hasn't resolved)."""
    assert not vscode.title_matches_worktree("tardis-community", Path("/w/tardis-community"), "feat-x")


def test_title_matches_worktree_rejects_a_prefix_sibling():
    """Root equality is on the parsed component, not a prefix: understory-lab
    is not understory."""
    assert not vscode.title_matches_worktree("understory-lab — feat-x", Path("/w/understory"), "feat-x")


def test_title_matches_worktree_without_a_branch_falls_back_to_root():
    assert vscode.title_matches_worktree("tardis-community — anything", Path("/w/tardis-community"), "")
    assert not vscode.title_matches_worktree("other — anything", Path("/w/tardis-community"), "")


# --- Window registry (primary path) -------------------------------------


def _write_entry(directory: Path, name: str, content: str, stale: bool = False) -> None:
    file = directory / name
    file.write_text(content)
    if stale:
        old = time.time() - 60
        os.utime(file, (old, old))


def test_registry_window_folders_is_none_without_the_directory(monkeypatch, tmp_path):
    # Extension not installed: callers fall back to the title match.
    monkeypatch.setattr(vscode, "_REGISTRY_DIR", tmp_path / "does-not-exist")
    assert vscode.registry_window_folders() is None


def test_registry_window_folders_parses_fresh_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(vscode, "_REGISTRY_DIR", tmp_path)
    _write_entry(
        tmp_path,
        "a.json",
        '{"sessionId": "a", "folders": ["/w/tardis-community", "/w/tardis-community/pkg"], '
        '"workspaceFile": "/w/tc.code-workspace", "updatedAt": "x"}',
    )
    _write_entry(tmp_path, "b.json", '{"sessionId": "b", "folders": [], "workspaceFile": null, "updatedAt": "x"}')
    assert vscode.registry_window_folders() == [["/w/tardis-community", "/w/tardis-community/pkg"], []]


def test_registry_window_folders_prunes_stale_entries(monkeypatch, tmp_path):
    # A closed window can't be relied on to delete its entry; the mtime
    # cutoff is the cleanup.
    monkeypatch.setattr(vscode, "_REGISTRY_DIR", tmp_path)
    _write_entry(tmp_path, "fresh.json", '{"sessionId": "f", "folders": ["/w/a"], "workspaceFile": null}')
    _write_entry(tmp_path, "stale.json", '{"sessionId": "s", "folders": ["/w/b"], "workspaceFile": null}', stale=True)
    assert vscode.registry_window_folders() == [["/w/a"]]


def test_registry_window_folders_skips_torn_and_foreign_files(monkeypatch, tmp_path):
    monkeypatch.setattr(vscode, "_REGISTRY_DIR", tmp_path)
    _write_entry(tmp_path, "good.json", '{"sessionId": "g", "folders": ["/w/a"], "workspaceFile": null}')
    _write_entry(tmp_path, "torn.json", '{"sessionId": "to')
    (tmp_path / "fallback.log").write_text("not json\n")
    assert vscode.registry_window_folders() == [["/w/a"]]


def test_registry_matches_worktree_exact_and_nested():
    assert vscode.registry_matches_worktree(["/w/repo"], Path("/w/repo"))
    # A window scoped to a subpackage is stranded by the removal too.
    assert vscode.registry_matches_worktree(["/w/repo/pkg"], Path("/w/repo"))
    assert not vscode.registry_matches_worktree(["/w/other"], Path("/w/repo"))


def test_registry_matches_worktree_respects_element_boundaries():
    # "/wt-a" must not match "/wt-a-b": a raw string prefix is not a path
    # containment check.
    assert not vscode.registry_matches_worktree(["/w/wt-a-b"], Path("/w/wt-a"))
