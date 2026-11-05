# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
python -m c64cast -u u64://192.168.2.64 -d 0
# or with a config file (overrides defaults; CLI flags still win):
python -m c64cast --config c64cast.toml
# or quick playback — one scene per positional MEDIA arg, no config file:
scripts/c64cast.sh -u tr:// clip.mp4 tune.sid assets/pictures/
```

[scripts/c64cast.sh](scripts/c64cast.sh) is the equivalent launcher for shells where direnv hasn't activated `.venv` — see ["Running from a checkout"](CONTRIBUTING.md#running-from-a-checkout).

**Connection target (`-u/--url`).** One scheme-aware string selects the backend and its endpoint: `u64://HOST` or `http(s)://HOST` (Ultimate 64), `tr://` (TeensyROM+ USB serial, auto-detected), `tr:///dev/cu.usbmodemXYZ` / `tr://COM3` (explicit serial), `tr://HOST[:PORT]` (TeensyROM+ TCP, default 2112); per-link knobs ride as `?query` params, `$C64CAST_URL` is the env fallback. It must not carry a `user:pass@` netloc — a secret goes in `C64CAST_DMA_PASSWORD` or `[ultimate64].dma_password` instead, never in the URL (which `--save-settings` can write to disk and echo to stdout). Details: [c64cast/app/connect.py](c64cast/app/connect.py)'s docstring.

**Quick playback** (positional `MEDIA` args, mutually exclusive with `--config`) builds an in-memory-only config — one scene per argument by extension/URL, no loop unless `--loop`. Details: [c64cast/app/quickcast.py](c64cast/app/quickcast.py)'s docstring; URL resolution is shared with config-driven video scenes ([`scene_factory.py`](docs/architecture/config.md#scene_factorypy)).

**Audio is on by default**; `--no-audio` mutes. U64 video audio uses the Ultimate Audio FPGA PCM sampler when available; `[audio].backend = "dac"` forces the 4-bit `$D418` DAC (the only path on TeensyROM and for mic/webcam audio). The sampler's effective clock is a measured constant, not the nominal 6.25 MHz — why, and how to re-measure: [audio.md → sampler](docs/architecture/audio.md#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm).

