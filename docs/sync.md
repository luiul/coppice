# Keeping worktrees current: `cop sync`

A worktree you come back to days later has fallen behind its base branch.
`cop sync` fixes that in one pass: it fetches each repo's base remote
(`origin/<default branch>`, resolved fresh from the remote, or `--base`),
fast-forwards the main worktree's own checkout of the base branch when
it's clean (`--no-main` skips that), then merges the base into every
managed worktree. Dirty worktrees, stale references, and detached HEADs
are skipped; worktrees whose merge *would* conflict are predicted with
`git merge-tree` and left untouched, reported as conflicts, so a worktree
is never left half-merged. `--dry-run` previews all of it accurately,
since the classification is pure plumbing. Every run closes with a `Needs
attention` extract listing the worktrees that did not sync correctly
(conflicts, warning-tier skips, errors, stale references), grouped by
repo, so a long run's action items don't scroll away above the summary
line.

Because sync merges (rather than rebases or squash-merges), the merge base
advances with every run: each conflict is resolved exactly once, pushed
branches never need force-pushing, and `wt`'s `main_state` moves from
`diverged` back to `ahead` once a worktree is current. The merge commits
collapse at landing time anyway, since `wt merge` squashes the branch.
