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

### Added

- **c64cast reads the C64 character ROM off your own machine.** Every glyph
  drawn as C64 text — the text overlays on bitmap modes (`scrolling_text`,
  `marquee`, `corner_text`, `logo`), `big_text`, the on-C64 menu, the
  oscilloscope's labels, the preview window and the stream recorder — comes from
  the character ROM. Previously the only way to have one was to find a dump and
  drop it at a working-directory-relative path in a source checkout, which meant
  an installed c64cast could never resolve it and a user report of "the
  scrolling text looks bad" was, in full, "there is no character ROM". Now the
  first run against a machine reads it off the C64 and caches it at
  `<data dir>/roms/chargen.bin`; every later run picks it up. It costs about a
  second, once per machine, and no ROM bytes are shipped or downloaded — they
  move from your hardware to your disk. `--dump-char-rom` re-reads on demand
  (after swapping in a different character ROM, say), `--install-char-rom PATH`
  installs a 2 KB or 4 KB dump you already have with no hardware involved, and
  `[hardware].dump_char_rom = false` turns the automatic read off. `--doctor`
  reports which ROM is in use and whether it verifies.
- **A second book: the Programmer's Reference Guide** (`docs/reference/`), the
  volume you open at the page you need rather than read in order. Chapters 1 to
  3 are written — the configuration language and its precedence rules, the
  catalogue of every scene and overlay, and the display pipeline from frame to
  VIC-II register; chapters 4 to 6 are outlines for now. Its appendices are
  complete and are *generated* from
  the code by `scripts/gen_reference_appendices.py`: every configuration section
  and field, every scene key, every overlay parameter, the overlay against
  display-mode matrix, every generator and effect, every live-tune target, every
  command-line flag and every packaged example. They read the same definitions
  that answer `--describe`, `--compat` and `--print-schema`, so a table in the
  book cannot disagree with the program. `make reference` renders it, `make
  books` renders every book, and `make reference-appendices` rewrites the
  generated ones — which CI checks for drift.

### Changed

- `[preview] charset_path` now defaults to unset, meaning "use the character ROM
  c64cast resolved". Set it to force a specific file. A configured path that
  doesn't exist now warns and falls back to the built-in font instead of raising
  `FileNotFoundError` and killing the run.
- The built-in fallback font now fills screen codes `$80-$FF` as the reverse-video
  complement of `$00-$7F`, like the real ROM. They were blank, so with no
  character ROM installed `big_text`'s glyph pixels, the `blocks` PETSCII style
  and most of the PETSCII shading ramp — all of which paint `$A0` and up —
  rendered as nothing.
- The User's Guide build now renders *a book* rather than *the guide*, in
  preparation for a second volume. `scripts/build_guide.py` is
  `scripts/build_book.py --book-dir docs/<book>`, the Typst template and the
  vendored OFL fonts moved from `docs/guide/` to `docs/shared/`, and each book's
  `book.toml` names the layout it takes. `make guide` and the released PDF are
  unchanged.

## [0.1.0] - 2026-07-30

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

**Documentation.** A typeset User's Guide (10 chapters), a full config
reference, symptom-first troubleshooting, an extension guide, and per-module
architecture notes covering the hardware constraints and the dead ends behind
each design decision. Every install instruction and every missing-extra hint
names `uv`; `pipx` is documented once as an equivalent fallback.

The guide is attached to every release as a PDF, stamped on its cover with the
version it documents, so a downloaded copy can always be matched to the install
it describes. `make guide` renders the same thing from a checkout.

A config written by `--init` or `--save-settings` carries a `#:schema`
directive pinned to its own release, so an editor validates it against the
schema this version actually accepts rather than whatever is currently on
`main`.

**A leading `~` works in config-file paths.** `file`, `videos_dir`,
`songlengths_file`, `charset_path`, `model_path`, a `logo` overlay's file, the
recording path and `log_file` all expand `~/…` when they are used. A TOML file
has no shell to do it, and `glob`/`os.path` treat `~` as a literal directory
name, so such a path previously matched nothing. A `Config` still holds the
string as written, so serialized configs keep the `~` rather than baking in an
absolute home directory.

**Windows is a supported platform.** It always worked — casting to a real
Commodore from Windows is a routine path for one of the contributors — but the
published metadata and the User's Guide both called it untested, because CI only
ever ran on Linux. The test matrix now covers macOS, Linux and Windows across
Python 3.11–3.14, so the claim is backed on both halves: the matrix for the
host-side code, real hardware for the pipeline. The one platform difference worth
knowing is that `SIGHUP` config reload is POSIX-only; `POST /reload` on the
control plane does the same thing everywhere.

[Unreleased]: https://github.com/kfox/c64cast/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kfox/c64cast/releases/tag/v0.1.0
