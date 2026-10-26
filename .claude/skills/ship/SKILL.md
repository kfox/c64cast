---
name: ship
description: >
  Take a change in this repository all the way to a pull request that is ready
  to merge: branch, implement, commit, review each change once at an effort set
  by its size and risk, open the PR as a draft, watch CI and GHAS until green,
  then mark it ready for review. Stops before merging — the merge is always the
  user's. Use when asked to implement a non-trivial change, or when asked to
  take a change through the full branch-review-PR workflow ("ship", "land",
  "take this to a PR").
---

# Ship a change

The standard workflow for non-trivial work in this repository. Every stage is
mandatory unless the user says otherwise, and the last one is a hard stop.

```
branch → (implement → commit → review that change)* → final pass (if needed)
       → draft PR → CI/GHAS green → ready for review → STOP
```

**Never merge.** The user merges. Do not run `gh pr merge`, do not enable
auto-merge, and do not ask to. Hand over a green PR and stop.

Skip this skill for genuinely trivial work — a typo, a one-line fix the user
dictated, a dependency bump. Say you're skipping it and why.

## 1. Branch, in a worktree

Never work on `main`, and never in the primary checkout — it is shared, and a
`git pull` there moves HEAD out from under uncommitted work. Call
`EnterWorktree` with a `name`, which lands it in `.claude/worktrees/<name>` on
a branch of that name; skip this if the session is in a worktree already.

Then, from the worktree, confirm the branch and give it an environment of its
own:

```bash
git fetch -q origin
git rev-parse --abbrev-ref HEAD   # not <type>/<short-slug>? then:
git checkout -q -b <type>/<short-slug> origin/main
env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --all-extras
```

`<type>` is `feat`, `fix`, `refactor`, `docs`, `test`, or `chore`. If the user
is already on a feature branch with related work, stay on it.

**Pass that environment to everything after this.** `make` takes it as an
argument; git reads a trailing `VAR=value` as a pathspec and needs it as a
prefix instead:

```bash
make check UV_PROJECT_ENVIRONMENT=<worktree>/.venv
UV_PROJECT_ENVIRONMENT=<worktree>/.venv git commit -F <message-file>
```

`git commit` needs it because its hooks run `pyright` and the suite through
`uv`. A shell exporting the primary checkout's `.venv` otherwise has every `uv`
command here reinstall this source into that environment, and the gate you just
ran graded a tree nothing uses. `make venv-check` is the guard that catches it.

## 2. Implement

Follow [CLAUDE.md](../../../CLAUDE.md). The rules that bite hardest here, because
they are enforced by tests that fail late:

- **Read the architecture section for a module before changing it**, and update
  it in the same change set. [docs/architecture.md](../../../docs/architecture.md)
  routes a module to its notes. This is a stated project rule, so a change that
  skips it is a review finding, not a nitpick.
- **American English everywhere** — prose, identifiers, log messages, commit
  messages. Three deliberate exceptions are listed in CLAUDE.md.
- **A test run prints only pass/fail/skip.** Wrap every by-product where it
  fires: `assertRaises`, `assertLogs`, `redirect_stdout`, or `quiet_logging()`.
  `quiet_logging` and `assertLogs` must never nest.
- **Prove a test can fail before claiming it pins anything.** Mutate the line it
  covers, watch a *named* assertion go red, revert, re-run green. "Tests cover
  this" is an argument; a named victim is evidence. Run `make mutation-ready`
  first — CPython validates bytecode against the source mtime in whole seconds,
  so a same-length edit applied and reverted inside one second silently runs
  stale bytecode and reports a false result. `PYTHONDONTWRITEBYTECODE=1` does
  not fix that, and neither does `touch`. Arming does not stay done, and every
  way it lapses is silent: a fresh worktree has no bytecode at all, `make clean`
  deletes it, and a `uv sync` that moves the Python minor invalidates it.
  `make mutation-check` verifies the state — run it before believing a proof
  whose arming happened earlier in the session or in another directory.
