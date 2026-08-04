"""Human-readable size formatting for `coppice clean`'s per-worktree and
total reclaimable-size estimates.
"""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
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


def _fork_context() -> multiprocessing.context.BaseContext | None:
    """A 'fork' multiprocessing context where available, None otherwise.

    'fork' starts a worker by cloning the already-running interpreter, so it
    skips re-importing `coppice` and its dependencies (typer, rich, click)
    in every worker, unlike the platform default ('spawn' on macOS and
    Windows). That matters here since `dir_sizes_kb` spins up a fresh pool
    on every call, there's no long-lived pool to amortize spawn's import
    cost over. Not available on Windows (`get_context` raises ValueError),
    where callers fall back to the platform default context instead.
    """
    try:
        return multiprocessing.get_context("fork")
    except ValueError:
        return None


def dir_sizes_kb(
    paths: Iterable[Path],
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[Path, int]:
    """`dir_size_kb` for every PATH in PATHS, computed in parallel.

    Uses a process pool, not threads: `dir_size_kb`'s walk is dominated by
    per-entry Python bytecode (joining paths, stat-ing, accumulating sizes)
    rather than time blocked in a syscall, so it holds the GIL for most of
    its work, threads mostly add contention instead of overlap, and can end
    up *slower* than sizing everything serially. Separate processes give
    each worktree's walk its own interpreter and a real CPU core, which is
    what actually pays off for a repo with many worktrees (e.g. a large
    monorepo with a dozen+ checkouts, each with its own
    node_modules/.venv/build output).

    Dedupes PATHS first since callers may pass the same path twice (unlikely,
    but free to guard against), and skips the pool entirely for 0 or 1 paths,
    there's nothing to parallelize and a pool's startup cost would be pure
    overhead.

    Calls ON_PROGRESS(done, total) after each path finishes, in completion
    order rather than PATHS' order (a slow worktree shouldn't hold up the
    count for ones that finished first), so a caller can drive a spinner or
    progress bar without waiting for the whole batch.
    """
    unique = list(dict.fromkeys(paths))
    total = len(unique)
    if total <= 1:
        results = {p: dir_size_kb(p) for p in unique}
        if on_progress and results:
            on_progress(1, 1)
        return results

    max_workers = min(total, os.cpu_count() or 4)
    results: dict[Path, int] = {}
    with ProcessPoolExecutor(max_workers=max_workers, mp_context=_fork_context()) as pool:
        future_to_path = {pool.submit(dir_size_kb, p): p for p in unique}
        for done, future in enumerate(as_completed(future_to_path), start=1):
            results[future_to_path[future]] = future.result()
            if on_progress:
                on_progress(done, total)
    return results


def human_kb(kb: int) -> str:
    """Format a KB integer as a short human-readable size, e.g. '482K', '1.3M', '2.1G'."""
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f}G"
    if kb >= 1024:
        return f"{kb / 1024:.1f}M"
    return f"{kb}K"
