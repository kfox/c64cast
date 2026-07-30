<img width="800" height="400" alt="c64cast Logo" src="https://raw.githubusercontent.com/kfox/c64cast/main/assets/logo.png" />

# c64cast

[![CI](https://github.com/kfox/c64cast/actions/workflows/ci.yml/badge.svg)](https://github.com/kfox/c64cast/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/kfox/c64cast/branch/main/graph/badge.svg)](https://codecov.io/gh/kfox/c64cast)
[![PyPI](https://img.shields.io/pypi/v/c64cast.svg)](https://pypi.org/project/c64cast/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/kfox/c64cast/blob/main/LICENSE)

c64cast turns a real Commodore 64 — driven over the network through an
[Ultimate 64](https://ultimate64.com/) or
[TeensyROM+](https://lectronz.com/products/teensyrom) — into a programmable
display and audio device. It runs a **playlist of scenes** on the real
hardware: play videos and images, stream a live webcam, visualize SID music
on a 3-voice oscilloscope, synthesize a MIDI keyboard or an ASID stream
through the real SID chip, render reactive generative visuals, or hand the
machine over to a native game or demo. Frames from any source are quantized
in real time to a VIC-II display mode (PETSCII, MCM, hi-res bitmap, multicolor
hi-res); audio plays through the SID's `$D418` DAC or the hi-fi Ultimate Audio
PCM sampler. Stackable **overlays** decorate any scene with scrolling text,
spectrum analyzers, clocks, weather, RSS, logos, and more — and **ensemble
mode** drives a whole wall of C64s at once.

## Install

```bash
uv tool install 'c64cast[all]'          # or: pipx install 'c64cast[all]'
c64cast --config example:hello -u u64://192.168.2.64
```

That puts a `c64cast` command on your `PATH`. To try it without installing
anything permanently:

```bash
uvx --from 'c64cast[all]' c64cast clip.mp4 -u u64://192.168.2.64
```

`[all]` pulls in every optional feature — video files and YouTube URLs, mic
capture, MIDI, webcam gestures, the WLED bridge, the HTTP control plane, the
config wizard. Plain `uv tool install c64cast` gets a much smaller core install
(no mediapipe, no yt-dlp) that still covers every generative scene, PETSCII/
bitmap rendering, SID playback, and overlays; add extras à la carte later
(`uv tool install 'c64cast[video,midi]'`). Extras don't accumulate, so name
every one you want in a single command.

You need a reachable [Ultimate 64 or TeensyROM+](#hardware-needed) — there is
no emulator path for the streaming side. On the Ultimate, enable **Ultimate DMA
Service** under **F2 → Network Settings** first; c64cast prints an actionable
error if it can't connect.

## What do you want to do?

Every row below is a runnable, single-scene demo that ships inside the
package — pass it to `--config` and it loops forever until you Ctrl+C. Point
it at your hardware with `-u` (see [Quick start](#quick-start)).
[`docs/usage.md`](https://github.com/kfox/c64cast/blob/main/docs/usage.md)
documents every option for these scenes and overlays.

| I want to…                          | Try                                                     | Reference |
|-------------------------------------|---------------------------------------------------------|-----------|
| Play a video (or YouTube URL)       | `c64cast clip.mp4` · `example:scene-video`              | [Quick playback](https://github.com/kfox/c64cast/blob/main/docs/usage.md#quick-playback-positional-media-args) |
| Show a live webcam as C64 art       | `example:scene-webcam-petscii`                          | [Scenes](https://github.com/kfox/c64cast/blob/main/docs/usage.md#scenes) |
| Visualize a SID tune (oscilloscope) | `example:scene-waveform`                                | [Scenes](https://github.com/kfox/c64cast/blob/main/docs/usage.md#scenes) |
| Play a SID from a MIDI keyboard     | `example:scene-midi`                                    | [Scenes](https://github.com/kfox/c64cast/blob/main/docs/usage.md#scenes) |
| Stream from DeepSID / SIDFactory II | `example:scene-asid`                                    | [Scenes](https://github.com/kfox/c64cast/blob/main/docs/usage.md#scenes) |
| Slideshow of images                 | `example:scene-slideshow`                               | [Scenes](https://github.com/kfox/c64cast/blob/main/docs/usage.md#scenes) |
| Generative / music-reactive visuals | `example:scene-generative-plasma`                       | [Scenes](https://github.com/kfox/c64cast/blob/main/docs/usage.md#scenes) |
| Run a native `.prg`/`.crt` game or demo | `example:scene-launcher`                            | [Scenes](https://github.com/kfox/c64cast/blob/main/docs/usage.md#scenes) |
| An info board (clock/weather/RSS)   | `example:overlay-clock`                                 | [Overlays](https://github.com/kfox/c64cast/blob/main/docs/usage.md#overlays) |
| Drive multiple C64s as one video wall | `example:ensemble/master`                             | [Ensemble mode](https://github.com/kfox/c64cast/blob/main/docs/usage.md#ensemble-mode-multi-system) |
| Make the C64 a WLED LED matrix       | `example:scene-wled`                                   | [WLED bridge](https://github.com/kfox/c64cast/blob/main/docs/usage.md#wled-bridge) |
| Control c64cast from the WLED app    | `example:wled-control`                                 | [WLED bridge](https://github.com/kfox/c64cast/blob/main/docs/usage.md#wled-bridge) |

Run any of them with `c64cast --config example:<name>`, or list the whole set
with `c64cast --list-examples` (one demo per scene type and per overlay).

## Features

**Scenes** — a TOML playlist runs any mix of these on the real C64, each
for a set duration, with an "UP NEXT" interstitial between them:

* **Video** — MP4/MKV/etc. (and YouTube/other URLs via yt-dlp), soundtrack
  and all, keyed off the audio clock so A/V can't drift.
* **Webcam** — live capture quantized to any display mode in real time.
* **Slideshow** — still images from a directory/glob, aspect-fit.
* **SID waveform** — plays a `.sid` natively on the C64 (via a small
  player PRG, not the firmware's own runner) with a per-voice oscilloscope.
  Handles multi-SID tunes — up to 8 chips using the U64's UltiSIDs.
* **MIDI → SID** — bridge a live MIDI source (USB controller, DAW) into
  the real SID and visualize each voice (`midi` extra).
* **ASID client** — receive an ASID stream (DeepSID in a browser,
  SIDFactory II, Plogue chipsynth C64, …) and play it on the real SID with
  the same 3-voice scope (`midi` extra).
* **Generative** — ≈20 procedural sources (plasma, tunnel, fire,
  mandelbrot, metaballs, game of life, …), optionally music-reactive and
  with pixel effects (trails, pulse, RGB shift, blur).
* **Launcher** — hand the machine over to a native `.prg`/`.crt` game or
  demo, then reclaim it.
* **WLED matrix** — turn the C64 into a virtual LED matrix and stream live
  pixels to it from LedFx / xLights (DDP or WLED realtime UDP); part of the
  WLED bridge below.
* **Blank** — a solid PETSCII canvas for title cards + overlays.

**Display + audio** — six VIC-II display modes (`hires`, `hires_edges`,
`mhires`, `petscii`, `mcm`, `blank`), each with its own vectorized
quantizer (≈30 fps bitmap, 50/60 fps char over a LAN). Audio plays through
the SID's lo-fi `$D418` DAC (4-bit, or ≈6-7-bit via the Mahoney companding
technique) or, on the U64, the high-fidelity Ultimate Audio FPGA PCM sampler.

**Overlays** — stack on any compatible scene: scrolling text, marquee, RSS
ticker, PETSCII spectrum analyzer, clock, weather, callsign, countdown,
network info, multi-line logo, demo-scene big text, OBS Studio status.

**Ensemble mode** — one process drives **N systems at once** as a
video wall, with cross-system orchestration (e.g. a `big_text` message
scrolling across every screen as a single canvas).

**Control surfaces** — the C64 keyboard itself (C= pauses, CTRL skips,
SHIFT cycles the style), an on-C64 SPACE menu for live scene tweaks, webcam
hand gestures (`vision` extra), a FastAPI control plane (`/pause`,
`/resume`, `/skip`, `/reload`), MIDI CC control, and `SIGHUP` to reload the
config.

**WLED bridge** — interoperate with the [WLED](https://kno.wled.ge/) LED
ecosystem in three directions, all under one `[wled]` config section: drive
real LED matrices *from* the C64's SID with no microphone (audio-sync
broadcast), present c64cast *as* a virtual WLED device the WLED app / Home
Assistant can discover and control (effects ↔ scenes, sliders ↔ live params,
presets), and turn the C64 *into* a virtual LED matrix that LedFx / xLights
stream live pixels to. See
[WLED bridge](https://github.com/kfox/c64cast/blob/main/docs/usage.md#wled-bridge)
for the full reference.

**Quick playback** — skip the config file entirely and pass media straight
on the command line: `c64cast clip.mp4 tune.sid pics/` plays each in turn.

**Preview + recording** — an optional local window mirroring what the C64 is
showing, plus recording to MP4. Both are cv2-based, so neither needs an extra.

## Quick start

```bash
# "Hello world": scrolls big text across a solid canvas. Needs nothing but a
# reachable U64/TR+ — no webcam, mic, SID, or video files. Ctrl-C to exit.
c64cast --config example:hello -u u64://192.168.2.64

# Save the connection target so you never type -u again:
c64cast -u u64://192.168.2.64 --save-settings
c64cast --config example:hello
```

`-u/--url` is a scheme-aware target that picks the backend + endpoint:
`u64://HOST` or `http(s)://HOST` (Ultimate 64 / II+), `tr://` (TeensyROM+ over
auto-detected USB serial), `tr:///dev/cu.usbmodem*` / `tr://COM3` (a specific
serial device), or `tr://HOST` (TeensyROM+ over TCP). `$C64CAST_URL` is the env
fallback, and `--save-settings` persists it (plus the capture device and SID
model) to `~/.config/c64cast/settings.toml`.

`example:hello` is the gentlest starting point. From there:

```bash
# Try a single feature in isolation — one demo per scene type / overlay:
c64cast --config example:scene-webcam-petscii
c64cast --config example:overlay-clock

# Build your own config the easy way — the interactive wizard walks you
# through either a single scene or a multi-scene playlist (with the "UP NEXT"
# interstitial, video interleaving, and loop control) and writes a c64cast.toml
# (needs the 'wizard' extra, included in [all]; a ./c64cast.toml is picked up
# automatically when no --config is given):
c64cast --init

# ...or by hand: the `c64cast.example` demo is a fully-annotated reference
# exercising every scene + overlay. Copy it out and edit:
c64cast --print-example c64cast.example > c64cast.toml && $EDITOR c64cast.toml
c64cast

# Validate any config + check which optional extras are installed without
# touching the C64 (skip the connectivity probe to keep it offline):
c64cast --doctor --config c64cast.toml --skip-probe
```

The demos ship **inside the package**, so `example:NAME` works the same from an
installed wheel, from `uvx`, or from a git checkout.
`c64cast --list-examples` prints the whole set with one-line summaries;
`--print-example NAME` copies one out to edit. See
[`c64cast/examples/README.md`](https://github.com/kfox/c64cast/blob/main/c64cast/examples/README.md)
for the narrative version.

`c64cast -h` lists every CLI flag grouped by section (`connection`,
`quick playback`, `video input`, `audio`, `vision input`, `playlist`,
`introspection`, `debug`).

### Quick playback (no config file)

Pass media files/directories/globs/URLs as positional arguments to play them
once, in order, without writing a TOML (mutually exclusive with `--config`).
Audio is on by default; `--no-audio` mutes.

```bash
# A video, a SID tune, then a folder of pictures, on an Ultimate 64:
c64cast -u u64://192.168.1.64 clip.mp4 tune.sid ~/Pictures/

# A clip on a TeensyROM+ over auto-detected USB serial:
c64cast -u tr:// clip.mp4

# A YouTube URL (needs the 'yt' extra, included in [all]):
c64cast 'https://youtu.be/dQw4w9WgXcQ'
```

## Configuration

A config is a single TOML file (`--config PATH`, else `./c64cast.toml`,
else built-in defaults) that defines the playlist and every overridable
option. Three ways to author one, plus tooling to discover and validate it —
none of which needs the C64:

```bash
# Build one interactively (single scene or multi-scene playlist):
c64cast --init                    # needs the 'wizard' extra

# Discover the config surface straight from the code (always in sync):
c64cast --list-scenes             # scene types
c64cast --list-overlays           # overlays + their restrictions
c64cast --list-modes              # display modes
c64cast --list-examples           # the packaged demo configs
c64cast --describe overlay:clock  # full reference for one thing
c64cast --compat                  # overlay × display-mode matrix
c64cast --print-schema            # JSON Schema for editor autocomplete

# Validate a config (and check which extras are installed) without hardware:
c64cast --doctor --config c64cast.toml --skip-probe
```

The discovery output and the JSON schema are generated from the same field
metadata the loader runs on, so they can't drift from the code.
`c64cast --print-example c64cast.example` is the fully-annotated reference; see
[docs/usage.md](https://github.com/kfox/c64cast/blob/main/docs/usage.md) for
the complete config walkthrough.

Machine-local defaults — connection target, capture device, SID model — live in
`~/.config/c64cast/settings.toml` and apply to every run, including quick
playback; write them with `--save-settings`. Persisted state (DAC calibrations,
WLED and loop presets) lives under `~/.local/share/c64cast/`.

## Live controls

While the stream is running, you control it from the C64 keyboard itself
(c64cast polls `$028D`, the kernal's keyboard-modifier scratch byte, at
10 Hz):

| Key on the C64                             | What it does                                                                                    |
|--------------------------------------------|-------------------------------------------------------------------------------------------------|
| **Commodore (C=)** — tap                   | Pause: scene + overlays tear down, screen clears, audio stops                                   |
| **Commodore (C=)** — hold 3 s while paused | Resume: re-sets-up the same scene (audio + polling threads all come back)                       |
| **CTRL** — tap while playing               | Skip: advance to the next interstitial after the current frame                                  |
| **SHIFT** — tap while playing              | Cycle the current scene's display style (palette mode / edge variant / waveform subtune / etc.) |

The C= + CTRL chord pressed on the same poll tick prefers **pause** —
skip is suppressed. SHIFT held alongside C= or CTRL is dropped so a
thumb resting on shift doesn't phantom-cycle the style. Cycled style
persists across single-scene loop iterations and across pause/resume,
but resets to the configured default on a real scene boundary
(multi-scene transitions construct fresh display_mode instances).

Same actions are exposed over HTTP when `[control] enabled = true`:

```bash
curl -X POST http://127.0.0.1:8765/pause
curl -X POST http://127.0.0.1:8765/resume
curl -X POST http://127.0.0.1:8765/skip
curl -X POST http://127.0.0.1:8765/reload   # re-read config from disk
```

Or send `SIGHUP` to the process to trigger a config reload from the
shell.

## Documentation

* [docs/guide/](https://github.com/kfox/c64cast/tree/main/docs/guide) —
  **the User's Guide**: a friendly, read-in-order introduction that starts
  from nothing and builds up. Start at
  [Quick Start](https://github.com/kfox/c64cast/blob/main/docs/guide/01-quick-start.md);
  `make guide` renders the whole thing to a typeset PDF.
* [docs/usage.md](https://github.com/kfox/c64cast/blob/main/docs/usage.md) —
  full config reference, scene/overlay catalog with options, suggested setups
* [docs/caveats.md](https://github.com/kfox/c64cast/blob/main/docs/caveats.md) —
  known quirks (6502 emulator scope, char ROM substitution, U64 endpoint
  variance, licensing of SIDs / videos)
* [docs/troubleshooting.md](https://github.com/kfox/c64cast/blob/main/docs/troubleshooting.md) —
  symptom-first index for "I saw X, what now?"
* [docs/extending.md](https://github.com/kfox/c64cast/blob/main/docs/extending.md) —
  how to add a new Scene, Overlay, DisplayMode, or interstitial Background
* [docs/architecture.md](https://github.com/kfox/c64cast/blob/main/docs/architecture.md) —
  per-module internals: design rationale, hardware constraints, and edge-case
  history. Split by topic area under
  [docs/architecture/](https://github.com/kfox/c64cast/tree/main/docs/architecture);
  the index routes each module to its notes
* [CHANGELOG.md](https://github.com/kfox/c64cast/blob/main/CHANGELOG.md) —
  what changed in each release

## Hardware needed

One of the following:

* An [Ultimate 64](https://ultimate64.com/) — confirmed with Elite I, Elite II,
  Ultimate II+ cartridge, or Commodore 64 Ultimate. Best results will be
  obtained from using the Elite II or the Commodore 64 Ultimate.
  Under **F2 → Network Settings**, enable **Ultimate DMA Service**,
  **Command Interface** (TCP port 64 — the Command
  Interface toggle gates command dispatch even when the socket is open),
  and **Ultimate Audio** for streaming PCM audio.
  The REST API is used for the few operations that have no DMA equivalent.
* A [TeensyROM+ Multi-Capable Cartridge for C64/128](https://lectronz.com/products/teensyrom)
  plugged into an original Commodore 64 or one of the above modern
  "ultimate" equivalents.

Depending on how you use it, you'll also want some of these things:

* Any C64 video output path supported by a U64/C64.
* A webcam (any cv2-compatible USB device) for live capture scenes.
* A microphone for live audio; otherwise the audio path can sit
  idle or play a video's soundtrack via PyAV.
* A MIDI controller if you want to use MIDI scenes or control
  playlists/scenes via MIDI CC messages.
* An HDMI capture device if you want to capture output directly from a
  U64 or C64 equipped with a Kawari Large. Example capture devices include
  the Elgato Cam Link 4K or the Genki ShadowCast.
* A [WLED](https://kno.wled.ge/) device (or a WLED-ecosystem sender like
  LedFx/xLights) on the same LAN if you want to use the WLED bridge in
  any direction — none of this is required for the core streaming
  experience.

There is no software emulator path for the *streaming* side — c64cast
writes directly to U64 memory/registers over the Ultimate DMA Service
(TCP port 64), with REST used only for the few non-DMA operations. SID
playback is
driven by a small player PRG uploaded into C64 RAM so the real 6510
calls PLAY at IRQ time (the U64 firmware's `runners:sidplay` runner is
deliberately avoided because it hijacks the HDMI output with its own
UI); see
[docs/caveats.md](https://github.com/kfox/c64cast/blob/main/docs/caveats.md)
for the PSID-only limitation.

## Contributing

Bug reports, feature ideas, and pull requests are all welcome. See
[CONTRIBUTING.md](https://github.com/kfox/c64cast/blob/main/CONTRIBUTING.md)
for the development setup (a git checkout and `uv sync --all-extras`), the
`make check` gate, and the conventions this repo follows. Security reports go
through [SECURITY.md](https://github.com/kfox/c64cast/blob/main/SECURITY.md)
rather than a public issue.

## Acknowledgments

* [Gideon Zweijtzer](https://1541ultimate.net/) for the Ultimate 64
  hardware and firmware.
* Travis Smith for the [TeensyROM+](https://github.com/SensoriumEmbedded/TeensyROM) -
  including cartridge, firmware, hands-on testing, and suggestions.
* [Bo Zimmerman](http://zimmers.net) for his excellent online and physical
  collections of all things Commodore.
* The [HVSC](https://hvsc.c64.org/) team for the SID archive and the
  Songlengths database.
* Pex 'Mahoney' Tufvesson for the 8-bit `$D418` DAC technique (his
  ["Musings in the Key of C64" white paper](https://livet.se/mahoney/c64-files/Musings_in_the_key_of_C64_by_Pex_Mahoney_Tufvesson.pdf))
  behind the optional `dac_curve = "mahoney_ultisid"` audio path.
* Jürgen Wothke (webSID / Tiny'R'Sid) for
  [documenting the `$D418` filter-bit "almost 8-bit" playback approach](https://www.wothke.ch/tinyrsid/index.php/digi-samples)
  behind Mahoney's technique.
* Antonio Savona for the
  [48 kHz `$D418` write-up](https://brokenbytes.blogspot.com/2018/03/a-48khz-digital-music-player-for.html).
* [CodeBase64](https://codebase64.net/) for the extensive reference material.
* Many open source contributors for all of the _many_ Python packages
  that make this app possible. <3

## License

MIT — see [LICENSE](https://github.com/kfox/c64cast/blob/main/LICENSE).

**Third-party assets.** The User's Guide is typeset in two fonts that are
redistributed in this repository under the [SIL Open Font
License 1.1](https://openfontlicense.org/), not under MIT: **Jost\***
(Copyright 2020 The Jost Project Authors) and **Inconsolata** (Copyright 2006
The Inconsolata Project Authors). They live in
[`docs/guide/fonts/`](https://github.com/kfox/c64cast/tree/main/docs/guide/fonts)
alongside their licence texts — see
[that directory's README](https://github.com/kfox/c64cast/blob/main/docs/guide/fonts/README.md)
for provenance and for what has to travel with them.