- **Grep for every other site before committing.** The finding reviews raise
  most often is a fix that covered the cited site and missed its sibling. Search
  for the pattern, predicate or idiom you changed, and fix the other sites in
  the same commit.
- **Check a claim before writing it.** A docstring, comment or doc sentence
  that names code is checked against the code first. A sentence whose job is
  reassurance or exhaustiveness ("no other caller", "the only site") is not
  written at all unless a search you ran backs it.
- **The suite cannot touch files outside the checkout**, and an audit hook
  enforces it. When a test trips the sandbox, point the code under test at a
  temp fixture — never widen the sandbox.
- **Config metadata is the single source of truth.** A new config field means
  filling in its `help`/`choices`/`applies_to`, then `make schema`.
- **A changelog fragment** (`changelog.d/<slug>.<category>.md`, format in
  `changelog.d/README.md`) for anything a user would notice. Never edit
  `CHANGELOG.md` for it.

## 3. Commit, then review the change

Commit related changes together, with a message that explains *why*. Separate
commits for separable concerns. Write the message carefully once: it is not
review surface, and it is not reworded after review unless it misstates what
the code does — and never after the push.

Run the gate before committing:

```bash
# each also takes UV_PROJECT_ENVIRONMENT=<worktree>/.venv, per step 1
make check        # lint + typecheck + test
make schema       # only if you touched config metadata; CI fails on drift
make site-check   # only if you touched docs/
```

**Review each change once, at the effort its size and risk call for.** A
*change* is one logical concern: usually one commit, sometimes a short run of
commits that only make sense together, reviewed as one `BASE..TIP` range.

| Change | Review |
|---|---|
| Prose-only: `git diff <base> <tip> -- ':!*.md'` is empty | none |
| Trivial: ≤10 changed code lines, mechanical (rename, typo, a line the user dictated, a dependency bump), no pinned path | none — the gate covers it, and the final pass sees it if one runs |
| ≤50 changed code lines, a merge or conflict resolution, or a reviewer's fix commit | `low` |
| Anything else | `medium` |
| Touches a pinned path ([CLAUDE.md](../../../CLAUDE.md) → Workflow) | `high` |
| Eligible for a panel by the thresholds in `/panel-review`'s "When a panel is an option" (50+ changed code lines plus a trust boundary, contract, concurrency or pinned-path behavior change; or an unsplittable 400+) | optionally `/panel-review <sha>` *instead of* the reviewer at `high`, never in addition |

Run it with the project's `reviewer` agent, which works directly in the
worktree you give it:

    Agent(subagent_type="reviewer", description="<short-sha>",
          prompt="Worktree: <absolute worktree path>\nTarget: <sha or BASE..TIP>\nEffort: <level>")

Do not wrap `/code-review` in a general-purpose agent: the wrapper repeats the
setup and the gate, and costs most of what the review it runs does. The
`/code-review` fork also resolves the session's primary checkout rather than
your worktree, so from a worktree it can review a different branch and report
it as yours. The agent's definition carries the scope rules and the report
format, and it reads the gates and the pinned paths from CLAUDE.md. So its
prompt needs only those three lines, plus any machine-local rule from
`CLAUDE.local.md`. Add context only when the change needs it.

When the user has authorized hardware for this work, add a fourth line, for
example `Hardware: authorized; target <url>; capture: yes`. The reviewer then
settles hardware claims itself, under CLAUDE.md's "Hardware during a review or
a ship run", rather than handing them back. Pass the target in the prompt and
never commit it.

**A small branch gets one review.** When the whole branch is one change under
about 300 changed code lines, review it once as `origin/main..HEAD` when it is
done. That review is the final pass too, and step 4 is skipped.

**The reviewer's fix commit gets one `low` review, and that ends the chain.**
That second reviewer fixes what it finds, and its fixes are not reviewed again;
anything that needs more depth goes to step 4 or to the user. Reviews deeper
than two links in a chain found less than one finding per pass.

