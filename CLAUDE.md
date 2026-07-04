# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
python -m c64cast -u u64://ultimate-64-ii.lan -d 0
# or with a config file (overrides defaults; CLI flags still win):
python -m c64cast --config c64cast.toml
```

[scripts/c64cast.sh](scripts/c64cast.sh) is a convenience launcher equivalent to `python -m c64cast`: it `cd`s to the repo root and forwards all args, running through `uv run` when `uv` is on `PATH` (so the project `.venv` is always used, matching the mise + direnv + uv workflow) and falling back to a bare `python` otherwise. Use it from any directory or from outside an activated shell (cron, systemd, ssh one-liners) where direnv hasn't activated `.venv`:

```bash
scripts/c64cast.sh --config c64cast.toml
scripts/c64cast.sh --doctor --skip-probe
```

**Connection target (`-u/--url`).** A single scheme-aware string selects both the hardware backend and its endpoint (the granular `--backend`/`--tr-*`/`--dma-port` flags were removed in favor of this — `git log` for the rationale). The parser is [c64cast/connect.py](c64cast/connect.py) (`parse_connection_uri` → `ConnectionSpec` → `apply_to_config`); it decomposes into the existing config fields (`[hardware].backend`, `[ultimate64].url`/`dma_port`, `[teensyrom].transport`/`serial_port`/`host`/…), which stay the canonical store a TOML sets directly. `make_backend` is unchanged. Schemes: `u64://HOST` or `http(s)://HOST` (Ultimate — the only HTTP-speaking backend, so http is deterministically Ultimate); `tr://` (TeensyROM+ USB serial, device auto-detected on macOS), `tr:///dev/cu.usbmodemXYZ` or `tr://COM3` (explicit serial device), `tr://HOST[:PORT]` (TeensyROM+ TCP, default port 2112). Rare per-link knobs as `?query` params (`u64://host?dma_port=64`, `tr://host?tcp_port=2113`, `tr:///dev/…?baud=2000000`, `tr://?storage=usb`). `$C64CAST_URL` is the env fallback. On the CLI the `-u` target overrides the config's connection sections (single-system runs only — in ensemble mode connection comes from the per-system TOMLs).

**Quick playback (positional `MEDIA` args).** Passing media files/dirs/globs/URLs as positional arguments (mutually exclusive with `--config`) builds an **in-memory-only** `Config` (no file on disk) with one scene per argument, in order, **no loop** (override with `--loop`). Each argument is mapped to a scene type by extension — video → `video`, `.sid` → `waveform`, image → `slideshow`, `.prg`/`.crt` → `launcher` — and a directory/glob is passed straight through as the scene's `file` spec (so the scene random-picks at setup, e.g. a dir of SIDs plays a random one). A URL becomes a `video`: direct media URLs play as-is (PyAV opens http(s)), and YouTube/other sites are resolved by yt-dlp (the optional `yt` extra) to a single progressive stream. URL resolution + audio-only rejection happen **once, in `config.build_scene`** (the single resolution path shared with config-driven runs — see the `config.py` note below), so `quickcast.classify_url` just stores the URL verbatim; it parses the URL's `?t=`/`&start=`/`#t=` timestamp (`90`, `90s`, `1m30s`, `1h2m3s`) offline into `start_s` so playback begins at that offset (no flag). Audio-only files (mp3/wav over a test pattern) are recognized but deferred to a follow-up. The classifier library is [c64cast/quickcast.py](c64cast/quickcast.py) (`build_config`; the shared URL resolver is `resolve_video_url`); `cli.main` dispatches to it via `_resolve_configs` when it sees positional args, then runs the result through the normal path (`build_stack` → `_run_playlists` → `teardown_stack`), so behavior matches a config-driven run.

```bash
scripts/c64cast.sh -u u64://192.168.2.64 clip.mp4 tune.sid assets/pictures/
scripts/c64cast.sh -u tr:// clip.mp4 tune.sid          # TeensyROM+ over auto-detected USB serial
scripts/c64cast.sh -u u64://192.168.2.64 'https://youtu.be/...'   # needs the `yt` extra
```

