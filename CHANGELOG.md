# Changelog

All notable changes to c64cast are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and c64cast follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) over its *user*
surface — the CLI flags, the config schema, the `example:` names, and the data
directory layout. The Python API carries no stability promise while the version
is `0.x`.

Work lands under `## [Unreleased]`; cutting a release renames that section to
the version and stamps it with the date.

## [Unreleased]

The first public release. c64cast has been in daily use against real hardware
since June 2026 — this is the point where it becomes installable rather than
cloneable.

### Added

**Two hardware backends, selected by URI.** `-u u64://HOST` (or `http(s)://`)
drives an [Ultimate 64](https://ultimate64.com/), Ultimate II+, or Commodore 64
Ultimate: memory writes go over the Ultimate DMA Service on TCP port 64 with
REST for the handful of operations that have no DMA equivalent. `-u tr://`
drives a [TeensyROM+](https://lectronz.com/products/teensyrom) cartridge in an
original C64 over auto-detected USB serial, an explicit serial device
(`tr:///dev/cu.usbmodemXYZ`, `tr://COM3`), or raw TCP (`tr://HOST[:PORT]`).
Per-link knobs ride along as query parameters (`u64://host?dma_port=64`,
`tr:///dev/…?baud=2000000`). `$C64CAST_URL` is the environment fallback.

**Ten scene types**, mixed freely in a TOML playlist with per-scene durations
and an "UP NEXT" interstitial between them:

- **video** — MP4/MKV/etc. with its soundtrack, paced off the audio clock so
  A/V cannot drift. YouTube and other streaming URLs resolve through yt-dlp.
- **webcam** — live capture quantized to any display mode in real time.
- **slideshow** — still images from a directory or glob, aspect-fit.
- **waveform** — plays a `.sid` on the real chip through a small player PRG
  DMA'd into C64 RAM (deliberately not the firmware's own runner, which
  hijacks the HDMI output), with a 3-voice oscilloscope driven by a host-side
  py65 SID emulator. Handles multi-SID tunes up to 8 chips on the U64's
  UltiSIDs, and matches 6581/8580 tunes to the installed chips.
- **midi** — bridge a live MIDI source into the real SID and scope each voice.
- **asid** — receive an ASID stream (DeepSID in a browser, SIDFactory II,
  Plogue chipsynth C64) and play it on the real SID with the same scope.
- **generative** — 20 procedural sources (plasma, tunnel, fire, mandelbrot,
  metaballs, game of life, fireworks, soap, …), optionally music-reactive.
- **launcher** — hand the machine over to a native `.prg`/`.crt` and reclaim it.
- **wled** — turn the C64 into a virtual LED matrix fed by a realtime pixel
  stream from LedFx or xLights (DDP or WLED realtime UDP).
- **blank** — a solid PETSCII canvas as a foundation for overlays.

**Six VIC-II display modes** — `petscii`, `mcm`, `hires`, `hires_edges`,
`mhires`, `blank` — each with its own vectorized quantizer (≈30 fps bitmap,
50/60 fps character modes over a LAN). The `[color]` pipeline shapes any source
before quantization: spatial dithering, perceptual color matching, per-cell
strategy selection, motion smoothing, scene fades, and a forced-palette mode
that remaps a frame onto a chosen subset of the 16 C64 colors — with a rolling
palette that re-clusters as the content changes. `--suggest-palette FILE`
analyzes an image or video and ranks the colors that represent it most
faithfully.

**Audio on the real SID.** By default, video and file audio play through the
U64's Ultimate Audio FPGA PCM sampler for high fidelity; the lo-fi `$D418` DAC
path (4-bit, or ≈6–7 bit via Mahoney companding) covers TeensyROM+, mic input,
and webcam audio everywhere. The sampler's effective clock ships calibrated, so
audio holds sync against host-paced video over long runs. `--calibrate-dac`
measures a per-machine DAC response curve through an HDMI capture device.

**Thirteen stackable overlays** — `scrolling_text`, `marquee`, `rss`,
`spectrum_petscii`, `spectrum_bitmap`, `clock`, `weather`, `callsign`,
`countdown`, `network`, `logo`, `big_text`, `obs_status` — composable onto any
compatible scene, with `--compat` printing the overlay × display-mode matrix.

**Ensemble mode.** One process drives N systems at once as a video wall, with
cross-system orchestration — a `big_text` message scrolling across every screen
as one continuous canvas, spans and mirrors — and audio-slot coordination so
the systems do not fight over the DAC.

**Live control surfaces.** The C64's own keyboard (C= pauses, CTRL skips, SHIFT
cycles the display style), an on-C64 menu for live scene tweaks, webcam hand
gestures, a FastAPI control plane (`/pause`, `/resume`, `/skip`, `/reload`),
MIDI CC mapped to any live parameter, and `SIGHUP` to reload the config. The
DJ/VJ layer adds a tempo/beat grid, a clip-launch grid with LED feedback on
grid controllers, a layerable chain of 8 pixel effects, snapshot-recall of
"looks", and a phone/web performance console.

**WLED bridge**, in three directions under one `[wled]` section: drive real LED
matrices *from* the C64's SID with no microphone, present c64cast *as* a virtual
WLED device that the WLED app and Home Assistant discover and control, and turn
the C64 *into* a matrix that LedFx/xLights stream pixels to.

**Quick playback.** `c64cast clip.mp4 tune.sid pics/ 'https://youtu.be/…'`
plays media straight from the command line with no config file, mapping each
argument to the right scene type by extension.

**Config authoring and discovery, all offline.** An annotated TOML reference,
an interactive wizard (`--init`), and a JSON Schema for editor autocomplete —
plus `--describe`, `--list-scenes`, `--list-overlays`, `--list-modes`,
`--compat`, and `--print-schema`, all generated from the same field metadata the
loader itself runs on, so they cannot drift from the code. `--doctor` collects
every config and environment problem in one pass. Machine-local defaults live in
`~/.config/c64cast/settings.toml` (written by `--save-settings`) and persisted
state in `~/.local/share/c64cast/`, both XDG-aware.

**Packaged demo configs.** Every feature has a runnable single-scene demo
shipped inside the wheel: `--config example:NAME` runs one, `--list-examples`
lists them all, `--print-example NAME` copies one out to edit.

**Preview and recording.** An optional local window mirroring what the C64 is
showing, and recording the same to MP4. Both are cv2-based, so neither needs an
optional dependency.

**Documentation.** A typeset User's Guide (10 chapters, rendered to PDF with
`make guide`), a full config reference, symptom-first troubleshooting, an
extension guide, and per-module architecture notes covering the hardware
constraints and the dead ends behind each design decision. Every install
instruction and every missing-extra hint names `uv`; `pipx` is documented once
as an equivalent fallback.

**A leading `~` works in config-file paths.** `file`, `videos_dir`,
`songlengths_file`, `charset_path`, `model_path`, a `logo` overlay's file, the
recording path and `log_file` all expand `~/…` when they are used. A TOML file
has no shell to do it, and `glob`/`os.path` treat `~` as a literal directory
name, so such a path previously matched nothing. A `Config` still holds the
string as written, so serialized configs keep the `~` rather than baking in an
absolute home directory.

[Unreleased]: https://github.com/kfox/c64cast/commits/main
