# Glossary

Git terms describe repository facts. Coppice terms describe the display and task state. Worktrunk terms describe its path rules and comparison results. These are separate sources, not interchangeable names for the same evidence.

## Git terms

| Term | Meaning |
|---|---|
| Repository (repo) | The Git database of objects and refs, with the project's worktrees attached to it. |
| Working tree | The actual checked-out files on disk. It is not the index or the commit history. |
| Worktree | A checkout attached to a repo, with its own working tree, index, and HEAD. It can use a branch or detached HEAD. |
| Main worktree | The original worktree made by `git init` or `git clone`. Its type does not say which branch it uses. |
| Linked worktree | An additional worktree made by `git worktree add`, including through Worktrunk. |
| Branch | A named ref pointing to a commit. A local branch uses `refs/heads/<name>`. A branch has no directory of its own. |
| HEAD | The current checkout's reference to a branch, or directly to a commit when detached. HEAD commit shows the current commit identity. |
| Detached HEAD | HEAD points directly to a commit instead of a branch. A detached worktree is still a worktree. |
| Index | The staging area for the next commit. Each worktree has its own index. |
| Staged | Paths with changes in the index relative to HEAD. |
| Unstaged | Paths with changes in the working tree relative to the index. |
| Untracked | Files not tracked in the index. Ignored files are not part of this count. |
| Unmerged | Paths with unresolved index entries from a conflict. This is not a branch's integration status. |
| Ahead | Commits reachable from HEAD but not from the named comparison ref. |
| Behind | Commits reachable from the named comparison ref but not from HEAD. |
| HEAD contained | HEAD is reachable from the named target by strict commit ancestry, including when the commits are equal. Equal file content alone does not prove this. |
| Remote-tracking ref | A local record of a remote branch, such as `refs/remotes/origin/main`. It can be older than the remote. |
| Prunable | Git's worktree registration can be pruned, usually because its directory is missing. A registration is not proof that files still exist. |

