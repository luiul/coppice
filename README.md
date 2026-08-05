# coppice

[![CI](https://github.com/luiul/coppice/actions/workflows/ci.yml/badge.svg)](https://github.com/luiul/coppice/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A path-based CLI for managing git worktrees across every repo on your
machine, from a single set of commands.

Built on top of [`wt`](https://worktrunk.dev) (worktrunk), which does the
actual worktree work (creating them, running hooks, etc.). `coppice` adds
what `wt` doesn't: commands that reach across *all* your repos from
*anywhere* on disk.

## Why

Working on more than one thing in a repo usually means switching
branches one at a time: stash, checkout, work, then stash again to switch
back. That switching is sequential, even when the tasks themselves
aren't.

Git worktrees fix that: each branch gets its own directory, all sharing
the same `.git` history, so several branches can be checked out at once
instead of switched between. But `wt` (and raw `git worktree`) only
operate on one repo, from inside it.

**What `coppice` adds:** commands that create, list, and clean up
worktrees as one-liners, from *anywhere* on disk, across *every* repo
you've touched, not just the one you're standing in.

### Parallelize work in a single repo

Each worktree is a full, isolated checkout that shares the same `.git`
history, so a human and any number of agents can work on the same repo at
once, each in their own directory: one agent adding a column, another
fixing a DAG's schedule, you debugging a failing pipeline. None of them
block each other, and none of them have to wait for a branch switch:

```mermaid
flowchart TD
    main(["main branch — shared git history"])

    subgraph wtA["worktree · add-customer-id-column"]
        agentA["🤖 Agent A<br/>editing dbt model"]
        tableA[("orders table<br/>+ customer_id column")]
        agentA --> tableA
    end

    subgraph wtB["worktree · update-dag-schedule"]
        agentB["🤖 Agent B<br/>editing Airflow DAG"]
        configB{{"schedule: 2am → 5am"}}
        agentB --> configB
    end

    subgraph wtC["worktree · debug-failing-pipeline"]
        you["🧑 You<br/>investigating an incident"]
        decisionC{"dbt run passing?"}
        failTask["❌ stg_orders model failing"]
        you --> decisionC
        decisionC -- no --> failTask
    end

    main --> wtA
    main --> wtB
    main --> wtC

    linkStyle default stroke:#94a3b8,stroke-width:1.5px;

    classDef agent fill:#eef2ff,stroke:#6366f1,stroke-width:2px,color:#312e81;
    classDef human fill:#fff7ed,stroke:#f97316,stroke-width:2px,color:#7c2d12;
    classDef mainNode fill:#f8fafc,stroke:#94a3b8,stroke-width:2px,color:#0f172a;
    classDef store fill:#f5f3ff,stroke:#8b5cf6,stroke-width:2px,color:#4c1d95;
    classDef config fill:#ecfdf5,stroke:#10b981,stroke-width:2px,color:#065f46;
    classDef incident fill:#fef2f2,stroke:#f43f5e,stroke-width:2px,color:#881337;
    class agentA,agentB agent;
    class you human;
    class main mainNode;
    class tableA store;
    class configB config;
    class decisionC,failTask incident;
```

`cop new ~/dbt-models` (or a bare `cop ~/dbt-models`) spins up the next
worktree; once that work is done, `cop clean` sweeps up whichever ones
are merged and idle.

### Reach every repo, from anywhere

Juggling worktrees across *several* repos, your dbt project, your Airflow
DAGs, your ingestion jobs, makes it easy to lose track of what's checked
out where, and stale worktrees quietly pile up on disk. `coppice` tracks
every repo it's touched in a shared registry, so `list`/`clean` sweep
across all of them, no matter which one you're standing in:

```mermaid
flowchart TD
    cli[["coppice — run from anywhere<br/>new · list · remove · clean"]]
    reg[("shared registry<br/>of known repos")]
    dbt["repo: dbt-models"]
    airflow["repo: airflow-dags"]
    ingestion["repo: ingestion-service"]

    cli --> reg
    reg --> dbt
    reg --> airflow
    reg --> ingestion

    linkStyle default stroke:#94a3b8,stroke-width:1.5px;

    classDef cliNode fill:#eef2ff,stroke:#6366f1,stroke-width:2px,color:#312e81;
    classDef repoNode fill:#f8fafc,stroke:#94a3b8,stroke-width:2px,color:#0f172a;
    classDef regNode fill:#f5f3ff,stroke:#8b5cf6,stroke-width:2px,color:#4c1d95;
    class cli cliNode;
    class dbt,airflow,ingestion repoNode;
    class reg regNode;
```

The registry is what makes this possible: each command takes an explicit
**path**, or defaults to every repo it already knows about, instead of
relying on your current directory. `cop new ~/dbt-models` works the same
whether you're standing in `~/dbt-models`, in `~/airflow-dags`, or in your
home directory.

## Concepts

- **Repo**: a git repository, identified by its root directory, inside
  which `.git` lives: the database of every commit and branch.
  `coppice` works across as many repos as you have on disk, not just the
  one you're standing in.
- **Commit**: a snapshot of the repo's files at a point in time, plus a
  pointer to its parent commit(s). Every worktree of a repo shares the same
  history of commits; making a commit in one worktree makes it visible to
  every other worktree of that repo immediately.
- **Branch**: a named pointer to a commit, e.g. `add-customer-id-column`.
  It's cheap: git can have many branches with none of them checked out
  anywhere. Normally you work on one branch at a time in one directory,
  and switching (`git switch`/`git checkout`) requires committing or
  stashing first.
- **Worktree**: a separate directory on disk checked out to one branch,
  reading from the same `.git` database as every other worktree of that
  repo (see [One `.git`, many working directories](#one-git-many-working-directories)
  below). Only the repo's original directory, its **main worktree**,
  actually contains `.git`; every additional (**linked**) worktree just
  holds a small `.git` file pointing back to it. A worktree's identity is
  its directory, not its branch, the branch is just what it's checked out
  to; git refuses to check out the same branch in two worktrees at once.
  Instead of a single directory that changes branches over time, each
  worktree stays checked out to its own branch, so switching between them
  is just changing directories, no stashing required.
- **Current worktree**: whichever one you happen to be standing in when
  you run a command, shown as `[current]` in `cop list`.
- **Registry**: the shared list of repos `coppice`/`wt` have seen before
  (`~/.cache/wt/known-repos`). It's what lets `cop list`/`cop remove`/`cop
  clean` operate across every repo you've touched, not just the one
  you're standing in.
- **Scope**: the set of repos a command like `list`/`remove`/`clean`
  operates over. An explicit path (or `--repo`) scopes to just that one
  repo; omitting it defaults to every repo in the registry, plus the one
  you're standing in.

### One `.git`, many working directories

`.git` is a **directory that's a database**: every commit, branch, and
the full history that ties them together, stored as git's own object
format, not plain files you'd edit directly. It lives *inside* your
working directory, e.g. `~/dbt-models/.git`, sitting alongside the files
you actually edit, not somewhere separate.

A normal checkout has exactly one working directory, with that one
`.git` database inside it, so switching branches means mutating that
same directory in place, stash or commit, then `git switch`, over and
over. A worktree adds another working directory elsewhere on disk, but
it doesn't get its own copy of `.git`; only the original directory (the
**main worktree**) holds the real `.git` database. Every other
(**linked**) worktree just contains a `.git` *file*, a few bytes of text
pointing back at the main one, so all of them read and write the same
history:

```mermaid
flowchart LR
    subgraph classic["Without worktrees — one directory, one branch at a time"]
        direction TB
        subgraph dirC["~/dbt-models (working directory)"]
            gitC[(".git/<br/>commit database")]
        end
        onA["checked out: branch A"]
        stash["git switch B<br/>(stash/commit first)"]
        onB["checked out: branch B"]
        dirC --> onA --> stash --> onB
    end

    subgraph worktrees["With worktrees — one .git, many directories"]
        direction TB
        subgraph mainWt["~/dbt-models (main worktree) · branch A"]
            gitW[(".git/<br/>commit database")]
        end
        subgraph wtB["~/dbt-models/.worktrees/B (linked worktree) · branch B"]
            ptrB[".git file<br/>(pointer, not a copy)"]
        end
        subgraph wtC["~/dbt-models/.worktrees/C (linked worktree) · branch C"]
            ptrC[".git file<br/>(pointer, not a copy)"]
        end
        gitW -. shared history .-> ptrB
        gitW -. shared history .-> ptrC
    end

    classDef gitNode fill:#f5f3ff,stroke:#8b5cf6,stroke-width:2px,color:#4c1d95;
    classDef stateNode fill:#eef2ff,stroke:#6366f1,stroke-width:2px,color:#312e81;
    classDef actionNode fill:#fff7ed,stroke:#f97316,stroke-width:2px,color:#7c2d12;
    classDef ptrNode fill:#ecfdf5,stroke:#10b981,stroke-width:2px,color:#065f46;
    class gitC,gitW gitNode;
    class onA,onB stateNode;
    class stash actionNode;
    class ptrB,ptrC ptrNode;
    style dirC fill:#f8fafc,stroke:#94a3b8,stroke-width:2px,color:#0f172a;
    style mainWt fill:#f8fafc,stroke:#94a3b8,stroke-width:2px,color:#0f172a;
    style wtB fill:#f8fafc,stroke:#94a3b8,stroke-width:2px,color:#0f172a;
    style wtC fill:#f8fafc,stroke:#94a3b8,stroke-width:2px,color:#0f172a;
```

A worktree's identity is its directory, not its branch name; the branch
is just what it's checked out to, set when the worktree is created (`cop
new`/`git worktree add`). Git enforces the one constraint that makes this
safe: the same branch can never be checked out in two worktrees at once,
so `wtB` and `wtC` above are always on distinct branches.

### Worktree status: dirty, merged, stale

Three states `cop list`/`cop clean` report on and act on differently:

- **Dirty**: the worktree has uncommitted changes (staged, modified,
  untracked, deleted, or renamed). `clean` always skips dirty worktrees;
  `remove` refuses them unless you pass `--force`/`-f`.
- **Merged**: the branch has been merged into the repo's default branch.
  `remove`/`clean` keep the branch by default unless it's merged, or
  `-D`/`--force-delete` is passed. It's also what `clean --merged` filters
  on, instead of age.
- **Stale (dangling)**: the worktree's directory is already gone from disk
  (removed outside `coppice`/`wt`) but git still has a record of it.
  `clean` always removes these, regardless of age or the `--merged` flag.

### Branches vs. worktrees, in coppice's commands

coppice identifies things by **branch name** where it can, since that's
what you already think in, but what its commands actually create, list,
or remove is that branch's **worktree**, not the branch itself:

| Command | Takes | Does |
|---|---|---|
| `cop new PATH [--branch B] [--base REF]` | a repo **path** | creates branch `B` if it doesn't exist yet (from `REF`, default: `wt`'s default branch), plus a worktree checked out onto it |
| `cop list [PATH]` | nothing, or a repo path | lists worktrees, one per checked-out branch, **not** every branch in the repo |
| `cop remove BRANCH...` | one or more branch **names** | deletes each branch's worktree directory; the branch itself survives unless it's merged or `-D`/`--force-delete` is passed |
| `cop clean` | filters (age or `--merged`) | the bulk version of `remove`: same branch-vs-worktree distinction applies |

A branch with no worktree still exists; `git log`/`git checkout` see it
fine, it's just not checked out anywhere. It won't show up in `cop list`,
and there's nothing for `cop remove`/`cop clean` to act on.

## What it looks like

```console
$ cop new ~/dbt-models --branch add-customer-id-column
Worktree branch: add-customer-id-column
Created worktree for add-customer-id-column @ /Users/you/dbt-models/.worktrees/add-customer-id-column

$ cop list
dbt-models:
  add-customer-id-column  2d [current]
  fix-ingestion-retry  5d

airflow-dags:
  update-dag-schedule  1d
  debug-failing-pipeline  9d (dirty)

Total: 4 worktree(s) across 2 repo(s).

$ cop clean --dry-run
Scanning 2 repo(s) for worktrees older than 14d...

airflow-dags:
  rm    16d  backfill-2023-orders  (240M on disk, merged, branch will be deleted)

Total reclaimable: 240M across 1 worktree(s).
Dry run, nothing removed.
```

## Install

**Prerequisite:** [`wt`](https://worktrunk.dev) (worktrunk) must already be
installed and on `PATH`; `coppice` shells out to it for every worktree
operation. If it's missing, commands fail fast with a clear message. See
[worktrunk.dev](https://worktrunk.dev) for install instructions.

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

This defines `coppice` and its shorter alias `cop` as shell functions,
behaving identically; use whichever you prefer.

Two more tools are auto-detected and used if present, neither is required:
[`fzf`](https://github.com/junegunn/fzf) powers `cop remove`'s interactive
picker, and [`gh`](https://cli.github.com) lets `cop clean` skip branches
with an open PR. Without them, those features just degrade gracefully.

## Usage

```bash
cop new ~/dbt-models                     # create branch + worktree for the repo at ~/dbt-models (or reuse it)
cop new .                                # ...for the repo you're standing in
cop new . --branch update-dag-schedule   # skip the prompt, use a specific branch name
cop list                                 # table of worktrees (age, size, dirty/merge status) across every known repo
cop list ~/dbt-models                    # ...just this one
cop list --no-size                       # skip the (directory-walking) size column, for a faster listing
cop list --json                          # same data, as JSON
cop remove add-customer-id-column        # remove a worktree by branch name (branch itself kept unless merged/-D)
cop remove a b --repo dbt-models --yes
cop remove                               # ...or omit the branch for an fzf multi-select picker
cop clean --dry-run                      # preview worktrees (not branches) older than 14 days, size + merge status
cop clean --yes                          # remove them (skips dirty worktrees and ones with an open PR)
cop clean --merged                       # sweep every worktree on a merged branch instead, regardless of age
cop clean 7 --repo dbt-models --merged   # ...scoped to one repo, merged only
cop status                               # is wt on PATH, table of the shared registry (worktree count, size, health)
```

Run `cop --help` or `cop <command> --help` for the full option list. A
bare `cop PATH` is shorthand for `cop new PATH`.

### How the registry works

`list`/`remove`/`clean` operate over a registry of "known repos"
(`~/.cache/wt/known-repos`), not just your current directory. It's
populated automatically: by `wt`'s own `registry` post-start hook if
configured, or otherwise by `coppice new` itself the first time it
touches a repo. After that, the repo stays visible to every command from
anywhere on disk.

### Shell integration, in more detail

`coppice`/`cop` is a plain executable, not a shell function, so it can't
change your shell's working directory on its own: a subprocess's `cd`
never outlives the subprocess. (`wt` has the same problem, and solves it
the same way, via `wt config shell install`.)

`coppice shell init <zsh|bash>` prints wrapper functions that run the real
binary with an env var pointed at a temp file, then `cd` there afterward
if `new` wrote a path into it (only `new` does, and only on success).
Without the wrapper, `new` still works; it just prints the resulting path
instead of moving you there:

```bash
cd "$(cop new . --branch update-dag-schedule | tail -1 | sed 's/.* @ //')"
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
