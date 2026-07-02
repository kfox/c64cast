# Usage

This document covers what you can put in a `c64cast.toml`, what the CLI
flags do, and how to assemble a working streaming setup. For architecture
notes, see [CLAUDE.md](../CLAUDE.md). For extending the codebase, see
[extending.md](extending.md). For gotchas, see [caveats.md](caveats.md).

## Prerequisite: enable Socket DMA on the U64

All memory writes go over the Ultimate 64's Socket DMA service (TCP
port 64). It is **off by default** in the firmware — enable it once
before the first run:

1. Press **F2** on the U64 to open the system menu.
2. Navigate to **Network Settings**.
3. Set **Ultimate DMA Service** → **Enabled**.
4. Set **Command Interface** → **Enabled**. *Both* toggles are required:
   the Command Interface gates the DMA command dispatcher even when the
   listening socket is open, so with it off the connection accepts but
   the first command times out.
5. Save settings and reboot (or exit the menu).

If a firewall sits between your host and the U64, allow inbound TCP 64.

**Password / authentication.** If the U64 has a network password set,
provide it via either:

* the `C64CAST_DMA_PASSWORD` environment variable (recommended), or
* `[ultimate64] dma_password = "..."` in your config file.

The env var wins when both are set. There is intentionally **no CLI
flag** for the password — secrets on the command line leak into shell
history and `ps` output.

If the DMA connection fails at startup the CLI exits with a clear
message pointing back to this section. The U64 REST endpoint is still
used for the operations that have no DMA equivalent (reads, runners,
machine reset, the startup reachability probe).

