# What it looks like

```console
$ cop new ~/dbt-models --branch add-customer-id-column
Worktree branch: add-customer-id-column

Created worktree for add-customer-id-column @ /Users/you/dbt-models/.worktrees/add-customer-id-column

$ cop list

airflow-dags
Type    Branch                 Local Task Origin  PR           Size
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Main   master                 Clean      -       -            410M
 Linked debug-failing-pipeline  Dirty      Pending Open #123    298M
 Linked update-dag-schedule    Clean      Reached Merged #101  312M

dbt-models
Type    Branch                 Local Task Origin  PR        Size
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Main   main                   Clean      -       -         1.2G
*Linked add-customer-id-column Clean      Pending Draft #45 1.1G
 Linked fix-ingestion-retry    Clean      Reached No PR     1.0G

Main worktrees: 2
Linked worktrees: 4
Hidden repositories without linked worktrees: 5. Use --all to show them.
Use --detail for all fields or --explain for definitions.

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
