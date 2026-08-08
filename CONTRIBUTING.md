# Contributing to c64cast

Bug reports, feature ideas, and pull requests are all welcome. This file covers
the development setup and the conventions the repo follows. If you only want to
*use* c64cast, the [README](README.md) has what you need — you do not need any
of this.

Security issues go through [SECURITY.md](SECURITY.md), not a public issue.
Everyone participating is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

c64cast uses the [uv](https://github.com/astral-sh/uv) project workflow.

```bash
git clone https://github.com/kfox/c64cast
cd c64cast
uv sync --all-extras    # creates/updates .venv from uv.lock: every runtime
                        # extra + the dev tool group
pre-commit install      # ruff + pyright + tests run before every commit
```

Then either prefix one-off commands with `uv run`, or let
[direnv](https://direnv.net/) activate `.venv` for you — `.envrc` is gitignored,
so write your own with `layout uv` in it (plus `use mise` if you use mise, and
an `export C64CAST_URL=…` so you can drop `-u` while developing).
The maintainer's setup is mise + direnv + uv; none of that is
required, but `uv` itself effectively is — `uv.lock` is the reproducible
definition of the environment CI runs.

> [!IMPORTANT]
> **Do not use `uv pip install -e .[...]` in this repo.** mise sets
> `UV_PYTHON` to the bare toolchain interpreter, and `uv pip` honors that over
> the active `.venv` — so packages land in the mise install while
> `python -m c64cast` runs from `.venv`. The symptom is a silent
> "PyAV unavailable" or a missing extra that you just installed.
> `uv sync` and `uv run` target the project environment and are immune.
>
> Note also that `dev` is a PEP 735 dependency *group*, not an extra, so
> `.[all,dev]` can never resolve it no matter which installer you use. With
> plain pip: `pip install -e .[all] && pip install --group dev`.

If you use VS Code, point the interpreter at `.venv/bin/python` rather than the
mise interpreter, or editor diagnostics will diverge from what actually runs.

`make doctor` is the fast offline self-check for all of the above: it reports the
c64cast version, flags a wrong interpreter, a hard dependency that will not
import, and `uv.lock` drift, before any of those cost you a debugging session.

## Running from a checkout

```bash
python -m c64cast --config example:hello -u u64://192.168.2.64
```

[`scripts/c64cast.sh`](scripts/c64cast.sh) is an equivalent launcher that `cd`s
to the repo root and forwards every argument, running through `uv run` when `uv`
is on `PATH` (so the project `.venv` is always used) and falling back to a bare
`python` otherwise. Use it from another directory, or from a context where
direnv has not activated `.venv` — cron, systemd, an ssh one-liner:

```bash
scripts/c64cast.sh --config example:hello
scripts/c64cast.sh --doctor --skip-probe
```

## The pre-PR gate

```bash
make check      # lint + typecheck + test — run this before opening a PR
```

Every target routes through `uv run`, so they hit the synced project env
whether or not the current shell has `.venv` activated:

| Target | What it does |
|---|---|
| `make sync` | `uv sync --all-extras` (refresh the project env) |
| `make lint` | `ruff check` |
| `make fmt` | `ruff format` |
| `make test` | the unittest suite, parallel across cores (`T=tests.test_foo` runs just that, serially) |
| `make coverage` | tests under coverage → report + HTML + `coverage.xml` + JUnit XML |
| `make typecheck` | `mypy --strict` on the state-bearing modules + `pyright` across the tree |
| `make doctor` | offline environment + config diagnostics |
| `make schema` | regenerate `c64cast/data/c64cast.schema.json` from the config metadata |
| `make guide` | render the User's Guide to a typeset PDF (needs `typst`) |
| `make bench` | the async write-pipeline benchmark |

CI runs the same lint, typecheck, and tests on every push and pull request
across Python 3.11–3.14 — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). Type-checking is
deliberately two-tiered: `pyright` in basic mode across the whole tree
(including tests), matching Pylance's VS Code defaults so editor diagnostics
align with CI, plus `mypy --strict` on the state-bearing modules listed in
`[tool.mypy] files` where a type slip would corrupt state.

## Tests

The suite is stdlib `unittest`, one module per subject under `tests/`, and it
runs entirely without hardware — the hardware backends are faked. A test run
should print **only** pass/fail/skip indicators: wrap any path that raises an
expected exception in `assertRaises`, any path that logs an expected
warning/error in `self.assertLogs("c64cast.<module>", …)`, and any path that
writes to stdout in a `redirect_stdout`. Expected output left to print buries
real failures. When a call both logs and raises, nest `assertLogs` *outside*
`assertRaises` so the records are actually verified.

Several tests exist purely to stop documentation from drifting — the JSON schema
against the config metadata, the annotated example TOML against the dataclass
fields, the `all` extra against the union of the other extras. If one of those
fails, the fix is usually to regenerate rather than to edit the test.

## Hardware for development

An HDMI capture device (Elgato Cam Link 4K, Genki ShadowCast, …) is highly
recommended: a RAM dump cannot prove what the VIC-II actually drew, so visual
changes need a capture to verify. [`scripts/diags/`](scripts/diags) holds the
committed diagnostic tooling that drives one — a U64 REST/DMA probe, HDMI still
capture, audio capture with level analysis, and a launch-capture-reset harness.
Improve those rather than writing fresh throwaway scripts.

If you touch the hardware paths, leave every machine you tested against silent
and reset when you are done.

## Commits and pull requests

- **Conventional commits**: `feat(scope): …`, `fix(scope): …`, plus `docs`,
  `build`, `test`, `refactor`, `perf`, `chore`, `ci`. The subject is a lowercase
  imperative phrase. The changelog is written from this history, so a subject
  line that reads as a user-visible statement is worth the extra few seconds.
- **One logical change per commit**, and per PR. Unrelated cleanup goes in its
  own commit.
- **Work on a branch and open a PR** — `main` is protected by CI and every
  change lands through review.
- `make check` must be green before you open the PR.
- Do not commit user media, personal configs, or machine-specific details (IP
  addresses, capture-device names, local paths). `assets/` tracks only its
  per-directory READMEs by design; everything else there is gitignored.

## Documentation is part of the change

A behavior change updates its documentation **in the same change set** — not in
a follow-up. Concretely, when you change functionality:

- [`docs/architecture.md`](docs/architecture.md) and the topic notes under
  [`docs/architecture/`](docs/architecture) carry the *why* for each module:
  design rationale, hardware constraints, and the dead ends that the code alone
  does not show. Read the relevant section before modifying a module, and update
  it in the same PR. The index's module table routes any module to its section.
- The three books under [`docs/`](docs) are the user-facing surface: the
  [User's Guide](docs/guide/README.md), the [Programmer's Reference
  Guide](docs/reference/README.md) and the [Performance
  Card](docs/card/README.md). [`caveats.md`](docs/caveats.md),
  [`troubleshooting.md`](docs/troubleshooting.md) and
  [`extending.md`](docs/extending.md) sit alongside them.
- New config knobs, scenes, or overlays: fill in the field's `help`/`choices`
  metadata (in [`c64cast/app/config.py`](c64cast/app/config.py)) or the overlay's
  `HELP`/`PARAM_HELP`, update
  [`c64cast/examples/c64cast.example.toml`](c64cast/examples/c64cast.example.toml)
  and add a demo under [`c64cast/examples/`](c64cast/examples), then run
  `make schema`. That single metadata model drives `--describe`, `--list-*`,
  `--compat`, the JSON schema, the config serializer, and the `--init` wizard,
  so filling it in is what keeps all of them from drifting — and the drift tests
  will tell you if you skipped it.
- Add `CHANGELOG.md` entries under `## [Unreleased]` for anything users would
  notice.
- Hand-encoded 6502 bytes (the NMI DAC handler, the REU pump, the SID player
  PRG, BASIC stubs) are annotated with the assembly they represent and why each
  instruction is there. Keep that up when you touch a byte array — a wall of hex
  is not reviewable.
- Write documentation in the present tense, describing what the code does now.
  It is not a record of what changed; that is what the changelog and git history
  are for.

[`docs/extending.md`](docs/extending.md) is the starting point for adding a new
Scene, Overlay, DisplayMode, or interstitial Background.

## What counts as a breaking change

c64cast's stable surface is the part users depend on: the **CLI flags**, the
**config schema**, the **`example:` names**, and the **data directory layout**.
Removing or renaming any of those needs a deprecation warning for one minor
release first. The Python API carries no stability promise while the version is
`0.x` — internal modules may be reshaped freely, as long as the four surfaces
above keep working.

Cutting a release is a maintainer task and lives in
[`RELEASING.md`](RELEASING.md). The one thing worth knowing as a contributor is
that the `## [Unreleased]` section of the changelog becomes the release notes
verbatim, so write an entry as the announcement it will be.
