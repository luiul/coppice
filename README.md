# coppice

[![CI](https://github.com/luiul/coppice/actions/workflows/ci.yml/badge.svg)](https://github.com/luiul/coppice/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A path-based CLI for git worktrees, built on top of [`wt` (worktrunk)](https://worktrunk.dev).

Installed as two identical commands, `coppice` and the shorter `cop` alias,
use whichever you like; every example below works with either name.

See [dotfiles#6](https://github.com/luiul/dotfiles/issues/6) for the design
rationale this repo implements.

`coppice` does not reimplement worktree lifecycle, hooks, or herdr
registration, `wt` stays the single source of truth for all of that. This
tool only shells out to `wt` (and `git`) and adds:

- **Path-first commands.** Every subcommand takes an explicit repo path
  instead of relying on your current working directory, e.g. `cop new
  ./tardis` from anywhere, not just `cd ./tardis && cop new`.
- **Branch-name normalization** for the interactive `new` prompt (lowercase,
  dash-joined, 40-character cap).
- **Cross-repo listing/removal** via a registry file (`~/.cache/wt/known-repos`)
  populated by `wt`'s own `registry` post-start hook (or self-healed by
  `cop new` when that hook isn't configured).

## Install

**Prerequisite:** [`wt`](https://worktrunk.dev) (worktrunk) must already be installed and on `PATH`.
`coppice` shells out to it for every worktree operation and does not work
without it; commands fail with a clear error (`'wt' (worktrunk) is not
installed. See https://worktrunk.dev`) rather than a partial or silent
failure if it's missing. See [worktrunk.dev](https://worktrunk.dev) for `wt`
install instructions.

```bash
uv tool install coppice
# or run ad hoc:
uvx coppice ...
```

Then add shell integration (so `new` can `cd` you into the resulting
worktree, see below) to your shell rc file:

```bash
# zsh (~/.zshrc) or bash (~/.bashrc)
eval "$(coppice shell init zsh)"   # or: bash
```

This defines both `coppice` and `cop` as shell functions, `cop` is just the
shorter, more intuitive alias, both behave identically.

Optional, for extra features (both auto-detected, no config needed):
[`fzf`](https://github.com/junegunn/fzf) for `cop remove`'s interactive
picker, and [`gh`](https://cli.github.com) for `cop clean`'s open-PR
safety check. Neither is required; each feature just degrades gracefully
without them.

## Usage

```bash
cop new ./tardis          # create/reuse a worktree for the repo at ./tardis
cop new .                 # ...for the repo you're standing in
cop new . --branch fix-x  # skip the prompt, use a specific branch name
cop list                  # list worktrees across every known repo
cop list ./tardis         # ...just this one
cop list --json           # same, as JSON
cop remove my-branch      # remove a worktree by branch name
cop remove a b --repo tardis --yes
cop remove                # ...or omit the branch for an fzf multi-select picker
cop clean --dry-run       # preview worktrees older than 2 weeks, with size + merge status
cop clean -v              # remove them (skips dirty worktrees and ones with an open PR)
cop status                # is wt on PATH, what's in the shared registry
```

`coppice` works identically everywhere above, `cop` is just shorter to type.

Run `cop --help` or `cop <command> --help` for the full option list.

### Shell integration

`coppice`/`cop` is a plain executable, not a shell function, so it can't
change your shell's working directory on its own, a subprocess's `cd` never
outlives the subprocess. `wt` itself has the same problem for its own
compiled binary, and solves it with `wt config shell install`: a shell
function wrapper reads a directive file the binary writes and `cd`s in the
*shell's own process*.

`coppice shell init <zsh|bash>` prints small wrappers mirroring that trick,
one for `coppice`, one for `cop`: each shadows the matching command, runs
the real binary with `COPPICE_CD_FILE` pointed at a temp file, and `cd`s
there afterward if `new` wrote a path into it (only `new` ever does, and
only on success). Without the wrapper, `new` still works, it just prints
the resulting path instead of moving you there, so `cd` into it yourself:

```bash
cd "$(cop new . --branch fix-x | tail -1 | sed 's/.* @ //')"
```

## Status

Early days. `new`, `list`, `remove` (with an interactive `fzf` picker),
`clean`, `status`, and shell integration cover the daily loop; see [open
issues](https://github.com/luiul/coppice/issues) for what's not there yet
(herdr registration independent of `wt`'s own hooks, and more).

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ty check
```

## License

MIT, see [LICENSE](LICENSE).
