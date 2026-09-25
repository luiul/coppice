# Worktree status: dirty, merged, stale, parked

Three git states `cop list`/`cop clean` report on and act on
differently:

- **Dirty**: the worktree has uncommitted changes (staged, modified,
  untracked, deleted, or renamed). `clean` always skips dirty worktrees;
  `remove` refuses them unless you pass `--force`/`-f`.
- **Merged**: the branch has been merged into the repo's default branch.
  `remove`/`clean` keep the branch by default unless it's merged, or
  `-D`/`--force-delete` is passed. It's also what `clean --merged` filters
  on, instead of age.
- **Stale** (git's porcelain calls this `prunable`): the worktree's
  directory is already gone from disk (removed outside `coppice`/`wt`)
  but git still has a record of it. `clean` always removes these,
  regardless of age or the `--merged` flag.

There's also a fourth, opt-in state: **parked** (`cop park`), for a
worktree whose task is complete but which stays on disk in case follow-up
arrives (review comments, QA, a hotfix). The states above are git state;
parked is *task* state. The mark is a `branch.<branch>.parked-at`
timestamp in the repo's git config: it dies with the branch (so `remove`
cleans it up) and never dirties the worktree. Parked worktrees render
dimmed in `cop list` with a blue `parked Nd` label and sort after live ones,
`cop sync` skips them (nothing to merge into a task-complete tree), and
`cop remove` and `cop clean` refuse them: a parked worktree is kept for
follow-up, so removing one needs a `cop unpark` first. The sweep path for
parked worktrees is `cop clean --parked`, which removes the ones parked a
week or more ago. The mark
means "nothing new since I marked it": if the branch head moves after the
mark, the worktree reads as active again with a `follow-up` note in
`cop list`, no cleanup job needed. Follow-up arrived? `cop unpark`, then
sync.

The Merge column in `cop list` (and `cop clean`'s preview labels) buckets
`wt`'s `main_state` into `merged` (nothing to integrate, safe to remove),
`unmerged` (has commits main doesn't, merges cleanly), `conflict` (has
commits main doesn't, and `wt`'s merge simulation says merging would
conflict), or `unknown` (`wt` can't relate the branch to main). Only the
`merged` bucket is removable via `clean --merged`; a `conflict` label is
an invitation to merge or rebase, never to delete.