**Audio is on by default** (`AudioCfg.enabled` defaults True); `--no-audio` mutes. On the U64, video audio defaults to the high-fidelity **Ultimate Audio FPGA PCM sampler** (`[audio].backend = "auto"` → sampler when available; see [sampler.py](c64cast/sampler.py)); `backend = "dac"` forces the lo-fi 4-bit `$D418` DAC (the only path on TeensyROM, and the path for mic/webcam audio everywhere). The U64 FPGA clocks the sampler ~1.44% **slow** vs the firmware-nominal 6.25 MHz, so at nominal the audio drifts against the (host-clock-paced) video over minutes. This is a firmware/FPGA-derivation property (identical across U64 units on the same firmware, not chip-to-chip), so `[audio].sampler_clock_hz` **ships defaulted to the measured effective clock, 6160000 Hz** (`SAMPLER_REF_CLOCK_DEFAULT`) — no per-unit calibration needed. It was measured rigorously with [scripts/diags/sampler_av_align_calib.py](scripts/diags/sampler_av_align_calib.py), which emits a SID reference tone (accurate system clock) and a sampler tone at each interval into one captured stream and fits their inter-marker drift — a differential that cancels the Cam Link capture's own rate error (unlike the older pitch-based [sampler_clock_calib.py](scripts/diags/sampler_clock_calib.py)). A confirmation run driven at 6160000 showed residual drift of only -1.3 ms per 5 s. Re-measure and update the constant after any firmware release that changes sampler timing; hardware/firmware that clocks it correctly can set 6250000. Flag groups (`-h` shows them grouped): `connection`, `quick playback`, `video input`, `audio`, `vision input`, `playlist`, `introspection`, `debug`.
Notable: `--config`, `-v` / `-vv` (info / debug logging), `--log-file PATH` (mirror logs to disk for headless runs). Terminal logging uses `rich.logging.RichHandler` (colored + timestamped) when the `logging` extra is installed; falls back to plain stdlib `StreamHandler` otherwise.

The DMA password (if the U64 has one set) is supplied via `C64CAST_DMA_PASSWORD` env var or `[ultimate64] dma_password` in the config — **no CLI flag**, so secrets don't leak into shell history or `ps` output. The env var takes precedence when both are set.

**Prerequisite:** the Ultimate DMA Service must be enabled on the U64 before `c64cast` will start: F2 → Network Settings → Ultimate DMA Service → Enabled, then save. The CLI prints an actionable error pointing at this if it can't connect.

