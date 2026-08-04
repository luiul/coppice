# coppice

[![CI](https://github.com/luiul/coppice/actions/workflows/ci.yml/badge.svg)](https://github.com/luiul/coppice/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A path-based CLI for managing git worktrees across every repo on your machine at once.

Built on top of [`wt`](https://worktrunk.dev) (worktrunk), which does the
actual worktree work (creating them, running hooks, etc.). `coppice` adds
the layer `wt` doesn't: a single set of commands that reach across *all*
your repos, from *anywhere* on disk, so you don't have to `cd` into a repo
just to check or clean up its worktrees.

## Why

Worktrees let you have several branches checked out side by side, no
stashing, no switching. The catch: once you're juggling worktrees across
several repos, it's easy to lose track of what's checked out where, and
stale ones quietly pile up on disk.

```
              ┌──────────────────────────────┐
              │  coppice, run from anywhere  │
              │ new · list · remove · clean  │
              └──────────────────────────────┘
                              │ shared registry of known repos
        ┬─────────────────────┼─────────────────────┬
        │                     │                     │
   ~/code/api          ~/code/frontend        ~/code/infra
      main                  main                  main
   fix-auth *            new-navbar          wip-old (stale)
   add-metrics            redesign
```

Each command takes an explicit **path** (or defaults to every repo it
already knows about), instead of relying on your current directory. That's
the whole idea: `cop new ./api` works the same whether you're standing in
`./api`, in `~/code/frontend`, or in your home directory.

## What it looks like

```console
$ cop new ./api --branch fix-auth
Worktree branch: fix-auth
Created worktree for fix-auth @ /Users/you/code/api/.worktrees/fix-auth

$ cop list
api:
  fix-auth  2d [current]
  add-metrics  5d

frontend:
  new-navbar  1d
  redesign  9d (dirty)

Total: 4 worktree(s) across 2 repo(s).

$ cop clean --dry-run
Scanning 2 repo(s) for worktrees older than 2w...

frontend:
  rm    16d  old-experiment  (240M on disk, merged, branch will be deleted)

Total reclaimable: 240M across 1 worktree(s).
Dry run, nothing removed.
```

## Install

**Prerequisite:** [`wt`](https://worktrunk.dev) (worktrunk) must already be
installed and on `PATH`, `coppice` shells out to it for every worktree
operation. If it's missing, commands fail fast with a clear message instead
of a partial or silent failure. See [worktrunk.dev](https://worktrunk.dev)
for install instructions.

```bash
uv tool install coppice
# or run ad hoc, no install:
uvx coppice ...
```

Then, so `cop new` can `cd` you into the worktree it creates, add shell
integration to your rc file:

```bash
# zsh (~/.zshrc) or bash (~/.bashrc)
eval "$(coppice shell init zsh)"   # or: bash
```

This defines `coppice` and its shorter alias `cop` as shell functions. They
behave identically, use whichever you prefer, every example below works
with either name.

Two more tools are auto-detected and used if present, neither is required:
[`fzf`](https://github.com/junegunn/fzf) powers `cop remove`'s interactive
picker, and [`gh`](https://cli.github.com) lets `cop clean` skip branches
with an open PR. Without them, those features just degrade gracefully.

## Usage

```bash
cop new ./api              # create/reuse a worktree for the repo at ./api
cop new .                  # ...for the repo you're standing in
cop new . --branch fix-x   # skip the prompt, use a specific branch name
cop list                   # list worktrees across every known repo
cop list ./api             # ...just this one
cop list --json            # same, as JSON
cop remove my-branch       # remove a worktree by branch name
cop remove a b --repo api --yes
cop remove                 # ...or omit the branch for an fzf multi-select picker
cop clean --dry-run        # preview worktrees older than 2 weeks, with size + merge status
cop clean -v               # remove them (skips dirty worktrees and ones with an open PR)
cop status                 # is wt on PATH, what's in the shared registry
```

Run `cop --help` or `cop <command> --help` for the full option list.

### How the registry works

`coppice list`/`remove`/`clean` operate over a registry of "known repos"
(`~/.cache/wt/known-repos`), not just your current directory. That registry
is populated automatically, either by `wt`'s own `registry` post-start
hook, or, if that hook isn't configured, by `coppice new` itself the first
time it touches a repo. Either way, once you've run `coppice new` in a repo
once, it stays visible to every other command from anywhere on disk.

### Shell integration, in more detail

`coppice`/`cop` is a plain executable, not a shell function, so it can't
change your shell's working directory on its own, a subprocess's `cd` never
outlives the subprocess. (`wt` has the same problem for its own binary, and
solves it the same way, via `wt config shell install`.)

`coppice shell init <zsh|bash>` prints small wrapper functions, one for
`coppice`, one for `cop`, that run the real binary with an env var pointed
at a temp file, then `cd` there afterward if `new` wrote a path into it
(only `new` ever does, and only on success). Without the wrapper, `new`
still works, it just prints the resulting path instead of moving you
there:

```bash
cd "$(cop new . --branch fix-x | tail -1 | sed 's/.* @ //')"
```

## Status

Early days. `new`, `list`, `remove` (with an interactive `fzf` picker),
`clean`, `status`, and shell integration cover the daily loop. See [open
issues](https://github.com/luiul/coppice/issues) for what's not there yet.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ty check
```

## License

MIT, see [LICENSE](LICENSE).
