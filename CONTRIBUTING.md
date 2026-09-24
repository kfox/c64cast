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
uv run --locked pre-commit install   # ruff + pyright + tests run before every commit
```

That installs two hook types, `pre-commit` and `commit-msg`. If you set this
checkout up before the `commit-msg` hook existed, run `install` again — git only
calls the hook types that were wired at install time, so the message check is
silently absent otherwise. It refuses a subject over 80 characters or a body
over 10 non-blank lines (trailers excluded); raise either with `git config
prose.subjectMax N` / `git config prose.bodyMax N`, and see
[scripts/check_commit_message.py](scripts/check_commit_message.py) for where the
numbers came from. The companion `lint-comments` hook reports section banners,
`TODO:` markers and commented-out code among the comment lines a commit *adds* —
`git config prose.lintComments false` switches it off.

A commit that touches `.github/workflows/` also runs `lint-workflows`
([`scripts/lint_workflows.py`](scripts/lint_workflows.py)), which refuses a
`needs:` naming no job in that workflow, a cycle in the `needs:` graph, and a
`needs.<job>` expression the job never declared. GitHub resolves all three when
it dispatches the run, which is after the push. The suite runs the same check
over every workflow, so `make check` and CI's test matrix reach it too.

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

> [!IMPORTANT]
> **In a git worktree, check where `UV_PROJECT_ENVIRONMENT` points.** Unset, uv
> resolves the project environment inside the worktree and there is nothing to
> do. If your shell exports an absolute path to another checkout's environment,
> every `uv` command here reinstalls this source into that environment, leaving
> that checkout's tests importing this code. Give the worktree its own with
> `env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --all-extras`,
> and pass the same override to `git commit` too — the commit hooks run
> `pyright` and the suite through `uv`. The `venv-matches-checkout` hook fails a
> commit whose environment does not import this checkout, and `make` runs the
> same check ahead of any target that invokes `uv`.

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
make check      # lint + typecheck + test — the quick one, for every commit
make preflight  # everything CI runs but coverage and the version matrix
```

`preflight` is the one to have green before you open a PR. `check` runs none
of the hygiene hooks CI's `pre-commit` job does, so a change to YAML, TOML or
Markdown gets a green `check` with no dependabot, schema, whitespace or
line-ending check behind it. `preflight` needs Node, for the web bundle and
the docs search test; without Node, run `check` and leave the rest to CI.

Every target routes through `uv run`, so they hit the synced project env
whether or not the current shell has `.venv` activated:

| Target | What it does |
|---|---|
| `make check` | `lint` + `typecheck` + `test` |
| `make preflight` | `lint` + `test`, the hygiene hooks, `pyright`/`mypy` once per target platform, the book and site renders, the docs search test, and the web bundle drift check |
| `make sync` | `uv sync --all-extras` (refresh the project env) |
| `make lint` | `ruff check` + `ruff format --check` |
| `make fmt` | `ruff format` |
| `make test` | the unittest suite, parallel across cores (`T=tests.test_foo` runs just that, serially) |
| `make coverage` | tests under coverage → report + HTML + `coverage.xml` + JUnit XML |
| `make typecheck` | `mypy --strict` on the state-bearing modules + `pyright` across the tree |
| `make doctor` | offline environment + config diagnostics |
| `make schema` | regenerate `c64cast/data/c64cast.schema.json` from the config metadata |
| `make web` | rebuild the web console into `c64cast/web/dist` (needs Node — only if you changed `web/`) |
| `make guide` | render the User's Guide to a typeset PDF (needs `typst`) |
| `make bench` | the async write-pipeline benchmark |
| `make mutation-ready` | arm the tree's bytecode for a mutation proof (see [Proving a test can fail](#proving-a-test-can-fail)) |
| `make mutation-check` | verify it is still armed — a clean, a new worktree or a `uv sync` un-arms it silently |

CI runs on every pull request and on pushes to `main`
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): the same tests across
Python 3.11–3.14 and three operating systems, the same type checks once per
target platform on Python 3.14 in the `types` job, the same book and site
renders and search test in `docs`, the same bundle rebuild in `web`, and the
whole of [`.pre-commit-config.yaml`](.pre-commit-config.yaml) in the
`pre-commit` job — not only lint and formatting but the dependabot, YAML and
TOML schema checks, the whitespace and line-ending hooks and the comment lint.
What that job leaves out is `pyright` and `unittest`, which other jobs own, and
`commit-message-shape`, a `commit-msg` hook that runs only where the message is
written. `make preflight` is that set run once on one platform; what it leaves
to CI is the coverage job and the twelve `os` x `python-version` legs.
Type-checking is deliberately two-tiered: `pyright` in basic mode across the
whole tree (including tests), matching Pylance's VS Code defaults so editor
diagnostics align with CI, plus `mypy --strict` on the state-bearing modules
listed in `[tool.mypy] files` where a type slip would corrupt state.

## Tests

