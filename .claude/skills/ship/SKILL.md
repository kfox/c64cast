---
name: ship
description: >
  Take a change in this repository all the way to a pull request that is ready
  to merge: branch, implement, commit, review each changeset as it lands, run a
  branch-wide review at high effort, open the PR, and watch CI and GHAS until
  green. Stops before merging — the merge is always the user's. Use when asked
  to implement a non-trivial change, or when asked to "ship", "land", or "take
  this to a PR". Trigger phrases include "ship this", "take it to a PR",
  "full workflow", "branch and review".
---

# Ship a change

The standard workflow for non-trivial work in this repository. Every stage is
mandatory unless the user says otherwise, and the last one is a hard stop.

```
branch → (implement → commit → review that changeset)* → branch-wide review
       at high effort → PR → CI/GHAS green → STOP
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
- **The suite cannot touch files outside the checkout**, and an audit hook
  enforces it. When a test trips the sandbox, point the code under test at a
  temp fixture — never widen the sandbox.
- **Config metadata is the single source of truth.** A new config field means
  filling in its `help`/`choices`/`applies_to`, then `make schema`.
- **CHANGELOG.md** gets an entry under `## [Unreleased]` for anything a user
  would notice.

## 3. Commit

Commit related changes together, with a message that explains *why*. Separate
commits for separable concerns — a reviewer reading the branch should be able to
follow the reasoning, not just the edits.

Run the gate before committing:

```bash
# each also takes UV_PROJECT_ENVIRONMENT=<worktree>/.venv, per step 1
make check        # lint + typecheck + test
make schema       # only if you touched config metadata; CI fails on drift
make site-check   # only if you touched docs/
```

**Then review the commit you just made, scoped to that commit alone.** Spawn a
subagent with the Agent tool and have it run:

    Skill(skill="code-review", args="high <sha> --fix")

The effort level goes **first** in `args`, or it is parsed as part of the target
and the run silently reuses whatever level ran last. Tell it to review that
commit's own diff, not `<sha>...HEAD` and not the branch.

Its prompt owes the three payloads step 4 lists — the gate summary, where this
repo states its rules, and the pinned paths — for the reason step 4 gives: it
cannot check a claim it was never shown. This is the net that reaches a one-line
change to a pinned path first, so it is the one that must not give it the cheap
pass.

**The reviewer fixes what it finds and commits the fixes itself.** `--fix` is
what reaches the tree — without it the run reports and leaves the tree
untouched. Committing is not part of `--fix`, so the prompt still has to ask for
that. A finding handed back as prose gets re-implemented from a description, and
that re-implementation is new code, which earns its own review; the hand-back is
the loop that spends an afternoon on a small change. Two classes stay with you:
a defect in the commit message, because rewriting a message changes the SHA, and
any editorial call about prose, which needs the whole-branch view a single-commit
reviewer does not have.

**Do not open a round over a commit message.** Reword one before the push only
when it misstates what the code does. A claim that is merely imprecise about
something the change does not turn on is left alone, and after the push a
message is history and is not reworded at all: the reword moves that SHA and
every SHA stacked on it, and the round costs more than the sentence was worth.

**The reviewer works in this checkout**, so do not commit or edit anything here
while it runs — it verifies findings by mutating the tree and running the suite,
and a concurrent commit fails its pre-commit hook on a mutation you never made.
Give it `isolation: "worktree"` if you need to keep working; its prompt then
also owes step 1's `uv sync` and the prefix/argument split, and must require
the worktree's path and every fix commit's SHA in its report. Its fixes come
back by cherry-pick, and a commit you cannot name is one you cannot pick.

**Prose is not review surface, and its prompt has to say so.** The commit
message, the comments and the docstrings are not reviewed for style, precision
or thinness, and a finding about one does not get reported. The exception is
prose that **misstates what the code does**, because whoever edits that line
acts on it. Every other finding names an actor, an action and a wrong result, or
it is out of scope — "a reader could be misled" is not one of those. This is a
scope rule, not a severity rule: a "low" prose finding still costs a full round
to read, decide and write up, and the fix for one is more prose carrying fresh
claims, so the loop has no fixed point. The `commit-message-shape` and
`lint-comments` hooks from step 3's gate decide the mechanical part before the
commit exists, which is the part worth deciding at all.

**Prove coverage by execution**, by step 2's mutation recipe: a coverage claim
this commit makes is checked by naming a victim, not by reading the test. A
bounds claim — "no other caller", "the only site" — is a search you ran and its
result, or it is cut.

**Write the review down, in the reviewer's report.** It says what it looked at,
what it found, and what it did about each finding — fixed, declined with the
reason, or deferred to a labeled GitHub issue. That report is what reaches the
user in step 7, and a decline that was only said out loud is re-litigated by the
next reader. A fix commit's message describes its fix, the way any commit message
does; it is not a review log.

Run `make check` over the reviewer's fixes — a fix that breaks the suite is not
a fix — and review each of those commits the way this step does.

Do not batch this to the end. The whole point is that the reviewer sees one
changeset instead of a branch: a wide scope spends its attention before it
reaches the small commit, and reads back as a clean pass.

## 4. Branch-wide review at high effort

The per-changeset reviews in step 3 are the first net and the one that catches
most defects. This is the **second** net: it sees what no single-commit review
can — how the commits interact, a guarantee one commit made and a later one
quietly dropped, a design the branch drifted into. Run it once, after every
commit has had its own review, never instead of them.

Spawn **one subagent** with the Agent tool and have it review the whole branch:

    Skill(skill="code-review", args="high origin/main...HEAD")

