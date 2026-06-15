# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
python -m c64cast --url http://ultimate-64-ii.lan -A -d 0
# or with a config file (overrides defaults; CLI flags still win):
python -m c64cast --config c64cast.toml
```

[scripts/c64cast.sh](scripts/c64cast.sh) is a convenience launcher equivalent to `python -m c64cast`: it `cd`s to the repo root and forwards all args, running through `uv run` when `uv` is on `PATH` (so the project `.venv` is always used, matching the mise + direnv + uv workflow) and falling back to a bare `python` otherwise. Use it from any directory or from outside an activated shell (cron, systemd, ssh one-liners) where direnv hasn't activated `.venv`:

```bash
scripts/c64cast.sh --config c64cast.toml
scripts/c64cast.sh --doctor --skip-probe
```

[scripts/cast.sh](scripts/cast.sh) (logic in [c64cast/quickcast.py](c64cast/quickcast.py), `python -m c64cast.quickcast`) is the **quick-playback shortcut** for testing without hand-writing a throwaway TOML: it builds an **in-memory-only** `Config` (no file on disk) with one scene per argument, in order, **no loop** (override with `--loop`). Each argument is mapped to a scene type by extension — video → `video`, `.sid` → `waveform`, image → `slideshow`, `.prg`/`.crt` → `launcher` — and a directory/glob is passed straight through as the scene's `file` spec (so the scene random-picks at setup, e.g. a dir of SIDs plays a random one). A URL becomes a `video`: direct media URLs play as-is (PyAV opens http(s)), and YouTube/other sites are resolved by yt-dlp (the optional `yt` extra) to a single progressive stream. Audio is **on by default** (`--no-audio` to mute); audio-only files (mp3/wav over a test pattern) are recognized but deferred to a follow-up. It reuses the normal run path (`build_stack` → `_run_playlists` → `teardown_stack`), so behavior matches a config-driven run.

```bash
scripts/cast.sh -u http://192.168.2.64 clip.mp4 tune.sid assets/pictures/
scripts/cast.sh 'https://youtu.be/...'   # needs the `yt` extra
```

Flag groups (`-h` shows them grouped): `hardware`, `ultimate 64`, `video input`, `audio`, `playlist`, `introspection`, `debug`.
Notable additions: `--config`, `--dma-port`, `-v` / `-vv` (info / debug logging), `--log-file PATH` (mirror logs to disk for headless runs). Terminal logging uses `rich.logging.RichHandler` (colored + timestamped) when the `logging` extra is installed; falls back to plain stdlib `StreamHandler` otherwise.

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

**Config metadata is the single source of truth.** Every dataclass field in [config.py](c64cast/config.py) carries `field(metadata={"help", "choices", "applies_to"})`, and every overlay class carries `HELP` + `PARAM_HELP` (plus the existing `REQUIRES_PETSCII`/`REQUIRES_AUDIO`/`COMPATIBLE_MODES` restriction attrs). [introspect.py](c64cast/introspect.py) reads all of that into one model and renders the discovery commands; [schema.py](c64cast/schema.py) renders the same model into a JSON Schema; [config_serialize.py](c64cast/config_serialize.py) renders a `Config` back to annotated TOML (inverse of `load`; `load(dumps(cfg)) == cfg`); [wizard.py](c64cast/wizard.py) drives `--init`'s interactive prompts from the same model (choices/defaults from metadata, overlays filtered via `--compat`, asset-aware file pickers). So `--describe`, `--list-*`, `--compat`, `c64cast.schema.json`, `--init`, and serialized configs can't drift from the code. Discovery commands (config-free, no hardware): `--list-scenes` / `--list-overlays` / `--list-modes`, `--describe NAME` (optionally prefixed `scene:`/`overlay:`/`section:`/`mode:`), `--compat` (overlay × display-mode matrix — also the worklist for widening overlay parity into bitmap modes), and `--print-schema`. The committed `c64cast.schema.json` (regenerate with `make schema`; CI fails on drift) plus a `#:schema` directive on the first line of a config gives Taplo/"Even Better TOML" editors live autocomplete. `c64cast --init [PATH]` (needs the `wizard` extra) builds either a single-scene config or a multi-scene playlist (add/remove/reorder scenes, then `[playlist]` loop + video interleaving + `[interstitial]` style) interactively and writes it via the serializer, then offers to launch it. The flow is a thin questionary shell over pure helpers (`make_scene`/`build_config`/`build_multi_config`/`validate_all`); multi-scene needed no serializer/schema/loader change because the round-trip already covered N `[[scenes]]`. `--doctor --skip-probe` is the offline, collect-all config check. Unknown keys get `difflib` "did you mean" suggestions (in `_apply_section` and `build_overlay`). When adding a config field or overlay, fill in its `help`/`PARAM_HELP`, run `make schema`, and the drift tests in [tests/test_example_toml_drift.py](tests/test_example_toml_drift.py) + [tests/test_introspect.py](tests/test_introspect.py) keep everything honest.

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
├── video.py          WebcamSource (shared cv2 camera broker) + AVFileSource (PyAV)
├── vision.py         VisionController: webcam hand-gestures → pause/skip/cycle
│                     (MediaPipe HandLandmarker; sibling to keyboard.py)
├── modes.py          VIC-II renderers: PETSCII, MCM, Hires, MultiHires
├── petscii_styles.py PETSCII glyph + color style packs (default, halftone,
│                     random_glyph, letter_rain, neon, inverse_pop, hatch,
│                     color_only) cycled by the SHIFT key
├── scenes.py         Scene base + Webcam + Blank + Slideshow + Video
│                     + Launcher (native .prg/.crt handoff)
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
├── cli.py            argparse + main()
└── __main__.py       `python -m c64cast` entry
```

### `api.py` — Ultimate64API + `socket_dma.py` — SocketDMAClient

Split-transport client:

* **Writes** go through [socket_dma.py](c64cast/socket_dma.py) — a persistent TCP socket to the U64's Ultimate DMA Service (port 64) sending opcode `0xFF06 DMAWRITE`. Per-connection FIFO ordering at the server, ~5 ms per write, ~200 writes/sec sustained. The constructor calls `connect()` immediately so failure (service disabled, auth rejected, etc.) surfaces as `SocketDMAError` at startup, before the playlist runs. `api.flush()` is a trailing IDENTIFY round-trip — when it returns, the server has drained every prior write.
* **Reads, reset, runners, probe** stay on REST via `requests`. These are low-rate and one-shot; the HTTP throughput wall (~50-70/sec) doesn't apply.

Two coalescing/caching layers on top:

1. **`write_regs(base_addr, *values)`** — packs N contiguous register writes into one DMA write (e.g. `D020-D023` border + 3 backgrounds in one packet).
2. **`write_region(address, data, region_id=…)`** — caches the last-pushed bytes per region; only sends the changed sub-range. Above `full_threshold` (0.6) it falls back to a full upload. Display modes call `api.invalidate_cache()` in `setup()` because a mode switch can repurpose the same address.

Latency tracking lives on the DMA client (`socket_dma.latency_summary()` / `format_latency()`); `api.format_dma_latency()` is the playlist-facing shim. The heartbeat line and the `--profile` summary both surface this.

### `audio.py` — AudioStreamer

NMI-driven 4-bit SID DAC (writes to `$D418` volume nibble). This is the only approach that works on a real C64 with active video output. PWM via `$D402` was tested and rejected: at 8 kHz NMI rate, the PWM carrier sits 9 dB above the audio signal (spectral capture confirmed); at 16 kHz, VIC-II badlines (40 stolen cycles in a 63-cycle period) cause the NMI handler to overrun and queue back-to-back, stretching audio samples and shifting a 440 Hz test tone to 421 Hz. The default sample rate is **10.5 kHz** (raised from 8 kHz after a 2026-06-15 HW A/B: it lifts the Nyquist to ~5.25 kHz so fricatives/sibilants survive — clearer speech — and the NMI consumer still tracks ~98% with no handler overrun on either standard). `c64.nmi_rate_safety` is the single source of truth for the safe ceiling (handler worst case 81 cycles + entry latency); `config.validate_nmi_sample_rate` rejects overrunning rates at load and `--doctor` reports them. PAL's slower clock makes it the tighter ceiling (NTSC tolerates ~11025, keep PAL ≤ ~10500). Two input modes:

* `start_mic(device, sens, gate)` — sounddevice capture, mic_callback pushes into the queue
* `start_for_external_source()` — no input thread; caller (PyAV demuxer) pushes via `push_samples(int16)`

The worker thread drains the queue, paced to chunk_size / sample_rate (= NMI consumption rate) so it can't lap NMI's read pointer and overwrite real audio with neutral pad. Each iteration collects up to chunk_size bytes by the pace deadline (no grace period — the deadline IS the collect deadline), pads with `NEUTRAL_SAMPLE=7` only on real underrun (deadline expires with nothing in the queue), then uploads to the ring buffer at `$4000-$5FFF`. After `PREBUFFER_CHUNKS * chunk_size` bytes of prebuffer it starts the CIA #2 timer (`$DD04/05`); the BASIC clear-loop is kicked separately at session startup, not per-scene. Pacing is **strict absolute** (`next_write_time + chunk_period`) and never snaps forward to wall-clock on overrun — the earlier snap-forward variant let DMA round-trip + Python wakeup overhead shrink the effective sample rate below NMI consumption, padding the gap with NEUTRAL on every chunk and producing audible chunk-rate AM sidebands (~−5 dB at the carrier) plus ~16 dB of overall level loss on video audio. The 8 KB ring (~1 s @ 8 kHz) absorbs occasional pace overshoots.

The ring lives at `$4000-$5FFF` (VIC bank 1) rather than `$8000-$9FFF` so it stays out of VIC banks 0 and 2 — the two banks with kernal char-ROM mapped (at `$1000` and `$9000`) that the REU-staged char display modes use as the off-screen swap target. The 6510 NMI handler sees `$4000` as normal main RAM regardless of VIC bank. Three patch offsets in the NMI routine bytes (read addr HI, end-compare HI, wrap-reset HI) come from `RING_BUFFER_HI` / `RING_BUFFER_END_HI`, so a future relocation is a one-line change. Bitmap modes that would want VIC bank 1 themselves need a follow-up relocation; PETSCII never selects bank 1.

**`[audio].use_reu_pump = true` on a webcam scene** (or any scene that calls `start_mic`) opts the mic path into REU-staged streaming. The mic callback REUWRITEs encoded samples into a 64 KB REU ring at offset `$100000` (bus-clean, no SID perturbation), and a C64-side IRQ handler at `$C100` drains the ring into the audio ring at the matched CIA #1 rate. The handler reloads the REU source registers (`$DF04`/`$DF05`/`$DF06`) from a 3-byte tracker in main RAM at `$C200` on every IRQ rather than trusting `$DF06` read-back — the U64's REU returns garbage in the upper bits of the src_hi register, which would have caused the handler's wrap check (`CMP #reu_end_hi`) to always succeed, resetting the src to the start of the prefilled NEUTRAL block and producing pure silence. Two pinned BCC displacements (+15 src wrap, +10 dst wrap) land on instruction boundaries; wrong values stomp the tracker or REU regs. Bootstrap latency is `REU_MIC_BOOTSTRAP_BYTES / sample_rate` (~200 ms at 8 kHz). The same `use_reu_pump` flag covers both video (`start_for_reu_staged`) and mic (`start_mic`) paths; the AudioStreamer picks the matching bring-up based on which start method is called.

