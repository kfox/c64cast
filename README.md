<img width="800" height="271" alt="c64cast Logo" src="https://raw.githubusercontent.com/kfox/c64cast/main/assets/logo.png" />

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
spectrum analyzers, clocks, weather, RSS, logos, and more; a **performance
layer** puts the whole show on a MIDI controller or a phone; and **ensemble
mode** drives a wall of C64s at once.

The full documentation lives at **<https://kfox.github.io/c64cast/>** — three
books (User's Guide, Programmer's Reference Guide, Performance Card) plus the
caveats and troubleshooting notes, rebuilt from this repository on every push.

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

Later, `uv tool upgrade c64cast` (or `pipx upgrade c64cast`) is the upgrade —
unpacking a release archive over a directory installs nothing, which is why
`c64cast --version` also prints the install it runs from. See
[Upgrading](https://github.com/kfox/c64cast/blob/main/docs/guide/04-setting-up.md#upgrading).

You need a reachable [Ultimate 64 or TeensyROM+](#hardware-needed) — there is
no emulator path for the streaming side. An Ultimate ships with the three
firmware services c64cast needs switched off; [Hardware
needed](#hardware-needed) lists the switches, and it is worth following
[Quick Start](https://github.com/kfox/c64cast/blob/main/docs/guide/01-quick-start.md)
through the menus the first time — miss one and c64cast connects and then
hangs rather than printing anything useful.

## What do you want to do?

Every row below is a runnable demo that ships inside the package — pass it to
`--config` and it loops forever until you Ctrl+C. Point it at your hardware
with `-u` (see [Quick start](#quick-start)). The
[Programmer's Reference Guide](https://github.com/kfox/c64cast/tree/main/docs/reference)
documents every option for these scenes and overlays.

| I want to…                          | Try                                                     | Reference |
|-------------------------------------|---------------------------------------------------------|-----------|
| Play a video (or YouTube URL)       | `c64cast clip.mp4` · `example:scene-video`              | [Quick playback](https://github.com/kfox/c64cast/blob/main/docs/reference/26-appendix-g-cli-flags.md#quick-playback-with-media-args) |
| Play an audio track with visuals that react to it | `c64cast tune.mp3`                    | [Quick playback](https://github.com/kfox/c64cast/blob/main/docs/reference/26-appendix-g-cli-flags.md#quick-playback-with-media-args) |
| Show a live webcam as C64 art       | `example:scene-webcam-petscii`                          | [`webcam`](https://github.com/kfox/c64cast/blob/main/docs/reference/03-vocabulary.md#webcam) |
| Visualize a SID tune (oscilloscope) | `example:scene-waveform`                                | [`waveform`](https://github.com/kfox/c64cast/blob/main/docs/reference/03-vocabulary.md#waveform) |
| Play a SID from a MIDI keyboard     | `example:scene-midi`                                    | [`midi`](https://github.com/kfox/c64cast/blob/main/docs/reference/03-vocabulary.md#midi) |
| Stream from DeepSID / SIDFactory II | `example:scene-asid`                                    | [`asid`](https://github.com/kfox/c64cast/blob/main/docs/reference/03-vocabulary.md#asid) |
| Slideshow of images                 | `example:scene-slideshow`                               | [`slideshow`](https://github.com/kfox/c64cast/blob/main/docs/reference/03-vocabulary.md#slideshow) |
| Generative / music-reactive visuals | `example:scene-generative-plasma`                       | [`generative`](https://github.com/kfox/c64cast/blob/main/docs/reference/03-vocabulary.md#generative) |
| Stack pixel effects on any scene    | `example:effect-chain`                                  | [Generators + effects](https://github.com/kfox/c64cast/blob/main/docs/reference/24-appendix-e-generators-effects.md) |
| Play the show live from a controller or a phone | `example:performance-clips`                 | [Performing](https://github.com/kfox/c64cast/blob/main/docs/reference/07-inputs-and-outputs.md#performing) |
| Run a native `.prg`/`.crt` game or demo | `example:scene-launcher`                            | [`launcher`](https://github.com/kfox/c64cast/blob/main/docs/reference/03-vocabulary.md#launcher) |
| An info board (clock/weather/RSS)   | `example:overlay-clock`                                 | [Overlays](https://github.com/kfox/c64cast/blob/main/docs/reference/03-vocabulary.md#overlays) |
| Drive multiple C64s as one video wall | `example:ensemble/master`                             | [Ensemble mode](https://github.com/kfox/c64cast/blob/main/docs/reference/02-config-rules.md#the-ensemble-cascade) |
| Make the C64 a WLED LED matrix       | `example:scene-wled`                                   | [`wled`](https://github.com/kfox/c64cast/blob/main/docs/reference/03-vocabulary.md#wled) |
| Control c64cast from the WLED app    | `example:wled-control`                                 | [WLED bridge](https://github.com/kfox/c64cast/blob/main/docs/reference/07-inputs-and-outputs.md#wled) |

Run any of them with `c64cast --config example:<name>`, or list the whole set
with `c64cast --list-examples` (one demo per scene type and per overlay).

## Features

* **Scenes** — video files and YouTube/other URLs (soundtrack and all, A/V
  locked to the audio clock), live webcam, slideshows, a `.sid` playing
  natively on the real chip under a per-voice oscilloscope (multi-SID too),
  live MIDI → SID and ASID streams, 20 procedural generative sources
  (optionally music-reactive), native `.prg`/`.crt` hand-off, a virtual WLED
  matrix fed live pixels by LedFx/xLights, and a blank canvas for title cards.
* **Display + audio** — six VIC-II display modes, each with its own vectorized
  quantizer (≈30 fps bitmap, 50/60 fps char over a LAN); audio through the
  SID's lo-fi `$D418` DAC (4-bit, or ≈6-7-bit via the Mahoney companding
  technique) or, on the U64, the high-fidelity Ultimate Audio PCM sampler.
* **Overlays** — scrolling text, marquee, RSS ticker, spectrum analyzers,
  clock, weather, callsign, countdown, network info, logos, demo-scene big
  text, OBS Studio status — stackable on any compatible scene.
* **Pixel effects** — eight of them (trails, pulse, RGB shift, blur, strobe,
  invert, mirror, posterize), layerable into an ordered chain, each tunable
  and bypass-toggleable live, and lockable to the music or the beat grid.
* **Live performance** — a MIDI controller or a phone drives the whole show;
  see [Live control](#live-control).
* **Ensemble mode** — one process drives **N systems at once** as a video
  wall, with cross-system orchestration.
* **WLED bridge** — interoperate with the [WLED](https://kno.wled.ge/) LED
  ecosystem in three directions: drive real LED matrices *from* the C64's SID,
  present c64cast *as* a WLED device the WLED app / Home Assistant can
  control, or turn the C64 *into* a virtual LED matrix. See
  [WLED bridge](https://github.com/kfox/c64cast/blob/main/docs/reference/07-inputs-and-outputs.md#wled).
* **Preview + recording** — an optional desktop window and an MP4 writer, both
  host-side *reconstructions* of the bytes c64cast sent rather than captures
  of the Commodore's own output. Know
  [what they cannot show you](https://github.com/kfox/c64cast/blob/main/docs/reference/07-inputs-and-outputs.md#the-preview-window)
  before you rely on them.

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
fallback, and `--save-settings` persists it — along with the capture device and
SID model — to `~/.config/c64cast/settings.toml`, where it applies to every
later run including quick playback.

The first run against a machine spends about a second reading that machine's
**character ROM** over the wire and caching it under `~/.local/share/c64cast/`,
so every glyph c64cast draws is your Commodore's own font rather than a
built-in approximation.

From `example:hello`, the next steps:

```bash
# Try a single feature in isolation — one demo per scene type / overlay:
c64cast --config example:scene-webcam-petscii
c64cast --config example:overlay-clock

# Build your own: the wizard walks you through a single scene or a whole
# playlist and writes a ./c64cast.toml, which later runs pick up on their own
# (needs the 'wizard' extra, included in [all]):
c64cast --init

# ...or by hand, starting from the fully-annotated reference config:
c64cast --print-example c64cast.example > c64cast.toml && $EDITOR c64cast.toml
c64cast

# Check a config and your installed extras without touching the C64:
c64cast --doctor --config c64cast.toml --skip-probe
```

The demos ship **inside the package**, so `example:NAME` works the same from an
installed wheel, from `uvx`, or from a git checkout —
[`c64cast/examples/README.md`](https://github.com/kfox/c64cast/blob/main/c64cast/examples/README.md)
is the narrative tour of them. `c64cast -h` lists every CLI flag, grouped by
section.

### Quick playback (no config file)

Pass media files/directories/globs/URLs as positional arguments to play them
once, in order, without writing a TOML (mutually exclusive with `--config`).
Audio is on by default; `--no-audio` mutes.

```bash
# A video, a SID tune, then a folder of pictures, on an Ultimate 64:
c64cast -u u64://192.168.1.64 clip.mp4 tune.sid ~/Pictures/

# A YouTube URL, on a TeensyROM+ over auto-detected USB serial (the 'yt'
# extra, included in [all]); an audio file works too — the track plays
# through the C64 while a generative visual reacts to it.
c64cast -u tr:// 'https://youtu.be/dQw4w9WgXcQ'
```

## Configuration

A config is a single TOML file (`--config PATH`, else `./c64cast.toml`, else
built-in defaults) that defines the playlist and every overridable option.
`c64cast --init` builds one interactively, `--print-example c64cast.example`
prints a fully-annotated one to edit, and `--doctor --skip-probe` validates the
result without touching the C64. The whole config surface is discoverable from
the command line (`--describe`, `--list-scenes`, `--list-overlays`,
`--list-modes`, `--compat`, `--print-schema`), and every one of those reads the
same field metadata the loader runs on, so the answers can't drift from the
code. See
[The Configuration Language](https://github.com/kfox/c64cast/blob/main/docs/reference/02-config-rules.md)
for the complete walkthrough and
[Appendix A](https://github.com/kfox/c64cast/blob/main/docs/reference/20-appendix-a-configuration.md)
for every section and field.

## Live control

While the show is running you can drive it from the C64's own keyboard (pause,
skip, cycle styles, an on-C64 menu of live knobs), a MIDI controller (a
clip-launch grid quantized to a beat grid, CC knobs mapped to live parameters,
pad LEDs driven from actual state, saved *looks* that recall a scene and its
whole effect chain in one press), a phone or laptop (`GET /perf` on the
control-plane server serves a touch console), webcam gestures, or plain HTTP +
signals with `[control] enabled = true`.

[Inputs and Outputs](https://github.com/kfox/c64cast/blob/main/docs/reference/07-inputs-and-outputs.md)
documents every surface in full, and the
[Performance Card](https://github.com/kfox/c64cast/tree/main/docs/card) is the
printable version for the desk beside the controller.

## Documentation

Everything below is also a website: **<https://kfox.github.io/c64cast/>** reads
all three books and the notes beneath them in one place, built from these same
files on every push to `main`.

* [docs/guide/](https://github.com/kfox/c64cast/tree/main/docs/guide) —
  **the User's Guide**: a friendly, read-in-order introduction that starts
  from nothing and builds up. Start at
  [Quick Start](https://github.com/kfox/c64cast/blob/main/docs/guide/01-quick-start.md),
  or download the typeset
  [PDF](https://github.com/kfox/c64cast/releases/latest/download/c64cast-users-guide.pdf).
* [docs/reference/](https://github.com/kfox/c64cast/tree/main/docs/reference) —
  **the Programmer's Reference Guide**: the volume you open at the page you
  need. The rules of the configuration language, every scene and overlay, the
  display and sound paths in full, what lands in the Commodore's memory, and
  ten appendices, nine of them generated from the code.
  [PDF](https://github.com/kfox/c64cast/releases/latest/download/c64cast-reference-guide.pdf).
* [docs/card/](https://github.com/kfox/c64cast/tree/main/docs/card) —
  **the Performance Card**: two printable pages of controls, live targets and
  clip-grid syntax for the desk beside the controller.
  [PDF](https://github.com/kfox/c64cast/releases/latest/download/c64cast-performance-card.pdf).
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

Each PDF link above always serves the newest release; every past release keeps
its own version-stamped copy on
[its release page](https://github.com/kfox/c64cast/releases).

## Hardware needed

One of the following:

* An [Ultimate 64](https://ultimate64.com/) — confirmed with Elite I, Elite II,
  Ultimate II+ cartridge, or Commodore 64 Ultimate. Best results will be
  obtained from using the Elite II or the Commodore 64 Ultimate.
  Three firmware switches, in two menus under **F2**, then save and reboot:
  * **Ultimate DMA Service** (*Network Settings*) — the socket on TCP port 64
    that carries every memory write. Without it nothing works at all.
  * **Command Interface** (*Memory Configuration* — a different menu, and the
    one people miss) — gates command dispatch even when the socket is open.
    Without it c64cast connects and then hangs forever.
  * **Web Remote Control Service** (*Network Settings*) — the REST service
    carrying the operations that have no DMA equivalent: reset, launching a
    program or a SID, and every memory *read*, including the keyboard poll and
    the character-ROM dump. Without it pixels still paint, but nothing starts.
    On older Ultimate 64 and Ultimate II+ firmware it has no switch of its own
    and is already on.

  Nothing else needs enabling by hand: c64cast turns on the REU and maps the
  Ultimate Audio sampler itself when a run needs them, and puts both back at
  teardown.
* A [TeensyROM+ Multi-Capable Cartridge for C64/128](https://lectronz.com/products/teensyrom)
  plugged into an original Commodore 64 or one of the above modern
  "ultimate" equivalents.

Depending on how you use it, you'll also want some of these things:

* Any C64 video output path supported by a U64/C64.
* A webcam (any cv2-compatible USB device) for live capture scenes.
* A microphone for live audio; otherwise the audio path can sit
  idle or play a video's soundtrack via PyAV.
* A MIDI controller if you want to use MIDI scenes, or to perform with the clip
  grid and live parameter knobs.
* An HDMI capture device if you want to capture output directly from a
  U64 or C64 equipped with a Kawari Large. Example capture devices include
  the Elgato Cam Link 4K or the Genki ShadowCast.
* A [WLED](https://kno.wled.ge/) device (or a WLED-ecosystem sender like
  LedFx/xLights) on the same LAN if you want to use the WLED bridge in
  any direction — none of this is required for the core streaming
  experience.

There is no software emulator path for the *streaming* side: c64cast writes
directly to C64 memory and VIC-II registers over the wire, and SID playback
runs a small player PRG in the machine's own RAM so the real 6510 calls PLAY at
IRQ time. See
[docs/caveats.md](https://github.com/kfox/c64cast/blob/main/docs/caveats.md)
for why, and for the PSID-only limitation that follows from it.

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

**Third-party assets.** The books are typeset in two fonts that are
redistributed in this repository under the [SIL Open Font
License 1.1](https://openfontlicense.org/), not under MIT: **Jost\***
(Copyright 2020 The Jost Project Authors) and **Inconsolata** (Copyright 2006
The Inconsolata Project Authors). They live in
[`docs/shared/fonts/`](https://github.com/kfox/c64cast/tree/main/docs/shared/fonts)
alongside their license texts — see
[that directory's README](https://github.com/kfox/c64cast/blob/main/docs/shared/fonts/README.md)
for provenance and for what has to travel with them.