Hard deps: `opencv-python`, `numpy`, `requests`, `py65` (the WaveformScene's host-side SID emulator). Optional extras grouped in [pyproject.toml](pyproject.toml): `mic` (sounddevice), `video` (PyAV), `preview` (pygame), `control` (FastAPI + uvicorn), `obs` (obsws-python), `midi` (mido + python-rtmidi), `logging` (rich), `wizard` (questionary, for `--init`), `all` (everything). Dev tools (ruff + coverage + mypy + pyright) live in a PEP 735 `[dependency-groups] dev` (not an extra) and are installed by default via `[tool.uv] default-groups`. The user manages the env via mise + direnv + uv.

**Setup / install is the uv project workflow — not `uv pip` and not raw `pip`:**

```bash
uv sync --all-extras    # creates/updates .venv from uv.lock: all runtime extras + dev group
```

Then either let direnv activate `.venv` (it does, via `layout uv`) or prefix one-off commands with `uv run`. **Do not use `uv pip install -e .[...]` in this repo:** mise sets `UV_PYTHON` to the bare toolchain interpreter, which `uv pip` honors over the active `.venv`, so packages land in the mise install while `python -m c64cast` runs from `.venv` (silent "PyAV unavailable" / missing-extra symptoms). `uv sync`/`uv run` target the project env (`.venv` / `UV_PROJECT_ENVIRONMENT`) and are immune. Also note `dev` is a group, so `.[all,dev]` can never resolve it via extras syntax regardless of interface. The VS Code interpreter must be `.venv/bin/python` (not the mise interpreter), or editor diagnostics diverge from the runtime.

**The `make` targets route through `uv run`** (`PY ?= uv run python`; `ruff`/`mypy`/`pyright` are prefixed too), so they always hit the synced project env regardless of whether the current shell has direnv-activated `.venv` — that's the fix for the recurring "works in CI, missing cv2 locally" trap when running from a fresh/agent shell. Local `make` targets depend on a `sync` prereq (`uv sync --all-extras`); CI sets `$CI` and the prereq is skipped there (CI manages its own pinned `uv sync --frozen` env). `make doctor` (= `c64cast --doctor --skip-probe`) is the fast offline self-check: its **ENVIRONMENT** section flags a wrong interpreter, a hard dep that won't import (the cv2 symptom), and `uv.lock` drift before they cost a debugging session.

Type-checking is two-tiered: `pyright` runs across the whole tree (and tests) in basic mode — matches Pylance's VS Code defaults so editor diagnostics align with CI. `mypy --strict` runs on the state-bearing modules (`api.py`, `audio.py`, `playlist.py`, `socket_dma.py`, `scenes.py`, `config.py`) where a type slip would actually corrupt state. The strict list lives in `[tool.mypy] files = [...]` in [pyproject.toml](pyproject.toml). Both run under `make typecheck` and in CI.

Target hardware: an [Ultimate 64](https://ultimate64.com/) on the LAN. Writes go over the **Ultimate DMA Service** (TCP port 64, persistent socket); reads, reset, run_prg, and probe go over REST. SID playback DMAs the SID payload + a tiny 6502 player into C64 RAM and kicks a `SYS` BASIC stub through `run_prg` — the firmware's `runners:sidplay` endpoint is deliberately avoided because it hijacks HDMI with its own player UI. See [api.run_sid_player](c64cast/api.py) and the "SID playback uses a C64-side player PRG" section of [docs/caveats.md](docs/caveats.md) for the design + limitations.

## Configuration

TOML file (`--config PATH` wins; else `./c64cast.toml` if present; else built-in defaults). Precedence: defaults → config → CLI. See [c64cast.example.toml](config/c64cast.example.toml) for an annotated reference.

The config defines the **playlist** (which scenes run, in what order, for how long) plus all CLI-overridable options. Every CLI flag has `default=None`; `config.merge_cli()` only overwrites a config field when the CLI value is non-None.

**Config metadata is the single source of truth.** Every dataclass field in [config.py](c64cast/config.py) carries `field(metadata={"help", "choices", "applies_to"})`, and every overlay class carries `HELP` + `PARAM_HELP` (plus the existing `REQUIRES_PETSCII`/`REQUIRES_AUDIO`/`COMPATIBLE_MODES` restriction attrs). [introspect.py](c64cast/introspect.py) reads all of that into one model and renders the discovery commands; [schema.py](c64cast/schema.py) renders the same model into a JSON Schema; [config_serialize.py](c64cast/config_serialize.py) renders a `Config` back to annotated TOML (inverse of `load`; `load(dumps(cfg)) == cfg`); [wizard.py](c64cast/wizard.py) drives `--init`'s interactive prompts from the same model (choices/defaults from metadata, overlays filtered via `--compat`, asset-aware file pickers). So `--describe`, `--list-*`, `--compat`, `c64cast.schema.json`, `--init`, and serialized configs can't drift from the code. Discovery commands (config-free, no hardware): `--list-scenes` / `--list-overlays` / `--list-modes`, `--describe NAME` (optionally prefixed `scene:`/`overlay:`/`section:`/`mode:`), `--compat` (overlay × display-mode matrix — text overlays now ✓ on bitmap modes via the TextSurface fold; the remaining bitmap gaps are `spectrum_petscii` and `big_text`), and `--print-schema`. The committed `c64cast.schema.json` (regenerate with `make schema`; CI fails on drift) plus a `#:schema` directive on the first line of a config gives Taplo/"Even Better TOML" editors live autocomplete. `c64cast --init [PATH]` (needs the `wizard` extra) builds either a single-scene config or a multi-scene playlist (add/remove/reorder scenes, then `[playlist]` loop + video interleaving + `[interstitial]` style) interactively and writes it via the serializer, then offers to launch it. The flow is a thin questionary shell over pure helpers (`make_scene`/`build_config`/`build_multi_config`/`validate_all`); multi-scene needed no serializer/schema/loader change because the round-trip already covered N `[[scenes]]`. `--doctor --skip-probe` is the offline, collect-all config check. Unknown keys get `difflib` "did you mean" suggestions (in `_apply_section` and `build_overlay`). When adding a config field or overlay, fill in its `help`/`PARAM_HELP`, run `make schema`, and the drift tests in [tests/test_example_toml_drift.py](tests/test_example_toml_drift.py) + [tests/test_introspect.py](tests/test_introspect.py) keep everything honest.

## Architecture

```
c64cast/
├── palette.py        C64 palette + fast vectorized quantizer
├── socket_dma.py     SocketDMAClient: persistent TCP socket to U64 port 64
│                     for memory writes (opcode 0xFF06)
├── api.py            Ultimate64API: routes writes through socket_dma +
│                     delta uploads; REST for read_memory, reset, run_prg
│                     (BASIC clear loop + SID-player SYS stub), probe
├── audio.py          AudioStreamer: NMI + SID DAC + ring buffer + sample tap
├── dac_curves.py     Mahoney 8-bit $D418 companding tables (baked emulated-
│                     UltiSID amplitude→$D418 sidtable) + resolve_dac_curve;
│                     drives [audio].dac_curve (auto | linear | mahoney_ultisid
│                     | calibrated)
├── dac_calibration.py  Per-system DAC calibration: system key (host/serial),
│                     load/save calibrated tables under calibration/ (gitignored),
│                     the "auto"/"calibrated" resolver + run_calibration
│                     (--calibrate-dac: Cam Link capture → measured sidtable)
├── sampler.py        UltimateAudioSampler: U64 "Ultimate Audio" FPGA PCM
│                     sampler ($DF20) — hi-fi video audio from a streaming
│                     REU ring, zero SID/$D418/NMI/CPU (off the C64 bus)
├── video.py          WebcamSource (shared cv2 camera broker) + AVFileSource (PyAV)
├── vision.py         VisionController: webcam hand-gestures → pause/skip/cycle
│                     (MediaPipe HandLandmarker; sibling to keyboard.py)
├── modes.py          VIC-II renderers: PETSCII, MCM, Hires, MultiHires
│                     (char + bitmap modes share a compose()/push() split so
│                     overlays fold into the frame before upload)
├── bitmap_text.py    Shared hires glyph rasterizer (load_glyphs / cell blit);
│                     used by voice_scope + the on-C64 menu
├── text_surface.py   Backend-neutral text grid overlays paint into
│                     (Char/Hires/MHires impls); folds glyphs into char screen
│                     codes or bitmap so text overlays render on any mode
├── petscii_styles.py PETSCII glyph + color style packs (default, halftone,
│                     random_glyph, letter_rain, neon, inverse_pop, hatch,
│                     color_only) cycled by the SHIFT key
├── scenes.py         Scene base + Webcam + Blank + Slideshow + Video
│                     + Launcher (native .prg/.crt handoff) + SourceScene
│                     (composable FrameSource × AudioSource × display × effect)
├── frame_source.py   FrameSource protocol + BaseFrameSource (read(t, mod))
├── generators.py     GenerativeSource registry (plasma, tunnel, fire); pure-
│                     numpy, deterministic-in-t; reactive when fed a MusicModulation
├── effects.py        FrameEffect registry (trails, pulse, rgb_shift) — pre-
│                     quantization frame xform; reactive when fed a MusicModulation
├── audio_source.py   AudioSource registry (Null/Mic/SidFile) for SourceScene
├── modulation.py     MusicModulation: frozen music-feature struct (level/onset/
│                     beat_phase/bpm/per-voice freq+gate) driving reactive visuals
├── music_features.py SidFeatureStream: persistent host-side SidHostEmu + poll
│                     thread → MusicModulation (no U64 traffic); the music driver
├── voice_scope.py    VoiceScopeRenderer mixin: the shared 3-voice hires
│                     oscilloscope renderer (layout, VIC hires bring-up,
│                     glyph text rows, per-voice render paths + knobs) used
│                     by both WaveformScene and MidiScene
├── waveform.py       WaveformScene: 3-voice SID oscilloscope (full-screen),
│                     SID-file playback; inherits VoiceScopeRenderer
├── midi_scene.py     MidiScene: live MIDI input → SID synth + 3-voice
│                     oscilloscope (inherits VoiceScopeRenderer; bitmap-only)
├── sidemu.py         Minimal SID waveform synthesizer (per-voice +
│                     ADSR; no filter/mixing) — drives the oscilloscope
│                     trace from live $D400-$D418 snapshots
├── sid_host_emu.py   py65 host-side SID register tracker — runs the
│                     SID file in parallel on a pure-Python 6502 to
│                     recover live $D4xx state the U64 won't read back
├── songlengths.py    HVSC SongLengths.md5 parser + lookup
├── framebuffer.py    Software VIC mirror used by preview + recording
├── preview.py        Pygame preview window + cv2.VideoWriter recorder
├── control_plane.py  FastAPI HTTP control plane (pause/resume/skip/reload)
├── c64.py            Centralized C64 hardware constants (VIC/SID/CIA/KERNAL)
├── interstitial.py   InterstitialScene: centered "UP NEXT" text + parallax bg
├── backgrounds.py    Parallax background styles for the interstitial
├── config.py         TOML loader, CLI merge, scene+overlay factory;
│                     field metadata (help/choices/applies_to) is the
│                     single source of truth for the introspection layer
├── introspect.py     Unified model over config metadata + overlay/mode
│                     attrs → renders --describe / --list-* / --compat
├── schema.py         build_schema(): JSON Schema from the introspect model
│                     (--print-schema; committed as c64cast.schema.json)
├── config_serialize.py  dumps(): Config → annotated TOML (inverse of
│                     config.load; reuses introspect help; load(dumps)==cfg)
├── wizard.py         --init interactive config builder (questionary extra);
│                     prompts driven by the introspect model + compat filter
├── keyboard.py       Polls $028D for Commodore key → pause/resume events
├── playlist.py       Scene state machine + overlay orchestration + pause loop
├── overlays/
│   ├── __init__.py     Overlay base, registry, slot constants, screen-code helpers
│   ├── corner_text.py  Base class for corner-positioned text overlays
│   ├── scrolling_text.py   Per-row scrolling messages
│   ├── marquee.py          Single-line slow ticker
│   ├── rss.py              RSS/Atom ticker (background fetch, marquee render)
│   ├── spectrum_petscii.py Audio FFT → vertical color bars in screen RAM
│   ├── clock.py            Time/date in a corner
│   ├── weather.py          Temp + conditions (open-meteo or wttr.in)
│   ├── callsign.py         Static text in a corner
│   ├── countdown.py        Time-until-event in a corner
│   ├── network.py          Local IP / hostname / U64 ping in a corner
│   ├── logo.py             Multi-line PETSCII art block from a file
│   ├── big_text.py         Demo-scene 8×-scaled scrolling glyphs (blank/mcm)
│   └── obs_status.py       OBS WebSocket scene + dropped-frame counter
├── connect.py        Scheme-aware -u/--url target parser
│                     (parse_connection_uri → ConnectionSpec →
│                     apply_to_config); decomposes u64://, http(s)://,
│                     tr:// into the [hardware]/[ultimate64]/[teensyrom]
│                     config fields make_backend reads
├── quickcast.py      Quick-playback classifier library (positional MEDIA
│                     args → in-memory Config); build_config, called by
│                     cli._resolve_configs (no standalone entry point)
├── cli.py            argparse + main(); _resolve_configs picks the
│                     config-driven vs quick-playback front door
└── __main__.py       `python -m c64cast` entry
```

Per-module internals — the design rationale, hardware constraints, and edge-case history for each file in the tree above — live in [docs/architecture.md](docs/architecture.md). **Read the relevant section there before modifying a module**; it carries the *why* (and the dead ends) that the code alone doesn't. Keep the two in sync: a behavior change to a module updates its `docs/architecture.md` section in the same change set (see the "Docs reflect functionality changes" working rule).

## Repository layout outside `c64cast/`

```
assets/              Non-code static content (ROMs, SIDs, logos, videos,
                     pictures).
                     Only the per-subdir READMEs are tracked; user files
                     are .gitignored. See [assets/README.md](assets/README.md).
config/examples/     Per-feature single-scene demo configs (one TOML per scene
                     type, one per overlay) + README. Run any of them with
                     `--config config/examples/<file>`; the playlist auto-enters
                     single-scene mode and loops the demo forever.
docs/                Markdown user/developer documentation.
                     [docs/usage.md](docs/usage.md), [docs/caveats.md](docs/caveats.md),
                     [docs/troubleshooting.md](docs/troubleshooting.md),
                     [docs/extending.md](docs/extending.md).
scripts/             Dev helpers ([scripts/coverage.sh](scripts/coverage.sh),
                     [scripts/pre-commit.sh](scripts/pre-commit.sh)) +
                     [scripts/c64cast.sh](scripts/c64cast.sh), the uv-aware
                     launcher (forwards args to `python -m c64cast`).
tests/               unittest suite. `python -m unittest discover tests`.
.github/workflows/   CI (lint + tests on push/PR).
.pre-commit-config.yaml  Git pre-commit hooks (ruff + tests).
pyproject.toml       PEP-621 package metadata + ruff + coverage config. Optional
                     deps grouped: `mic`, `video`, `preview`, `control`,
                     `logging`, `obs`, `midi`, `all`, `dev`.
config/             Per-feature demo configs + c64cast.example.toml (annotated
                    kitchen-sink reference).
```

## Visual verification on real hardware

The U64's HTTP API lets you confirm *what was written* to screen / color RAM / VIC
registers (`/v1/machine:readmem`), but it can't tell you *what the VIC actually
rendered* — character-ROM mismatches, MCM bit-3 surprises, and mode-switch
artifacts only show up on the screen itself. When you need that ground truth and
a USB video capture device is wired to the U64's HDMI output (e.g. Elgato Cam
Link, AverMedia, any UVC capture stick), `cv2.VideoCapture(index)` will return
a 1080p BGR frame you can `imwrite()` and Read.

Ask the user before assuming a capture is available — they vary by machine. If
one is present, use it for verification of any visual change (overlays, display
modes, scene transitions) instead of guessing from RAM dumps alone. Local-only
machine specifics (which OpenCV index is the capture device on this host, what
else is on the LAN) belong in `.claude/settings.local.json` or auto-memory, not
in this file.

## Quirks worth knowing

- `C64_PALETTE_BGR` is OpenCV BGR order, not RGB.
- Color shaping is the global `[color]` section (`ColorCfg`), applied before quantization in MCM/MHires/PETSCII (not Hires) and **orthogonal to `palette_mode`** (which only allocates per-cell slots). Three stages, all in `palette.py`: `channel_boost` (per-channel BGR gain, default `[1.3, 1.2, 1.0]` — blue/green lift, red neutral; the old `0.9` red-cut measurably raised Lab error and starved warm colors so it was dropped) and `hue_corrections` (hue-band snap+boost, default ships one `purple_rescue` band closing the C64's single true palette gap — dark violets → purple) are **static** (same for every source); `parse_channel_boost`/`parse_hue_corrections` validate at load/doctor time, `modes._resolve_color_shaping` resolves the effective values. `auto_fit` (default **true**) is the **per-source adaptive** stage for video + slideshow scenes only: the scene pre-scans the source (`video.prescan_color_fit` / `palette.ColorFitAccumulator`) → one `ColorFit` (luma contrast/levels stretch + gentle saturation lift; faithful/hue-preserving; do-no-harm guards: percentile black/white points, min-span gain cap, sat floored at 1.0, identity → no-op) → `display_mode.set_color_fit(fit)`; the mode applies it via `palette.apply_color_fit` as the FIRST step of `compose`/`render`, after the downscale. Webcam never calls `set_color_fit` (no pre-scan, would flicker) so `_color_fit` stays None = no-op. `auto_fit_strength` (0..1) lerps toward identity (0 = off). The earlier `palette_mode = "c64"` (which fused slot-picking + corrections) was removed in favor of this split.
- Audio uses 4-bit samples (0-15) written to the SID volume nibble; quality is intentionally low-fi. `[audio].dac_curve` opts into the **Mahoney 8-bit `$D418`** technique (park the SID voices as DC sources + write the full `$D418` byte per sample → ~6-7 effective bits) via a 256-entry amplitude→`$D418` companding table; `"linear"` is the classic 4-bit path, bit-identical. Default is `"auto"` (`config.resolve`/`dac_calibration.resolve_dac_curve_for_backend`): a **per-system calibrated** table if one exists, else the baked `mahoney_ultisid` on the Ultimate (its emulated SID is deterministic across units), else `"linear"` (a physical/unknown SID with no calibration — the baked emulated table wouldn't match it). It shapes the `$D418` DAC only (TeensyROM+ audio + mic/webcam everywhere) — the U64 video default uses the off-bus sampler. Physical 6581/8580 chips (and SID replacements) vary too much chip-to-chip for a baked table, so **`--calibrate-dac`** measures the connected SID's transfer curve via a Cam Link capture on the SID output and writes a table keyed by connection target (host address / serial device) under `calibration/dac/` (gitignored); `"auto"` then uses it, `"calibrated"` requires it. Non-linear curves auto-install the Mahoney SID env (mutually exclusive with `digi_boost`; `"auto"` yields to an explicit `digi_boost` by staying linear). See the `dac_curve` note in [docs/architecture.md](docs/architecture.md), [c64cast/dac_curves.py](c64cast/dac_curves.py) + [c64cast/dac_calibration.py](c64cast/dac_calibration.py). `validate_dac_curve_cfg` + `--doctor` report the resolved curve and flag `"calibrated"` with no table.
- **Bitmap + `$D418`-DAC tempo compensation** (`[audio].dac_bitmap_tempo_hires` / `dac_bitmap_tempo_mhires`, default **0.88 ON**). On the host-DMA 4-bit DAC path (`[audio].backend = "dac"`) over a **bitmap** display mode (hires/hires_edges/mhires), video+audio play ~12% **slow at correct pitch**: the audio worker shares the single socket-DMA link with heavy REU bank-swap bitmap writes, so the host-DMA servo reads the ring pointer biased under that load and throttles the worker ~12% (`clock/wall` ≈ 0.88 mhires vs ≈1.0 petscii). The `$D418` **output** rate stays ≈ `sample_rate` (a pure 1000 Hz tone reads ~993 Hz → pitch correct), so it's a **pitch-preserving time stretch** (ring under-fills, NMI re-reads samples). No host-side servo tuning fixes both speed and smoothness. The fix **pre-compresses the content** by the inverse factor `1/s`: `config.build_scene` computes `tempo_scale = s` (gated to `backend == "dac"` + `isinstance(mode, BitmapDisplayMode)` + not `use_reu_pump`; hires vs mhires by mode) and threads it to `VideoScene` → `AVFileSource`, which time-compresses the audio pitch-preserving via an `atempo` filter graph (`1/s`, fed the existing s16/mono resampler output, flushed at EOF) and scales each rebased video PTS by `s`. The existing drain-clock A/V sync (which reads ~`s`) then lands both content streams at real time, in sync. **Off (`tempo_scale` 1.0)** for the off-bus Ultimate Audio sampler (the U64 video default — unaffected), the REU pump, char modes, and muted scenes. The defaults (mhires `0.88`, hires `0.89`) are the measured **U64-II NTSC** fractions (hires drains slightly faster = lighter load); other platforms (U64+PAL, U2P, TR+ PAL/NTSC) differ — measure per platform with [scripts/diags/mhires_tempo_clock_ab.py](scripts/diags/mhires_tempo_clock_ab.py) (reads the app's `clock/wall` A/V-lag telemetry) and set the field. `validate_dac_bitmap_tempo_cfg` + `--doctor` bound it to 0.5..1.0 (atempo's single-stage floor). This is **orthogonal** to `pitch_mult_*` (which shorten the NMI rate to correct pitch — a tempo-blind axis). See the `video.py`/`config.py` tempo-compensation notes in [docs/architecture.md](docs/architecture.md) + [docs/caveats.md](docs/caveats.md).
- `AudioStreamer` shares the render path's `Ultimate64API` instance. The U64 DMA service is single-connection only: a second concurrent socket TCP-accepts but its IDENTIFY never gets a reply, and the first socket blocks new ones for a few seconds after close. The shared `SocketDMAClient` is thread-safe (per-command mutex around sendall) and the combined write rate (audio ~8/sec + render ~30-60/sec) sits well under the ~200/sec DMA ceiling.
- Address strings passed to `write_memory*` work in either case (`"d018"` and `"D018"` are both fine).
- The dirty cache is keyed by `region_id` (small ints) not address — so a mode switch from PETSCII to MCM (both writing $0400) gets a clean diff baseline via `api.invalidate_cache()`. `InterstitialScene` reuses `REG_SCREEN`/`REG_COLOR` for the same reason.
- `backgrounds.py` constants are C64 *screen codes* (what goes to $0400), not PETSCII codes — the encodings diverge above 0x40 (e.g. `@` is PETSCII 0x40 but screen code 0x00).
- Memory writes go over the **Ultimate DMA Service** (TCP port 64, persistent socket, ~5 ms / ~200 writes/sec). See [docs/caveats.md](docs/caveats.md) → "Socket DMA replaced HTTP for writes" for the migration result and "U64 HTTP throughput wall" for the historical REST measurements that motivated it. Reducing write *count* (coalesce via `write_regs`, dirty-skip via `write_region`) is still the right move under DMA — it's cheaper than tightening the per-write floor.
- **SID playback** uses a hand-encoded 6502 player (73 bytes) plus a SHIFT-driven re-INIT stub (35 bytes), both relocated per-tune by `_choose_player_layout` in [api.py](c64cast/api.py). Default layout is player at `$C300` / stub at `$C400` (the historical fixed location, used when no conflict); SIDs whose payload would overlap get a contiguous bundle (`player_base + 80 = stub_base`) placed in the largest footprint-clean RAM hole (or just past / below the payload as fallback). The BASIC SYS stub's decimal argument is built dynamically by `_build_basic_sys_stub` to match the chosen `player_base`. The player banks `$01` **per call** — `_init_bank_for`/`_play_bank_for` patch the value to `$36` (BASIC ROM banked out, KERNAL + I/O kept) around the `JSR init`/`JSR play` whose entry lives under BASIC ROM (`$A000-$BFFF`, e.g. Hyperion 2 at `$AE2A`) so the call reaches the tune's RAM instead of executing ROM (which lands on the ROM SYNTAX-error stub → `?SYNTAX ERROR IN 10`), then restore `$37` (the resting default; tunes like Comic Bakery that read BASIC ROM as data keep it mapped between calls). Per-call banking replaced an earlier permanent-`$36` scheme that crashed tunes like Election which assume the `$37` resting environment between PLAY calls. The real 6510 calls INIT once and PLAY at IRQ time; the IRQ handler chains to kernal `$EA31` so keyboard scan (`$028D`) + cursor-blink suppression survive. After installing the IRQ vector the player spins forever (`JMP *`) rather than RTSing back to BASIC — INIT routinely clobbers BASIC zero-page state, so a return would print a syntax error on the next interpreter step. The default `$C300` location was chosen because [audio.py](c64cast/audio.py) owns `$C000-$C2FF` (NMI DAC + REU pump); the relocation picker refuses any layout that would overlap that region. PSID-only — RSIDs, SIDs whose `load_addr` < `$0820`, SIDs whose `play_addr` is zero, and SIDs under KERNAL ROM (`$E000-$FFFF`) are refused. PAL/NTSC speed flag is ignored (v1 limitation; kernal-default CIA #1 Timer A rate is used). **Backend-agnostic orchestration** (parse/layout/build/divider-tune/subtune-reinit) lives in `_SidPlayerBackend`; only the *kick* differs (abstract `_launch_sid_player`). The Ultimate POSTs the `SYS` stub to `run_prg`. The **TeensyROM** uses a **pure-DMA `$0314` vector-swap** instead (the same primitive as `cue_song_reinit`): over the running IRQ-enabled clear-loop, DMA the blobs then swap `$0314` → the re-INIT stub, which the next kernal IRQ runs to `JSR init` + install the PLAY handler — no LaunchFile/reset/boot, so no async-boot race. `run_sid_player(defer_audio=True)` + `begin_sid_audio()` split load from start so WaveformScene paints the scope **before** the first note (TR), or asserts the bitmap after the player as before (U64, where `run_prg` resets VIC); the scene anchors its host-emu clock to `sid_audio_start_time()`. The TR path is gated on `supports_read` (cycle-clean fw v0.7.2.5+ — the spin-stub idle on older fw masks IRQs so the swap can't fire); older fw raises `BackendCapabilityError`. The TR has no REUWRITE, so `cli._coerce_reu_for_backend` forces `use_reu_pump` / explicit `use_reu_staged=true` off on a no-REU backend (host-DMA NMI DAC audio + host-DMA video; `--doctor` reports it). See [docs/caveats.md](docs/caveats.md) "SID playback uses a C64-side player PRG" (incl. the TeensyROM vector-swap subsection) for the full rationale.
- **Ensemble audio coordination.** In ensemble mode (`[ensemble]` in the master TOML) at most one system's playlist may hold the ensemble audio slot — tracked as `Ensemble.audio_holder` + `audio_lock` in [ensemble.py](c64cast/ensemble.py). Scenes whose class sets `WANTS_AUDIO_LOCK = True` (`VideoScene`, `WaveformScene`, `MidiScene`, `LauncherScene`) try to claim the slot in `Playlist._resolve_next_index`; on contention they get **skipped** to the next non-gated scene in the playlist, with `_safe_teardown` releasing the slot when the holder's scene ends. (`LauncherScene` overrides `competes_for_audio_lock()` so `bypass_audio_lock = true` opts out — several systems can then run interactive launchers at once, each player hearing their own SID.) Live scenes (`WebcamScene`, `BlankScene`) are built with `audio = None` in ensemble mode regardless of TOML — they never compete for the SID. Single-system runs keep `ensemble = None` and bypass the gate entirely. An ensemble system whose playlist is entirely audio-bearing scenes will idle when the slot is held elsewhere; the loader emits a WARNING on this configuration.

- **TR launch/upload errors surface the firmware's reason.** `teensyrom_dma._expect_ack` captures the trailing text the TR emits after a NAK and puts it in the raised error — `TRBusyError` (subclass of `TRError`) on a `"Busy!"` reply (program running / menu handler inactive), and the literal text (`"Not enough room"`, `"File already exists."`, …) appended otherwise — instead of a bare `FailToken (0x9B7F)`. **Known pre-existing issue (under investigation, NOT yet fixed):** the TR launcher (`launch_program` = PostFile + LaunchFile) can produce an intermittently-corrupt upload — the keyboard poller's `ReadC64Mem` (and likely the launcher's own input poll) shares the TR link with the launcher's reset+PostFile, and a poll read landing in the post-reset chatter can desync the stream so the next PostFile drops a byte (the `.prg` loads one byte short → `?SYNTAX ERROR`). It's a race (intermittent; reliable single-threaded + on the Ultimate). Candidate fixes (desync-safe `read_segment`, poller suspend across reset+upload, pre-upload drain) need a soak harness to verify. See [docs/caveats.md](docs/caveats.md) + [[tr_launcher_poller_upload_race]].
