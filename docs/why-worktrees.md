# Why worktrees, and why coppice

Working on more than one thing in a repo usually means switching
branches one at a time: stash, checkout, work, stash again. That's
sequential even when the tasks aren't.

Git worktrees fix that: each branch gets its own directory, all sharing
the same `.git` history, so several branches can be checked out at once.
But `wt` (and raw `git worktree`) only operate on one repo, from inside
it.

**What `coppice` adds:** commands that create, list, and clean up
worktrees as one-liners, from anywhere on disk, across every repo
you've touched.

## Parallelize work in a single repo

Each worktree is a full, isolated checkout sharing the same `.git`
history, so a human and any number of agents can work on the same repo at
once, each in their own directory: one agent adding a column, another
fixing a DAG's schedule, you debugging a failing pipeline. No branch
switching, no blocking each other:

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

## Reach every repo, from anywhere

Juggling worktrees across several repos, your dbt project, your Airflow
DAGs, your ingestion jobs, makes it easy to lose track of what's checked
out where, and stale worktrees pile up unnoticed. `coppice` tracks every
repo it's touched in a shared registry, so `list`/`clean` sweep across
all of them, regardless of which one you're standing in:

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
from `~/dbt-models`, `~/airflow-dags`, or your home directory.

## Automate everything that happens around a worktree

A plain `git worktree add` gets you an empty checkout: no `.venv`, no
editor window, no local `.env`. Closing that gap is `wt`'s job, and it's
the other big reason coppice builds on `wt` instead of shelling out to
raw git: `wt` runs user-defined **hooks**, shell commands fired at points
in a worktree's life (`post-start`, `pre-remove`, and more), scoped to
every repo or to one specific repo by URL. A `post-start` hook is what
turns `cop new` from an empty checkout into a ready one every time, and
it's also how coppice's own registry gets populated (see [How the
registry works](commands.md#how-the-registry-works)).

For real examples, see [my `wt`
config](https://github.com/luiul/dotfiles/blob/main/worktrunk/.config/worktrunk/config.toml):
`.venv` symlinking, copying gitignored config, opening an editor,
project-scoped `dbt deps`, and a `pre-remove` guard against removing
protected branches. Run `wt config create --project` to scaffold your
own; see the [worktrunk hooks docs](https://worktrunk.dev) for the full
reference.
