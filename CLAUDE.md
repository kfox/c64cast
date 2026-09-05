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

**Audio is on by default**; `--no-audio` mutes. U64 video audio uses the Ultimate Audio FPGA PCM sampler when available; `[audio].backend = "dac"` forces the 4-bit `$D418` DAC (the only path on TeensyROM and for mic/webcam audio). The sampler's effective clock ships as 6160000 Hz, not the nominal 6.25 MHz — why, and how to re-measure: [audio.md → sampler](docs/architecture/audio.md#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm).

Flag groups (`-h` shows them grouped): `connection`, `quick playback`, `video input`, `audio`, `vision input`, `playlist`, `web console`, `introspection`, `updates`, `debug`. Notable: `--config`, `-v` / `-vv`, `--log-file PATH` (each scene activation logs a `SCENE_CONFIG_JSON` snapshot safe for a public video description — [`recording_metadata.py`](docs/architecture/config.md#recording_metadatapy--per-scene-scene_config_json-logging)).

The DMA password (if the U64 has one set) is supplied via `C64CAST_DMA_PASSWORD` env var or `[ultimate64] dma_password` in the config — **no CLI flag**, so secrets don't leak into shell history or `ps` output. The env var wins when both are set.

**Firmware prerequisites:** three U64 switches in two menus (Ultimate DMA Service, Web Remote Control Service, Command Interface), plus **Bus Operation Mode = `Writes`** when a TeensyROM+ sits in an Ultimate's cartridge port — walkthrough in [docs/guide/04-setting-up.md](docs/guide/04-setting-up.md), symptoms in [docs/caveats.md](docs/caveats.md). The REU and the sampler are *not* switches — `hw_provision` enables them live+volatile per run.

Hard deps: `opencv-python`, `numpy`, `requests`, `py65`; optional extras are grouped in [pyproject.toml](pyproject.toml) (`video` = PyAV, `yt` = yt-dlp, `wizard` = `--init`, …, `all`), and dev tools are a PEP 735 `dev` group installed by default.

**Setup is `uv sync --all-extras` — never `uv pip` and never raw `pip`** (the mise/`UV_PYTHON` trap: ["Development setup"](CONTRIBUTING.md#development-setup)). The `make` targets route through `uv run`, so they hit the synced project env from any shell; `make doctor` is the fast offline self-check (environment-probe rationale on the probe docstrings in [c64cast/app/doctor.py](c64cast/app/doctor.py)); type-checking is `pyright` basic tree-wide + `mypy --strict` on the state-bearing modules — ["The pre-PR gate"](CONTRIBUTING.md#the-pre-pr-gate).

Target hardware: an [Ultimate 64](https://ultimate64.com/) on the LAN. Writes go over the **Ultimate DMA Service** (TCP port 64, persistent socket); reads, reset, run_prg, and probe go over REST. SID playback DMAs the payload + a tiny 6502 player into C64 RAM and kicks a `SYS` stub through `run_prg` — the firmware's `runners:sidplay` endpoint is deliberately avoided because it hijacks HDMI. See [api.run_sid_player](c64cast/hw/api.py) and [docs/caveats.md](docs/caveats.md).

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
Changelog — anything a user would notice gets an entry under `## [Unreleased]`.
README links are **absolute** GitHub/raw URLs — it is the PyPI long_description,
where relative paths 404. **Releasing** is [RELEASING.md](RELEASING.md), guarded by
[tests/test_release.py](tests/test_release.py). Visual verification on real hardware
is the `hw-visual-verify` skill.

## Spelling

**Everything in this repository is written in American English — prose, code,
comments, docstrings, identifiers, log messages, and commit messages.** British
spellings are forbidden: write `color`, `behavior`, `serialize`, `normalize`,
`recognize`, `analyzer`, `catalog`, `artifact`, `judgment`, `center`,
`optimization`, `labeled`, `modeled`, `traveling`, `license`, `defense`. The
program has always named itself that way — `color_match`, `grayscale`,
`palette_mode` — so British prose puts the text at odds with the keys it is
telling the reader to type.

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
with `make test` (never a raw runner; see ["Tests"](CONTRIBUTING.md#tests) for
the full conventions).

**A test run must print only pass/fail/skip indicators, and committing a test
that leaks anything else is forbidden.** Expected output buries real failures,
so every by-product gets wrapped where it fires:

- Raises → `assertRaises`. Logs → `self.assertLogs("c64cast.<module>", …)`.
  Writes to stdout → `redirect_stdout` (plus an assertion on the buffer).
- When a call both logs and raises, nest `assertLogs` *outside* `assertRaises`,
  or the records go unverified.
- Where the message is incidental — a warning from a subsystem the test only
  had to set up — use `quiet_logging()` from [tests/_fakes.py](tests/_fakes.py)
  instead, and make sure some *other* test asserts that message. It also
  restores the root logger, which anything driving `cli.main()` needs.
- `logging.disable` outranks `assertLogs`, so `quiet_logging()` and
  `assertLogs` must never nest — put the asserting tests in their own class.
- `patch.object(...).start()` with no matching `stop()` leaks past the test
  into its whole worker process. Use a `with` block or `addCleanup`.

Check with `make test` and read the output: anything between the dots is a leak.

**The suite cannot touch files outside the checkout, and this is enforced, not
asked for.** Every entry point sets `PYTHONPATH=tests` so
[tests/sitecustomize.py](tests/sitecustomize.py) arms
[tests/_fs_sandbox.py](tests/_fs_sandbox.py) at interpreter startup: the machine
settings and data dir are redirected to a throwaway directory for the whole run,
`char_rom`'s cwd-relative ROM fallback is blanked, and an audit hook fails any
test that reads or writes outside the checkout + temp dirs, or reaches an
`assets/` file git does not carry. When a test trips it, point the code under
test at a `tempfile.mkdtemp()` fixture, or wrap the block in `tmp_cwd()` from
[tests/_fakes.py](tests/_fakes.py) if what it resolves is a *relative* default
(`assets/videos/`, `./c64cast.toml`). Never widen the sandbox to make a test
pass — `allow_outside_checkout(path)` exempts that one path, for a test whose
subject genuinely is a real path, and nothing else.

## Quirks worth knowing

Cross-cutting traps that belong to no single module. **Per-subsystem design rationale
lives in [docs/architecture.md](docs/architecture.md) — read the relevant section there
before touching one of those subsystems.**

- `C64_PALETTE_BGR` is OpenCV BGR order, not RGB.
- **The `[preview]` window must be pumped from the main thread.** cv2's HighGUI may only create/service a window on the process's main thread (hard Cocoa requirement on macOS — an off-thread `namedWindow` raises "Unknown C++ exception from OpenCV code"). Every playlist runs on a worker thread, so `PreviewWindow` is deliberately *not* self-driving like `StreamRecorder` is: it's `open()`/`pump()`/`close()`, driven by `session._pump_previews_until_done` from the otherwise-parked main thread. Don't "simplify" it back into a thread — that's what the pre-cv2 pygame version did, and it never worked on macOS. See [docs/caveats.md](docs/caveats.md) → "Preview window fidelity + limits".
- `AudioStreamer` shares the render path's `Ultimate64API` instance. The U64 DMA service is single-connection only: a second concurrent socket TCP-accepts but its IDENTIFY never gets a reply, and the first socket blocks new ones for a few seconds after close. The shared `SocketDMAClient` is thread-safe (per-command mutex around sendall) and the combined write rate (audio ≈8/sec + render ≈30-60/sec) sits well under the ≈200/sec DMA ceiling.
- Address strings passed to `write_memory*` work in either case (`"d018"` and `"D018"` are both fine).
- The dirty cache is keyed by `region_id` (small ints) not address — so a mode switch from PETSCII to MCM (both writing $0400) gets a clean diff baseline via `api.invalidate_cache()`. `InterstitialScene` reuses `RegionID.SCREEN`/`RegionID.COLOR` for the same reason.
- `backgrounds.py` constants are C64 *screen codes* (what goes to $0400), not PETSCII codes — the encodings diverge above 0x40 (e.g. `@` is PETSCII 0x40 but screen code 0x00).
- Memory writes go over the **Ultimate DMA Service** (TCP port 64, persistent socket, ≈5 ms / ≈200 writes/sec). See [docs/caveats.md](docs/caveats.md) → "Writes go over Socket DMA, not REST" for the transport measurements and why REST can't carry writes. Reducing write *count* (coalesce via `write_regs`, dirty-skip via `write_region`) is still the right move under DMA — it's cheaper than tightening the per-write floor. **Payload below ~2.4 KB is free on this link** (8 B and 2 KB both cost ≈5.2 ms), which is *why* count is the lever — so never split a write to save bytes, and widen one to cover a clean gap without hesitation. That trade is per-link and inverted on the TeensyROM+, so it lives on `HardwareProfile.write_cost_s` rather than in any caller; measure with `scripts/diags/link_cost_model.py`.

Where the per-subsystem detail lives (the [architecture reference](docs/architecture.md) is split by topic area; its module index routes any module to its notes):

| Topic | Section |
|---|---|
| `[color]` shaping, `dither`, `color_match`, `cell_strategy`, `motion_smoothing`, `palette_mode`, fades | [`video/modes/`](docs/architecture/video-color.md#modes--displaymode-hierarchy) |
| Forced-palette remap + rolling palette (`force_palette`) | [`rolling_palette.py` + `palette.py`](docs/architecture/video-color.md#rolling_palettepy--palettepy--forced-palette-remap) |
| DAC curves, Mahoney `$D418`, per-system calibration, REU pump | [`audio.py`, `audio_handlers.py`, `dsp.py`](docs/architecture/audio.md#audiopy--audiostreamer) |
| Ultimate Audio FPGA sampler | [`sampler.py`](docs/architecture/audio.md#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm) |
| Audio-input music features (reactive visuals from live input), `[audio_features]` | [`audio_features.py`](docs/architecture/audio.md#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input) |
| Bitmap + `$D418`-DAC tempo compensation (`tempo_scale`) | [`video.py`](docs/architecture/video-color.md#videopy--webcamsource-shared-broker--avfilesource-pyav) |
| SID player PRG — relocation, per-call `$01` banking, TR vector-swap | [SID player PRG](docs/architecture/sid.md#sid-player-prg--6502-player-relocation-and-per-call-banking) |
| SID Player Autoconfig (`sid_model`, 6581/8580 matching) | [`waveform.py` + `sidemu.py` + `sid_host_emu.py`](docs/architecture/sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene) |
| SID stereo panning (`sid_panning`, per-source U64 mixer pan) | [`sid_panning.py`](docs/architecture/sid.md#sid-panning) |
| SID mixer volume (`sid_volume`) + LED mirroring of socketed SIDs | [`sid_volume.py`](docs/architecture/sid.md#sid-volume) |
| ASID decode + buffered ring player | [`asid.py` + `asid_scene.py`](docs/architecture/sid.md#asidpy--asid_scenepy--asidscene-asid-client--real-sid--oscilloscope) |
| WLED bridge — broadcast / listen / pixel sink | [`wled_sync.py`, `wled_device.py`, `wled_sink.py`](docs/architecture/wled.md#wled_syncpy--wled-audio-sync-broadcast-wled-bridge-mode-3) |
| MIDI live-tune Phases 1-5 (live params, transport, loops, resync, wizard) | [`midi_control.py`, `transport.py`, `midi_setup.py`](docs/architecture/control.md#midi_controlpy--process-wide-midi-control-surface-optional-live-performance) |
| Live DJ/VJ arc — tempo/beat grid (Phase 1) + clip-launch grid (Phase 2) + layerable effect chain (Phase 3) + grid-controller LED feedback (Phase 4) + phone/web console (Phase 5) + vision gestures / WLED tempo / look snapshot-recall (Phase 6) | [`tempo.py`, `performance.py`](docs/architecture/control.md#performancepy--clip-launch-grid-live-djvj-phase-2), [`effects.py` chain](docs/architecture/scenes.md#the-layerable-chain-live-djvj-phase-3), [LED feedback](docs/architecture/control.md#grid-controller-led-feedback-live-djvj-phase-4), [`perf_console.py`](docs/architecture/control.md#perf_consolepy--phone--web-performance-console-live-djvj-phase-5), [vision perf mode](docs/architecture/control.md#visionpy--webcam-gesture-control-optional-camera-as-input) + [looks](docs/architecture/control.md#performancepy--clip-launch-grid-live-djvj-phase-2) + [WLED tempo](docs/architecture/wled.md#wled_syncpy--wled-audio-sync-broadcast-wled-bridge-mode-3) |
| Ensemble audio slot contention | [`ensemble.py`](docs/architecture/config.md#ensemblepy--audio-slot-coordination) |
| TeensyROM link errors + launcher upload race | [`teensyrom_dma.py`](docs/architecture/hardware-io.md#teensyrom_dmapy--teensyrom-link-errors--the-launcher-upload-race) |
| Cross-ensemble span/mirror broadcasts | [`app/orchestrator.py` + `app/orchestrators/`](docs/architecture/config.md#orchestratorpy--orchestrators--cross-ensemble-scene-coordination) |