The effort level goes **first** in `args`, or it is parsed as part of the target
and the run silently reuses whatever level ran last.

**Its prompt has to tell it to fix what it finds** — that call is a report-only
run, and nothing reaches the tree unless the subagent applies it. This is the
reviewer with the whole-branch view, so the editorial calls step 3 sends back
are its to make. Only a commit-message rewrite stays with you, because it
changes that commit's SHA and every SHA after it. Route what it hands back once
it has reported, below.

Do not commit, or edit anything in this checkout, while it runs, for the reason
step 3 gives; `isolation: "worktree"` is the same escape hatch here, on the same
terms.

A clean pass here does not mean the branch is clean — it means nothing survived
*both* nets. Read a wide pass that finds nothing as weak evidence.

Three more things its prompt has to carry, because it cannot work them out for
itself:

- **That prose is not review surface**, in the words step 3 uses. The wide pass
  is where prose findings are cheapest to produce and least worth having, and
  this is the reviewer whose findings land with the PR already in sight.
- **The gate summary** from step 3, so it doesn't spend findings on things
  `ruff`, `mypy`, `pyright`, and the suite already prove.
- **Where this repo states its rules**, so it can check code against claim:
  `CLAUDE.md`, `CONTRIBUTING.md`, `docs/architecture/`,
  `c64cast/data/c64cast.schema.json`, `CHANGELOG.md`, and
  `c64cast/examples/c64cast.example.toml`. It cannot check a claim it was never
  shown.
- **The pinned paths.** Diff size is a bad proxy for risk: a one-line change to
  a boundary is exactly the diff that must not get the cheap pass. Name these
  explicitly and tell it that any diff touching one gets full attention
  regardless of size. This list names credential-handling and hardware-write
  sites specifically because they are where a one-line change does the most
  damage; it is not a substitute for reading CLAUDE.md's own security notes,
  which may grow a site this list has not caught up to yet:
  - `hw/api.py`, `hw/socket_dma.py`, `hw/teensyrom_dma.py`, `hw/backend.py` —
    the DMA write path to the hardware, including the shared `write_memory*`/
    `write_regs`/`write_region` implementation every backend sits on top of.
  - `tests/_fs_sandbox.py` — the suite's filesystem sandbox, which is never
    widened to make a test pass.
  - `app/connect.py`, `app/config_serialize.py`, `app/recording_metadata.py`,
    `app/cli.py` — connection-target parsing, the rule that the DMA password
    never rides in a URL or CLI flag, the redaction list `--save-settings`
    and the scene-log snapshot both depend on, and the env/config precedence
    that decides which value wins.
  - `control/auth.py`, `control/web_api.py`, `app/serve.py` — the
    `[web]`/`[control]` token gate itself (`auth.py`: token comparison,
    viewer/full-role checks), the routes that depend on it, and where the
    token is actually minted and its precedence resolved (`serve.py`:
    `resolve_tokens`, `_generated_token`, `build_daemon_app`). The same class
    of secret as the DMA password, gating the same kind of remote control of
    the host.

Then, once it has reported:

- Run `make check` over the fixes. A fix that breaks the suite is not a fix.
- Commit the fixes it left in this checkout, or cherry-pick the ones it
  committed in a worktree of its own, and review each of those commits the way
  step 3 does, as you make it. Batching them to the end is the batching step 3
  forbids, done at the point where the branch is closest to shipping.
- **Write down what was declined and why**, where step 3 puts it: the message of
  a fix commit from the same pass, or the report to the user when the pass made
  none. A declined finding with a reason is a legitimate outcome; one that was
  only said out loud is re-litigated by the next reader. Not the PR body, which
  is for the change and not for the history of reviewing it.
- **Route everything it handed back before step 5.** Fix it on this branch when
  a commit here introduced it or the fix fits the spirit of the change, as its
  own commit under the rule above; otherwise open a labeled GitHub issue. A
  finding that is only mentioned is one nothing tracks, and one fixed after
  step 6 costs another commit, review and push with the PR already green.

A defect still open when the subagent is done is a stop, not a pass. Report what
remains and ask the user how to proceed before opening a PR.

## 5. Open the PR

```bash
gh pr create --title "<type>: <what changed>" --body "<why, and what to look at>"
```

The body should say what the change does, why, and anything a reviewer should
look at closely — and nothing else. Not the findings the review declined, not
which review passes ran, not what this branch left for later: the commit
messages hold the declines, and whatever was left for later is a labeled issue
by now, which the body links rather than recounts.

## 6. Watch until green

Watch the checks and fix what breaks:

```bash
gh pr checks --watch
```

CI runs the tests across Python 3.11–3.14 and three operating systems, lint and
formatting once in the `pre-commit` job, and the type checks once per target
platform in the `types` job. GHAS code scanning
runs too, and its findings are frequently regex-flavored false positives on this
codebase — read each one before changing code to satisfy it, and say so if you
think it is wrong rather than contorting the code around it. That leeway ends
at the pinned paths from step 4: a GHAS finding on any of them, or on anything
touching `dma_password` or the `[web]`/`[control]` tokens, must be fixed or
explicitly escalated to Kelly — never self-dismissed as a false positive.

A CI failure that is a real defect goes back through step 4: review the fix at
high effort rather than quietly patching it.

## 7. Stop

Report to the user:

- The PR URL and its check status.
- What the review found, fixed, and declined — with reasons for the declines.
- Anything a review handed back rather than applied, and where it was routed —
  the commit that fixed it here, or the issue it became.
- Anything still open, stated plainly.

Then stop. The merge is the user's, and they squash-merge from the GitHub UI.