The suite is stdlib `unittest`, one module per subject under `tests/`, and it
runs entirely without hardware — the hardware backends are faked. A test run
should print **only** pass/fail/skip indicators: wrap any path that raises an
expected exception in `assertRaises`, any path that logs an expected
warning/error in `self.assertLogs("c64cast.<module>", …)`, and any path that
writes to stdout in a `redirect_stdout`. Expected output left to print buries
real failures. When a call both logs and raises, nest `assertLogs` *outside*
`assertRaises` so the records are actually verified.

Where the message is incidental — a warning from a subsystem the test only had
to set up — `quiet_logging()` from [`tests/_fakes.py`](tests/_fakes.py) swallows
it instead of asserting it. It also restores the root logger, which anything
driving `cli.main()` needs: `configure_logging` replaces the root handlers
process-wide, so without it that handler outlives the test and every later INFO
record in the same worker prints, from modules with no connection to the CLI.

**The suite may not touch your own files, and a hook enforces that rather than
trusting the convention.** Every entry point runs with `PYTHONPATH=tests`, which
makes `site` import [`tests/sitecustomize.py`](tests/sitecustomize.py) at
interpreter startup — in `unittest_parallel`'s worker processes as well as the
parent, since only the environment reaches those. That points the machine
settings and the data dir at a throwaway directory for the whole run, blanks
`char_rom`'s cwd-relative ROM fallback, and installs an audit hook that fails
any test reading or writing outside the checkout and the temp directories, plus
anything under `assets/` that git does not carry and anything under `.git/`,
which the "outside the checkout" rule could never have reached. The hook also
watches `subprocess`: a `git` call that names one repository with `-C` while
`GIT_DIR` names a different one is refused, because `GIT_DIR` wins and the
write lands in the repository the caller did not name — but only for a `git`
it can see, which is one started from an argv list and not through a shell.
The rule, the reasoning and the known blind spots are in
[`tests/_fs_sandbox.py`](tests/_fs_sandbox.py)'s docstring.

**A test may not leave a thread running either, and the same startup hook
enforces that.** A thread outlives every guard the test was wrapped in —
`quiet_logging()` is `logging.disable`, which ends with its block, and
`assertLogs` swaps a handler for the same span — so a poll thread still
ticking afterwards logs into the middle of an unrelated test.
`tests/sitecustomize.py` also arms
[`tests/_thread_sandbox.py`](tests/_thread_sandbox.py), which fails the test
that ends with a thread it started still alive, naming the thread. `PollThread`
names every loop at its construction site, so the name identifies the owner;
the fix is to call that object's teardown from `addCleanup`. A stray gets half
a second to finish first, so a thread genuinely winding down is not a failure.

**A test that stops making progress is interrupted, not waited out.**
`unittest_parallel` exposes no timeout and the CI jobs bound only the whole
job, so a hung test used to spend that budget and identify itself nowhere.
[`tests/_timeout_sandbox.py`](tests/_timeout_sandbox.py), armed from the same
startup hook, caps a test at 60 seconds — far above the slowest legitimate one
here, which measures about a second. Past the cap it writes every thread's
stack to stderr under the test's name and raises `TestTimedOut` in the thread
running it, so the run reports that test and goes on to the next. Set
`C64CAST_TEST_TIMEOUT_S=0` to turn the watchdog off while stepping through a
test under a debugger. What it cannot reach is a test blocked in a call that
never returns to the interpreter — the module docstring has that and the rest
of the blind spots.

**A test module starts a child process only through `run_bounded`.** `subprocess.run`
with no `timeout` waits forever; on Windows it waits in `Popen._communicate`,
where `endtime` is None and the reader-thread join is `join(None)`. That is
what PR #491's Windows job hit — a `node --check` that never returned, blocking
until the per-test cap above reported "no progress", which names the test and
not the cause. `run_bounded()` in
[`tests/_child_process.py`](tests/_child_process.py) is `subprocess.run` under
a 20-second bound, a third of that cap and ~27x the slowest child this suite
actually runs; when it expires the child is killed and the test fails naming
the command and the tail of whatever it wrote. Pass `timeout=` to it for a
child genuinely slower than the default. An AST sweep in
[`tests/test_child_process.py`](tests/test_child_process.py) fails any module
under `tests/` that reaches `subprocess` without a bound — a missing
`timeout=` and a `timeout=None` alike — including
`Popen`, which takes none, so a test that needs one extends
`_child_process.py` rather than hand-rolling the bound. `scripts/` is out of
scope: the ones a gate runs already bound their own calls, and
`scripts/diags/` drives real hardware from a terminal, where a child running
for minutes is the measurement rather than a hang.