Flag groups (`-h` shows them grouped): `connection`, `quick playback`, `video input`, `audio`, `vision input`, `playlist`, `web console`, `introspection`, `updates`, `debug`. Notable: `--config`, `-v` / `-vv`, `--log-file PATH` (each scene activation logs a `SCENE_CONFIG_JSON` snapshot safe for a public video description — [`recording_metadata.py`](docs/architecture/config.md#recording_metadatapy--per-scene-scene_config_json-logging)).

The DMA password (if the U64 has one set) is supplied via `C64CAST_DMA_PASSWORD` env var or `[ultimate64] dma_password` in the config — **no CLI flag**, so secrets don't leak into shell history or `ps` output. The env var wins when both are set.

**Firmware prerequisites:** three U64 switches in two menus (Ultimate DMA Service, Web Remote Control Service, Command Interface), plus **Bus Operation Mode = `Writes`** when a TeensyROM+ sits in an Ultimate's cartridge port — walkthrough in [docs/guide/04-setting-up.md](docs/guide/04-setting-up.md), symptoms in [docs/caveats.md](docs/caveats.md). The REU and the sampler are *not* switches — `hw_provision` enables them live+volatile per run.

Hard deps: `opencv-python`, `numpy`, `requests` (with `urllib3`), `py65`; optional extras are grouped in [pyproject.toml](pyproject.toml) (`video` = PyAV, `yt` = yt-dlp, `wizard` = `--init`, …, `all`), and dev tools are a PEP 735 `dev` group installed by default.

**Setup is `uv sync --all-extras` — never `uv pip` and never raw `pip`** (the mise/`UV_PYTHON` trap: ["Development setup"](CONTRIBUTING.md#development-setup)). The `make` targets route through `uv run`, so they hit the synced project env from any shell; `make doctor` is the fast offline self-check (environment-probe rationale on the probe docstrings in [c64cast/app/doctor.py](c64cast/app/doctor.py)); type-checking is `pyright` basic tree-wide + `mypy --strict` on the state-bearing modules — ["The pre-PR gate"](CONTRIBUTING.md#the-pre-pr-gate).

Target hardware: an [Ultimate 64](https://ultimate64.com/) on the LAN. Writes and PRG kicks (DMARUN) go over the **Ultimate DMA Service** (TCP port 64, persistent socket); reads, reset, `.crt` launches, and probe go over REST. The REST `run_prg` runner is avoided because it leaves a numbered copy in the device's `/Temp` on every launch ([hardware-io.md](docs/architecture/hardware-io.md#prg-kicks-go-over-dmarun-not-run_prg)). SID playback DMAs the payload + a tiny 6502 player into C64 RAM and kicks a `SYS` stub through DMARUN — the firmware's `runners:sidplay` endpoint is deliberately avoided because it hijacks HDMI. See [api.run_sid_player](c64cast/hw/api.py) and [docs/caveats.md](docs/caveats.md).

## Configuration

TOML file (`--config PATH` wins; else `./c64cast.toml` if present; else built-in defaults). See [c64cast.example.toml](c64cast/examples/c64cast.example.toml) for an annotated reference.

**Precedence (single system):** dataclass defaults → **machine settings** → project/per-system TOML → CLI → env (`C64CAST_DMA_PASSWORD`). **Ensemble (per system):** defaults → machine settings → per-system TOML → master cascade (fills only fields still at the machine-overlaid baseline) → CLI/env. Every layer above the defaults overrides the ones below it.

Every CLI flag has `default=None`; `config.merge_cli()` only overwrites a config field when the CLI value is non-None.

Three mechanisms are detailed in [docs/architecture/config.md](docs/architecture/config.md):

- **Machine settings** — `~/.config/c64cast/settings.toml`, cross-run defaults (connection target, capture device, SID model — never playlists) overlaid on every run type including quick playback; `--save-settings` writes it and can never write the DMA password. See [the machine-settings layer](docs/architecture/config.md#machine-settings-layer) and [`cli.py`](docs/architecture/config.md#clipy).
- **Packaged resources** — the demo configs (`--config example:NAME`; `--list-examples` / `--print-example NAME`) and the JSON schema ship *inside* the package so they work from a wheel; see [`paths.py`](docs/architecture/config.md#pathspy).
- **Data dir** — persisted machine state (DAC calibrations, WLED + loop presets) under `~/.local/share/c64cast/` (`$C64CAST_DATA_DIR` overrides), resolved at use time; see [`paths.py`](docs/architecture/config.md#pathspy).

**Config metadata is the single source of truth.** Every dataclass field in [config.py](c64cast/app/config.py) carries `help`/`choices`/`applies_to` metadata; overlays carry `HELP`/`PARAM_HELP` plus restriction attrs. Four surfaces render that one model and cannot drift from the code (design rationale in each module's docstring): [introspect.py](c64cast/app/introspect.py) (the config-free discovery commands — `--list-*`, `--describe`, `--compat`, `--print-schema`, `--suggest-palette`), [schema.py](c64cast/app/schema.py) (the committed JSON Schema — `make schema`, CI fails on drift), [config_serialize.py](c64cast/app/config_serialize.py) (`load(dumps(cfg)) == cfg`), and [wizard.py](c64cast/app/wizard.py) (`--init`). When adding a config field or overlay: fill in its `help`/`PARAM_HELP`, run `make schema`, and the drift tests in [tests/test_example_toml_drift.py](tests/test_example_toml_drift.py) + [tests/test_introspect.py](tests/test_introspect.py) keep everything honest. `--doctor --skip-probe` is the offline, collect-all config check.

## Architecture

Per-module internals — design rationale, hardware constraints, and edge-case history
— live in [docs/architecture.md](docs/architecture.md). It is an index: the notes
themselves are split by topic area under [docs/architecture/](docs/architecture/), and
the index's module table routes a module to its section. Modules whose notes haven't
been written yet are listed under "Not covered here", where the module docstring
carries the rationale instead; [tests/test_architecture_index.py](tests/test_architecture_index.py)
holds the two lists to a partition of the tree and checks every row's anchor resolves,
so a new module has to be routed somewhere. **Read the relevant section before
modifying a module**; it carries the *why* (and the dead ends) that the code alone
doesn't. Keep the two in sync: a behavior change to a module updates its architecture
section in the same change set (see ["Documentation is part of the
change"](CONTRIBUTING.md#documentation-is-part-of-the-change) in CONTRIBUTING.md).

The three **books** under `docs/` — the [User's Guide](docs/guide/README.md), the
[Programmer's Reference Guide](docs/reference/README.md), and the
[Performance Card](docs/card/README.md) — are the end-user surface (each book's
README says what belongs in it), and the same Markdown is the documentation site
at <https://kfox.github.io/c64cast/>; `docs/architecture*` is deliberately not
published. [scripts/bookdoc.py](scripts/bookdoc.py) owns the shared dialect and
anchor rules for both renderers — its docstring has the design. `make books`
renders the PDFs; `make site-check` is the parse check CI runs on every PR.

Other docs: [caveats.md](docs/caveats.md), [troubleshooting.md](docs/troubleshooting.md),
[extending.md](docs/extending.md). [CHANGELOG.md](CHANGELOG.md) follows Keep a
Changelog — anything a user would notice gets a fragment under `changelog.d/`.
README links are **absolute** GitHub/raw URLs — it is the PyPI long_description,
where relative paths 404. **Releasing** is [RELEASING.md](RELEASING.md), guarded by
[tests/test_release.py](tests/test_release.py). Visual verification on real hardware
is the `hw-visual-verify` skill.

## Workflow

Non-trivial changes go through the [`ship` skill](.claude/skills/ship/SKILL.md)
— see it for the stages, the pinned-paths review policy, and the never-merge
rule. **Never merge** — hand over a green PR; the merge is Kelly's.

**Every change happens in a worktree of its own**, under
`.claude/worktrees/<name>` and with its own environment — call `EnterWorktree`
with a `name` before the first edit, whether or not the change goes through
`ship`. The primary checkout is shared, so another session may be standing in
it and a `git pull` there moves HEAD out from under uncommitted work. Two hooks
hold the line: [`require-worktrees-in-checkout.py`](.claude/hooks/require-worktrees-in-checkout.py)
refuses a `git worktree add` aimed anywhere else, and
[`require-edits-in-a-worktree.py`](.claude/hooks/require-edits-in-a-worktree.py)
asks before an edit lands in the primary checkout. The environment half is
[`scripts/check_venv_target.py`](scripts/check_venv_target.py), which fails a
commit whose project environment does not import the checkout it ran from. In a
worktree, `make` takes the override as an argument (`make check
UV_PROJECT_ENVIRONMENT=<worktree>/.venv`) while git needs it as a prefix
(`UV_PROJECT_ENVIRONMENT=<worktree>/.venv git commit …`), because git reads a
trailing `VAR=value` as a pathspec. A write driven through Bash (`sed -i`, a
redirection) reaches neither hook, so there it rests on this rule alone.

### Findings: fix it, file it, or drop it

These rules bind every session and subagent that works here: a ship run, a
one-off fix, a finder, a review lane, the `reviewer`. An agent's definition or
prompt may narrow its lane; it never loosens these. A prompt that hands work to
a subagent says "follow CLAUDE.md → Findings" rather than paraphrasing them.

1. **A finding is a real defect:** a concrete input or state that produces a
   wrong result, a crash, a hang, a leak or a security hole. A test that fails
   on the current code proves it before the fix lands. Style, refactors and
   prose as writing are not findings.
2. **Fix it — the default.** Grep for the pattern, predicate or shape you
   changed and fix every sibling site in the same commit. Another instance of
   the class the change fixes — the same mechanism one call away — is fixed in
   this change as its own commit with its own failing-first test, not filed.
   "Small" is a reason to fix it, never to file it; a two-line fix costs less
   than the issue describing it. A defect outside that class stays out of this
   change's commits and PR, even when it sits in a file the change touches:
   file it under rule 4 or decline it under rule 5.
3. **A guard ships only with the input that reaches it.** A check, clamp,
   retry, fallback, `try`/`except`, `None` test or validation names the caller
   at `file:line`, the value, and the wrong result without it. That value comes
   from an **observed failure**, an **external contract** (a file format, a
   device or firmware reply, a third-party API) or a **trust boundary**
   (network, user config, a MIDI or web peer). A value this code produced, or
   one its types already rule out, is not an input. The guard's test feeds that
   value and fails without the guard; a guard no test can reach is dead code.
   At a trust boundary the bar is reachability, not likelihood: cite the path
   from entry point to sink, and validate once where the value enters, not
   again at each layer it passes.
4. **File it** only when it cannot be fixed here *and* it names (1) the caller
   at `file:line`, (2) the input that reaches it — observed, from an external
   contract, or chosen across a trust boundary — and (3) the wrong result.
   "Cannot be fixed here" means it is outside the class the change fixes, or
   it needs a design decision that is Kelly's, hardware nobody authorized,
   files another in-flight branch is changing, or would make this change
   materially bigger or riskier. One labeled issue per
   item, linked to the PR — never a list in a PR description.
5. **Drop it — everything else.** Suspicions no input reaches, latent traps
   ("would become real if…"), guards for failures never seen, design opinions,
   behavior that matches its docs, flake risks never seen failing. Each gets one
   line under "Declined" in the report that reaches the user; none is committed
   or filed. Don't manufacture work — there is plenty of real work queued.

**A defect found in merged code is labeled `escaped-review`**: on its issue when
it is filed, otherwise on the PR that fixes it. The `review-audit` skill counts
those labels to tell whether review is missing things.

**Reviews** run through the agents in [.claude/agents/](.claude/agents/):
`reviewer` for every change, and the four lanes `/panel-review`
([.claude/skills/panel-review/](.claude/skills/panel-review/SKILL.md)) launches
when a panel is chosen instead. The `ship` skill's step 3 says which review a
change gets. A reviewer runs `make check` with the worktree's override as above,
and runs `make mutation-ready` before any mutation proof.

**Pinned paths.** A change touching any of these is reviewed at `high`,
whatever its size, because a one-line change there does the most damage. This
list does not replace the security notes elsewhere in this file, which may name
a site the list has not caught up to yet:

- `c64cast/hw/api.py`, `hw/socket_dma.py`, `hw/teensyrom_dma.py`,
  `hw/backend.py`: the DMA write path to the hardware, including the shared
  `write_memory*`/`write_regs`/`write_region` implementation that every backend
  sits on top of.
- `tests/_fs_sandbox.py`: the suite's filesystem sandbox, which is never
  widened to make a test pass.
- `c64cast/app/connect.py`, `app/config_serialize.py`,
  `app/recording_metadata.py`, `app/cli.py`:
  - connection-target parsing;
  - the rule that the DMA password never rides in a URL or CLI flag;
  - the redaction list that `--save-settings` and the scene-log snapshot both
    depend on;
  - the env/config precedence that decides which value wins.
- `c64cast/control/auth.py`, `control/web_api.py`, `app/serve.py`:
  - the `[web]`/`[control]` token gate itself (`auth.py`: token comparison,
    viewer/full-role checks);
  - the routes that depend on it;
  - where the token is minted and its precedence resolved (`serve.py`:
    `resolve_tokens`, `_generated_token`, `build_daemon_app`).

  This token is the same class of secret as the DMA password, and it gates the
  same kind of remote control of the host.
- `.github/workflows/`: the jobs that decide whether a PR may merge, and the
  ones holding publish rights (PyPI, the Pages site, GitHub releases). A change
  here can turn a gate green without running it, or ship what nobody reviewed.

**Hardware during a review or a ship run** is allowed only when the caller's
prompt authorizes it for that run, and then only under the rules in
[.claude/skills/hw-visual-verify/hardware-runs.md](.claude/skills/hw-visual-verify/hardware-runs.md).
Without that authorization, a claim only hardware can settle goes back to the
caller as an open question.

## Spelling

**Everything in this repository is written in American English — prose, code,
comments, docstrings, identifiers, log messages, and commit messages.** The
program names itself that way (`color_match`, `grayscale`), so British prose
contradicts the keys it tells the reader to type.

Two variants are interchangeable in American English and both are accepted:
`grey`/`gray` and `canceled`/`cancelled`. Neither is a defect to fix.

Three places keep a British spelling on purpose, and none of them is prose:

- [`palette.py`](c64cast/video/palette.py) resolves `grey` as an alias for
  `gray`, so a user can type either — the alias is a feature, not a typo.
- `cancelled()` in [.github/workflows/](.github/workflows) is a GitHub Actions
  expression function.
- The `{"colour": "red"}` fixture in
  [tests/test_fuzzy_suggestions.py](tests/test_fuzzy_suggestions.py) is a
  plausible user typo the fuzzy matcher has to answer with `fg_color`.

The changelog is a record: an entry that quotes a spelling it changed keeps the
quote as written.

## Tests

Stdlib `unittest`, one module per subject under `tests/`, no hardware — run it
with `make test` (never a raw runner). The conventions, and the reasoning and
blind spots behind each guard below, are in ["Tests"](CONTRIBUTING.md#tests).

**A test run must print only pass/fail/skip indicators, and committing a test
that leaks anything else is forbidden.** Check with `make test` and read the
output: anything between the dots is a leak.

- Raises → `assertRaises`. Logs → `self.assertLogs("c64cast.<module>", …)`.
  Writes to stdout → `redirect_stdout` plus an assertion on the buffer.
- A call that both logs and raises nests `assertLogs` *outside* `assertRaises`.
- An incidental message gets `quiet_logging()` from
  [tests/_fakes.py](tests/_fakes.py), with some *other* test asserting it; it
  also restores the root logger, which anything driving `cli.main()` needs.
- `quiet_logging()` and `assertLogs` never nest — `logging.disable` outranks
  `assertLogs` — so asserting tests go in their own class.
- `patch.object(...).start()` needs a `stop()`: use `with` or `addCleanup`.

**[tests/sitecustomize.py](tests/sitecustomize.py) arms guards that fail a
test and name it:** filesystem ([_fs_sandbox.py](tests/_fs_sandbox.py)),
threads that outlive their test, a 60 s hang timeout
(`C64CAST_TEST_TIMEOUT_S=0` under a debugger), child processes outside
`run_bounded()`, and global-RNG seeding outside the test. What each one
enforces and how to fix a test it trips are in ["Tests"](CONTRIBUTING.md#tests)
and each module's docstring. Never widen the filesystem sandbox to pass a test;
use a `tempfile.mkdtemp()` fixture or `tmp_cwd()`, and keep
`allow_outside_checkout(path)` for a test whose subject is a real path.

## Quirks worth knowing

Cross-cutting traps that belong to no single module. **Per-subsystem design rationale
lives in [docs/architecture.md](docs/architecture.md) — read the relevant section there
before touching one of those subsystems.**

- `C64_PALETTE_BGR` is OpenCV BGR order, not RGB.
- **The `[preview]` window must be pumped from the main thread** (a macOS Cocoa requirement), so `PreviewWindow` is `open()`/`pump()`/`close()` driven by `session._pump_previews_until_done`. Don't turn it back into a thread — see [caveats.md](docs/caveats.md) → "Preview window fidelity + limits".
- `AudioStreamer` shares the render path's `Ultimate64API`, because the U64 DMA service is single-connection; the shared `SocketDMAClient` is thread-safe and redials after the firmware's idle close. Render and audio together can reach the ≈200 commands/s ceiling — [hardware-io.md](docs/architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient), [caveats.md](docs/caveats.md).
- Address strings passed to `write_memory*` work in either case (`"d018"` and `"D018"` are both fine).
- The dirty cache is keyed by `region_id` (small ints) not address — so a mode switch from PETSCII to MCM (both writing $0400) gets a clean diff baseline via `api.invalidate_cache()`. `InterstitialScene` reuses `RegionID.SCREEN`/`RegionID.COLOR` for the same reason.
- `backgrounds.py` constants are C64 *screen codes* (what goes to $0400), not PETSCII codes — the encodings diverge above 0x40 (e.g. `@` is PETSCII 0x40 but screen code 0x00).
- **On the U64 DMA link, write *count* is the lever, not bytes:** payload below ~2.1 KB is free, so coalesce (`write_regs`), dirty-skip (`write_region`), never split a write to save bytes, and widen one over a clean gap. The trade inverts on the TeensyROM+, so it lives on `HardwareProfile.write_cost_s`, not in callers — measurements in [hardware-io.md](docs/architecture/hardware-io.md).
