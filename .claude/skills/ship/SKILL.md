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
subagent, hand it the output of `pudding brief`, and have it run:

    Skill(skill="code-review", args="high <sha>")

The effort level goes **first**. Tell it to review that commit's own diff, not
`<sha>...HEAD` and not the branch.

**The reviewer works in its own checkout.** `pudding worktree <sha>` gives it an
isolated tree, so this checkout stays yours and you can keep committing while it
runs. It fixes what it finds there and commits the fixes as their own commits;
`pudding worktree --done <sha>` names them to cherry-pick back. Tell it to fix
what it finds: `/code-review` without `--fix` is a report-only run, and nothing
reaches its tree unless the prompt demands it.

The environment is not isolated with it. `pudding worktree` builds one only when
`pudding.envsetup` is set — `git config --get pudding.envsetup` says whether it
is. Unset, it prints that it is unset and hands over a tree that inherits
whatever `UV_PROJECT_ENVIRONMENT` the shell exports: the failure step 1
describes, where a `uv` command in the worktree reinstalls that source into the
primary checkout's environment. Then pass the environment by hand there too,
exactly as step 1 does. `pudding worktree --done` reports on the primary
checkout's environment only when `pudding.envcheck` is set; unset it prints
`unchecked`, so that hazard has no automated backstop either.

What it reports instead of applying: `contract` and `design` findings, which are
advisory and never block, and any editorial call about prose, which needs the
whole-branch view a single-commit reviewer does not have. A defect in the commit
message it reports too, because rewriting a message is yours.

**What the commit owes is computed.** `pudding derive <sha>` prints one
`TRIGGER` line per obligation, read from the diff, and the class it computed:
`primary` for the full set, `fix` for a `Closes-findings:` commit, which owes a
verification pass only. Answer the triggers and nothing else — a trailer that
was not demanded must be **absent**.

A claim of coverage is a pointer, not a sentence:

    Mutation: victim=<path>[:<line>] replace="<exact text>" with="<new>" test=<test id>
    Evidence-search: symbol="<text>" paths=<a,b> result=present|absent

`red` means a test noticed the break. `green` refuses the record and *is* the
finding, and so does `unexecutable` — the verdict for a pointer whose `replace=`
text is missing from the victim, changes nothing, or carries a double quote.
`inconclusive` and `unconfigured` are accepted. Bounds claims — "no other caller", "the only site" — arrive as an
`Evidence-search:` pointer or they are cut.

Running the `Mutation:` pointer needs `pudding.testcmd`. Without it `record`
stamps the verdict `unconfigured` and accepts the report anyway, so the coverage
rests on prose and `pudding status` lists it under "Records that measured
nothing". Either way the proof is still yours to run the way step 2 says — arm
`make mutation-ready`, watch a named assertion go red, revert — and the pointer
records the mutation you ran. `Evidence-search:` needs no configuration.

Run `make check` over the cherry-picked fixes, then record. Start from the
skeleton, so the slots come from the tool rather than from memory:

```bash
pudding template <sha> > report
# fill each slot from something you ran; a '# ' line is refused
pudding record <sha> --file report
```

Each fix you cherry-picked is a commit of its own, and each owes a record of its
own: the gate reads every non-merge commit between `origin/main` and HEAD, not
only the one you reviewed.

If the commit was amended after its review, the carry-over is declared and then
verified — `pudding record <sha> --carry <old-sha> --file report`. What carries
over is the discharge of every trigger, not the prose: the new record still owes
a filled slot of its own, an identical tree earns a `carried` header, and a
changed one is refused with the delta printed and a `Delta:` trailer demanded.
Without `--carry` the amended SHA has no record at all.

This is not the branch-wide pass in step 4; it is a narrow pass, and it is the
one that catches things. Both `git push` and `gh pr create` are denied while
any commit on the branch has no recorded review — so skipping this does not
defer the cost, it blocks step 5.

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
are its to make. It still reports rather than applies an advisory *design*
finding, and anything that rewrites a commit message: here that moves the SHA
a recorded review is keyed to. Route what it hands back once it has reported,
below.

Do not commit, or edit anything in this checkout, while it runs: this pass gets
no worktree of its own, and it verifies findings by mutating the tree and
running the suite, so a concurrent commit fails its pre-commit hook on a
mutation you never made.

A clean pass here does not mean the branch is clean — it means nothing survived
*both* nets. Read a wide pass that finds nothing as weak evidence.

Three more things its prompt has to carry, because it cannot work them out for
itself:

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
- Commit the fixes and review each of those commits the way step 3 does, as you
  make it. Batching them to the end is the batching step 3 forbids, done at the
  point where the branch is closest to shipping — and the push gate counts these
  commits, so leaving them unreviewed blocks `gh pr create` in step 5.
- **Write down what was declined and why**, in the step-3 report for the commit
  it belongs to. A declined finding with a reason is a legitimate outcome; one
  that was only said out loud is re-litigated by the next reader. The record is
  where that belongs — not the PR body, which is for the change and not for the
  history of reviewing it.
- **Route everything it handed back before step 5.** Fix an advisory finding on
  this branch when a commit here introduced it or the fix fits the spirit of the
  change, as its own commit under the rule above; otherwise open a labeled
  GitHub issue. A commit-message rewrite moves that SHA and every SHA after it,
  and records are keyed by SHA, so each commit the gate then reports as
  unreviewed needs `pudding record <sha> --carry <old-sha>` to carry its record
  over; `pudding orphans` lists the ones left behind. A finding that is only
  mentioned is one nothing tracks, and one fixed after step 6 costs another
  commit, review and push with the PR already green.

A defect still open when the subagent is done is a stop, not a pass. Report what
remains and ask the user how to proceed before opening a PR.

## 5. Open the PR

```bash
gh pr create --title "<type>: <what changed>" --body "<why, and what to look at>"
```

The body should say what the change does, why, and anything a reviewer should
look at closely — and nothing else. Not the findings the review declined, not
which review passes ran, not what this branch left for later: the step-3 record
holds the declines, and whatever was left for later is a labeled issue by now,
which the body links rather than recounts.

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
- Anything the review handed back rather than applied, and where step 4 routed
  it — the commit that fixed it here, or the issue it became.
- Anything still open, stated plainly.

Then stop. The merge is the user's, and they squash-merge from the GitHub UI.
