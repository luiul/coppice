"""Read-only "is a VS Code window open on this worktree?" detection, for
the confirmation prompts of the commands that delete worktree directories
(`remove`, `clean`). Deleting a worktree out from under a VS Code window
that has it open strands the window on a deleted folder, so the prompt
marks those worktrees and suggests closing the window first.

The source is the window registry: every VS Code window self-registers
into ~/.local/state/vscode-windows/ via dashkit's vscode-window-registry
extension (one small JSON file per window, with a heartbeat). A worktree
matches when a fresh entry's folder IS the worktree path or sits inside
it. Identity is a folder path, never a title, so same-named worktrees and
phantom branches (a main-checkout window whose SCM view has the worktree
as its active repository) cannot produce false warnings.

Everything is best-effort: an unreadable registry (the extension isn't
installed) returns None, and callers treat None as "can't tell" and stay
silent rather than ever claiming "not open". Code simply not running is
not a failure, just an empty registry.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from pathlib import Path

# The registry contract (writer: dashkit's vscode-window-registry
# extension): one <sessionId>.json per window, rewritten every 5s, so an
# entry whose mtime is older than 30s belongs to a closed window and is
# dropped. Window close can't be relied on to delete the file; staleness
# pruning is the cleanup.
_REGISTRY_DIR = Path.home() / ".local" / "state" / "vscode-windows"
_REGISTRY_STALENESS_SECONDS = 30.0


def registry_window_folders() -> list[list[str]] | None:
    """The workspace folders of every window with a fresh registry entry,
    or None when the registry directory can't be read (the extension
    isn't installed). An empty list is a real answer ("the registry
    works, no window is fresh"), distinct from None.

    Stale, unreadable, and unparseable files are skipped, never fatal:
    one torn write must not take detection down for every window.
    """
    if not _REGISTRY_DIR.is_dir():
        return None
    now = time.time()
    windows: list[list[str]] = []
    for file in _REGISTRY_DIR.glob("*.json"):
        try:
            if now - file.stat().st_mtime > _REGISTRY_STALENESS_SECONDS:
                continue
            entry = json.loads(file.read_text())
        except OSError:
            continue
        except ValueError:  # torn write; not JSON
            continue
        folders = entry.get("folders")
        if isinstance(folders, list):
            windows.append([f for f in folders if isinstance(f, str)])
    return windows


def registry_matches_worktree(folders: Iterable[str], path: Path) -> bool:
    """Whether a window with FOLDERS open would be stranded by deleting
    the worktree at PATH: a folder equals the path or sits inside it, on
    a path-element boundary ("/wt-a" must not match "/wt-a-b"). Strict
    by construction: no branch, no title, so the phantom-branch class of
    false positives cannot occur.
    """
    target = str(path)
    prefix = target + "/"
    return any(f == target or f.startswith(prefix) for f in folders)
