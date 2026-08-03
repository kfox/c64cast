<!--
Thanks for the pull request. Fill in what applies and delete what doesn't —
a one-line fix does not need a long form. CONTRIBUTING.md has the conventions
in full.
-->

## What this changes

<!-- What the change does, and why. If it fixes an open issue: "Fixes #123". -->

## How it was verified

<!--
`make check` is the gate (lint + typecheck + tests). Say what else you ran —
especially anything hardware-side, since CI cannot do that part.
-->

- [ ] `make check` is green
- [ ] Tests added or updated for the change
- [ ] Verified on real hardware (say which machine, firmware, and connection
      scheme — or note that the change does not touch a hardware path)

## Documentation

<!--
Docs are part of the change, not a follow-up. Tick what applied; delete the
lines that didn't.
-->

- [ ] Architecture notes updated (`docs/architecture/…`) for a module whose
      behavior changed
- [ ] User-facing docs updated (the User's Guide, the Reference Guide, the
      Performance Card, `caveats.md`, `troubleshooting.md`, `extending.md`)
- [ ] `CHANGELOG.md` entry added under `## [Unreleased]`
- [ ] New config knob: field `help`/`choices` metadata filled in,
      `c64cast/examples/` updated, `make schema` re-run
- [ ] Not applicable — no behavior or config surface changed

## Anything reviewers should know

<!--
Trade-offs you made, approaches you rejected, parts you are unsure about, or
follow-up work you deliberately left out of scope.
-->
