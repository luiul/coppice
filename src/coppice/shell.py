"""Shell integration: lets `coppice new` actually `cd` you into the new worktree.

`coppice` is a plain executable, not a shell function, so it cannot change its
parent shell's working directory on its own (a subprocess's `chdir` never
outlives the subprocess). `wt` (worktrunk) solves the same problem for its
own compiled binary via `wt config shell install`, which installs a shell
function wrapper that reads a directive file the binary writes and does
`builtin cd` in the *shell's own process*. This module mirrors that
mechanism for `coppice`, at smaller scale: one env var, one temp file, no
separate exec-file/completion machinery.

Wire-up (zsh), see README:

    eval "$(coppice shell init zsh)"

That defines `coppice` and `cop` shell functions (both names install as real
binaries, `cop` is just the shorter, more intuitive alias) which shadow the
real binaries: each points `COPPICE_CD_FILE` at a temp file, runs `command
<name> "$@"`, and if that file ends up non-empty (only `new` ever writes to
it, and only on success), `cd`s there before returning.
"""

from __future__ import annotations

import os
from pathlib import Path

CD_FILE_ENV_VAR = "COPPICE_CD_FILE"

_ZSH_TEMPLATE = """\
# coppice shell integration for zsh
_coppice_cd_wrapper() {
  local bin="$1"
  shift
  local cd_file
  cd_file="$(mktemp)"
  COPPICE_CD_FILE="$cd_file" command "$bin" "$@"
  local exit_code=$?
  if [[ -s "$cd_file" ]]; then
    # `builtin cd` bypasses any user `cd` alias/function (e.g. zoxide's
    # `alias cd=__zoxide_z`), same reasoning as worktrunk's own wrapper.
    builtin cd -- "$(<"$cd_file")"
  fi
  rm -f "$cd_file"
  return $exit_code
}
coppice() { _coppice_cd_wrapper coppice "$@"; }
cop() { _coppice_cd_wrapper cop "$@"; }
"""

_BASH_TEMPLATE = """\
# coppice shell integration for bash
_coppice_cd_wrapper() {
  local bin="$1"
  shift
  local cd_file
  cd_file="$(mktemp)"
  COPPICE_CD_FILE="$cd_file" command "$bin" "$@"
  local exit_code=$?
  if [[ -s "$cd_file" ]]; then
    builtin cd -- "$(cat "$cd_file")"
  fi
  rm -f "$cd_file"
  return $exit_code
}
coppice() { _coppice_cd_wrapper coppice "$@"; }
cop() { _coppice_cd_wrapper cop "$@"; }
"""

TEMPLATES = {"zsh": _ZSH_TEMPLATE, "bash": _BASH_TEMPLATE}


def write_cd_file(path: Path) -> None:
    """Record PATH for the shell wrapper to `cd` into, if one is running us.

    A no-op when `COPPICE_CD_FILE` isn't set, i.e. whenever `coppice` is
    invoked directly rather than through the shell function from
    `coppice shell init`. Called only after a successful `coppice new`.
    """
    cd_file = os.environ.get(CD_FILE_ENV_VAR)
    if cd_file:
        Path(cd_file).write_text(str(path))
