# coppice

[![CI](https://github.com/luiul/coppice/actions/workflows/ci.yml/badge.svg)](https://github.com/luiul/coppice/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A path-based CLI for managing git worktrees, built to parallelize work: several
agents (and you) checked into the same repo at once, and every repo on your
machine reachable from a single set of commands.

Built on top of [`wt`](https://worktrunk.dev) (worktrunk), which does the
actual worktree work (creating them, running hooks, etc.). `coppice` adds
the layer `wt` doesn't: a single set of commands that reach across *all*
your repos, from *anywhere* on disk, so you don't have to `cd` into a repo
just to check or clean up its worktrees.

## Why

Worktrees let you check out several branches side by side, no stashing, no
switching. That matters more than ever now that work isn't just yours:
you're often running one or more coding agents alongside your own edits,
adding a column here, fixing a DAG's schedule there, while you're
debugging a failing pipeline, and each of them needs an isolated checkout
to work in without stepping on the others. `coppice` makes spinning those
up (and cleaning them back up)
a one-liner, from anywhere, whether it's one repo or twenty.

### Parallelize work in a single repo

Each worktree is a full, isolated checkout sharing the same `.git` history,
so a human and any number of agents can work the same repo at once: one
agent adding a column, another fixing a DAG's schedule, you debugging a
failing pipeline, none of them blocking each other:

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

Shapes carry meaning here: the cylinder is a data store (a warehouse
table), the hexagon is a config change (a DAG's schedule), the diamond is
a decision point (is the run healthy?). `cop new ~/dbt-models` (or a bare
`cop ~/dbt-models`) spins up the next worktree; `cop clean` sweeps up
whichever ones are done, merged, and idle.

### Reach every repo, from anywhere

The catch with juggling worktrees across *several* repos, your dbt
project, your Airflow DAGs, your ingestion jobs, is that it's easy to lose
track of what's checked out where, and stale ones quietly pile up on disk.
`coppice` tracks every repo it's touched in a shared registry, so
`list`/`clean` sweep across all of them, no matter which one you're
standing in:

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

The registry (cylinder, same as the warehouse table above, it's a data
store too) is what makes this possible: each command takes an explicit
**path**, or defaults to every repo it already knows about, instead of
relying on your current directory. That's the whole idea: `cop new
~/dbt-models` works the same whether you're standing in `~/dbt-models`, in
`~/airflow-dags`, or in your home directory.

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
cop new ~/dbt-models                     # create/reuse a worktree for the repo at ~/dbt-models
cop new .                                # ...for the repo you're standing in
cop new . --branch update-dag-schedule   # skip the prompt, use a specific branch name
cop list                                 # list worktrees across every known repo
cop list ~/dbt-models                    # ...just this one
cop list --json                          # same, as JSON
cop remove add-customer-id-column        # remove a worktree by branch name
cop remove a b --repo dbt-models --yes
cop remove                               # ...or omit the branch for an fzf multi-select picker
cop clean --dry-run                      # preview worktrees older than 14 days, with size + merge status
cop clean -v                             # remove them (skips dirty worktrees and ones with an open PR)
cop clean --merged                       # sweep every merged branch instead, regardless of age
cop clean 7 --repo dbt-models --merged   # ...scoped to one repo, merged only
cop status                               # is wt on PATH, what's in the shared registry
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
