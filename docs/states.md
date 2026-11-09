# Worktree facts, task state, and integration evidence

Git facts, Coppice task state, and Worktrunk results answer different questions. [The glossary](glossary.md) defines their terms. The human `cop list` display keeps them separate instead of putting them in one status or Merge cell.

## Git status and registration

- **Dirty**: the worktree has uncommitted changes. Staged, unstaged, untracked, and unmerged path counts are separate in `cop list`. Deleted and renamed paths can contribute to these counts. A path can appear in more than one category. Rename detection is disabled, so old and new paths count separately. The existing `clean` dirty check and `remove --force` policy do not change.
- **Stale**: Git still has a worktree registration, usually after its directory was removed outside Coppice or Worktrunk. Git calls a registration eligible for pruning `prunable`. Coppice shows prunable records as stale references, not clean worktrees. Its existing cleanup sweeps these records regardless of age or `--merged`.

Worktree type, Current, Branch, HEAD state, HEAD commit, Prunable, and Path mismatch are separate fields. `Main` means the original worktree from `git init` or `git clone`, not a default branch. `Linked` means an additional worktree. Either can use detached HEAD, shown as HEAD state `Detached` with Branch `N/A`. A path mismatch is Worktrunk's report about its path template, not a change to the branch name.

## Coppice task state

**Parked** (`cop park`) means a task is complete but its worktree stays on disk for follow-up, such as review comments or a hotfix. This is Coppice task state, not Git status. The mark is a `branch.<branch>.parked-at` timestamp in the repo's local Git config. It does not dirty the working tree and dies with the branch.

`cop list` shows Parked, Parked at, and Follow-up as separate fields. The Parked field records whether the mark is present, even when Follow-up makes the task active again. `cop sync` skips effectively parked worktrees. `cop remove` and ordinary `cop clean` refuse them until `cop unpark`. The existing sweep path is `cop clean --parked`, which selects worktrees parked a week or more ago, subject to its other checks.

The existing follow-up rule is a commit-time heuristic: if HEAD's reported commit timestamp is newer than the parked mark, the worktree reads as active again. The mark stays in config. An unknown HEAD timestamp does not disprove the mark, so the worktree stays parked. This is not a comparison with the HEAD identity at parking time. A changed HEAD with an older timestamp, or uncommitted edits alone, need not trigger Follow-up. The display does not change this shared heuristic. Use `cop unpark` to make the task active explicitly.

## Separate times

Directory created uses the directory's filesystem birth time. HEAD committed uses HEAD's committer timestamp. Local file changed uses the newest available file mtime among Git status's changed and untracked paths. Ignored files are not part of that status scan. Deleted or unreadable paths cannot supply a file time.

Each field shows `Unknown` when its own evidence is absent. Times use ISO format in the local time zone with a UTC offset. There is no fallback from directory time to commit time, or from file time to commit time, in the new human display. These fields are not branch creation dates and do not prove task activity.

The old shared age rule and `clean --modified` rule are unchanged. The shared age rule can fall back from directory birth time to HEAD commit time. The old modified rule uses the later of HEAD commit time and available changed-file times. JSON's existing `last_modified` field and the `cop new` preview retain those rules. Separate display times do not redefine cleanup policy.

## Local ancestry

Ahead, Behind, and HEAD contained compare against an explicit local ref, `refs/heads/<cached default name>`. Worktrunk's cached default name supplies the name, with cached `origin/HEAD` as a fallback. The main worktree's checked-out branch is never the source of that target.

Ahead counts commits in HEAD but not in the ref. Behind counts commits in the ref but not in HEAD. HEAD contained means HEAD is reachable from the ref by strict ancestry, including equal commits. Missing local refs or shallow history show `Unknown`. This is the reachability meaning of [Git's `branch --merged`](https://git-scm.com/docs/git-branch). Content equality after a squash merge is not proof of HEAD ancestry.

## Worktrunk comparison

Worktrunk's Reported relation, Content integrated, Integration method, and Predicted conflicts stay separate from local ancestry. Its comparison target is explicit repo metadata. The schema 1 data does not name its exact comparison ref, so the target shows `Unknown`. A content result can describe integrated changes even when the original commits are not reachable from another ref. A predicted conflict is not an unresolved index entry. `Yes` needs explicit conflict evidence. Other compared states show `Unknown`, not a promise of no conflicts. Neither result proves HEAD is contained in Coppice's local ref.

Older previews and cleanup keep their existing buckets for Worktrunk's `main_state`:

| Legacy bucket | Worktrunk states |
|---|---|
| `merged` | `empty`, `integrated`, `same_commit`, `behind` |
| `unmerged` | `ahead`, `diverged` |
| `conflict` | `would_conflict` |
| `unknown` | Other or absent states, including `orphan` and `is_main` |

These buckets are Coppice policy labels, not Git's strict merged test. The unchanged `clean --merged` filter uses the legacy `merged` bucket along with its other checks. `remove` and `clean` retain their existing branch-preservation rules. No comparison result grants permission to remove a checkout. Check uncommitted and ignored files, publication, attached tools, and other workers separately.

## Cached tracking data

Tracking remote, Tracking branch, Tracking ahead, and Tracking behind display Worktrunk's existing `remote` data. The tracking branch is the configured upstream, not necessarily the remote default. Counts describe the cached remote-tracking branch, not a fresh server check. These fields add no collection or network calls. They stay separate from local branch history and fresh remote-default integration. A missing tracking record shows `N/A`; incomplete fields within it show `Unknown`.

## Remote evidence and PR history

Remote default checks the current committed HEAD against the fresh actual default branch of `origin`. Direct ancestry can prove `Reached`. Exact merged-PR evidence can also prove integration when the original HEAD matches and its merge commit is reachable from the observed remote default. New commits do not inherit an older PR's result. Dirty edits are independent of this check.

PR history still matches by repo and branch name. PR lookup, PR state, PR number, and Additional PRs are separate fields. Reused branch names can match older PRs. `--pr-detail` prints every match with its full URL. Historical PR state is not proof that the current HEAD reached the remote default.

Main worktrees are display rows, not a promise of new GitHub checks. A row without collected remote-default or PR evidence shows `Not checked`, not an invented result. `Unknown` means needed evidence is missing or incomplete. `N/A` means a check does not apply.

`--no-pr` and `--no-remote-default` skip their existing checks independently. JSON has no new fields or collection and performs neither GitHub lookup. The display adds no new family of network calls and does not change cleanup or removal. See [the command reference](commands.md#remote-default-in-list) for the remote evidence rules and budgets.
