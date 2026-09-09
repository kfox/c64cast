---
name: ship
description: >
  Take a change in this repository all the way to a pull request that is ready
  to merge: branch, implement, commit, review each changeset as it lands, run an
  adversarial panel over the branch to convergence, open the PR, and watch CI and
  GHAS until green. Stops before merging — the
  merge is always the user's. Use when asked to implement a non-trivial change,
  or when asked to "ship", "land", or "take this to a PR". Trigger phrases
  include "ship this", "take it to a PR", "full workflow", "branch and review".
---

# Ship a change

The standard workflow for non-trivial work in this repository. Every stage is
mandatory unless the user says otherwise, and the last one is a hard stop.

```
branch → (implement → commit → review that changeset)* → adversarial panel
       over the branch, to convergence → PR → CI/GHAS green → STOP
```

**Never merge.** The user merges. Do not run `gh pr merge`, do not enable
auto-merge, and do not ask to. Hand over a green PR and stop.

Skip this skill for genuinely trivial work — a typo, a one-line fix the user
dictated, a dependency bump. Say you're skipping it and why.

## 1. Branch

Never work on `main`. Branch from an up-to-date `main`:

```bash
git fetch -q origin
git checkout -q -b <type>/<short-slug> origin/main
```

`<type>` is `feat`, `fix`, `refactor`, `docs`, `test`, or `chore`. If the user
is already on a feature branch with related work, stay on it.

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
  not fix that, and neither does `touch`.
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
make check     # lint + typecheck + test
make schema    # only if you touched config metadata; CI fails on drift
make site-check   # only if you touched docs/
```

**Then review the commit you just made, scoped to that commit alone** —
`/code-review <sha>`, and tell it to review that commit's own diff, not
`<sha>...HEAD` and not the branch. Act on what it finds, then record it; the
report is read from **stdin**, and an empty one is refused:

```bash
~/.claude/hooks/changeset-review.sh record <sha> <<'REPORT'
...what you looked at, what you found, what you did about each finding...
REPORT
```

This is not the panel in step 4; it is a narrow pass, and it is the one that
catches things. Both `git push` and `gh pr create` are denied while any commit
on the branch has no recorded review — so skipping this does not defer the
cost, it blocks step 5.

Do not batch this to the end. The whole point is that the reviewer sees one
changeset instead of a branch: a wide scope spends its attention before it
reaches the small commit, and reads back as a clean pass. Fixes for what it
finds are their own commits, and get their own review.

## 4. Adversarial panel over the branch, to convergence

The per-changeset reviews in step 3 are the first net and the one that catches
most defects. This is the **second** net: the panel sees what no single-commit
review can — how the commits interact, a guarantee one commit made and a later
one quietly dropped, a design the branch drifted into. Run it once, after every
commit has had its own review, never instead of them.

Invoke the `adverse-review` skill in its **convergence loop** shape, scoped to
`origin/main...HEAD`. Do not hand-roll a review; the skill's deterministic
triage, ledger, and stop condition are the point.

A clean panel here does not mean the branch is clean — it means nothing
survived *both* nets. Read a wide pass that finds nothing as weak evidence.

Three things to pass it that it cannot work out for itself:

- **The gate summary** from step 3, so reviewers don't spend findings on things
  `ruff`, `mypy`, `pyright`, and the suite already prove.
- **Where this repo states its rules**, for the Steward lane: `CLAUDE.md`,
  `CONTRIBUTING.md`, `docs/architecture/`, `c64cast/data/c64cast.schema.json`,
  `CHANGELOG.md`, and `c64cast/examples/c64cast.example.toml`. Its lane is
  code-versus-claim and it cannot check a claim it was never shown.
- **The pinned paths.** The review plan sizes the panel from the diff, and size
  is a bad proxy for risk: a one-line change to a boundary is exactly the diff
  that must not get the cheap pass. Pass each of these to `plan.mjs` as
  `--pin <substring>` so any diff touching them gets the full panel, every
  size-based skip overridden. This list names credential-handling and
  hardware-write sites specifically because they are where a one-line change
  does the most damage; it is not a substitute for reading CLAUDE.md's own
  security notes, which may grow a site this list has not caught up to yet:
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

Then work the loop:

- Fix the blocking findings. Commit the fixes — and review each of those
  commits the way step 3 does, as you make it. They are commits on the branch,
  the gate counts them, and leaving them to the end is the batching step 3
  forbids, done at the point where the branch is closest to shipping.
- **Record a decision for every finding, not only the blocking ones —
  including the ones you decline.** The ledger is what stops the next pass
  from re-litigating them, whether the finding was blocking or advisory; a
  declined finding with a reason is a legitimate outcome either way.
- Re-run the gate after fixing. A fix that breaks the suite is not a fix.
- Loop until `converge.mjs` exits 0.

If it exits 3, the iteration cap was reached with findings still open. **That is
a stop, not a pass.** Report what remains and ask the user how to proceed
before opening a PR.

Advisory (`design`) findings never block a loop iteration, but they still get a
recorded decision like any other finding (see above). Report them to the user
as a backlog alongside the PR either way.

## 5. Open the PR

```bash
gh pr create --title "<type>: <what changed>" --body "<why, and what to look at>"
```

The body should say what the change does, why, and anything a reviewer should
look at closely. Mention findings you declined during review and the reason —
that is exactly the context a human reviewer would otherwise have to rediscover.

## 6. Watch until green

Watch the checks and fix what breaks:

```bash
gh pr checks --watch
```

CI runs lint, typecheck, and tests across Python 3.11–3.14. GHAS code scanning
runs too, and its findings are frequently regex-flavored false positives on this
codebase — read each one before changing code to satisfy it, and say so if you
think it is wrong rather than contorting the code around it. That leeway ends
at the pinned paths from step 4: a GHAS finding on any of them, or on anything
touching `dma_password` or the `[web]`/`[control]` tokens, must be fixed or
explicitly escalated to Kelly — never self-dismissed as a false positive.

A CI failure that is a real defect goes back through step 4's loop; record it in
the ledger as a regression rather than quietly patching it.

## 7. Stop

Report to the user:

- The PR URL and its check status.
- What the review found, fixed, and declined — with reasons for the declines.
- Any advisory findings, as a backlog.
- Anything still open, stated plainly.

Then stop. The merge is the user's, and they squash-merge from the GitHub UI.
