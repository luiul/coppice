"""Read-only "is a VS Code window open on this worktree?" detection, for
the confirmation prompts of the commands that delete worktree directories
(`remove`, `clean`). Deleting a worktree out from under a VS Code window
that has it open strands the window on a deleted folder, so the prompt
marks those worktrees and suggests closing the window first.

One osascript call lists every open VS Code window's title (via System
Events), and a title is matched against a worktree by the ecosystem's
`window.title` convention (`${rootName} — ${branch} — ${editor}`): the
title's root component must be the worktree path's basename and its branch
component the worktree's branch, so a window on the main checkout
("tardis-community — master — ...") never matches a worktree of the same
repo ("tardis-community — feat-x — ...").

A stricter port of mycelium's matchVSCodeWindowTitle (the Go original
the dashkit dashboards poll for their "VS Code open?" column), same rule
as mycelium's matchVSCodeWindowTitleStrict: the dashboards' match keeps a
branchless title as a weak fallback (open-or-focus semantics, where a
false "open" merely focuses a window), while this one drops it: a false
"open" on a deletion prompt cries wolf, and the weak fallback would fire
on every removal while any bare-titled window of that repo is around (a
main checkout whose SCM branch hasn't resolved renders as the plain
folder name). Observed live on the sandbox repo: two bare "sandbox"
scratch windows made every sandbox worktree removal warn.

What the strictness cannot fix: a window scoped to the main checkout
whose SCM view has a worktree as its active repository renders that
worktree's branch in its title without having the worktree's folder
open, indistinguishable from a genuine worktree window's title. Those
phantoms still match; the warning's advice is harmless for them.

Everything is best-effort: a failed listing (osascript missing, the macOS
Automation permission not granted) returns None, and callers treat None
as "can't tell" and stay silent rather than ever claiming "not open".
Code simply not running is not a failure, just an empty listing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

# What VS Code's `${separator}` template variable renders as: a dash (any
# width) flanked by spaces. Same regex as mycelium's titleSeparator.
# (escaped, not literal: ruff's RUF001 reads a literal en dash as a typo)
_TITLE_SEPARATOR = re.compile(r"\s+[-\u2013\u2014]\s+")

# Same AppleScript as mycelium's vscodeWindows: titles only, with "Code
# simply isn't running" answering an empty list rather than an error.
_LIST_WINDOWS_SCRIPT = """\
if application "Visual Studio Code" is running then
	tell application "System Events"
		tell process "Code"
			get name of every window
		end tell
	end tell
else
	return ""
end if
"""


def open_window_titles() -> list[str] | None:
    """Titles of every open VS Code window, or None when they can't be
    listed (osascript missing, or the macOS Automation permission denied).

    osascript renders the AppleScript list as one comma-separated line;
    titles here are folder and branch names, where a comma doesn't occur,
    so the naive split is the same one mycelium ships.
    """
    if shutil.which("osascript") is None:
        return None
    proc = subprocess.run(["osascript", "-e", _LIST_WINDOWS_SCRIPT], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if not out:
        return []
    return [t.strip() for t in out.split(", ")]


def title_matches_worktree(title: str, path: Path, branch: str) -> bool:
    """Whether TITLE is a window open on the worktree at PATH on BRANCH.

    The title's root component must equal the path's basename and its
    branch component must equal BRANCH: a title naming a different branch
    is a window on a different same-named folder (the main checkout, a
    sibling worktree), and a branchless title carries no evidence the
    window has this worktree open (no weak fallback here, unlike
    mycelium's open-or-focus match; see the module docstring). Without a
    BRANCH to key on, root equality is the best a title can do.
    """
    base = path.name
    if not base:
        return False
    parts = _TITLE_SEPARATOR.split(title, maxsplit=2)
    if parts[0] != base:
        return False
    if not branch:
        return True
    if len(parts) < 2:
        return False
    return parts[1] == branch
