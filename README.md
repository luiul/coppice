# coppice

[![CI](https://github.com/luiul/coppice/actions/workflows/ci.yml/badge.svg)](https://github.com/luiul/coppice/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A path-based CLI for git worktrees, built on top of [`wt` (worktrunk)](https://worktrunk.dev).

`coppice` is the planned Python replacement for [dotfiles' `wtx`](https://github.com/luiul/dotfiles/blob/main/zsh/.zsh_config/funcs_wt.zsh),
a zsh entrypoint that wraps `wt` with branch naming, cross-repo listing, and
cleanup. See [dotfiles#6](https://github.com/luiul/dotfiles/issues/6) for the
plan this repo implements.

`coppice` does not reimplement worktree lifecycle, hooks, or herdr
registration, `wt` stays the single source of truth for all of that. This
tool only shells out to `wt` (and `git`) and adds:

- **Path-first commands.** Every subcommand takes an explicit repo path
  instead of relying on your current working directory, e.g. `coppice new
  ./tardis` from anywhere, not just `cd ./tardis && coppice new`.
- **Branch-name normalization** for the interactive `new` prompt (lowercase,
  dash-joined, 40-character cap).
- **Cross-repo listing/removal** via a registry file shared with `wtx`
  (`~/.cache/wt/known-repos`), so both tools see the same set of repos
  regardless of which one created a worktree.

## Install

Requires [`wt`](https://worktrunk.dev) on `PATH`.

```bash
uv tool install coppice
# or run ad hoc:
uvx coppice ...
```

Then add shell integration (so `coppice new` can `cd` you into the resulting
worktree, see below) to your shell rc file:

```bash
# zsh (~/.zshrc) or bash (~/.bashrc)
eval "$(coppice shell init zsh)"   # or: bash
```

## Usage

```bash
coppice new ./tardis          # create/reuse a worktree for the repo at ./tardis
coppice new .                 # ...for the repo you're standing in
coppice new . --branch fix-x  # skip the prompt, use a specific branch name
coppice list                  # list worktrees across every known repo
coppice list ./tardis         # ...just this one
coppice list --json           # same, as JSON
coppice remove my-branch      # remove a worktree by branch name
coppice remove a b --repo tardis --yes
```

Run `coppice --help` or `coppice <command> --help` for the full option list.

### Shell integration

`coppice` is a plain executable, not a shell function, so it can't change
your shell's working directory on its own, a subprocess's `cd` never outlives
the subprocess. `wt` itself has the same problem for its own compiled binary,
and solves it with `wt config shell install`: a shell function wrapper reads
a directive file the binary writes and `cd`s in the *shell's own process*.

`coppice shell init <zsh|bash>` prints a small wrapper mirroring that trick:
it shadows the `coppice` command, runs the real binary with `COPPICE_CD_FILE`
pointed at a temp file, and `cd`s there afterward if `coppice new` wrote a
path into it (only `new` ever does, and only on success). Without it,
`coppice new` still works, it just prints the resulting path instead of
moving you there, so `cd` into it yourself:

```bash
cd "$(coppice new . --branch fix-x | tail -1 | sed 's/.* @ //')"
```

## Status

Early days. `new`, `list`, `remove`, and shell integration cover the basics;
see [open issues](https://github.com/luiul/coppice/issues) for what's not
there yet (`clean`, an interactive removal picker, herdr registration
independent of `wt`'s own hooks, and more).

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ty check
```

## License

MIT, see [LICENSE](LICENSE).
