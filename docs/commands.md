# Command reference

## Branches vs. worktrees, in coppice's commands

coppice identifies things by **branch name**, since that's what you
think in, but what its commands actually create, list, or remove is
that branch's **worktree**, not the branch itself:

| Command | Takes | Does |
|---|---|---|
| `cop new PATH [--branch B] [--base REF]` | a repo **path** | creates branch `B` if it doesn't exist yet, locally or on the remote (from `REF`, default: the repo's actual default branch, resolved fresh from its remote rather than trusting `wt`'s own cache; an explicit `REF` is fetched from its remote first, best-effort, since `wt` forks from the remote-tracking ref as it stands locally), plus a worktree checked out onto it; if `B` already exists either way, asks before switching to it instead, unless `--yes`/`-y`; if `B`'s worktree path is occupied by a worktree checked out on another branch (a directory created for `B` earlier, later `git switch`ed away by hand), offers a remedy and retries: switch the occupier back to `B` in place (the default remedy, `s`: the directory stays put, the occupier's commits stay on its branch), or, when `wt` can move the occupier, relocate it to its own expected path (explicit `r` only: a fresh `B` worktree then takes over the old path, and tools attached to the old path, pi sessions, editors, terminals, keep pointing at it); never moves or creates directories when stdin isn't a terminal |
| `cop list [PATH]` | nothing, or a repo path | lists worktrees, one per checked-out branch, **not** every branch in the repo, and **not** the main worktree (see [Concepts](../README.md#concepts)); a worktree sitting at another branch's path (see the `cop new` remedy) is flagged ` @ <that-branch>/` next to its checked-out branch |
| `cop sync [BRANCH...]` | nothing, or branch **names** | merges the repo's base remote into each branch's worktree (all of them by default), keeping long-lived worktrees current |
| `cop relocate [PATH]` | nothing, or a repo path | moves each mismatched worktree (a directory created for one branch, later `git switch`ed to another, so the path and the checkout disagree) back to the path the worktree-path template assigns its branch, after a preview and confirmation; every move is computed by `wt step relocate`, never by a local copy of the template |
| `cop remove BRANCH...` | one or more branch **names** | deletes each branch's worktree directory; the branch itself survives unless it's merged or `-D`/`--force-delete` is passed |
| `cop clean` | filters (age or `--merged`) | the bulk version of `remove`: same branch-vs-worktree distinction applies |

A branch with no worktree still exists; `git log`/`git checkout` see it
fine, it's just not checked out anywhere. It won't show up in `cop list`,
and there's nothing for `cop remove`/`cop clean` to act on.

## Usage

```bash
cop new ~/dbt-models                     # create branch + worktree for the repo at ~/dbt-models (or reuse it)
cop new .                                # ...for the repo you're standing in
cop new . --branch update-dag-schedule   # skip the prompt, use a specific branch name
cop new . --branch update-dag-schedule --yes   # skip the confirmation when the branch already exists
cop sync                                 # fetch each repo's base remote and merge it into every worktree
cop sync --dry-run                       # preview what would be merged, changing nothing
cop sync feat-a feat-b                   # ...just these branches' worktrees
cop sync --repo ~/dbt-models             # ...scoped to one repo
cop sync --no-main                       # leave the main worktree's own checkout of the base branch alone
cop park                                 # mark the current worktree's branch parked (task complete, kept for follow-up)
cop park feat-a feat-b                   # ...or park by branch name (a worktree's directory name works too)
cop unpark feat-a                         # follow-up arrived: back to active (sync merges into it again)
cop relocate                             # move mismatched worktrees (flagged '@ other-dir/' in cop list) back to their template paths
cop relocate --dry-run                   # ...preview the moves, changing nothing
cop relocate ~/dbt-models                # ...scoped to one repo
cop list                                 # worktrees across every known repo (age, size, dirty/merge status)
cop list ~/dbt-models                    # ...just this one
cop list --all                           # ...also showing repos with no extra worktrees (hidden by default)
cop list --verbose                       # ...with a column for each worktree's on-disk path
cop list --no-size                       # skip the (directory-walking) size column, for a faster listing
cop list --json                          # same data, as JSON
cop remove add-customer-id-column        # remove a worktree by branch name (branch kept unless merged/-D; parked worktrees are refused: cop unpark first)
cop remove a b --repo dbt-models --yes
cop remove                               # ...or omit the branch for an fzf multi-select picker
cop clean --dry-run                      # preview worktrees (not branches) older than 14 days, size + merge status
cop clean --yes                          # remove them (skips dirty, parked, and open-PR worktrees)
cop clean --merged                       # sweep every worktree on a merged branch instead, regardless of age
cop clean --parked                       # sweep worktrees parked 7+ days ago (same dirty/open-PR safety rails)
cop clean 3 --parked --dry-run           # preview worktrees parked 3+ days ago
cop clean 7 --repo dbt-models --merged   # ...scoped to one repo, merged only
cop status                               # is wt on PATH, table of the shared registry (worktree count, size, health)
```

Run `cop --help` or `cop <command> --help` for the full option list. A
bare `cop PATH` is shorthand for `cop new PATH`.

## Confirmation prompts

Every destructive (or merely surprising) action asks first, and every
prompt behaves the same way: one keypress, no enter needed.

- `y` confirms; `n`, `esc`, or `enter` cancel; any other key is ignored;
  `ctrl+c` quits. Prompts end in `[y/N]`: the capitalized letter is the
  default answer, so a bare enter cancels.
- The prompt text follows one template, `<Verb> <target>? <Consequence
  sentence>. [y/N]`, in yellow for an ordinary destructive action and red
  when a force flag (`--force`/`--force-delete`) is in play.
- `--yes`/`-y` skips the prompt entirely, for scripts.
- When a worktree about to be removed has a VS Code window open on it,
  `remove` and `clean` mark its line with `(VS Code window open)` and
  suggest closing it first: deleting the directory out from under the
  window strands it. Detection lists the window titles (one System
  Events osascript call; the dotfiles `window.title` setting renders
  each title as the opened folder's full path, branch never matched)
  and matches exact folder paths, so same-named worktrees and phantom
  branches can't warn wrongly. A failed listing, or an empty one while
  Code runs (macOS culls the accessibility tree of a backgrounded
  app), stays silent rather than ever claiming "not open".

Same discipline as the canopy/understory dashboards (see dashkit's
[CONVENTIONS.md](https://github.com/luiul/dashkit/blob/main/CONVENTIONS.md)),
except the 10s auto-cancel: that exists because a dashboard's rows keep
repolling under an open prompt, and a one-shot CLI prompt has nothing
moving underneath it.

## How the registry works

`list`/`remove`/`clean` operate over a registry of known repos
(`~/.cache/wt/known-repos`), not just your current directory. It's
populated automatically: by `wt`'s own `registry` post-start hook if
configured (see [Automate everything that happens around a
worktree](why-worktrees.md#automate-everything-that-happens-around-a-worktree)), or
otherwise by `coppice new` the first time it touches a repo. After that,
the repo stays visible to every command from anywhere on disk.

## Shell integration, in more detail

`coppice`/`cop` is a plain executable, not a shell function, so it can't
change your shell's working directory on its own: a subprocess's `cd`
never outlives the subprocess. (`wt` has the same problem, and solves it
the same way, via `wt config shell install`.)

`coppice shell init <zsh|bash>` prints wrapper functions that run the real
binary with an env var pointing at a temp file, then `cd` there if `new`
wrote a path into it (only `new` does, and only on success). Without the
wrapper, `new` still works, it just prints the path instead of moving you
there:

```bash
cd "$(cop new . --branch update-dag-schedule | sed -n 's/.* @ //p' | tail -1)"
```