**A child the code under test starts is bounded too, and the sweep cannot see
it.** The sweep reads `tests/`; production code starts children of its own,
under bounds chosen for a user at a terminal, and several of those are
`_timeout_sandbox`'s cap exactly — `doctor._probe_uv_lock` runs a real `uv
lock --check` under `timeout=60` in 31 of `test_doctor`'s tests, and
[`tests/_child_sandbox.py`](tests/_child_sandbox.py)'s docstring lists the rest
alongside what each does when its child's expiry is swallowed. A bound equal to
the cap cannot fire first in any useful way: measured with a `uv` that never
returns, one shape reported `TestTimedOut: no progress for 60s` and the other
spent the same 60 seconds and then caught its own `TimeoutExpired` into a
`warn` diagnostic, so the test failed on an unrelated assertion — or passed.
That module, armed from the same startup hook, shortens any wait past `BOUND_S`
inside the test process, kills the child and raises `ChildProcessHung` naming
the command, the bound the caller had asked for and the tail of whatever the
child wrote. It derives from `BaseException` for the reason `TestTimedOut`
does: every one of those sites catches the `TimeoutExpired` it replaces and
carries on, and `doctor` and `upgrade` degrade through `except Exception`
elsewhere, so nothing short of a `BaseException` clears every such handler. The production numbers stay where
they are — `--doctor` run by hand still gives `uv` its 60 seconds — and a
caller that asked for *no more than* `BOUND_S` keeps its own `TimeoutExpired`
(`upgrade._stop`'s interrupt grace is `BOUND_S` exactly), because that bound is
the caller's own behavior and its own tests grade it.

**A test may not leave the process-wide RNG seeded.** `random` and numpy's
legacy global generator both carry state across tests in a worker, and this
program draws from both, so a `random.seed()` left behind decides what a later
test's production code draws — and which tests share a worker changes per run.
[`tests/_rng_sandbox.py`](tests/_rng_sandbox.py) reseeds both from the test's
own id before every test. A test that wants a particular sequence still calls
`random.seed()` itself, in the test or in `setUp`; a seed set in `setUpClass`
or at module import is overwritten before the first test under it runs. Prefer
a `random.Random()` instance or a `np.random.Generator`, which are nobody
else's state.

If a test trips the filesystem hook, the fix is almost always to point the code
under test at a file the test writes under `tempfile.mkdtemp()`, or to run the
block from `tmp_cwd()` (in [`tests/_fakes.py`](tests/_fakes.py)) when what it
resolves is a *relative* default like `assets/videos/`.
`MachineSettingsIsolation` is still
there for a module that wants a settings/data directory of its own, fresh and
untouched by anything else.

### Proving a test can fail

"A test covers this" is an argument; a test you watched go red is evidence. The
cheap way to get the evidence is to break the line under test on purpose, run
the suite, and check that a *named* assertion failed — then revert and re-run
green. A fix whose test would have passed either way is a fix nobody can
maintain.

Run `make mutation-ready` first. CPython validates a compiled module against
its source's modification time in **whole seconds**, so an edit applied and
reverted inside one second, with the file's length unchanged, leaves both the
timestamp and the size where they were: the interpreter reads the cached
bytecode from before your edit and reports a green run that means nothing.
`PYTHONDONTWRITEBYTECODE=1` does not help — it suppresses writing, not reading
— and neither does `touch`. The target recompiles the tree in PEP 552
hash-based mode, where the check is over the source's contents instead, and
then verifies that every module an import here could read really is armed —
which includes having bytecode at all. Absence is not a gap in what the check
can see: `compileall` compiles every source under a root whether or not
anything imports it, so a missing `.pyc` after arming means the arming lapsed,
and it is the dangerous shape rather than a benign one, because the first
import then writes a *timestamp-mode* file.

It has to be re-run more often than it looks: `make clean` deletes the armed
bytecode, a fresh worktree has none to begin with, and a `uv sync` that moves
the Python minor invalidates the lot. Every one of those is silent, and
they all happen *after* the arming — so `make mutation-check` is the check on
its own, to run at the moment a proof's green is about to be believed. It is
deliberately not part of `make test`: arming matters only for a mutation proof,
and failing every ordinary run on an unarmed tree would teach everyone to
bypass it.

Several tests exist purely to stop documentation from drifting — the JSON schema
against the config metadata, the annotated example TOML against the dataclass
fields, the `all` extra against the union of the other extras. If one of those
fails, the fix is usually to regenerate rather than to edit the test.

The web console is the same idea in build output rather than in docs: its
sources are in `web/` and its compiled bundle is committed under
`c64cast/web/dist` so that installing c64cast never needs Node. Change the
sources and `make web` in the same commit — CI rebuilds it and fails on a diff.
Which Node that rebuild runs on is [`.node-version`](.node-version) at the repo
root: CI's `actions/setup-node` reads it, as does fnm. mise and asdf each need
idiomatic version files switched on first — `mise.toml` does that for mise, and
asdf wants `legacy_version_file = yes` in `~/.asdfrc`. nvm reads `.nvmrc` only,
never this file.

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
- `make preflight` must be green before you open the PR — see
  [The pre-PR gate](#the-pre-pr-gate).
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
- Write in American English — `color`, `behavior`, `serialize`, `center` — in
  prose, code, comments and identifiers alike. The config keys the reader types
  are spelled that way, so British prose disagrees with its own examples.
  `grey`/`gray` and `canceled`/`cancelled` are interchangeable and both fine.

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
