"""Read-only "is a VS Code window open on this worktree?" detection, for
the confirmation prompts of the commands that delete worktree directories
(`remove`, `clean`). Deleting a worktree out from under a VS Code window
that has it open strands the window on a deleted folder, so the prompt
marks those worktrees and suggests closing the window first.

The source is the window titles: one osascript call (System Events,
process "Code", name of every window). The dotfiles window.title setting
is "${rootPath}${separator}${activeRepositoryBranchName}", so each title
parses to the opened folder's full path before the first " — "; the
branch component is never matched. Identity is a folder path, so
same-named worktrees and phantom branches (a main-checkout window whose
SCM view has the worktree as its active repository) cannot produce false
warnings.

Everything is best-effort: a failed listing (the Automation permission
for scripting System Events not granted, most likely) returns None, and
so does an empty listing while Code runs — macOS culls the
accessibility tree of a backgrounded app, so "no windows listed" can
mean "couldn't see" (luiul/dashkit#9). Callers treat None as "can't
tell" and stay silent rather than ever claiming "not open". Code simply
not running is not a failure, just an empty answer.

This is the Python twin of dashkit's mycelium/vscode.go; keep the wire
format and the parse contract in sync with it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

# What VS Code's ${separator} template variable renders: space, em dash
# (U+2014), space. Titles split on the first one only; a folder name
# containing a spaced em dash of its own would parse short, and isn't a
# thing in this ecosystem.
_TITLE_SEPARATOR = " — "

# One listing of every window title, plus whether Code is running at
# all (the "1"/"0" flag before the ASCII-31 separator), so "Code closed"
# (a definitive nothing-open) is told apart from "running but no windows
# listed" (possibly an accessibility cull, see the module docstring).
# Same wire format as mycelium's vscodeWindows: titles joined by ASCII
# 30, which no window title can contain.
_LIST_SCRIPT = """
if application "Visual Studio Code" is running then
	tell application "System Events"
		tell process "Code"
			set out to ""
			set RS to (ASCII character 30)
			repeat with w in windows
				set out to out & name of w & RS
			end repeat
			return "1" & (ASCII character 31) & out
		end tell
	end tell
else
	return "0" & (ASCII character 31) & ""
end if
"""


def _run_osascript(script: str) -> str | None:
    """stdout of `osascript -e script`, or None on any failure (the
    Automation permission prompt unanswered, Code's process not
    inspectable, osascript missing, ...). Best-effort by contract: no
    exception escapes."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _window_titles() -> tuple[list[str], bool] | None:
    """(titles, code_running) from one System Events listing, or None
    when the listing itself failed. See _LIST_SCRIPT for the wire
    format."""
    out = _run_osascript(_LIST_SCRIPT)
    if out is None:
        return None
    flag, sep, rest = out.partition("\x1f")
    if not sep:
        return None  # output shape the script never produces; don't guess
    titles = [t.strip("\r\n") for t in rest.split("\x1e")]
    return [t for t in titles if t], flag == "1"


def title_path(title: str) -> str | None:
    """The folder path a window title identifies: everything before the
    first separator, with a leading ~ expanded. None when what remains
    is not an absolute path (a no-folder window's empty title, a foreign
    title format): such titles never match.

    For a multi-root window ${rootPath} renders the .code-workspace file
    path rather than a member folder, so such a window matches only on
    that path — an accepted limitation (luiul/dashkit#14).
    """
    path = title.split(_TITLE_SEPARATOR, 1)[0]
    home = str(Path.home())
    if path == "~":
        path = home
    elif path.startswith("~/"):
        path = home + path[1:]
    return path if path.startswith("/") else None


def window_paths() -> list[str] | None:
    """The folder path of every open VS Code window (parsed from its
    title), or None for "can't tell": the listing failed, or came back
    empty while Code runs (possible accessibility cull). Code not
    running is a real empty answer, distinct from None.
    """
    result = _window_titles()
    if result is None:
        return None
    titles, running = result
    if running and not titles:
        return None
    return [path for title in titles if (path := title_path(title)) is not None]


def matches_worktree(window_paths: Iterable[str], path: Path) -> bool:
    """Whether a window on one of WINDOW_PATHS would be stranded by
    deleting the worktree at PATH: a path equals it or sits inside it,
    on a path-element boundary ("/wt-a" must not match "/wt-a-b").
    Strict by construction: no branch is ever matched, so the
    phantom-branch class of false positives cannot occur.
    """
    target = str(path)
    prefix = target + "/"
    return any(p == target or p.startswith(prefix) for p in window_paths)
