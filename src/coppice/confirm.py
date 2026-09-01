"""Single-keypress confirmation prompts, one shared implementation.

Every destructive (or merely surprising) action in coppice asks first, and
asks the same way everywhere: the confirmation discipline the dashkit
dashboards (canopy/understory) documented in dashkit's CONVENTIONS.md,
adapted for a one-shot CLI.

- `y` confirms; `n`, `esc`, or `enter` cancel; every other key is
  swallowed; `ctrl+c` quits (typer turns the propagating KeyboardInterrupt
  into exit 130). Enter cancels because the prompt says `[y/N]`: the
  capitalized letter is the default answer, and enter selects it.
  Swallowing everything else means no answer can land by accident, the
  prompt keeps waiting for a key that means something.
- One keypress is the whole answer, no enter after the `y`. That is the
  point of reading the terminal in cbreak mode instead of line-buffered
  (`typer.confirm` reads a line, so it cannot do this).
- Color tiers: yellow for an ordinary destructive action (`remove`,
  `clean`), red for the force tier (`--force`/`--force-delete` in play),
  plain for a non-destructive check (`new`'s existing-branch prompt).

Deliberately not carried over from the dashboards: the 10s auto-cancel and
the poll-driven target revalidation. Those exist because a dashboard's
rows keep repolling and reordering under an open prompt; a one-shot CLI
prompt has nothing moving underneath it, so a timeout would only surprise.

`_read_key` is a module-level function so tests can swap it out and drive
`ask` with synthetic keys, the same convention the rest of the repo uses
for external calls (see tests/test_confirm.py). Where termios is missing
(Windows) or stdin is not a terminal (a pipe, a test harness), reading
degrades to plain buffered reads: the prompt still works there, it just
wants an enter again.
"""

from __future__ import annotations

import os
import select
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from importlib.util import find_spec
from typing import Literal

from rich.console import Console

Tier = Literal["plain", "destructive", "force"]

# One color per tier, and one meaning per color: yellow for the ordinary
# destructive action, red for the force tier, no color for a check that
# destroys nothing.
_TIER_STYLES: dict[Tier, str] = {"plain": "", "destructive": "yellow", "force": "red"}

# highlight=False, same reason as cli.py's console: Rich's ReprHighlighter
# would otherwise rainbow-color anything number- or path-shaped in the
# prompt, accidental noise on top of the deliberate tier color.
console = Console(highlight=False)

_HAVE_TERMIOS = find_spec("termios") is not None


@contextmanager
def _cbreak() -> Iterator[None]:
    """cbreak mode on stdin for the duration of one prompt: keypresses
    arrive one at a time, unechoed, with no enter needed, while ISIG stays
    enabled (unlike raw mode) so ctrl+c still arrives as a KeyboardInterrupt
    instead of a literal byte. Only ever entered when stdin is a terminal;
    the import is local so the module still imports where termios doesn't
    exist (Windows).
    """
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _read_byte(fd: int) -> str:
    """One raw byte from FD, decoded lossily: a multi-byte character arrives
    as several of these, each decoding to a replacement char, all of which
    `ask` swallows anyway."""
    return os.read(fd, 1).decode(errors="replace")


def _input_ready(fd: int) -> bool:
    """Whether another byte is already waiting on FD, with a 50ms grace for
    the tail of an escape sequence (whose bytes arrive together)."""
    ready, _, _ = select.select([fd], [], [], 0.05)
    return bool(ready)


def _read_key() -> str:
    """One keypress from stdin, without waiting for enter on a terminal.

    Returns the pressed key as a one-character string, a whole escape
    sequence (an arrow key's "\\x1b[A") as one multi-character string so the
    caller can swallow it whole instead of mistaking its leading esc for a
    cancel, or "" on EOF (a closed or exhausted stdin cancels rather than
    spinning the prompt forever).
    """
    if not _HAVE_TERMIOS or not sys.stdin.isatty():
        # Pipes, test harnesses, Windows: a plain buffered read. In a
        # terminal's canonical mode this still returns per character once a
        # line arrives, which is exactly the degraded behavior documented
        # up top.
        return sys.stdin.read(1)
    # Read straight from the fd, not through sys.stdin: TextIOWrapper's own
    # buffer could already hold the tail of an escape sequence, and select
    # on the fd would be blind to it, misreading an arrow key as a bare
    # esc.
    fd = sys.stdin.fileno()
    key = _read_byte(fd)
    if key == "\x1b":
        # A bare esc and the leading byte of an escape sequence (arrows,
        # function keys) are the same byte; only the sequence has a tail.
        while _input_ready(fd):
            key += _read_byte(fd)
    return key


def ask(question: str, *, tier: Tier = "plain") -> bool:
    """Ask QUESTION, returning True only on a `y` keypress.

    QUESTION follows the shared template `<Verb> <target>? <Consequence
    sentence>.`; the ` [y/N] ` suffix is added here so every prompt carries
    it (and its capitalized default) identically. The resolved answer is
    echoed after the prompt (`y` or `n`), since cbreak reads echo nothing
    and a transcript should show what the question was answered with.
    """
    style = _TIER_STYLES[tier]
    text = f"[{style}]{question}[/]" if style else question
    console.print(f"{text} [dim]\\[y/N][/] ", end="")

    context = _cbreak() if _HAVE_TERMIOS and sys.stdin.isatty() else nullcontext()
    with context:
        while True:
            try:
                key = _read_key()
            except KeyboardInterrupt:
                # ctrl+c quits, it does not cancel. The newline keeps the
                # next shell prompt off the prompt line, since cbreak mode
                # swallowed the terminal's own "^C" echo.
                console.print()
                raise
            if key in ("y", "Y"):
                answer = True
            elif key in ("n", "N", "\r", "\n", "\x1b", ""):
                # enter selects the capitalized default (no), esc cancels,
                # EOF cancels. A bare esc arrives alone; an escape sequence
                # is longer and gets swallowed below instead.
                answer = False
            else:
                continue
            break

    console.print("y" if answer else "n")
    return answer