Reference: [Ultimate-64 docs](https://1541u-documentation.readthedocs.io/en/latest/)
and the protocol source at
[GideonZ/1541ultimate/software/network/socket_dma.cc](https://github.com/GideonZ/1541ultimate/blob/master/software/network/socket_dma.cc).

## Installation

The repo is set up for **uv** (with mise + direnv). The canonical setup —
creates/updates a `.venv` from `uv.lock` with every runtime extra plus the dev
tooling group:

```bash
uv sync --all-extras
```

direnv activates `.venv` automatically (via `layout uv`); otherwise prefix
commands with `uv run`. Avoid `uv pip install -e .[...]` here — mise's
`UV_PYTHON` sends `uv pip` to the bare toolchain interpreter instead of `.venv`,
so packages install where the app doesn't run from. Plain-pip users without uv:
`pip install -e .[all]` for runtime extras, then `pip install --group dev` for
the dev tools (`dev` is a PEP 735 dependency-group, not an extra, so it can't be
requested via `.[all,dev]`).

Optional-dep groups in [pyproject.toml](../pyproject.toml):

| Group         | What it pulls in                      | Why you'd want it                                      |
|---------------|---------------------------------------|--------------------------------------------------------|
| `mic`         | `sounddevice`                         | Live microphone input (the SID DAC streaming path)     |
| `video` | `av` (PyAV)                           | `video` scenes — plays MP4/MKV/etc. through SID   |
| `preview`     | `pygame`                              | Local preview window mirroring the U64 + recording     |
| `control`     | `fastapi`, `uvicorn`                  | HTTP control plane (pause/resume/skip/reload)          |
| `obs`         | `obsws-python`                        | `obs_status` overlay — polls OBS WebSocket v5          |
| `midi`        | `mido`, `python-rtmidi`               | `midi` scenes — live MIDI input → SID synth + scope    |
| `logging`     | `rich`                                | Colored timestamped terminal logging (RichHandler)     |
| `wizard`      | `questionary`                         | `--init` interactive config builder                    |
| `all`         | every runtime extra above             | The "give me everything" install                       |
| `dev`         | `ruff`, `coverage`, `mypy`, `pyright` | Lint + coverage + type-check                           |

## CLI

```bash
python -m c64cast [args...]
```

Run with `-h` for the grouped flag list. The flags shown there are the
overrides; everything has a sensible config-file equivalent.

| Group               | Flag                                  | Notes                                                       |
|---------------------|---------------------------------------|-------------------------------------------------------------|
| `--config PATH`     | n/a                                   | Use a specific TOML file. Else `./c64cast.toml`, else defaults. |
| `introspection`     | `--init [PATH]`                       | Interactively build a config (needs the `wizard` extra). Writes `./c64cast.toml` or `PATH`. See "Creating a config". |
| `connection`        | `-u TARGET`, `--system NTSC\|PAL`     | `-u` selects the backend **and** endpoint via a scheme (see below). `$C64CAST_URL` is the env fallback. |
| `video input`       | `-d N`                                | Webcam index.                                               |
| `audio`             | `--audio` / `--no-audio`, `-D N`,     | Audio is **on by default**; `--no-audio` mutes.             |
|                     | `--sample-rate`, `--mic-sensitivity`, `--noise-gate` |                                              |
| `playlist`          | `--videos DIR`                           | Directory of videos for auto-interleaving        |
| `debug`             | `-v`, `-vv`, `--heartbeat S`,         | `-v` info, `-vv` debug, `--skip-probe` skips the U64 reachability check. |
|                     | `--skip-probe`, `--list-devices`      | `--list-devices` enumerates audio + video devices and exits. |

**CLI vs config precedence:** built-in defaults < TOML config < CLI
flags. Every overridable CLI option has `default=None` so the merge can
tell "user passed the default" from "user didn't pass it."

## Connecting to hardware (`-u` target)

`-u/--url` takes a single scheme-aware **connection target** that selects both
the hardware backend and its endpoint, replacing per-backend flags. `$C64CAST_URL`
is the env fallback when `-u` is omitted. In a TOML config the same choices live
in `[hardware].backend` + `[ultimate64]` / `[teensyrom]`; the `-u` target
overrides those sections (single-system runs only — ensemble systems keep their
per-system TOML identity).

| Target                          | Backend / transport                                  |
|---------------------------------|------------------------------------------------------|
| `u64://HOST[:PORT]`             | Ultimate 64 / II+ over REST + socket DMA (`http://HOST`) |
| `http://HOST` / `https://HOST`  | Same — passed to the REST client verbatim            |
| `tr://`                         | TeensyROM+ USB serial, device **auto-detected** (macOS) |
| `tr:///dev/cu.usbmodemXYZ`      | TeensyROM+ USB serial on that device node            |
| `tr://COM3`                     | TeensyROM+ USB serial on a Windows COM port          |
| `tr://HOST[:PORT]`              | TeensyROM+ over raw TCP (default port 2112)          |

Rare per-link knobs ride along as `?query` params, so they need no extra flags:
`u64://host?dma_port=64`, `tr://host?tcp_port=2113`,
`tr:///dev/cu.usbmodem?baud=2000000`, `tr://?storage=usb`.

## Quick playback (positional `MEDIA` args)

For ad-hoc testing without writing a TOML, pass media files/dirs/URLs as
positional arguments to `c64cast` (mutually exclusive with `--config`). It
builds an **in-memory-only** config — nothing is written to disk — with one
scene per argument, in the order given, and **plays through once** (no loop;
`--loop` to repeat).

```bash
# A video, then a SID, then a slideshow of a folder of pictures:
scripts/c64cast.sh -u u64://192.168.2.64 clip.mp4 tune.sid assets/pictures/

# Same, but driving a TeensyROM+ over auto-detected USB serial:
scripts/c64cast.sh -u tr:// clip.mp4 tune.sid

# Direct play a YouTube URL (needs the `yt` extra: `uv sync --extra yt`):
scripts/c64cast.sh -u u64://192.168.2.64 'https://youtu.be/dQw4w9WgXcQ'

# A URL timestamp (?t= / &start= / #t=) starts playback at that offset:
scripts/c64cast.sh -u u64://192.168.2.64 'https://youtu.be/dQw4w9WgXcQ?t=1m30s'
```

Each argument is mapped to a scene type:

| Argument                | Scene type   |
|-------------------------|--------------|
| video (`.mp4`, `.mkv` …) | `video` |
| `.sid`                  | `waveform`   |
| image (`.jpg`, `.png` …) | `slideshow`  |
| `.prg` / `.crt`         | `launcher`   |
| directory or glob       | inferred from the contents (a single kind); the spec is passed through, so the scene random-picks at setup |
| URL                     | `video` (direct media plays as-is; YouTube/others resolved by yt-dlp). A `?t=`/`&start=`/`#t=` timestamp (`90`, `90s`, `1m30s`, `1h2m3s`) seeks playback to that offset |

Audio is **on by default** — pass `--no-audio` to mute. Flags:
`-u/--url`, `-s/--system`, `--display MODE` (default `mhires` for video/slideshow),
`-t/--duration S` (for scenes that honor it — waveform/slideshow), `--loop`,
`--skip-probe`, `-v`/`-vv`. Audio-only files (mp3/wav over a test pattern) are
recognized but not yet supported.

## Creating a config

Three ways to get a working `c64cast.toml`, easiest first:

1. **The interactive wizard** (`--init`) — needs the `wizard` extra
   (`uv sync --extra wizard`, or `pip install c64cast[wizard]`):

   ```bash
   python -m c64cast --init                 # writes ./c64cast.toml
   python -m c64cast --init my-stream.toml   # writes a named file
   ```

   It first asks whether to build a **single scene** or a **multi-scene
   playlist**, then walks you through the rest:

   * **Single scene** — pick a scene type, then a display mode, an asset (it
     scans `assets/` and offers the files it finds), optional overlays (only
     the ones compatible with your chosen display are offered), and the
     essential globals (U64 URL, NTSC/PAL, audio). The result runs in
     single-scene loop mode — the same shape as every file in
     `config/examples/`.
   * **Multi-scene playlist** — choose audio once for the whole playlist, then
     add / remove / reorder scenes in a small management loop (each scene is
     questioned exactly like the single-scene flow, with a per-scene "mute
     this scene?" option when global audio is on). After the scene list you're
     asked the playlist options: loop-after-last-scene, optional video
     interleaving (`interleave_videos` + `videos_dir`), and an optional pass to
     customize the "UP NEXT" `[interstitial]`. The output is N `[[scenes]]`
     plus `[playlist]`/`[interstitial]`, run in order with the interstitial
     between scenes.

   Every choice and default comes from the same metadata `--describe` reads,
   so the wizard can't offer something the loader would reject. Either mode
   previews the generated TOML, validates it (collecting one message per
   invalid scene in playlist mode), writes it (with a `#:schema` directive for
   editor autocomplete), and offers to launch it straight away.

2. **Copy the annotated reference** and edit it:

   ```bash
   cp config/c64cast.example.toml c64cast.toml && $EDITOR c64cast.toml
   ```

   `c64cast.example.toml` documents every section and field inline.

3. **Start from a feature demo** in `config/examples/` (one per scene type
   and per overlay) and tweak:

   ```bash
   python -m c64cast --config config/examples/scene-waveform.toml
   ```

Whichever you pick, validate it with `--doctor` (below) before going live.

## Discovering the config surface

You don't have to cross-reference this doc or the source to find out what
options exist. The CLI introspects the code directly (no config file or
hardware needed) — every option, default, valid value, and compatibility
rule is generated from the same metadata the program runs on, so it can't
go stale.

```bash
python -m c64cast --list-scenes        # the 7 scene types
python -m c64cast --list-overlays      # the 12 overlays + their restrictions
python -m c64cast --list-modes         # the display modes

# Full reference for one thing — options, types, defaults, valid values:
python -m c64cast --describe overlay:clock
python -m c64cast --describe scene:waveform
python -m c64cast --describe section:audio
python -m c64cast --describe mode:mhires
# The prefix is optional when the name is unambiguous: `--describe clock`.

# Which overlay works on which display mode (✓ / ·):
python -m c64cast --compat
```

### Editor autocomplete (JSON schema)

`python -m c64cast --print-schema` emits a JSON Schema for the whole TOML.
The repo ships a committed copy at
[`c64cast.schema.json`](../c64cast.schema.json); point a TOML-aware
editor at it with a directive on the **first line** of your config:

```toml
#:schema ./c64cast.schema.json
```

With Taplo (the VS Code "Even Better TOML" / JetBrains TOML plugins) you
then get key + value completion, hover docs, inline defaults, and red
squiggles on typos and bad enum values **as you type**. Every config in
`config/examples/` and `c64cast.example.toml` already carries the
directive (adjust the relative path for where your config lives). Maintainers
regenerate the committed schema with `make schema` after changing any config
field; CI fails if it drifts.

## Validating a config

`--doctor` runs every per-scene / per-overlay / per-orchestrator
validation check **without** starting the stream, then prints a grouped
report and exits. Use it to catch typos before powering on the U64, or
to find out which optional install extras are missing.

```bash
# Full check: config + extras + ping every system's U64 over DMA
python -m c64cast --doctor --config c64cast.toml

# Same, but skip the U64 connectivity probe (no hardware needed)
python -m c64cast --doctor --config c64cast.toml --skip-probe

# Works on ensemble configs too — each system is validated independently
python -m c64cast --doctor --config config/examples/ensemble/master.toml
```

Doctor never aborts on the first error: a bad scene 1 won't hide a bad
scene 5. Exit code is `0` when every check is `ok` or `warn`, `1` when
any `error` row is present (so it's safe to gate CI pipelines on).

What it surfaces:

* **scene** — unknown display modes, unresolvable `file =` specs on
  video / waveform scenes (bad globs, empty directories, wrong
  extensions), missing required fields (`midi.midi_adsr` length),
  overlay/display-mode incompatibilities (e.g. a PETSCII-only overlay
  attached to an mhires scene).
* **orchestrator** — scenes with `orchestrate = true` whose shape no
  registered subclass claims; ensemble conductors with no same-name
  follower in every other system (warn-level: the Playlist will fall
  back to the conductor's cfg, but rarely by design).
* **extras** — per-extra `[mic, video, preview, control, obs,
  midi, logging]` install status; warn rows include the exact
  `pip install c64cast[<name>]` command.
* **connectivity** — per-system DMA + REST reach to the Ultimate 64. The
  DMA-service-disabled error includes the F2-menu hint.

## Example configs

The recommended first run is
[`config/examples/hello.toml`](../config/examples/hello.toml) — a big-text
scroller on a solid canvas that needs **nothing but a reachable U64** (no
webcam, mic, SID, or video files, no optional extras). Edit the
`[ultimate64].url` at the top and run it:

```bash
python -m c64cast --config config/examples/hello.toml
```

The annotated [`c64cast.example.toml`](../config/c64cast.example.toml) is a
heavily-annotated reference that exercises every scene type and overlay
in one file — useful as documentation but unwieldy as a starting point.

For runnable single-feature demos, see
[`config/examples/`](../config/examples/) — one TOML per scene type and
per overlay. Pick the one you want to see and run it:

```bash
python -m c64cast --config config/examples/overlay-clock.toml
```

Each demo runs in **single-scene mode** (see below): no interstitial, no
CTRL skip, the one scene loops until you Ctrl+C.

## Single-scene mode

When the loaded config defines exactly one scene, the Playlist enters
single-scene mode automatically (no config flag — it's auto-detected
from `len(scenes) == 1`):

* The interstitial scene is never built or shown.
* On `is_done` (e.g. `duration_s` expires, or the scene's `process_frame`
  returns False), the scene tears down and sets up again so it loops
  forever. Works for every scene type — webcam re-opens the camera,
  video re-opens the file, waveform restarts the SID.
* CTRL skip events are ignored (with a debug log line); the event is
  still cleared so it doesn't accumulate.
* C= pause/resume still works — useful for blanking the display
  mid-demo.
* SHIFT cycle still works — the cycled style persists across the
  loop's teardown+setup iterations because the display_mode instance
  survives the loop.
* `[playlist] interleave_videos` is short-circuited when the user-defined
  playlist is a single scene (an inserted video would promote the playlist
  to 2 scenes and silently defeat the mode).

## Config file

Discovery: `--config PATH` wins; else `./c64cast.toml` is loaded if it
exists; else built-in defaults apply. See
[c64cast.example.toml](../config/c64cast.example.toml) for a fully-annotated
reference; the sections below summarize each.

### `[ultimate64]`

```toml
[ultimate64]
url = "http://ultimate-64-ii.lan"   # bare hostname or IP works too
system = "NTSC"                     # NTSC | PAL (affects fps, frame_time, cycles)
dma_port = 64                       # Ultimate DMA Service TCP port
# dma_password = ""                 # prefer C64CAST_DMA_PASSWORD env var
```

See the [prerequisite section](#prerequisite-enable-socket-dma-on-the-u64)
above for how to enable the DMA service on the U64.

### `[video]`

```toml
[video]
device = -1                         # -1 = system default camera; `--list-devices` to enumerate
```

### `[audio]`

```toml
[audio]
enabled = true                      # on by default; --no-audio mutes
device = -1                         # sounddevice input index; -1 = system default
sample_rate = 11600                 # 4-bit $D418 DAC rate; HW ceiling ~13.6k NTSC/~13.1k PAL
backend = "auto"                    # video audio: "auto" (U64 Ultimate Audio FPGA
                                    #   sampler when available, else DAC), "dac"
                                    #   (lo-fi 4-bit $D418, all backends), "sampler"
                                    #   (force the hi-fi FPGA PCM sampler)
sampler_sample_rate = 44100         # sampler backend rate (1000..48000); CD quality
sampler_bits = 16                   # sampler PCM depth: 8 (signed) or 16 (signed LE)
mic_sensitivity = 1.5               # pre-DAC gain
noise_gate = 0.05                   # below this RMS, sample is silenced
```

On the Ultimate 64, `backend = "auto"` plays a video's soundtrack through the
**Ultimate Audio FPGA PCM sampler** — far higher fidelity than the 4-bit DAC
and entirely off the C64 bus. See "High-fidelity video audio" in
[caveats.md](caveats.md). Mic/webcam audio always uses the 4-bit DAC.

### `[interstitial]`

```toml
[interstitial]
duration_s = 4.0
text_color = "rainbow"              # C64 color name, "rainbow" (per-row), "random"
background = "random"               # starfield|petscii_bars|raster_bars|checker|nature|city|none|random
```

### `[playlist]`

```toml
[playlist]
videos_dir = "assets/videos"
interleave_videos = true
loop = true                                              # false = exit after one pass
songlengths_file = "assets/sids/C64Music/DOCUMENTS/Songlengths.md5"  # optional, for waveform scenes
```

When `interleave_videos` is true and `videos_dir` contains any video files (and
PyAV is installed), a video scene is inserted between every pair of
non-video scenes. The video file rotates round-robin per insertion.

`loop` (also `--loop` / `--no-loop` on the CLI) controls what happens
after the last scene finishes. Default `true` restarts from scene 1 —
in single-scene mode, the one scene loops back-to-back forever. Set to
`false` to exit the streamer cleanly after one full pass; the common
use case is "play one video and quit":

```bash
python -m c64cast --config one-video.toml --no-loop
```

### `[preview]`

```toml
[preview]
enabled = false                     # requires the `preview` extra
fps = 30
scale = 3                           # window pixels per C64 pixel
charset_path = "assets/roms/characters.901225-01.bin"
```

### `[recording]`

```toml
[recording]
enabled = false                     # cv2.VideoWriter; uses opencv-python (already a hard dep)
path = "recording.mp4"
fps = 30
scale = 2
fourcc = "mp4v"
```

### `[control]`

```toml
[control]
enabled = false                     # requires the `control` extra
host = "127.0.0.1"
port = 8765
```

HTTP endpoints: `POST /pause`, `POST /resume`, `POST /skip`,
`POST /reload`. Same surface as the keyboard poller (C= and CTRL keys).

### `[menu]`

```toml
[menu]
enabled = false                     # SPACE on the C64 keyboard opens the menu
prompt_to_save = true               # offer to write changes back to the config
```

The on-C64 menu: press **SPACE** on the real keyboard to open an on-screen
panel of context-sensitive knobs for the running scene (display mode,
palette mode, style, scope settings, …). Cursor keys navigate and RETURN
selects; SPACE closes it. While the menu is open the C= / CTRL / SHIFT
controls are suspended. Needs a read-capable backend — the Ultimate, or a
cycle-clean TeensyROM+ (fw v0.7.2.5+, which added ReadC64Mem) — since the
kernal keyboard buffer is polled, like the modifier keys. On exit with unsaved
changes,
`prompt_to_save = true` offers to persist them back to the source config
file; `false` applies to the running scene only (handy for demos). Text
parameters are shown read-only — text entry is a deferred follow-up.

### `[vision]`

```toml
[vision]
enabled = false                     # webcam hand-gesture control (needs `vision` extra)
model_path = "assets/models/hand_landmarker.task"
```

A second control surface alongside the keyboard: a webcam + MediaPipe
HandLandmarker maps hand gestures to the same pause/resume/skip/cycle
events. **Running:** pinch = pause, fast horizontal swipe = skip, open
hand = cycle. **Paused:** hold a pinch for `hold_threshold_s` to resume.
Because it watches the camera (not C64 memory) it works on any backend.
Needs the `vision` extra (`mediapipe`) plus a downloaded HandLandmarker
`.task` model (see `assets/models/README.md`); a missing dep/model logs
"vision control disabled" and the stream runs without it. Sensitivity
knobs (`pinch_threshold`, `swipe_velocity`, `gesture_dwell_s`,
`gesture_cooldown_s`, …) are in `--describe section:vision`.

### `[debug]`

```toml
[debug]
verbose = 0                         # 0 info (default), 1+ debug (-v)
heartbeat = 10.0                    # seconds between heartbeat log lines
skip_probe = false                  # skip the initial U64 reachability ping
profile = false                     # per-frame profiling (see below)
profile_interval = 10.0             # seconds between profiler summary lines
```

#### Per-frame profiling

`--profile` (or `profile = true` in `[debug]`) enables a lightweight per-scene
frame timer. Every `profile_interval` seconds the playlist logs one line per
active scene, e.g.:

```text
profile[webcam:mcm] n=58 | frame avg=33.4 p50=33.3 p95=34.1 max=41.2 ms |
  cpu_render avg=12.8 p50=12.6 p95=15.0 max=23.1 ms | compose avg=5.4 ... |
  overlay_compose avg=0.3 ... | push avg=7.0 ... | wait avg=20.5 ... |
  writes/frame avg=24 p95=27 | bytes/frame avg=8192 p95=8192
u64 dma latency: n=256 avg=5.1 p50=4.9 p95=7.8 max=18.4 ms
```

How to read it:

* `frame` — total wall-clock per frame (sleep + work). At steady state this
  should sit near `1 / target_fps`.
* `cpu_render` — time inside `Scene.process_frame` and the overlay
  `process_frame` loop. Sum of `compose + overlay_compose + push` plus any
  scene-specific work that runs outside `_render_with_overlays`.
* `compose` / `overlay_compose` / `push` — the three sub-stages of the
  compose-based render path (PETSCII / MCM / Hires / MultiHires modes).
  Bitmap modes that bypass `compose()` emit a single `render` stage instead.
* `wait` — time spent asleep waiting for the next frame deadline. High
  `wait` + low `cpu_render` = pipeline is idle and CPU work isn't the
  bottleneck.
* `writes/frame`, `bytes/frame` — DMA volume per frame.
* `u64 dma latency` — rolling sendall round-trip on the DMA socket
  (~5 ms is typical; persistent spikes mean the network or U64 is
  congested).

The profiler is off by default and has zero overhead when off (every hook
resolves to a no-op).

## Scenes

Each `[[scenes]]` block is a scene; they run in declaration order and
each plays for `duration_s` seconds, then advances. Common fields:

* `type` — `"webcam"`, `"video"`, `"slideshow"`, `"waveform"`,
  `"midi"`, `"blank"`, `"launcher"`, or `"generative"`
* `duration_s` — seconds before advancing. Rejected on `video`
  scenes — they run until the video file ends.
* `name` — display name (shown in the interstitial). Optional.
* `target_fps` — per-scene FPS cap. Default: system rate (60 NTSC /
  50 PAL), with two groups defaulting lower to stay under the DMA
  bus-halt ceiling (an explicit `target_fps` always wins):
  * **Bitmap (hires/mhires) frame-pushing scenes** — `video`, live
    `webcam`, and `generative` with `audio_source = "mic"` — cap at
    **20 fps** while streaming digitized audio and at half rate
    (30 NTSC / 25 PAL) when muted. **Video on the U64 Ultimate Audio
    sampler is the exception:** that audio is off the C64 bus, so it
    uncaps to the full system rate (60/50) — which, via frame dedup,
    plays back at the source video's own fps. Char modes (petscii/blank)
    stay at the system rate. See "Bitmap video/webcam scenes default
    lower when digitized audio streams" in [caveats.md](caveats.md).
  * **`waveform` and `midi`** default to half rate (30 NTSC / 25 PAL) —
    see "WaveformScene defaults to half the video rate" in
    [caveats.md](caveats.md).

If a scene's `duration_s` expires while an overlay reports itself busy
(e.g. a `big_text` message still mid-scroll), the Playlist defers the
transition until the overlay clears. CTRL skip always cuts through.

### `type = "webcam"`

```toml
[[scenes]]
type = "webcam"
display = "hires_edges"             # hires_edges | hires | petscii | mhires | mcm
name = "Live Hi-Res Edges"
duration_s = 45.0
```

Display modes:

| Mode          | What it is                                                    |
|---------------|---------------------------------------------------------------|
| `hires`       | 320×200 monochrome-per-cell bitmap. Slow but sharp.           |
| `hires_edges` | `hires` after `cv2.Canny` — feels live even with stale frames |
| `mhires`      | Multicolor hi-res bitmap. Half the horizontal resolution, 4 colors per cell. |
| `petscii`     | 40×25 PETSCII text rendering — fast, atmospheric                 |
| `mcm`         | 40×25 multicolor text — three FG colors + global BG per cell    |
| `blank`       | 40×25 PETSCII char mode with no video — pure canvas for overlays |

The two palette-mapping modes (`mcm` and `mhires`) also accept:

```toml
palette_mode = "percell"              # percell (default) | cheap | vivid | grayscale
```

* `percell` — `mhires` only: picks `bg0` globally (most-populated palette
  index, EMA-smoothed) then for every 4×8 cell picks its own top-3
  non-`bg0` colors by population. VIC-II MCBM lets `c1`/`c2`/`c3` vary
  per-cell via screen RAM + color RAM, so a frame carries up to
  `bg0 + 3×1000 = 3001` distinct colors instead of the global 4 the
  older modes assumed. Webcam/video content gains substantially:
  cells without `bg0` stop wasting one of their 4 slots on it, and cells
  in unrelated regions of the frame stop being forced to share a 4-color
  set tuned for the dominant subject. For `mcm`, `percell` is accepted
  as an alias for `cheap` (MCM already picks the FG per cell).
* `cheap` — legacy global-4. HSV saturation boost (×1.8) before
  quantization + gray-axis penalty on the per-pixel argmin. Global color
  slots are picked by raw pixel-frequency. The effective default for `mcm`
  (which aliases `percell` to `cheap`), and useful for `mhires` only if you
  specifically want the older look or to compare.
* `vivid` — global-4 with same biases as `cheap`, plus the 3 (`mcm`) /
  4 (`mhires`) slots are picked for hue-diversity rather than frequency.
  The single most-populated palette entry always wins slot 0; subsequent
  slots prefer the most-populated remaining entry whose hue is at least
  45° from already-chosen chromatic picks. Useful when the frame keeps
  reducing to two or three near-shades — for `mhires`, `percell` covers
  this case better and is usually a better choice.
* `grayscale` — restricts every per-pixel argmin to the 5 gray-axis
  palette entries for an "old TV broadcast" aesthetic. Global color
  slots are **fixed** in luminance order (not picked per frame): `mhires`
  uses black + dark gray + gray + light gray (white is dropped for better
  mid-tone resolution); `mcm` uses the three mid-grays as bgs with FG
  resolving to black/white for full 5-level coverage. Fixing the slots
  is the perf path — adaptive picking from only 5 entries shuffled the
  slot order on every frame, which busted the bitmap delta cache and
  forced full re-uploads at ~13 fps.

`palette_mode` only allocates the per-cell color slots. **Color shaping** —
nudging source colors toward the C64 gamut before quantization — is the
separate global `[color]` section, applied to `mcm`, `mhires`, and `petscii`
regardless of `palette_mode`:

```toml
[color]
channel_boost = [1.3, 1.2, 1.0]         # per-channel gain [blue, green, red] (BGR)
# [[color.hue_corrections]] bands tune any hue; the built-in default rescues
# dark violets to C64 purple (the one true gap in the 16-color palette).
hue_corrections_replace_defaults = false
auto_fit = true                         # per-source adaptive contrast/saturation fit
auto_fit_strength = 1.0                 # 0..1 (0 = off)
```

The C64's only purple (index 4) is a bright magenta, so dark real-world
violets quantize to gray/blue and never to purple. The built-in
`purple_rescue` hue band fixes that; add your own bands to tune other hues, or
set `hue_corrections_replace_defaults = true` with no bands to disable it.

`channel_boost` and `hue_corrections` are static (the same for every source).
`auto_fit` (on by default) is the **adaptive** counterpart: for `video`
and `slideshow` scenes it pre-scans the source and stretches its contrast +
saturation to fill the C64 gamut, so dark/flat content stops looking muddy and
monochromatic. It's faithful (hue preserved) and do-no-harm (well-exposed
sources are left unchanged); webcam scenes ignore it (no pre-scan). Dial it down
or off with `auto_fit_strength` (`0.0` = off). See
[caveats.md](caveats.md) → "`[color].auto_fit`".

The `petscii` mode accepts a `style` field that picks one of several
glyph + color policies (SHIFT cycles through them at runtime):

```toml
style = "default"                       # default | halftone | random_glyph
                                        # | letter_rain | neon | inverse_pop
                                        # | hatch | color_only | random
```

* `default` — luma → 11-char ramp (space → dots → … → full block) +
  nearest-palette color per cell. Informative, low-key "live feed" look.
* `halftone` — 5-level block-coverage ramp (space → ¼ → ½ → ¾ → full).
  Chunky, high-contrast geometric.
* `random_glyph` — distinctive graphics glyph per cell, stable across
  frames so it doesn't strobe; color still tracks the video. "Alien
  text" — the grid feels alive but content is purely abstract.
* `letter_rain` — luma → A-Z (screen codes 0x01-0x1A). Matrix cascade.
* `neon` — default char ramp, but color RAM is clamped to the 10
  chromatic palette entries (no grays/whites). 80s-arcade saturated.
* `inverse_pop` — every cell is either space or full block by luma
  threshold; FG limited to a 4-color pop-art palette (white, light
  red, cyan, yellow). Black border + black background.
* `hatch` — luma → 5-level cross-hatch shading (space → / → \ → X →
  full). Sketchy, line-art look.
* `color_only` — every cell is a full block; the image lives entirely
  in color RAM. Pure 40×25 color blocks — a Mondrian of the source.
* `random` — at scene setup, pick one of the above at random. SHIFT
  cycling then proceeds from that pick (the random sentinel is
  one-shot, not reapplied per cycle).

Each concrete style declares its own preferred border + background
(both default to black); the mode pokes them on setup and on every
SHIFT cycle.

### `type = "video"`

```toml
[[scenes]]
type = "video"
file = "assets/videos/cool-video.mp4"  # see "file spec" below
display = "hires_edges"             # any display mode
```

Requires the `video` extra. Audio is fed through the SID DAC; video
is keyed off the audio-master clock so drift can't accumulate.

**`file =` spec** — accepts a comma-separated list of any of: literal
file paths, directories, or `glob`-style patterns. Each scene `setup()`
re-resolves the spec and picks one random match from the pool, so a
directory's contents rotate naturally across single-scene loops. A
single literal path stays deterministic. Examples:

```toml
file = "assets/videos/promo.mp4"                 # one fixed file
file = "assets/videos"                           # any file in the dir
file = "assets/videos/*.mkv"                     # glob
file = "assets/videos, assets/extra/intro.mp4"   # union of all
```

If `file =` is omitted entirely, the scene falls back to scanning the
default directory `assets/videos/`. Recognised extensions:
`.mp4 .avi .mkv .mov .webm .m4v`.

**URLs** — `file =` may also be a single media URL, resolved when the config
loads (the same path the `c64cast MEDIA…` shortcut uses): a direct link plays
as-is (PyAV opens http(s)), and a YouTube/etc. page is resolved by yt-dlp (needs
the `yt` extra: `uv sync --extra yt`). A `?t=`/`&start=`/`#t=` timestamp on the
URL fills `start_s` automatically (an explicit `start_s` wins). If the `yt`
extra is missing, `--doctor` and config load flag it up front.

```toml
file = "https://youtu.be/<id>?t=18m18s"   # starts at 18:18
```

**`start_s =`** — seconds into the source to begin playback (video-only; omit or
`0` = from the start). Seeks to the keyframe at/just-before that time; accuracy
is keyframe-granular.

The scene's lifetime is video-driven — it runs until the file's last frame
is decoded, then the playlist advances. `duration_s` is **rejected** by
the config loader on video scenes: a finite value would silently
either truncate a long clip or do nothing on a short one. To loop a single
video as the entire playlist (single-scene mode), simply omit any
other `[[scenes]]` entries; the playlist tears the scene down at EOF and
immediately restarts it. To play a single video **once** and exit,
set `[playlist] loop = false` (or pass `--no-loop`).

On scene setup the file's audio stream is scanned end-to-end to find its
peak amplitude, and every pushed sample is scaled to bring that peak to
~90% of full scale (capped at +24 dB so a near-silent file isn't amplified
into noise). The 4-bit SID DAC has no usable dynamic range for sources
peaking below ~30% of int16 full scale, so without this normalization a
quiet clip plays as silence-with-clicks. The pre-scan adds <1 s to scene
setup, hidden under the interstitial in normal playlists. There's no TOML
knob — the heuristic is fixed.

### `type = "waveform"`

```toml
[[scenes]]
type = "waveform"
file = "assets/sids/MyFavorite.sid"  # see "file spec" below
song = 0                            # 0 = use SID's default subtune; otherwise 1-based
duration_s = 180.0                  # omit to let Songlengths.md5 decide
target_fps = 30.0                   # default = HALF system rate (30 NTSC / 25 PAL); see caveats.md
color_mode = "per_voice"            # per_voice | per_waveform
voice_colors = ["cyan", "yellow", "light green"]
```

**`file =` spec** — same grammar as the `video` scene: a
comma-separated list of literal paths, directories, and/or globs. Each
scene `setup()` picks a random `.sid` from the resolved pool. Candidates
that fail validation (PSID payload would clobber the visualizer's hires
bitmap area, song-out-of-range, etc.) are skipped at pick time with a
warn log; bounded retries before the scene aborts. Examples:

```toml
file = "assets/sids/MyFavorite.sid"             # one fixed tune
file = "assets/sids"                            # whole directory
file = "assets/sids/C64Music/MUSICIANS/G/Galway_Martin/*"
file = "assets/sids/Tune1.sid, assets/sids/Tune2.sid"
```

Single-file pools keep `cycle_style` (SHIFT) mutations across loop
iterations. Multi-file pools reset to the SID's `start_song` on each
re-pick, which is the natural shape — a fresh tune restarts from its
designated opener.

If `file =` is omitted entirely, the scene falls back to scanning the
default directory `assets/sids/`.

PSID-only — RSIDs and SIDs whose `load_addr` is below `$0820` are rejected
at scene start. See [caveats.md](caveats.md) for the player-PRG design and
its PAL/NTSC speed limitation.

`per_waveform` mode also accepts an explicit table:

```toml
[scenes.waveform_colors]
triangle = "light green"
sawtooth = "light red"
pulse    = "cyan"
noise    = "yellow"
off      = "dark gray"
```

**SHIFT cycles the subtune** on multi-song SIDs. The scene unhooks its
IRQ, silences the SID, rebuilds the host emulator on the new song,
re-runs the player PRG, and resets the duration timer so the new song
gets its full length (a SongLengths re-lookup updates `duration_s` when
the DB is loaded; explicit `duration_s` wins as it does at startup).
Single-song SIDs ignore SHIFT. The cycle log line looks like
`cycle: 'SID: foo #1' → scene=song 2/5`.

When the SongLengths DB is loaded, cycle also **skips subtunes shorter
than `WaveformScene.MIN_CYCLE_SUBTUNE_S` (5 s)** — most game SIDs carry
1-3 second SFX as their tail subtunes, and the scope view of those is
flat for most of the displayed time. The skip is bounded at `n-1`
attempts so a SID that's entirely short SFX still advances by one slot
on each press. Skip only applies on SHIFT: scene **startup** plays the
configured `song` (or PSID `start_song`) regardless of length — pinning
a short SFX as the start song is a strong "play this" signal. An
explicit `duration_s` also disables skip (treated the same way).

### `type = "midi"`

```toml
[[scenes]]
type = "midi"
midi_port = ""                      # "" = first available port; substring match also works
midi_waveform = "pulse"             # default waveform: triangle | sawtooth | pulse | noise
midi_voice_waveforms = ["pulse", "sawtooth", "triangle"]  # per-voice; '+' combines
midi_voice_mode = "shared"          # shared (1 channel → 3 voices) | multitimbral
midi_voice_channels = [1, 2, 3]     # multitimbral: MIDI channels for voices 1/2/3
midi_program_change = true          # honor Program Change → waveform select
midi_adsr = [0, 8, 12, 8]           # attack / decay / sustain / release, each 0..15
midi_pulse_width = 2048             # 0..4095 (SID 12-bit PW)
midi_filter_cutoff = 1024           # 0..2047
midi_filter_mode = "lowpass"        # lowpass | bandpass | highpass
midi_master_volume = 15             # 0..15
voice_colors = ["light green", "cyan", "yellow"]
duration_s = 120.0
```

Requires the `midi` extra (`pip install -e .[midi]`). Voices are visualized the
same way the waveform scene does — per-voice traces colored by `voice_colors`
(or by waveform with `color_mode = "per_waveform"`).

**Per-voice waveforms.** Each of the three voices can run its own waveform, and
an entry may be a `+`-combo (e.g. `"pulse+triangle"`) for the SID's combined
waveform — the audience hears the real chip's combined wave, and the scope draws
a bitwise-AND approximation of its (sparse, "metallic") shape. Set the starting
waveforms with `midi_voice_waveforms` (≤3, padded by repeating the last; empty =
every voice uses `midi_waveform`). **SHIFT** advances every voice one step
through the waveform cycle — the four single waveforms, then `pulse+triangle` —
keeping per-voice offsets. **MIDI Program Change** selects a waveform live
(disable with `midi_program_change = false`).

> **Combined-waveform caveat.** On a real 6581 the waveform outputs share a bus
> and AND together, and any combination containing **sawtooth** ANDs down to
> near-silence (`pulse+triangle` is the one combination that reliably sounds).
> So the interactive SHIFT/Program-Change rotation only includes `pulse+triangle`;
> saw/noise combos are still settable via `midi_voice_waveforms` for
> experimentation (and may behave differently on an 8580), but expect them quiet
> on a 6581. The scope's AND trace currently *over*-shows those — a faithful
> chip-modeled trace is a future refinement.

**Voice allocation.** In the default `shared` mode one MIDI channel is spread
across all three voices: held notes keep their voice (a polyphonic pad), and a
new note that needs a voice when all three are gated steals the *most-recently
-started* one (so the held pad survives and an overlapping melody cycles on the
top voice). With `midi_voice_mode = "multitimbral"`, MIDI channels route to
fixed voices (`midi_voice_channels`, default channels 1/2/3 → voices 1/2/3),
each voice monophonic with last-note priority; notes on unmapped channels are
ignored. In multitimbral mode Program Change targets only the message's channel.

### `type = "blank"`

```toml
[[scenes]]
type = "blank"
display = "blank"                   # "blank" (or omit — the default is accepted too)
name = "Title Card"
duration_s = 12.0
border = 0                          # 0..15 palette index for the border
background = 6                      # 0..15 palette index for the canvas
```

No video input — every cell starts as `SC_SPACE` with FG = `background`,
so the canvas looks solid until an overlay paints. Pairs naturally with
the `big_text` overlay for demo-scene-style title cards. Every overlay
that works on `petscii` also works on `blank` (the `is_petscii_compatible`
flag whitelists both modes).

### `type = "slideshow"`

```toml
[[scenes]]
type = "slideshow"
display = "mhires"                  # mhires | hires | mcm | petscii | random
file = "assets/pictures"            # see "file spec" below
duration_s = 60.0                   # total scene runtime (default 30 s)
image_duration_s = 5.0             # per-image display time before advancing
```

Cycles through still images for the scene's `duration_s`. Each image
shows for `image_duration_s` (independent of `duration_s`, which controls
total runtime). The picker is shuffle-and-walk: every image plays once
before any repeats, and no image appears twice back-to-back across
reshuffle boundaries.

**`file =` spec** — same grammar as `video` / `waveform`: a
comma-separated list of literal paths, directories, and/or globs. Omitted
entirely, it falls back to the default directory `assets/pictures/`.
Loaded via `cv2.imread`, so any format OpenCV decodes works
(`.jpg .jpeg .png .bmp .webp`).

```toml
file = "assets/pictures/photo.jpg"               # one fixed image
file = "assets/pictures"                          # whole directory
file = "assets/pictures/*.png"                    # glob
file = "assets/pictures, assets/extra/*.jpg"      # union of all
```

Display notes: `display = "random"` resolves to a fresh mode at every
`setup()` (so single-scene loops vary per iteration), and
`display = "hires_edges"` is substituted with `mhires` — the
`hires_edges` default is tuned for live webcam Canny edges, not photos
(use `display = "hires"` for a plain monochrome bitmap). No audio.

### `type = "launcher"`

```toml
[[scenes]]
type = "launcher"
file = "assets/programs"            # see "file spec" below (.prg / .crt)
duration_s = 120.0                  # idle timeout (see below), not a hard cap
input_source = "cia"               # cia | kernal | auto | none
min_duration_s = 0.0               # floor before the idle timeout can advance
# max_duration_s = 600.0           # optional hard ceiling (unset = no cap)
reset_before_launch = true         # reset the U64 for a clean machine state
```

Launches a native C64 program and hands the machine over to it. The
U64 is reset (when `reset_before_launch`), then the file is uploaded and
run — a `.prg` via firmware `run_prg`, a `.crt` cartridge via `run_crt`,
chosen by extension. Once launched the program owns the VIC, SID, and
CIAs; c64cast stops painting and only polls for player input.

**Duration model:** `duration_s` is an *idle timeout*, not a fixed
runtime. It counts down from launch and resets whenever the player
provides input, so an actively-played game stays up while an untouched
demo advances after the full `duration_s`. `min_duration_s` is a floor
(the scene won't advance before it elapses, even if idle); the optional
`max_duration_s` is a hard ceiling (advance regardless of input).

**`input_source`** selects what counts as player activity (the
pause/skip/cycle modifier keys at `$028D` are deliberately excluded):

* `cia` — CIA1 `$DC00/$DC01` joystick bits. Works regardless of whether
  the program keeps the kernal IRQ, but can race the program's own
  keyboard scan (best-effort).
* `kernal` — kernal scratch `$00C5/$00C6`; clean, but only live while the
  kernal IRQ runs (BASIC games / kernal-friendly demos).
* `auto` — both signals OR'd together.
* `none` — no input polling; pure `duration_s` timer (for demos).

**`file =` spec** — same grammar as the other scenes (paths / dirs /
globs, default dir `assets/programs/`, extensions `.prg .crt`). In
ensemble mode `bypass_audio_lock = true` lets several launcher systems
run interactive programs at once, each player hearing their own SID.

### `type = "generative"`

```toml
[[scenes]]
type = "generative"
display = "mhires"                  # any quantizing mode (not blank/random)
duration_s = 60.0
source = "plasma"                   # plasma | tunnel | fire
audio_source = "mic"               # none | mic | sid
reactive = true                     # music drives the visuals (sid only)
effect = "trails"                  # optional: trails | pulse | rgb_shift
# file = "assets/sids/Tune.sid"     # required when audio_source = "sid"
```

A procedural scene composed from three orthogonal choices — a **frame
source**, an **audio source**, and an optional pixel **effect** —
rendered through any quantizing display mode. The generators are pure
numpy and deterministic in time:

* `source` — `plasma`, `tunnel`, or `fire`.
* `audio_source` — `none` (silent), `mic` (live mic through the SID DAC;
  needs `[audio] enabled = true` + the `mic` extra), or `sid` (play the
  `file` `.sid` on the real chip). A `sid` source forces a host-DMA
  display and pairs most robustly with a **char** display (`mcm` /
  `petscii`); a bitmap display works only with a tune that loads high
  enough to clear `$2000`.
* `reactive` (default `true`) — when `audio_source = "sid"`, a host-side
  SID emulator extracts BPM / onsets / per-voice features (no extra U64
  traffic) and the generator reacts: BPM cycles the colors, transients
  pulse them. Inert for `mic`/`none`. Set `false` for the pure
  time-driven look.
* `effect` — a pre-quantization pixel transform applied to any
  frame-bearing scene: `trails` (motion echo), `pulse` (beat-punch
  zoom), `rgb_shift` (channel separation on a transient). `pulse` and
  `rgb_shift` only visibly react on a music-reactive (`sid` + reactive)
  scene; elsewhere they're inert.

`song`, `palette_mode`, `style` (petscii), and the `target_fps` bitmap
caps above all apply.

## Overlays

Overlays are stackable decorations that attach to a scene via
`[[scenes.overlays]]` arrays. Order matters — later overlays paint on top
of earlier ones in overlapping cells. The full overlay catalog is in
[CLAUDE.md](../CLAUDE.md#overlays); the most-used options:

```toml
[[scenes.overlays]]
type = "scrolling_text"
row = 24
speed_cells_per_s = 6.0
messages = [
  { text = "WELCOME TO VCFSW 2026", color = "yellow" },
  { text = "CCUG MEETUP @ 6PM",     color = "cyan",   pause_time_s = 2.0 },
  { text = "73",                    color = "white",  style = "static", pre_delay_s = 0.5 },
]

[[scenes.overlays]]
type = "marquee"
text = "C64CAST // PRESS COMMODORE KEY TO PAUSE // PRESS CTRL TO SKIP"
row = 0
speed_cells_per_s = 3.0
fg_color = "yellow"

[[scenes.overlays]]
type = "rss"
url = "https://news.ycombinator.com/rss"
row = 1
max_items = 8
refresh_minutes = 15

[[scenes.overlays]]
type = "spectrum_petscii"
placement = "center"                # bottom | center | split
height_rows = 10
gain = 1.0

[[scenes.overlays]]
type = "clock"
corner = "top-right"                # top-left | top-right | bottom-left | bottom-right
format = "%H:%M"
show_date = false
fg_color = "white"
bg_color = "black"                  # "none" = leave underlying cells alone

[[scenes.overlays]]
type = "weather"
provider = "open-meteo"             # open-meteo | wttr.in
lat = 30.27
lon = -97.74
# location = "Austin"               # use with provider = "wttr.in"
units = "F"                         # F | C
corner = "top-left"
refresh_minutes = 10

[[scenes.overlays]]
type = "callsign"
text = "JACK"
corner = "bottom-right"
fg_color = "light blue"

[[scenes.overlays]]
type = "countdown"
target = "2026-12-31T23:59:59"      # ISO 8601
format = "auto"                     # auto | "{d}D {h:02d}:{m:02d}:{s:02d}"
corner = "bottom-left"
fg_color = "yellow"

[[scenes.overlays]]
type = "network"
items = ["ip", "ping"]              # ip | hostname | ping (in this order)
corner = "top-left"
fg_color = "light gray"
refresh_s = 5.0

[[scenes.overlays]]
type = "logo"
file = "assets/logos/ccug.txt"
corner = "bottom-left"
fg_color = "white"

[[scenes.overlays]]
type = "big_text"                   # blank or mcm scenes only
row = "middle"                      # top | middle | bottom
speed_cells_per_s = 8.0             # screen cells (= source pixels) per second
inter_message_pause_s = 1.5
loop = true                         # default; cycle messages forever within
                                    # the scene's duration_s. false = play
                                    # each message once, then defer scene
                                    # auto-advance until the last has scrolled off.
# Glyph size is fixed: each source PETSCII char expands 1 source-pixel →
# 1 solid screen cell, i.e. an 8×8 cell footprint per character.
messages = [
  { text = "C64CAST",        color = "rainbow" },
  { text = "DEMO SCENE VIBES", color = "yellow"  },
  { text = "GREETZ TO CCUG",   color = "cyan"    },
]

[[scenes.overlays]]
type = "obs_status"                 # requires the `obs` extra
host = "localhost"
port = 4455
password = ""                       # set in OBS → Tools → WebSocket Server
show_dropped = true
corner = "bottom-right"
fg_color = "light green"
refresh_s = 2.0
```

### Overlay restrictions

The loader validates these at config-load time, not at runtime:

| Overlay restriction | Meaning                                                                                                       |
|---------------------|---------------------------------------------------------------------------------------------------------------|
| `REQUIRES_PETSCII`  | Paints PETSCII glyphs. Allowed on any display mode with `is_petscii_compatible` set — `petscii` and `blank`.  |
| `COMPATIBLE_MODES`  | Whitelist of display-mode names this overlay supports (e.g. `big_text` → `blank`/`mcm`). Empty = no limit.    |
| `REQUIRES_AUDIO`    | Needs `[audio] enabled = true`. Loader raises with a clear message otherwise.                                 |

`big_text` paints into screen + color RAM but is built for the blank canvas (`blank`) or the multicolor-character canvas (`mcm`); it explicitly refuses `petscii` (would stomp the live PETSCII frame) and bitmap modes. Everything else is PETSCII-only (which also means it works on `blank`).

**`big_text` does not compose with other overlays.** To scroll smoothly it commandeers the VIC hardware X-scroll register (`$D016`, which shifts every row) and page-flips screen RAM between `$0400` and `$0C00` (`$D018`). Any other PETSCII overlay sharing the scene (marquee, clock, etc.) gets dragged sideways by the scroll register and blinks out on the frames that show the alternate page. Give `big_text` a scene to itself.

## Suggested setups

**Convention booth (audience can read it from across the room):**

```toml
[[scenes]]
type = "webcam"
display = "petscii"
name = "Live PETSCII"
duration_s = 45.0

  [[scenes.overlays]]
  type = "marquee"
  text = "CCUG @ VCFSW 2026 // C= TO PAUSE // CTRL TO SKIP"
  row = 0
  fg_color = "yellow"

  [[scenes.overlays]]
  type = "clock"
  corner = "top-right"

  [[scenes.overlays]]
  type = "spectrum_petscii"
  placement = "bottom"
  height_rows = 6
```

**SID jukebox:**

```toml
[[scenes]]
type = "waveform"
file = "assets/sids/Tune1.sid"
color_mode = "per_waveform"

[[scenes]]
type = "waveform"
file = "assets/sids/Tune2.sid"
duration_s = 240.0
```

**SID shuffle (single scene, random tune per loop):**

```toml
[[scenes]]
type = "waveform"
# Pool from a directory — each loop picks a different .sid.
file = "assets/sids/C64Music/MUSICIANS/G/Galway_Martin"
color_mode = "per_waveform"
```

## Ensemble mode (multi-system)

A single `c64cast` process can drive **N Ultimate 64s concurrently**.
Each system gets its own `Ultimate64API` socket, audio streamer, scene
list, keyboard poller, and worker thread. The trigger is a **master
TOML** with an `[ensemble]` table:

```toml
# master.toml
[ensemble]
systems = [
    { name = "left",   config = "left.toml"   },
    { name = "middle", config = "middle.toml" },
    { name = "right",  config = "right.toml"  },
]

[interstitial]              # cascaded to each per-system file
duration_s = 3.0

[control]                   # one control plane for the whole ensemble
enabled = true
```

Each per-system file is a fully standalone config (so you can run any
of them with `--config left.toml` to test that one system in
isolation). The order in `systems` is **load-bearing** — index 0 is
the leftmost physical screen, the last entry is the rightmost — and
ensemble-aware orchestrators (see below) rely on this to map content
across the wall.

Run with:

```bash
python -m c64cast --config config/examples/ensemble/master.toml
```

See [`config/examples/ensemble/`](../config/examples/ensemble/) for a
working three-system example.

### Master-defaults cascade

Most `[section]` blocks in the master TOML are inherited by every
per-system config when the per-system file doesn't override them:
`[ultimate64]` (except `url`), `[audio]`, `[interstitial]`,
`[playlist]`, `[debug]`, `[preview]`, `[recording]`. Sections that
are **per-system only** (never inherited): `[[scenes]]`, `[video]`,
`[control]`.

The cascade is approximate — "user explicitly set this field" is
detected as "field value differs from the dataclass default". An
explicit `verbose = 0` per-system looks identical to "didn't set
it"; if you really need 0 to override, set the master to 0 too.

### CLI compatibility

`--url` and `--device` are rejected in ensemble mode (they pick one
system's hardware — set them in the per-system TOMLs instead). Every
other CLI flag applies uniformly to every system.

### Control plane (multi-system)

When `[control].enabled = true` in the master TOML, one FastAPI server
serves the whole ensemble. Endpoints take an optional `?system=NAME`
query param:

| Request                              | Effect                                       |
|--------------------------------------|----------------------------------------------|
| `GET /status`                        | Map of `{ systems: { name: status, ... } }`  |
| `GET /status?system=left`            | One system's status                          |
| `POST /pause`                        | Pause every system                           |
| `POST /pause?system=left`            | Pause one                                    |
| `POST /resume?system=left`           | Resume one                                   |
| `POST /skip?system=all`              | Skip on every system                         |
| `POST /reload?system=middle`         | Reload middle's per-system TOML              |
| `?system=UNKNOWN`                    | 404 listing valid names                      |

Per-system reload errors don't block siblings; the response carries
`reloaded: {name: n_scenes}` plus `errors: {name: msg}` for partial
success. In single-system mode the `?system` param is optional and
endpoints return today's un-wrapped JSON shape (back-compat).

### Cross-system scene orchestration

A scene with `orchestrate = true` triggers an *ensemble effect*. When
the playlist of any system enters such a scene, that system becomes
the **conductor**; the rest of the ensemble runs synchronized
*follower* scenes for the duration of the broadcast, then each
follower resumes its saved playlist position.

The cross-system **match key is the scene's `name`**. Per-system
TOMLs can declare a scene with the same `name` (and
`orchestrate = false`) — that local definition wins for that
system's follower render, so per-system visual params (palette,
border, fg_color, ...) apply. If a follower has no matching scene,
the conductor's cfg is used as a sensible fallback.

```toml
# right.toml — conductor
[[scenes]]
type = "blank"
name = "morning-hello"
duration_s = 60
orchestrate = true              # name is REQUIRED when orchestrate = true
  [[scenes.overlays]]
  type = "big_text"
  messages = [ { text = "GOOD MORNING", color = "rainbow" } ]
```

Today's only built-in orchestrator subclass is
**BigTextSpan** — when a blank/mcm scene with a `big_text` overlay is
declared `orchestrate = true` on the rightmost system, the message
scrolls right-to-left across **all** screens as if they form one
320·N-pixel canvas. The scene's `duration_s` should be long enough
for the whole scroll; the orchestrator releases followers when the
message has scrolled past the leftmost screen.

A follower being interrupted while paused is force-resumed for the
duration of the broadcast and left un-paused afterward (matches the
"emergency broadcast overrides pause" UX). CTRL skip on the
conductor mid-broadcast ends the broadcast cleanly across the wall;
CTRL skip on a follower mid-broadcast is dropped (the follower is
locked into the broadcast loop until the conductor releases it).

The framework also supports a future **mirror** pattern (same scene
played in lockstep across systems — useful for synchronized
video playback, SID playback, or a webcam input only one
system is wired to). The contract is in
[`c64cast/orchestrator.py`](../c64cast/orchestrator.py).
