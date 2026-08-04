"""Human-readable size formatting for `coppice clean`'s per-worktree and
total reclaimable-size estimates.
"""

from __future__ import annotations

import os
from pathlib import Path


def dir_size_kb(path: Path) -> int:
    """Best-effort on-disk size of PATH in KB (apparent size, not disk blocks).

    Walks the tree in Python rather than shelling out to `du`, so this works
    the same on every platform `coppice` otherwise supports. Doesn't follow
    symlinks (a linked `.venv`, for instance, would otherwise get counted
    twice), and skips anything unreadable rather than raising, since this is
    only ever used for a size *estimate*.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _err: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total // 1024


def human_kb(kb: int) -> str:
    """Format a KB integer as a short human-readable size, e.g. '482K', '1.3M', '2.1G'."""
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f}G"
    if kb >= 1024:
        return f"{kb / 1024:.1f}M"
    return f"{kb}K"
