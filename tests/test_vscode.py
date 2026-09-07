"""VS Code window detection for the `remove`/`clean` confirmation warnings.

The source is the window registry (~/.local/state/vscode-windows/,
written by dashkit's vscode-window-registry extension), tested here
against files in a tmp_path directory: no real Code instance on CI, and
no title parsing anywhere (identity is a folder path).
"""

import os
import time
from pathlib import Path

from coppice import vscode


def _write_entry(directory: Path, name: str, content: str, stale: bool = False) -> None:
    file = directory / name
    file.write_text(content)
    if stale:
        old = time.time() - 60
        os.utime(file, (old, old))


def test_registry_window_folders_is_none_without_the_directory(monkeypatch, tmp_path):
    # Extension not installed: callers treat None as "can't tell" and
    # stay silent.
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