**Do not commit or edit in the worktree while a reviewer runs**, and run one
reviewer per worktree at a time. It verifies findings by mutating the tree and
running the suite, so a concurrent commit fails its pre-commit hook on a
mutation you never made.

When it reports, confirm its fix commit is on your branch
(`git -C <worktree> branch --contains <fix-sha>`), run `make check`, and route
what it handed back:

- **Advisory** — fix it here as its own commit, reviewed by the table above, or
  open a labeled GitHub issue when it is genuinely separate work.
- **Outside this change** — file each as its own GitHub issue, linked back to
  the PR, unless it is fixed here. Never list them in the PR description.
- **A commit-message finding** — the reviewer is told not to raise one; if one
  arrives anyway, drop it.

## 4. Final pass

Run it only when the branch had **two or more** reviewed changes. It looks for
what a single-change review cannot see — how the changes interact, a guarantee
one made and a later one dropped — not for a second reading of each diff.

    Agent(subagent_type="reviewer", description="<branch-slug> final",
          prompt="Worktree: <absolute worktree path>\nTarget: origin/main..HEAD\nEffort: medium\nEvery change on this branch has had its own review. Look for interactions between them, not for defects inside one diff.")

`medium` is the level. Raise it to `high` only when a pinned path changed after
its own review, and say why.

This reviewer has the whole-branch view, so editorial calls across the branch
are its to make. Its fix commit gets one `low` review, by step 3's rule, and
nothing after that. A defect still open when it is done is a stop, not a pass:
report what remains and ask the user before opening a PR.

## 5. Open the PR

Have the pre-PR gate green first. Step 3's `make check` is the per-commit one;
`preflight` is what CONTRIBUTING.md asks for before a PR, and it is what covers
the `pre-commit`, `docs` and `web` jobs that `check` never reaches. It needs
Node; without it, CONTRIBUTING.md says to run `check` and leave the rest to
CI.

```bash
make preflight UV_PROJECT_ENVIRONMENT=<worktree>/.venv \
  && gh pr create --draft --title "<type>: <what changed>" --body "<why, and what to look at>"
```

The body should say what the change does, why, and anything a reviewer should
look at closely — and nothing else. Not the findings the review declined, not
which review passes ran, not what this branch left for later: the declines
go to the user in the step 7 report, and whatever was left for later is a
labeled issue by now, which the body links rather than recounts.

## 6. Watch until green

Watch the checks and fix what breaks:

```bash
gh pr checks --watch
```

CI runs the tests across Python 3.12–3.15 and three operating systems,
`.pre-commit-config.yaml`'s hooks in the `pre-commit` job — bar `pyright`,
`unittest` and `commit-message-shape`, which other gates own — the type checks
once per target platform in the `types` job, the book and site renders and the
docs search test in `docs`, and the bundle rebuild in `web`. GHAS code
scanning runs too, and its findings are frequently regex-flavored false
positives on this codebase — read each one before changing code to satisfy
it, and say so if you think it is wrong rather than contorting the code
around it. That leeway ends
at the pinned paths listed in CLAUDE.md: a GHAS finding on any of them, or on anything
touching `dma_password` or the `[web]`/`[control]` tokens, must be fixed or
explicitly escalated to Kelly — never self-dismissed as a false positive.

A CI failure that is a real defect gets its fix reviewed by step 3's table,
rather than quietly patched.

Once every check is green and nothing is left to commit — no unrouted finding,
no verification still to run — mark the PR ready:

```bash
gh pr ready
```

Ready for review tells Kelly the next step is the merge. If more work turns up
after that, `gh pr ready --undo` puts it back to a draft until it is done.

## 7. Stop

Report to the user:

- The PR URL, its check status, and that it is marked ready for review.
- Each review that ran: its target, its effort, and what it fixed and declined
  — with reasons for the declines.
- Advisory findings and where each was routed — the commit that fixed it here,
  or the issue it became.
- The issues filed for "Outside this change" findings.
- Anything still open, stated plainly.

Then stop. The merge is the user's, and they squash-merge from the GitHub UI.
