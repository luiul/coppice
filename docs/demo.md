# What it looks like

```console
$ cop new ~/dbt-models --branch add-customer-id-column
Worktree branch: add-customer-id-column

Created worktree for add-customer-id-column @ /Users/you/dbt-models/.worktrees/add-customer-id-column

$ cop list
Branch                      Created   Size    Working tree   Merge
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
airflow-dags (main: master)
  update-dag-schedule           1d    312M    clean          merged
  debug-failing-pipeline        1w    298M    dirty          unmerged

dbt-models (main: main)
  add-customer-id-column        2d    1.1G    clean          unmerged
  fix-ingestion-retry           5d    1.0G    clean          merged

4 worktrees in 2 repos · 2.7G on disk · 2 merged (cop clean --merged) · 1 dirty.
5 more repos with no extra worktrees (show with: cop list --all)

$ cop clean --dry-run
Scanning 2 repos for worktrees older than 14d...

airflow-dags (~/airflow-dags):
  rm       2w  backfill-2023-orders  (240M on disk, merged, branch will be deleted)

Scanned 2 repos, 5 worktrees: 1 removable, 0 dirty, 0 with an open PR, 4 under 14d old.
Total reclaimable: 240M across 1 worktree.
Dry run, nothing removed.

$ cop sync
Worktree                     Result     Detail
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
dbt-models (base: origin/main)
  main worktree              ff         2 commits from origin/main
  add-customer-id-column     synced     2 commits from origin/main
  fix-ingestion-retry        conflict   would conflict; left untouched
  debug-pipeline             skip       uncommitted changes

Synced 1 worktree, fast-forwarded 1 main checkout, 1 skipped, 1 conflict.
```