**Doctor-mode REU enable check** (`c64cast --doctor`): when the config opts into a REU-staged path as a **hard** requirement (`[audio].use_reu_pump`, or `[video].use_reu_staged = true` — the `"auto"` default is excluded, see below), the connectivity probe also GETs `/v1/configs/C64 and Cartridge Settings` from the U64 to confirm `"RAM Expansion Unit": "Enabled"`. If disabled, the diagnostic is an `error` because the staged paths silently produce silent audio / unchanged video (no error from the host side — REUWRITE succeeds, REU→main DMA reads zeroes). The hint points at the F2 menu path to enable + offers turning the TOML opt-in off as the alternative. (`doctor.reu_is_enabled(api)` is the same REST query factored out for cli.py's `"auto"` resolution.)

Sample encoding can optionally apply **TPDF dither** (±1 LSB triangular, controlled by `[audio].dither`, **default false** after real-6581 A/B testing). At 4 bits the noise floor is high enough that dither's added hiss outweighs the buzz-reduction it offers — on a real 6581 SID the user A/B'd both and consistently preferred dither off. Flip to true if your hardware or source material disagrees (it converts signal-correlated rounding distortion into smooth white-noise hiss, which can sound better on already-noisy sources).

**`[audio].digi_boost` (EXPERIMENTAL, default off)** initializes all three SID voices with a locked pulse waveform (control = `$49` = gate+pulse+test, sustain = `$F0`) so the ADSR envelope D/As feed a steady DC offset into the master mixer. The C=Hacking #20 digi article documents this as mandatory on 8580s and emulated SIDs (where `$D418`-only playback is near-silent because the volume DAC has nothing to scale); on a real 6581 the residual ADSR offset is enough on its own, but digi-boost still raises the output level meaningfully (~3x with all 3 voices stacked). Marked experimental until tested across more hardware variants — flip on per-system in TOML to A/B.

**`position_seconds()`** is the audio-master clock: `(pushed - queued) / sample_rate`. The C64-side ring buffer adds ~1 s of constant latency past this, harmless for relative sync.

### `video.py` — WebcamSource (shared broker) + AVFileSource (PyAV)

**`WebcamSource`** is an always-on shared camera broker. A single `cv2.VideoCapture` is single-consumer (every `.read()` consumes the next device frame; concurrent reads from two threads aren't safe), so one background grab thread owns the capture, continuously reads the newest frame, and `read()` hands out an independent **copy** of the latest frame. That lets the webcam scene (when active) and the always-on vision controller (`vision.py`) share **one** physical camera with no contention — and keeps the live-webcam path low-latency (always the freshest frame, stale ones overwritten). `WebcamScene._read_frame()` is unchanged — it still calls `source.read()`. The camera is opened once per stack in `cli.py` when `needs_webcam or cfg.vision.enabled`, stored on `SystemStack.source`, released at teardown.

**`AVFileSource`** is for video playback. The demuxer thread reads packets from one container, pushes resampled mono int16 audio straight through to AudioStreamer, and queues decoded video frames keyed by PTS. Consumers call `current_frame(audio_position_s)` which returns the latest frame whose PTS ≤ the clock and drops anything behind. **Drift can't accumulate** because the audio clock IS the reference.

**EOF handling**: `current_frame` normally keeps the chosen frame in `_video_buf` so a clock stall doesn't black-frame the display. After demux EOFs (`self._eof = True`) that stall-protection becomes a trap — the buffer stays size-1 forever, `finished` (which checks `_eof and not _video_buf`) never flips, `VideoScene.process_frame` never returns False, and the audio worker pads NEUTRAL indefinitely (visible as a 3-min `writes=4/s bytes=4KiB/s` streak in audio logs). The fix is in `current_frame`: when `_eof` is set AND the consumed index is the last buffered frame, clear the buffer entirely so `finished` can flip on the next check.

### `modes.py` — DisplayMode hierarchy

Each mode does VIC register setup + frame quantization + push to the right addresses. All uploads go through `write_region` so the delta cache applies. Key vectorization tricks:

* `palette.quantize_distances()` returns the full (N, 16) distance matrix via the `(x-p)²` expansion — avoids the (N, 16, 3) broadcast tensor the naive form would build.
* `MCMDisplayMode` reuses one distance matrix across both the bg-color picker and the per-cell FG search, and vectorizes the original 8-iteration Python loop into one `argmin`.
* `MultiHiresDisplayMode` has two render paths. The legacy global-4 path (cheap/vivid/grayscale palette modes) uses a 16-entry LUT to remap every palette index to the nearest of the 4 globally-chosen colors (in weighted BGR space) rather than zero-defaulting unused indices to bg0 — that older behavior silently bled large patches of background into the image. The new per-cell path (default `palette_mode = "percell"`) uses VIC-II MCBM's per-cell `c1`/`c2`/`c3` capacity: picks `bg0` globally, then for every 4×8 cell picks its own top-3 non-bg colors by population and resolves each of the 32 cell pixels against {bg0, c1_cell, c2_cell, c3_cell}. Frames carry up to `bg0 + 3×1000 = 3001` distinct colors instead of 4, which is what VIC-II MCBM was designed to support; the older global path was leaving most of that capacity unused.
* `PETSCIIDisplayMode` delegates glyph + color selection to a `PetsciiStyle` from `petscii_styles.py` (see below). The default style is the original luma → 11-char ramp + per-cell quantized color; cycling via SHIFT swaps in increasingly abstract alternatives (halftone blocks, random graphics glyphs, letter rain, etc.).

`MCMDisplayMode` and `MultiHiresDisplayMode` accept a `palette_mode` constructor argument (configurable per-scene via `palette_mode = "percell"|"cheap"|"vivid"|"grayscale"` in TOML, default `"percell"`):

* `"percell"` — MultiHires only (MCM treats it as an alias for `"cheap"`, since MCM already picks the fg per cell). Picks `bg0` globally as the EMA-smoothed most-populated palette index, then for every 4×8 cell picks its own top 3 non-bg colors by population (using a per-cell bincount on the same `(N,16)` distance matrix the global path uses). Per-cell picks are sorted by palette index for delta-cache stability; bg0 is excluded from the per-cell search so the cell's effective palette stays at 4. Each of the cell's 32 pixels resolves directly against `{bg0, c1_cell, c2_cell, c3_cell}` via `take_along_axis` on the (1000, 32, 16) cell-shaped distance tensor — no LUT step, because there's no global slot remap to apply. Screen RAM ($0400) carries (c1<<4)|c2 per cell and color RAM ($D800) carries c3 per cell; both writes are now per-cell content instead of one repeated byte, so they bust the delta cache more often (still well under the DMA budget). Black-dominated content like the vintage videos benefits the most: cells that don't contain bg0 stop wasting one of their 4 slots on it, and regional content (laptop screen, kid's sweater, monitor glow) keeps its colors instead of being collapsed to the global dominant pick.
* `"cheap"` — legacy global-4. HSV saturation boost (`boost_saturation`, factor 1.8) before quantization plus a `make_gray_penalty` bias added to the per-pixel distance matrix. The penalty pushes the 5 gray-axis palette entries + cyan (which sits at the pale-chromatic boundary and over-selects on warm-gray skin) far enough that borderline pixels flip to a chromatic neighbor. Top-N slot picks go through `_ema_counts` (EMA-smoothed bincount, `PALETTE_PICK_EMA_ALPHA = 0.25`) and are then sorted by palette index, so the chosen SET only flips on sustained scene changes and a stable SET always lands in a stable slot ORDER — without this the picks flickered between e.g. cyan and orange every few frames as borderline counts tied differently, rewriting screen + color RAM + bg registers and producing a visible palette flash. Still the default for MCM.
* `"vivid"` — same biases, plus the 3 (MCM) / 4 (MultiHires) global slots are picked by `pick_diverse_top_n` instead of raw frequency: the most-populated index always wins slot 0, then each subsequent slot prefers a populated entry whose hue is at least 45° away from already-chosen chromatic picks. Falls back to most-populated when no diverse candidate exists. Use when a scene keeps reducing to two-or-three near-shades.
* `"grayscale"` — restricts every quantization decision to the 5 gray-axis palette entries (black, white, dark gray, gray, light gray). Skips the saturation boost (wasted work on gray-only output) and uses `make_gray_penalty(chromatic_strength=GRAYSCALE_CHROMATIC_PENALTY=1e10)` so every chromatic entry is dominated in the per-pixel argmin. Global slot picking is **fixed** (not adaptive) in luminance order: MHires uses `(0, 11, 12, 15)` = black, dark gray, gray, light gray (pure white is dropped for better mid-tone resolution); MCM uses bgs `(11, 12, 15)` with FG resolving to `{0, 1}` for full 5-level coverage per screen. The MHires LUT is precomputed once at `__init__`. Adaptive picking from only 5 gray entries was a perf disaster: per-frame tie-break shuffles flipped the slot order, which rebuilt the LUT, which remapped every pixel to a different slot in the 8 KB bitmap, which busted the chunked-delta cache and forced full bitmap + screen RAM + color RAM uploads every frame. Result is the same "old TV broadcast" aesthetic but at the full system frame rate (60 NTSC / 50 PAL) instead of ~13 fps. Note that in MCM only black (0) and white (1) survive into the FG slot (color RAM bit 3 = multicolor flag steals the high bit, so FG is restricted to indices 0..7).

`petscii_styles.py` registers the styles in `STYLE_NAMES` (default, halftone, random_glyph, letter_rain, neon, inverse_pop, hatch, color_only). Each subclass owns its own char ramp + color policy and declares its preferred border + background; the mode pokes those on setup and on every SHIFT cycle. The `random` config sentinel is resolved at scene `setup()` to a concrete style — subsequent cycles proceed from there in declared order, so SHIFT behavior stays predictable instead of re-randomizing each press. New styles are one PetsciiStyle subclass + a registry entry away (no PETSCIIDisplayMode change needed).

`BlankDisplayMode` is a standard PETSCII char mode with no video input — every cell is `SC_SPACE` (0x20) with FG = `background`, so the canvas reads as solid color until an overlay paints over it. Takes `border` and `background` palette indices (masked to 4 bits). `is_petscii_compatible = True` (class flag, parallel to `PETSCIIDisplayMode`), so every overlay that writes PETSCII screen codes works on blank scenes too. Used as a clean foundation for demo-scene title cards via the `big_text` overlay. `BlankScene` (in `scenes.py`) is the matching no-source Scene subclass.

**`[video].use_reu_staged`** routes video pushes through the REU. **Tri-state: `true | false | "auto"` (default `"auto"`).** `config.resolve_use_reu_staged(setting, display, reu_available)` resolves it **per scene's display mode** at build time: `"auto"` → True only for a bitmap mode (`_REU_BITMAP_MODES` = hires/hires_edges/mhires) AND when the startup probe confirmed the REU is on; char modes (petscii/blank) stay on host-DMA under auto because their delta cache makes a full per-frame REU→main DMA a net regression. Explicit `true`/`false` ignore the probe. `reu_available` is computed once in `cli._resolve_reu_available` (gated on `"auto"` + `api.profile.supports_reu` + not `--skip-probe`, via `doctor.reu_is_enabled`), stashed on `SystemStack.reu_available`, and threaded through `scenes_from_config`/`build_scene` (and the SIGHUP/control-plane reload + ensemble-follower rebuilds). A `display = "random"` slideshow stores the raw tri-state + `reu_available` and re-resolves per concrete mode each setup. Any uncertainty (no REU, failed query, `--skip-probe`, non-REU backend) degrades to host-DMA so video never silently freezes.

Two pipelines, picked by display mode. **Char modes (PETSCII/Blank), single-buffer:** `push()` calls `modes._push_screen_via_reu(api, screen_bytes, $0400)` — REUWRITE the 1000-byte screen to `REU_VIDEO_SCREEN_BASE = $E00000` (bus-clean), configure REC `$DF02`/`$DF04`/`$DF07` for a one-shot REU→main DMA, trigger via `$DF01 = $91`. Color RAM at `$D800` isn't VIC-banked, so it stays on the delta-cached DMAWRITE path. **Bitmap modes (Hires/MultiHires), double-buffer:** bitmap+screen are REUWRITE-staged then DMA'd into the *off-screen* VIC bank; a C64-side raster IRQ at `$0314` flips `$DD00` at vblank for a tear-free swap (this is what eliminates the scene-cut whole-screen flashes — see [[mhires_scene_cut_tearing]]). Coexistence with the REU audio pump is fine on **any** scene: the bank-swap installer picks a **merged** `$0314` dispatcher whose non-raster branch JMPs the audio pump at `$C100`, servicing both IRQ sources through one hook (this lifted the earlier `validate_scene_cfg` mutex against `use_reu_staged + use_reu_pump`, which no longer exists). MCM doesn't support staging yet (future work).

### `scenes.py` — Scene state machine

Playlist calls `setup()` → `process_frame()` * → `teardown()` for each scene, with an interstitial scene (built by an injected `interstitial_factory`) between them. `WebcamScene` is tuned for low latency: each camera frame is pushed straight through, no delay buffer. Audio follows the global `[audio].enabled` flag — when on, the webcam/blank scene picks up the AudioStreamer automatically; a per-scene `audio = false` opts back out (useful for muting one segment in an otherwise audible playlist). When attached it runs uncorrelated to the video — sync between mic and display isn't preserved. Backpressure is handled at the Playlist layer via deadline-based frame-dropping rather than a per-scene queue check — the DMA socket's TCP send buffer absorbs short bursts, and missed frames get dropped at the deadline instead of bursting to catch up. `VideoScene` is the opposite: it uses the audio playback position as the master clock and picks the closest video frame against it, so A/V never drift. Its lifetime is video-driven — `process_frame` returns False once the source reports `finished`, and `__init__` pins `self.duration_s = math.inf` so the base-class duration timer can't truncate playback. `config.validate_scene_cfg` rejects any user-supplied `duration_s` on a video cfg (the field would either be a silent no-op or a truncation footgun); `Scene.setup` formats the inf as `duration=video-driven` in the startup log.

Each Scene also carries an `overlays: list[Overlay]`. The Playlist runs every overlay's `setup`/`process_frame`/`teardown` around the scene's lifecycle — scene paints first, overlays paint on top (in declaration order).

Each Scene also has an optional `target_fps` attribute. When set, the Playlist honors it for the duration of the scene. If unset, the Playlist uses the playlist's system default (60 NTSC / 50 PAL) for all modes. `WaveformScene` is the exception: it defaults to **half** the system rate (30 NTSC / 25 PAL) because 60 fps powers off the U64 on bank-2-relocated SIDs (~170 writes/s into `$A000-$BFFF` — a suspected firmware badline/DMA bug); an explicit `target_fps` (CLI/TOML) still wins. The host-emu poll thread stays at the full video rate regardless.

`SlideshowScene` cycles through still images for the scene's `duration_s`. File spec mirrors VideoScene's grammar (comma-separated paths / dirs / globs via `resolve_file_spec`, default dir `assets/pictures/`). Per-image timer is `image_duration_s` (default 5s) — independent of `duration_s`, which controls total runtime. Picker is shuffle-and-walk: every image in the pool plays once before any repeats, and the first pick after a reshuffle is swapped with the second when the pool has >1 entry, so no image appears twice back-to-back across reshuffle boundaries. No audio, no `WANTS_AUDIO_LOCK`, no CLAHE/temporal-EMA smoothing (the webcam blend would cross-fade unrelated stills). `display = "random"` resolves to a fresh mode in `SLIDESHOW_RANDOM_DISPLAYS` at every `setup()` (so single-scene loops vary per iteration); `display = "hires_edges"` is substituted with `mhires` (the SceneCfg global default is tuned for live webcam Canny edges, not photos — use `display = "hires"` for plain bitmap). Images load via `cv2.imread` (no extra dependency).

### `voice_scope.py` — shared 3-voice oscilloscope renderer

`VoiceScopeRenderer` is the SID-source-agnostic hires oscilloscope renderer, factored out of `WaveformScene` so `MidiScene` can reuse it. It's a **mixin** (both `WaveformScene` and `MidiScene` are `class X(VoiceScopeRenderer, Scene)`): the extraction kept every `self._<attr>` reference intact, so `WaveformScene`'s byte-output and its full test suite are unchanged (the regression guard). It owns the layout constants (`BITMAP_STRIPS`, `TITLE_ROW`/`META_ROW`, the `D011`/`D016`/`D018` pokes), the VIC hires bring-up (`_apply_vic_hires_bank`), the glyph + text-row rasterizer (`_paint_text_row`, `_load_glyphs`, `_ascii_to_screen_code`, `_layout_lr`/`_layout_lcr`), the per-voice color helpers, the three render paths (`_render_voice_fast`/`_scroll`/`_echo` → `_render_hires`), and the knob parsing + buffer allocation (`_init_scope_knobs` / `_alloc_scope_buffers`). A host scene satisfies a documented attribute contract (`api`, `emulator`, `_reg_lock`, `_screen_base`/`_bitmap_base`/`_dd00`/`_d018`, …) and supplies its own text-row CONTENT. `waveform.py` re-exports the moved consts so historical `from .waveform import …` (config, tests) keeps working.

### `waveform.py` + `sidemu.py` + `sid_host_emu.py` — SID oscilloscope scene

`WaveformScene` (inherits `VoiceScopeRenderer`) plays a SID file on the U64 via `api.run_sid_player(...)` (DMA SID payload + 73-byte 6502 player relocated per-tune by `_choose_player_layout` — default $C300, bumped past the SID payload on overlap — then POST a matching `10 SYS <player_base>` BASIC stub via `runners:run_prg`) and visualizes the three SID voices' waveforms across the full screen. Display is bitmap-only (320×200 hires). Voices stack vertically — voice 1 top, voice 3 bottom.

The firmware's `runners:sidplay` endpoint is deliberately avoided: on firmware 3.14d it draws its own "ULTIMATE C-64 SID PLAYER" UI on the HDMI scaler that covers everything we paint into VIC RAM. PSID-only — RSIDs (which install their own raster IRQ in INIT), tunes whose `load_addr` is below `$0820` (would collide with the BASIC SYS stub), tunes whose `play_addr` is zero (INIT installs own IRQ), and tunes with code/data under KERNAL ROM (`$E000-$FFFF`, where the player can't bank KERNAL out without losing its `$EA31` IRQ chain) are refused at scene setup with a clear error. Tunes under **BASIC** ROM (`$A000-$BFFF`) are supported — the player banks BASIC out per-call around the affected `JSR init`/`JSR play` (`$01 = $36`; see below). PAL/NTSC speed flag is ignored — the kernal's default CIA #1 Timer A rate is used. See [docs/caveats.md](docs/caveats.md) for the full design discussion.

The visualization is driven by a hybrid setup:

* **Audio** comes from the U64's native SID chip (the audience hears the real hardware).
* **Per-voice waveforms** come from a Python-side `SIDEmulator` in `sidemu.py`. The U64's FPGA SID is faithful to real hardware — `$D400-$D418` is write-only and reads return open-bus zeros, so we can't ask the U64 what the SID is doing. Instead, `SidHostEmu` in `sid_host_emu.py` runs the same SID file in parallel on a host-side [py65](https://github.com/mnaberez/py65) pure-Python 6502. A `TrappedRam` wrapper around the emulator's 64 KB array intercepts writes to `$D400-$D418` into a 25-byte shadow. The background poll thread ticks the host emulator at the system video rate (60 NTSC / 50 PAL — the SID's effective PLAY-per-frame cadence on a kernal IRQ) and feeds the shadow snapshot to `SIDEmulator`, which mirrors per-voice waveform-select / pulse-width / ADSR state and synthesizes samples on demand. Phase is owned by Python (not synced to the real chip), so what you see is a faithful per-voice oscilloscope trace at the right frequency and envelope — not a phase-accurate scope of the audio output. PSID validation is shared with `api.run_sid_player` via `parse_psid_for_player`, so a tune the U64 side refuses is also refused at host-emulator construction with the same error.
* **Combined waveforms** are decided by priority (noise > pulse > sawtooth > triangle) rather than ANDed bit-patterns — the real SID's "metallic" combinations don't have a clean visual shape.
* **No filter, no master volume**: irrelevant to the per-voice oscilloscope view.

Coloring modes:

* `per_voice` — each voice gets a fixed C64 color from `voice_colors`.
* `per_waveform` — color reflects the currently-selected wave type (e.g. cyan for pulse, light red for sawtooth). Color RAM is rewritten only on transitions, not every frame.

Teardown order matters: `api.restore_kernal_irq_vector()` puts `$0314` back to `$EA31` first (so our IRQ handler is unhooked and PLAY stops being called), then `api.flush()`, then `api.silence_sid()` writes 0 to `$D418` and clears each voice's gate. If silence ran first, the IRQ could fire between the volume-clear and the gate-clears and PLAY would rewrite both. No reset, so the next scene paints over the waveform without the BASIC banner flashing.

The scene runs for a fixed `duration_s` — `runners:run_prg` doesn't surface a "finished" signal for the BASIC GOTO loop. Use SongLengths data via `[playlist].songlengths_file` if you have it; otherwise pick a duration that matches the tune.

Visualization knobs (all compose; defaults preserve the redraw-from-scratch, wallclock-locked behavior so existing configs render identically):

* `time_base = "wallclock" | "auto"` — `auto` derives the per-voice time window from `v.freq` so `auto_cycles` complete cycles always fit, regardless of pitch. Silent voices (freq=0, wave=off, or envelope=0) fall back to wallclock per-voice so the trace doesn't collapse to a flat line on a divide-by-zero.
* `persistence = "off" | "short" | "medium" | "long" | "random"` — replaces the per-frame-cleared bool canvas with a per-voice `uint8` intensity strip that decays each frame (faded pixels fall under a fixed mid-scale threshold and turn off). `random` resolves to one of the named presets at scene setup (same sentinel pattern as `petscii_styles`); the resolved name is logged on the scene's startup line.
* `scroll_columns = 0 | N | [N1, N2, N3]` — per-voice FIFO: shift the intensity strip left by N columns and draw only the new N columns on the right edge. Scalar broadcasts to all three voices; list assigns per voice (so one strip can scroll fast, one slow, one stay redraw-style). Scroll mode rewrites the whole strip per frame and busts the dirty cache on purpose; cost is bounded (~700 KB/s of DMA at 60 fps, well under the ceiling). SHIFT-cycle zeroes the strip buffers so a `persistence = "long"` trail from the prior subtune doesn't ghost-merge into the new one.

### `midi_scene.py` — MidiScene (live MIDI → SID + oscilloscope)

`MidiScene` (inherits `VoiceScopeRenderer`) turns the C64 into a 3-voice MIDI sound module and visualizes it with the **same** hires oscilloscope as `WaveformScene`. Note on/off → voice freq + gate; pitch-bend → ±2 semitones on gated voices. **Voice allocation** layers a mono melody over a polyphonic sustain pad: held notes keep their voice, and a new note over capacity steals the *most-recently-started* voice (`max` t_changed) so the older/held notes form a stable pad while an overlapping line/arp cycles on the top voice; freeing a voice resurrects the most-recent still-held suspended note (LIFO). **Gate-edge hard restart:** the real SID re-attacks only on a gate 0→1 edge, so re-using an already-gated voice (re-press / steal / trill) writes a gate-off control byte *before* the new voice block — without it the chip changes pitch but never re-triggers (silent note while the host-emulator waveform, fed a `retrigger` flag, still moves). The MIDI reader thread coalesces continuous-controller floods (wheel sweeps) to ≤60 Hz so they can't burst the DMA socket; notes stay immediate.

**Instrument controls** (the "mostly-serious synth" layer): **velocity → loudness** — since the SID has no per-voice volume, `_program_voice` maps note velocity to that voice's **sustain** nibble (`velocity >> 3`). **SHIFT** (`cycle_style`) cycles the global waveform pulse→saw→tri→noise, re-emitting it on held + idle voices. **CC map**: CC1→pulse width (mapped to an audible `[128, 3968]` window so wheel-to-zero doesn't mute), CC7→master volume, CC74→filter cutoff, CC71→resonance, CC73/CC75/CC72→attack/decay/release. The filter is actually audible because `_program_global_sid` routes all three voices through it (`$D417` low 3 bits) — the old default routed none, so CC74 did nothing; cutoff defaults open so a lowpass patch is neutral until swept. `$D418` writes always carry the filter-mode nibble (CC7 used to clobber it).

The key difference from `WaveformScene`: **no py65 host emulator.** MidiScene *is* the writer — it computes every SID byte it sends — so it keeps a 25-byte `$D400-$D418` register **shadow** (`_sid_shadow`, indexed by `addr - $D400`) updated alongside every SID write and feeds `SIDEmulator.update_registers(...)` directly under `_reg_lock`. A re-gate of an already-gated voice (re-trigger / voice steal) shows no off→on edge to `update_registers`, so `_program_voice` passes a per-voice `retrigger` mask to force a hard re-attack (else a plucked sustain=0 voice would flatline after one decay). A background `PollThread` (`midi-env`) advances the ADSR envelopes at the video rate (60/50 Hz) so attack/decay/release tails evolve on screen between MIDI events; render phase is owned by the emulator (advanced by `voice_samples` during render), same as WaveformScene.

Display is fixed bank-0 hires (no relocation — MidiScene uploads no SID payload and leaves the audio ring idle). Bitmap-only ⇒ `_validate_midi` reports a `hires` display so PETSCII overlays are rejected (same as waveform). **Voice strips show activity by color**: `process_frame` change-detects each voice's sounding state (gated or envelope > eps) and repaints its strip color — its configured/per-waveform color while sounding, **gray when idle** (a released voice's flat trace then reads as "off", which is why no per-voice note text is needed). The two bottom text rows are change-detected (repainted only on note/CC events): row 22 = global `MIDI <WAVEFORM>` + `VOL nn`; row 23 = a live controller readout (`PW nn%  CUT … RES … A. D. R.`). `target_fps` defaults to half the video rate (30/25), like WaveformScene; the scope knobs (`color_mode`/`time_base`/`auto_cycles`/`persistence`/`scroll_columns`/`voice_colors`/`waveform_colors`) all apply. Per-voice (multi-)waveform mode is a deferred follow-up.

### Framerate pacing & frame-dropping

`Playlist.run` uses deadline-based pacing: each frame advances a `next_deadline` by `frame_time` (resolved per-scene by `_frame_time_for(scene)`). If the wall clock has fallen more than two frame_times behind the deadline, the deadline snaps forward — dropping the missed frames — instead of bursting to catch up. All built-in scenes follow the system rate except `WaveformScene` (half rate; see above). Animation logic that uses `current_time` keeps tracking wall-clock time correctly across dropped frames.

`_crop_to_aspect()` is shared aspect-correction logic; previously inlined in three places.

### `overlays/`

Stackable scene decorations. Each overlay subclasses `Overlay` and registers via `@register("name")`. `config.scenes_from_config` builds them from `[[scenes.overlays]]` TOML blocks and `validate_for_scene` rejects incompatible combinations (e.g. `clock` overlay on a `hires` scene — the clock writes PETSCII screen codes that only render in standard char mode; or on an `mcm` scene — color RAM bit 3 reinterprets the cell as multicolor).

Restrictions:

* `REQUIRES_PETSCII = True` — writes PETSCII screen codes to $0400/$D800; accepted on any display mode with `is_petscii_compatible = True` (currently `PETSCIIDisplayMode` and `BlankDisplayMode`). Rejects `mcm` (color RAM bit 3 reinterprets the cell as multicolor + halved horizontal pixel resolution) and all bitmap modes (no character matrix at all).
* `COMPATIBLE_MODES = ("a", "b", ...)` — whitelist of display-mode names this overlay supports. Empty tuple (default) = no restriction. Used for overlays that don't map onto the binary "PETSCII vs bitmap" split — e.g. `big_text` paints into blank or MCM buffers but not into a PETSCII webcam scene (where it would stomp the live-frame PETSCII glyphs).
* `REQUIRES_AUDIO = True` — needs `[audio]` enabled. `build_overlay` raises with a clear message otherwise.

The built-in overlays:

| Overlay            | Restriction               | What it writes                                                                 |
|--------------------|---------------------------|--------------------------------------------------------------------------------|
| `scrolling_text`   | petscii / blank           | One row of screen + color RAM, configurable row/speed/messages.                |
| `marquee`          | petscii / blank           | One row, single text string, ticker-style continuous loop with separator.      |
| `rss`              | petscii / blank           | Marquee fed by a background RSS/Atom fetch (stdlib `ElementTree`).             |
| `spectrum_petscii` | petscii / blank, audio    | A strip of cells (bottom / center / split mode), 8 bands × 5 cols.             |
| `clock`            | petscii / blank           | Time/date in a corner; only updates when the formatted string changes.         |
| `weather`          | petscii / blank           | Temp + conditions in a corner; background thread polls every N minutes.        |
| `callsign`         | petscii / blank           | Static text in a corner. Single paint, then change-detect zero traffic.        |
| `countdown`        | petscii / blank           | Time-until-target in a corner; auto-format or `{d}{h}{m}{s}` template.         |
| `network`          | petscii / blank           | IP / hostname / U64 ping latency in a corner; background socket poll.          |
| `logo`             | petscii / blank           | Multi-line PETSCII art from a `.txt` file at `corner` or explicit `row`+`col`. |
| `big_text`         | blank / mcm only          | Demo-scene 8×-scaled scrolling text (each source PETSCII char → 8×8 cells).    |

Most corner-positioned overlays (`clock`, `weather`, `callsign`, `countdown`, `network`) share `overlays/corner_text.py` — subclass `CornerTextOverlay` and just implement `compute_strings(t) → Optional[list[str]]`. The base handles change-detection, blanking-on-shrink, and teardown cleanup.

`marquee` and `rss` share `overlays/marquee.py:MarqueeBase` — subclass and implement `_current_text()`.

Audio overlays read recent float samples from `AudioStreamer.get_recent_samples(n)`, which exposes a 2048-sample tap filled by every input path (mic, WAV, PyAV).

`Overlay.is_busy()` (default `False`) lets a slow-paint overlay defer the scene's auto-advance. When a scene's `duration_s` timer expires, the Playlist checks every attached overlay's `is_busy()` and, if any returns True, flips `is_done` back to False so the scene runs another frame. `big_text` uses this in `loop = false` mode to make the Playlist wait for the last message to finish scrolling off-screen before the interstitial appears. In the default `loop = true` mode, `is_busy()` always returns False (the message list is effectively infinite — busy-defer would freeze the playlist) and `duration_s` is the source of truth. **CTRL skip always wins**: when `skip_event` is set, `is_done = True` is forced regardless of busy state — the busy guard runs above the CTRL branch, so the CTRL branch overwrites it. For the busy-defer to actually paint frames past `duration_s`, the scene's `process_frame` must keep rendering after the deadline; `BlankScene` does (it returns `still_active = False` but renders the frame first), `WebcamScene` and `VideoScene` short-circuit.

**Single-scene mode**: when `len(scenes) == 1` at `Playlist.__init__` (or after a reload), `Playlist.single_scene` is True. `_advance` skips the interstitial path entirely — the one scene is set up directly on first call, and on `is_done` it's torn down and re-set-up back-to-back so it loops forever. CTRL skip events are dropped (with `log.debug`) and the event is cleared so it doesn't accumulate; C= pause/resume still work. `scenes_from_config` also short-circuits `interleave_videos` when the user-defined playlist is a single scene (an inserted video would promote the playlist to 2 scenes and silently defeat the mode). This is the mode every file in [config/examples/](config/examples/) runs in.

**Playlist loop control**: `[playlist] loop` (also `--loop` / `--no-loop`) controls what happens at the end of the playlist. Default `true` preserves the looping behavior above. `false` makes `_advance` set `stop_event` instead of looping — single-scene mode tears down after one play; multi-scene tears down after one full pass through the scene list. Used for "play one video and exit" and "play these N videos then quit" workflows. Live-streaming scenes (webcam, blank) typically leave it at the default and run until the user kills the streamer.

### `interstitial.py` + `backgrounds.py`

`InterstitialScene` is what plays between scenes ("UP NEXT: …"). It renders two centered text lines (the label `UP NEXT:`, a blank row, then the upcoming scene name) on top of an animated parallax background. Color is configurable (`rainbow` gives each line a different color from the rainbow palette).

`backgrounds.py` registers 7 styles: `starfield`, `petscii_bars`, `raster_bars`, `checker`, `nature`, `city`, `none`. Each implements `render(t, top_rows, bottom_rows, bg_color) -> (chars[1000], colors[1000])` that fills only the strips above and below the text — the InterstitialScene writes its text into the middle rows on top. `"random"` rotates through styles per setup() call. All writes go via `write_region` so the delta cache absorbs the static cells.

### `config.py`

Dataclasses for each section, `load()` parses TOML, `merge_cli()` overlays argparse values (only non-None ones — argparse defaults to None for every overridable option so the merge is unambiguous). `scenes_from_config()` is the factory that turns `[[scenes]]` entries into real Scene instances; it also handles video-interleaving from `[playlist].videos_dir`.

### `cli.py`

Loads config (`--config` flag or `./c64cast.toml`), merges in CLI overrides, then calls `config.scenes_from_config()` to build the playlist. The Playlist gets an `interstitial_factory` built from the `[interstitial]` section and a `CommodoreKeyPoller` for pause/resume.

### Startup: BASIC clear-and-loop program

After `api.reset()`, `api.run_basic_clear_loop()` POSTs a 25-byte tokenized BASIC PRG (`10 PRINT CHR$(147) : 20 GOTO 20`) to `/v1/runners:run_prg`. `PRINT CHR$(147)` wipes the BASIC READY banner and homes the cursor; the infinite `GOTO 20` keeps BASIC out of the editor's direct-input mode so the kernal cursor-blink IRQ stays naturally suppressed (the editor is what flips `$CC` between 0 and 1 — when BASIC is busy in a tight loop, the blink never re-arms). Audio bring-up still just uploads the NMI routine and starts the CIA #2 timer; the NMI fires regardless of what the BASIC loop is doing.

### `keyboard.py` — Commodore-key pause/resume, CTRL-key skip, SHIFT-key style cycle

`CommodoreKeyPoller` runs a background thread that polls `$028D` (the kernal's keyboard-modifier scratch byte) at 10 Hz via `api.read_memory`. Bit 0 = SHIFT, bit 1 = Commodore key, bit 2 = CTRL.

* **C= edge while running:** sets `Playlist.pause_event`. The run loop tears down the current scene + overlays, calls `api.reset()`, then waits. No BASIC clear-loop on pause — the user sees the default C64 boot screen (BASIC banner + blinking READY cursor) as a visual indicator that the stream is paused.
* **C= held continuously for 3 s while paused:** sets `Playlist.resume_event`. The run loop calls `api.reset()` + `run_basic_clear_loop()` to clear the boot screen, then `_advance()` re-sets-up the same scene (we don't bump `self.index`). The "same scene" includes the same overlays — they each get a fresh `setup()` so audio + poll threads come back online cleanly.
* **CTRL edge while running:** sets `Playlist.skip_event`. The run loop forces `current.is_done = True` after the current frame, so the scene tears down cleanly and the playlist advances to the next interstitial. CTRL while paused is a no-op.
* **SHIFT edge while running:** sets `Playlist.cycle_event`. The run loop calls `scene.cycle_style(api)` on the current scene (for scenes without a display_mode that still want SHIFT behavior — e.g. `WaveformScene` cycles to the next SID subtune) AND `display_mode.cycle_style(api)` on the display mode AND `overlay.cycle_style(api, scene)` on every attached overlay; each surface rotates through its own list of named styles, invalidates whatever caches it owns so the next push fully repaints, and returns a label that gets logged (combined: `cycle: <scene> → scene=<s>, display=<x>, <overlay>=<y>`). `Scene` doesn't define `cycle_style` at all (the playlist checks via `getattr` + `callable`), and default `DisplayMode.cycle_style()` / `Overlay.cycle_style()` return None — opt-in scenes/modes/overlays are the only ones that actually rotate. `BigTextOverlay` opts in and rotates the message FG color through `(config, rainbow, *spectrum)`. `WaveformScene.cycle_style()` advances `self.song` modulo `header.num_songs` (single-song SIDs return None), tears down + re-runs `api.run_sid_player(...)` on the new song, rebuilds `SidHostEmu` so the visualizer tracks the right subtune, and resets `start_time` so the new song gets its full `duration_s` (re-resolved from the SongLengths DB when present; explicit user `duration_s` wins). When the DB is loaded, candidates whose looked-up length is below `WaveformScene.MIN_CYCLE_SUBTUNE_S` (5 s) are skipped — most game SIDs put 1-3 s SFX in their tail subtunes and the scope view of those is flat for most of the displayed time. Skip is bounded at `n-1` attempts so an all-SFX SID still advances by one slot. Startup is exempt: a user-configured `song` (or PSID `start_song`) plays no matter how short — pinning an SFX as the start song is itself a strong "play this" signal, same reasoning as why an explicit `duration_s` also disables skip. Suppressed during interstitial transitions. Cycled style lives on the display_mode + overlay + scene instance, so it persists across single-scene loop iterations and across pause/resume, but resets on a real scene boundary (multi-scene transition constructs fresh instances from config).
* **Chord rules:** C= + CTRL same tick → pause wins, skip suppressed. SHIFT held with C= or CTRL → SHIFT dropped (a thumb resting on shift while reaching for pause/skip shouldn't phantom-cycle the style).

The poller distinguishes "definitely not pressed" from "couldn't tell" — a failed HTTP read returns `None` and is ignored rather than phantom-resetting the held-time counter. The kernal IRQ must be intact for $028D to stay current.

### `vision.py` — webcam gesture control (optional, camera-as-input)

`VisionController` is a **second control surface** alongside the keyboard poller: a background thread reads frames from the shared `WebcamSource` broker, runs MediaPipe HandLandmarker, and sets the *same* `pause`/`resume`/`skip`/`cycle` events. The Playlist starts/stops it interchangeably with the key poller (`for controller in (key_poller, vision_controller)`), so the run loop can't tell which surface a press came from — exactly like the control plane. Reading the camera (not C64 memory) means it works on **any** backend, including TeensyROM. Enabled by `[vision].enabled`; needs the `vision` extra (`mediapipe`) + a downloaded HandLandmarker `.task` model (`assets/models/README.md`). A missing dep/model logs "vision control disabled" and the stream runs without gesture control — it never crashes the playlist.

Gesture → control mapping mirrors the keyboard semantics: **running** → PINCH=pause, fast horizontal SWIPE=skip, OPEN_HAND=cycle; **paused** → PINCH held `hold_threshold_s`=resume, others no-op (same "pause means only the resume gesture matters" UI contract). Priority pinch > swipe > open-hand (the chord-rule analogue), at most one event per tick, with a `gesture_cooldown_s` debounce on top of edge detection since gestures are noisier than key bits. A no-hand/failed frame returns `None` and skips the tick (no phantom state change), the visual analogue of `keyboard.py`'s `None`-on-read-failure.

The hand tracker is pluggable behind the `GestureRecognizer` protocol; `MediaPipeHandRecognizer` lazy-imports mediapipe (like `video.py`'s `_ensure_pyav`) so the rest of the app and the whole test suite run without the extra. The **static** pose classifiers (`is_pinch`, `count_extended_fingers`, `classify_static`) are pure functions on a `(21,3)` landmark array — directly unit-tested on synthetic fixtures, no camera/mediapipe. SWIPE is temporal (wrist x-velocity), handled in the controller. `latest_hands()` exposes the raw landmark snapshot as continuous state for future consumers (fingerpainting, pose/face scenes). The example config `config/examples/vision-gesture.toml` runs the controller over a blank scene to show gestures work over any scene, not just webcam ones.

### `control_plane.py` — HTTP control plane (optional)

When `[control] enabled = true` and the `control` extra is installed (`pip install c64cast[control]`), a FastAPI app runs on `127.0.0.1:8765` exposing:

* `POST /pause` → `playlist.pause_event.set()`
* `POST /resume` → `playlist.resume_event.set()`
* `POST /skip` → `playlist.skip_event.set()` (same path as a CTRL press)
* `POST /reload` → re-reads the config from disk and rebuilds the scene list at the next interstitial boundary

The same three events are fed by the keyboard poller, so HTTP and the C64 keyboard are equivalent control surfaces.

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
- Audio uses 4-bit samples (0-15) written to the SID volume nibble; quality is intentionally low-fi.
- `AudioStreamer` shares the render path's `Ultimate64API` instance. The U64 DMA service is single-connection only: a second concurrent socket TCP-accepts but its IDENTIFY never gets a reply, and the first socket blocks new ones for a few seconds after close. The shared `SocketDMAClient` is thread-safe (per-command mutex around sendall) and the combined write rate (audio ~8/sec + render ~30-60/sec) sits well under the ~200/sec DMA ceiling.
- Address strings passed to `write_memory*` work in either case (`"d018"` and `"D018"` are both fine).
- The dirty cache is keyed by `region_id` (small ints) not address — so a mode switch from PETSCII to MCM (both writing $0400) gets a clean diff baseline via `api.invalidate_cache()`. `InterstitialScene` reuses `REG_SCREEN`/`REG_COLOR` for the same reason.
- `backgrounds.py` constants are C64 *screen codes* (what goes to $0400), not PETSCII codes — the encodings diverge above 0x40 (e.g. `@` is PETSCII 0x40 but screen code 0x00).
- Memory writes go over the **Ultimate DMA Service** (TCP port 64, persistent socket, ~5 ms / ~200 writes/sec). See [docs/caveats.md](docs/caveats.md) → "Socket DMA replaced HTTP for writes" for the migration result and "U64 HTTP throughput wall" for the historical REST measurements that motivated it. Reducing write *count* (coalesce via `write_regs`, dirty-skip via `write_region`) is still the right move under DMA — it's cheaper than tightening the per-write floor.
- **SID playback** uses a hand-encoded 6502 player (73 bytes) plus a SHIFT-driven re-INIT stub (35 bytes), both relocated per-tune by `_choose_player_layout` in [api.py](c64cast/api.py). Default layout is player at `$C300` / stub at `$C400` (the historical fixed location, used when no conflict); SIDs whose payload would overlap get a contiguous bundle (`player_base + 80 = stub_base`) placed in the largest footprint-clean RAM hole (or just past / below the payload as fallback). The BASIC SYS stub's decimal argument is built dynamically by `_build_basic_sys_stub` to match the chosen `player_base`. The player banks `$01` **per call** — `_init_bank_for`/`_play_bank_for` patch the value to `$36` (BASIC ROM banked out, KERNAL + I/O kept) around the `JSR init`/`JSR play` whose entry lives under BASIC ROM (`$A000-$BFFF`, e.g. Hyperion 2 at `$AE2A`) so the call reaches the tune's RAM instead of executing ROM (which lands on the ROM SYNTAX-error stub → `?SYNTAX ERROR IN 10`), then restore `$37` (the resting default; tunes like Comic Bakery that read BASIC ROM as data keep it mapped between calls). Per-call banking replaced an earlier permanent-`$36` scheme that crashed tunes like Election which assume the `$37` resting environment between PLAY calls. The real 6510 calls INIT once and PLAY at IRQ time; the IRQ handler chains to kernal `$EA31` so keyboard scan (`$028D`) + cursor-blink suppression survive. After installing the IRQ vector the player spins forever (`JMP *`) rather than RTSing back to BASIC — INIT routinely clobbers BASIC zero-page state, so a return would print a syntax error on the next interpreter step. The default `$C300` location was chosen because [audio.py](c64cast/audio.py) owns `$C000-$C2FF` (NMI DAC + REU pump); the relocation picker refuses any layout that would overlap that region. PSID-only — RSIDs, SIDs whose `load_addr` < `$0820`, SIDs whose `play_addr` is zero, and SIDs under KERNAL ROM (`$E000-$FFFF`) are refused. PAL/NTSC speed flag is ignored (v1 limitation; kernal-default CIA #1 Timer A rate is used). See [docs/caveats.md](docs/caveats.md) "SID playback uses a C64-side player PRG" for the full rationale.
- **Ensemble audio coordination.** In ensemble mode (`[ensemble]` in the master TOML) at most one system's playlist may hold the ensemble audio slot — tracked as `Ensemble.audio_holder` + `audio_lock` in [ensemble.py](c64cast/ensemble.py). Scenes whose class sets `WANTS_AUDIO_LOCK = True` (`VideoScene`, `WaveformScene`, `MidiScene`, `LauncherScene`) try to claim the slot in `Playlist._resolve_next_index`; on contention they get **skipped** to the next non-gated scene in the playlist, with `_safe_teardown` releasing the slot when the holder's scene ends. (`LauncherScene` overrides `competes_for_audio_lock()` so `bypass_audio_lock = true` opts out — several systems can then run interactive launchers at once, each player hearing their own SID.) Live scenes (`WebcamScene`, `BlankScene`) are built with `audio = None` in ensemble mode regardless of TOML — they never compete for the SID. Single-system runs keep `ensemble = None` and bypass the gate entirely. An ensemble system whose playlist is entirely audio-bearing scenes will idle when the slot is held elsewhere; the loader emits a WARNING on this configuration.