Official references: [Git worktree](https://git-scm.com/docs/git-worktree), [Git glossary](https://git-scm.com/docs/gitglossary), [Git status](https://git-scm.com/docs/git-status), and [Git branch](https://git-scm.com/docs/git-branch). Git's `branch --merged` uses strict reachability, not content equality or PR history.

## Coppice display and task terms

| Term or field | Meaning |
|---|---|
| Project | The codebase as one unit, its repo and attached worktrees. Coppice names it from the main directory's basename. |
| Registry | The shared list of known repos at `~/.cache/wt/known-repos`. This repo registry is separate from Git's worktree registrations. |
| Scope | The repos a command lists or changes. An explicit repo path selects one repo. An omitted path uses known repos and the current repo. |
| Worktree type | `Main` or `Linked`, using Git's worktree terms. It does not identify a default branch. |
| Current | Whether the caller's resolved worktree root matches this worktree. Nested worktrees use exact root identity, not path-prefix matching. |
| Branch | The checked-out branch name, or `N/A` for detached HEAD. |
| HEAD state | `Attached` when HEAD names a branch, `Detached` when it points directly to a commit. |
| Worktree ID | A separate display identifier, such as `W1`, repeated across the repository's tables and collection issues. It is not a branch or Git ref. |
| HEAD commit | The current committed HEAD identity. |
| Path | The worktree directory. Sessions, terminals, and editors attach to this path, not to a branch name. `cop list -v` shows the full path without truncation and wraps it when needed. The home directory can appear as `~`. |
| Prunable | Whether the reported worktree registration is eligible for pruning. Prunable entries do not get assumed clean status or invented filesystem facts. |
| Path mismatch | Worktrunk reports that the actual directory does not match its expected path for the checked-out branch. It is separate from Branch. |
| Staged paths | The count of staged paths, relative to HEAD. |
| Unstaged paths | The count of tracked paths changed relative to the index. |
| Untracked paths | The count of untracked files, excluding ignored files. |
| Unmerged paths | The count of paths with unresolved index conflicts. |
| Git operation | Worktrunk's reported operation, such as merge or rebase. An absent report does not prove that no operation is in progress. |
| Directory created | The filesystem birth time of the worktree directory, where available. It is not the branch creation date or the HEAD commit time. |
| HEAD committed | The committer timestamp of the current HEAD commit. It is not evidence of a recent local edit. |
| Local file changed | The newest available filesystem modification time among files reported changed or untracked by Git status. Deleted or unreadable paths cannot provide a file time. A clean tree or absent evidence shows `Unknown`, not a commit-time fallback. |
| Parked | Whether Coppice's stored branch mark is present. This is task state, not Git status. Follow-up shows the existing rule that can make a marked task active again. |
| Parked at | The `branch.<branch>.parked-at` timestamp in local Git config. It is separate from directory and commit times. |
| Follow-up | The existing heuristic finds a HEAD commit timestamp newer than the parked mark. This is not a saved-HEAD comparison and does not detect every change. |
| Disk usage | The existing estimate of the worktree's on-disk size. `--size` shows it and `--no-size` skips its directory walk. |
| Local comparison branch | The explicit local comparison ref, `refs/heads/<cached default name>`. The name comes from Worktrunk's cached default name, with cached `origin/HEAD` as a fallback. It never comes from the main worktree's checked-out branch. |
| Local target selection | The cached source used to choose the local comparison name. It is not a fresh remote lookup. |
| Local target commit | The observed commit identity of the local comparison ref. |
| Commits ahead | Commits in HEAD but not in the local comparison ref. |
| Commits behind | Commits in the local comparison ref but not in HEAD. |
| HEAD integration | The Remote default integration result for the current committed HEAD, against the fresh actual default branch of `origin`. Dirty edits remain a separate fact. |
| Tracking remote | The remote name in Worktrunk's existing tracking data. It need not be `origin`. |
| Tracking branch | The configured upstream branch in that tracking data, not the remote default branch. |
| Tracking ahead | The reported count of commits ahead of the cached remote-tracking branch. It is not a fresh server check. |
| Tracking behind | The reported count of commits behind the cached remote-tracking branch. It is not a fresh server check. |
| Remote name | The remote checked for HEAD integration, currently `origin`. |
| Remote default branch | The actual default branch observed for that remote during this run. |
| Observed remote commit | The commit identity of that observed remote default. |
| Remote checked at | The observation time of the remote snapshot. |
| PR lookup | Whether the branch-name lookup is complete, failed, does not apply, or was not collected. Only a complete lookup can establish that no PR matches. |
| PR state | The selected PR's state, such as Draft, Open, Merged, or Closed. `None` after a complete lookup means no PR matches. It is separate from lookup status. |
| PR number | The selected PR number, shown separately from state. |
| Additional PRs | The count of other PRs matching the same repo and branch name. `--pr-detail` prints all matches with full URLs. |
| Unknown | A value needs evidence that is missing, incomplete, or failed. It does not mean zero, clean, contained, or safe to remove. |
| Not checked | No result was collected for this row. Main display rows do not imply extra remote-default or PR checks. |
| N/A | The check does not apply, such as a PR lookup for detached HEAD. |
| Explain | `cop list --explain` prints a concise field glossary without a Git, `gh`, or `wt` scan. |

The staged, unstaged, untracked, and unmerged fields count paths from Git status, not lines or commits. Rename detection is disabled, so old and new paths count separately. Categories can overlap and must not be added to get a unique file total.

The three time fields never substitute for each other. Missing values show `Unknown`. Display times use ISO format in the local time zone with a UTC offset. The human display does not change JSON fields, collection, cleanup rules, or the `cop new` preview.

Cached tracking fields only display Worktrunk's existing `remote` data. They trigger no new collection or network calls. A missing tracking record shows `N/A`. Incomplete fields within a tracking record show `Unknown`. Tracking counts are separate from the local comparison and fresh remote-default integration.

## Worktrunk terms

| Term or field | Meaning |
|---|---|
| Expected path | The directory Worktrunk's path template assigns to a branch. Coppice uses Worktrunk to compute and move it. |
| Worktrunk comparison target | The target of Worktrunk's reported comparison, shown separately from Coppice's local ref and the fresh remote default. The schema 1 data does not identify the exact target ref, so this metadata shows `Unknown`. |
| Reported relation | Worktrunk's reported comparison state, not Coppice's strict ancestry result against a named local branch. |
| Content integrated | Worktrunk's reported content result, including rewritten history where content may be integrated without strict HEAD ancestry. It is not proof that HEAD is contained in Coppice's local comparison ref. |
| Integration method | Worktrunk's reported content-equivalence method, when present. |
| Predicted conflicts | Worktrunk's merge prediction for its comparison target. `Yes` needs explicit conflict evidence. Other compared states show `Unknown`, not an assumed `No`. It is separate from current unmerged index paths and does not guarantee a future merge outcome. |
| Legacy merged bucket | Coppice's existing grouping of Worktrunk states for cleanup and older previews. The word `merged` here is not Git's strict reachability test and does not grant removal permission. |

See [list fields and options](commands.md#list-layout-and-local-fields) and [state and evidence limits](states.md) for details.
