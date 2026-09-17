"""VS Code window detection for the `remove`/`clean` confirmation warnings.

The source is the window titles (one System Events osascript call; the
dotfiles window.title setting puts the opened folder's full path before
the first " — ", branch never matched), tested here by stubbing the
osascript run: no real Code instance on CI. The parse and match logic is
the Python twin of dashkit's mycelium/vscode.go, kept in sync with it.
"""

from pathlib import Path

from coppice import vscode


def _listing(titles: list[str], running: bool = True) -> str:
    """The wire format _LIST_SCRIPT produces: running flag, then titles
    joined by ASCII 30, after the ASCII 31 separator."""
    return ("1" if running else "0") + "\x1f" + "".join(t + "\x1e" for t in titles)


def test_title_path_parses_path_and_expands_tilde():
    home = str(Path.home())
    assert vscode.title_path("~/dotfiles — main") == f"{home}/dotfiles"
    assert vscode.title_path("~/projects/my repo — feat/x") == f"{home}/projects/my repo"
    assert vscode.title_path("/opt/repo — main") == "/opt/repo"
    # A branch component that never rendered: the path is still
    # everything before the separator.
    assert vscode.title_path("~/dotfiles — ") == f"{home}/dotfiles"
    assert vscode.title_path("~/dotfiles") == f"{home}/dotfiles"
    # Only the first separator splits: branch names carry dashes.
    assert vscode.title_path("~/dotfiles — patch/ISA-1 — wip") == f"{home}/dotfiles"


def test_title_path_rejects_titles_without_a_path():
    # No-folder windows and foreign title formats never match.
    assert vscode.title_path("") is None
    assert vscode.title_path("Welcome — Visual Studio Code") is None
    assert vscode.title_path("~root/repo — main") is None  # ~user form not expanded


def test_window_paths_is_none_when_the_listing_fails(monkeypatch):
    # Automation permission not granted, most likely: callers treat None
    # as "can't tell" and stay silent.
    monkeypatch.setattr(vscode, "_run_osascript", lambda _script: None)
    assert vscode.window_paths() is None


def test_window_paths_is_none_for_an_empty_listing_while_running(monkeypatch):
    # Zero windows listed while Code runs is the accessibility-cull
    # signature (luiul/dashkit#9): "can't tell", not "nothing open".
    monkeypatch.setattr(vscode, "_run_osascript", lambda _script: _listing([]))
    assert vscode.window_paths() is None


def test_window_paths_is_empty_when_code_is_not_running(monkeypatch):
    # Code closed is a definitive nothing-open: a real empty answer.
    monkeypatch.setattr(vscode, "_run_osascript", lambda _script: _listing([], running=False))
    assert vscode.window_paths() == []


def test_window_paths_parses_titles_and_drops_pathless_ones(monkeypatch):
    home = str(Path.home())
    monkeypatch.setattr(
        vscode,
        "_run_osascript",
        lambda _script: _listing(["~/dotfiles — main", "", "Welcome — Visual Studio Code", "~/repo"]),
    )
    assert vscode.window_paths() == [f"{home}/dotfiles", f"{home}/repo"]


def test_window_paths_is_none_for_garbage_output(monkeypatch):
    # Output in a shape the script never produces: don't guess.
    monkeypatch.setattr(vscode, "_run_osascript", lambda _script: "garbage")
    assert vscode.window_paths() is None


def test_matches_worktree_exact_and_nested():
    assert vscode.matches_worktree(["/w/repo"], Path("/w/repo"))
    # A window scoped to a subpackage is stranded by the removal too.
    assert vscode.matches_worktree(["/w/repo/pkg"], Path("/w/repo"))
    assert not vscode.matches_worktree(["/w/other"], Path("/w/repo"))


def test_matches_worktree_respects_element_boundaries():
    # "/wt-a" must not match "/wt-a-b": a raw string prefix is not a path
    # containment check.
    assert not vscode.matches_worktree(["/w/wt-a-b"], Path("/w/wt-a"))
