"""`confirm.ask`'s key semantics, driven with synthetic keys through a
swapped-out `_read_key` (the repo's convention for external calls), plus
`_read_key`'s own escape-sequence handling against a faked tty stdin.
"""

from __future__ import annotations

import io
import re
import sys

import pytest
from rich.console import Console

from coppice import confirm

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def output(monkeypatch):
    """Capture confirm's console output at a wide, terminal-forced width, so
    assertions see the actual ANSI markup and never a word wrap."""
    buf = io.StringIO()
    monkeypatch.setattr(confirm, "console", Console(file=buf, force_terminal=True, width=200, highlight=False))
    return buf


def _script(monkeypatch, keys: list[str]) -> None:
    """Swap `_read_key` for a scripted key sequence, one entry per keypress
    (a multi-character entry stands in for a whole escape sequence)."""
    it = iter(keys)
    monkeypatch.setattr(confirm, "_read_key", lambda: next(it))


def test_y_confirms_without_enter(output, monkeypatch):
    """The headline convention: a single `y` keypress is the whole answer,
    no enter after it."""
    _script(monkeypatch, ["y"])

    assert confirm.ask("Remove the 1 worktree listed above? Branches survive unless already merged.") is True
    # the resolved answer is echoed, cbreak reads echo nothing themselves
    assert output.getvalue().endswith("y\n")


def test_capital_y_confirms(output, monkeypatch):
    _script(monkeypatch, ["Y"])
    assert confirm.ask("Q?") is True


@pytest.mark.parametrize("key", ["n", "N", "\r", "\n", "\x1b", ""])
def test_cancels(output, monkeypatch, key):
    """`n`, `esc`, enter (selecting the capitalized default), and EOF all
    cancel, and echo the `n` they resolved to."""
    _script(monkeypatch, [key])
    assert confirm.ask("Q?") is False
    assert output.getvalue().endswith("n\n")


def test_other_keys_are_swallowed(output, monkeypatch):
    """A key that means nothing changes nothing: the prompt keeps waiting
    until a real answer arrives, so no answer lands by accident."""
    _script(monkeypatch, ["x", "q", " ", "?", "\t", "y"])
    assert confirm.ask("Q?") is True


def test_escape_sequences_are_swallowed_not_cancelled(output, monkeypatch):
    """An arrow key arrives as one multi-character sequence (see
    `_read_key`), and must not be mistaken for the bare esc that cancels."""
    _script(monkeypatch, ["\x1b[A", "\x1b[B", "y"])
    assert confirm.ask("Q?") is True


def test_ctrl_c_quits_rather_than_cancelling(output, monkeypatch):
    """ctrl+c propagates as KeyboardInterrupt (typer turns it into exit
    130): a quit, not a silent cancel that could be mistaken for an answer."""

    def _interrupted() -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(confirm, "_read_key", _interrupted)
    with pytest.raises(KeyboardInterrupt):
        confirm.ask("Q?")
    # a newline keeps the next shell prompt off the prompt line
    assert output.getvalue().endswith("\n")


def test_prompt_carries_the_yN_suffix(output, monkeypatch):
    """Every prompt ends in ` [y/N] `, added here so no caller can forget
    the capitalized default the enter key selects."""
    _script(monkeypatch, ["n"])
    confirm.ask("Remove it? Branches survive.")
    assert "Remove it? Branches survive. [y/N] " in _ANSI.sub("", output.getvalue())


def test_color_tiers(output, monkeypatch):
    """Yellow for the ordinary destructive action, red for the force tier,
    no color for a check that destroys nothing."""
    for tier, code in [("destructive", "33"), ("force", "31"), ("plain", None)]:
        output.seek(0)
        output.truncate()
        _script(monkeypatch, ["y"])
        confirm.ask("Q?", tier=tier)
        text = output.getvalue()
        if code is None:
            assert "\x1b[33m" not in text and "\x1b[31m" not in text
        else:
            assert f"\x1b[{code}m" in text


class _FakeTtyStdin:
    """A tty-flavored stdin stand-in for `_read_key`'s terminal path:
    `isatty` true and a dummy `fileno`, while the low-level byte readers
    (also swapped) drain a scripted queue."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 0


def _fake_tty(monkeypatch, keys: str) -> None:
    queue = list(keys)
    monkeypatch.setattr(sys, "stdin", _FakeTtyStdin())
    monkeypatch.setattr(confirm, "_read_byte", lambda _fd: queue.pop(0) if queue else "")
    monkeypatch.setattr(confirm, "_input_ready", lambda _fd: bool(queue))


def test_read_key_plain_key(monkeypatch):
    _fake_tty(monkeypatch, "y")
    assert confirm._read_key() == "y"


def test_read_key_bare_esc_stays_a_cancel(monkeypatch):
    """A lone esc has no tail arriving, so it resolves to the bare esc that
    cancels, not to a swallowed sequence."""
    _fake_tty(monkeypatch, "\x1b")
    assert confirm._read_key() == "\x1b"


def test_read_key_drains_an_escape_sequence(monkeypatch):
    """An arrow key's bytes arrive together and are returned as one string,
    so `ask` can swallow the whole sequence instead of cancelling on its
    leading esc."""
    _fake_tty(monkeypatch, "\x1b[A")
    assert confirm._read_key() == "\x1b[A"


def test_read_key_non_tty_reads_one_char(monkeypatch):
    """Piped input (and the test harness) takes the buffered path: one
    character, no enter needed, no termios involved."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert confirm._read_key() == "y"


def test_read_key_eof_returns_empty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert confirm._read_key() == ""
