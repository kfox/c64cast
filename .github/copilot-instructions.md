# Copilot instructions

The project's rules live in [CLAUDE.md](../CLAUDE.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md); read them before reviewing. Where this
file and those disagree, they win. Per-module design rationale is in
[docs/architecture.md](../docs/architecture.md), which routes each module to
its notes.

## Reviewing a pull request

- **Report defects, not style.** A finding names an actor, an action and a
  wrong result. Leave formatting, lint and types to CI (ruff, pyright,
  `mypy --strict`, pre-commit, the schema drift tests); don't repeat them.
- **Pinned paths get the closest reading.** CLAUDE.md's "Pinned paths" section
  lists the files where a one-line change does the most damage: the DMA write
  path, connection-target parsing, the DMA-password and redaction rules, and
  the web/control token gate, and the CI and release workflows.
- **The DMA password never rides in a URL or a CLI flag.** It comes from
  `C64CAST_DMA_PASSWORD` or the config file only; flag any change that lets it
  reach a URL, a log, `--save-settings` output or argv. The web/control token
  is the same class of secret: flag a change that exposes it somewhere new.
- **American English** in prose, code, identifiers and messages. `grey` and
  `cancelled` are accepted; CLAUDE.md's "Spelling" section lists the deliberate
  exceptions.
- **A test run prints only pass/fail/skip indicators.** Flag a new test that
  lets a log line or stdout leak into the run.
- **No machine-specific details** (hostnames, IP addresses, device names, local
  paths) in code, docs or commit messages.
- **Generated files** (`c64cast/data/c64cast.schema.json`, the web bundle
  under `c64cast/web/dist/`) change only through their `make` target; review
  the source, not the output.
- **Docs travel with the change.** A behavior change updates its
  `docs/architecture/` section, and anything a user would notice gets a
  `changelog.d/` fragment.
