# Example configs

Each TOML in this directory is a runnable single-scene demo of one
scene type or overlay. Use them when you want to *see* a feature in
isolation, without comments and unrelated scenes obscuring it.

For the full annotated reference covering every option, see
[`c64cast.example.toml`](../c64cast.example.toml).

## Start here: `hello.toml`

[`hello.toml`](hello.toml) is the simplest demo and the recommended
first run. It scrolls big text across a solid canvas and needs **nothing
but a reachable Ultimate 64** — no webcam, microphone, SID files, video
clips, or optional extras. Every other demo below assumes some piece of
hardware or media; this one doesn't.

```bash
python -m c64cast --config config/examples/hello.toml
```

Edit the `[ultimate64].url` at the top of the file (or pass
`--url http://YOUR-U64`) and you should see scrolling text immediately.

## Running

```bash
python -m c64cast --config config/examples/overlay-clock.toml
```

Any `--url`, `-A`, or other CLI flag still overrides the config.

## Single-scene mode

When a config defines exactly one scene, the Playlist enters
**single-scene mode** automatically:

- No interstitial appears between cycles.
- The scene loops forever — when `duration_s` expires (or
  `process_frame` returns False), the scene tears down and sets back
  up to run again.
- CTRL key skip events are ignored (there's nowhere to skip *to*).
- The C= (Commodore key) pause/resume still works.
- `[playlist] interleave_videos` is ignored (an inserted video would
  promote the playlist to multi-scene mode and defeat the demo).

Exit with `Ctrl+C`.

## File index

### Scene demos

| File                              | What it demonstrates                                       |
|-----------------------------------|------------------------------------------------------------|
| `hello.toml`                      | **Start here** — big scroller, no hardware/media needed.   |
| `scene-webcam-petscii.toml`       | Live webcam → PETSCII char mode (40×25).                   |
| `scene-webcam-hires.toml`         | Live webcam → 320×200 hi-res bitmap.                       |
| `scene-webcam-hires_edges.toml`   | Hi-res bitmap with Canny edge detection.                   |
| `scene-webcam-mcm.toml`           | Multicolor character mode.                                 |
| `scene-webcam-mhires.toml`        | Multicolor hi-res bitmap (4 colors, 160×200 effective).    |
| `scene-webcam-audio.toml`         | PETSCII webcam + live mic through the SID DAC.             |
| `scene-blank.toml`                | Blank PETSCII canvas + a `big_text` overlay.               |
| `scene-slideshow.toml`            | Cycle through still images from a directory/glob.          |
| `scene-video.toml`           | Video + soundtrack playback. Requires `video` extra. |
| `scene-waveform.toml`             | SID jukebox + oscilloscope. Requires a `.sid` file.        |
| `scene-midi.toml`                 | MIDI → SID synth. Requires `midi` extra + MIDI source.     |
| `scene-launcher.toml`             | Launch a native `.prg`/`.crt` and hand over the machine.   |
| `color-force-palette.toml`        | EXTREME `[color].force_palette` remap. Needs PyAV.         |
| `teensyrom-blank.toml`            | TeensyROM+ backend → blank canvas + scrolling text.        |

### Ensemble demo

| Directory                  | What it demonstrates                                  |
|----------------------------|-------------------------------------------------------|
| `ensemble/`                | 3-system video wall + cross-system big_text scroll.   |

See [`ensemble/README.md`](ensemble/README.md) for a walkthrough of
the cross-system orchestration model. Unlike the single-scene files
above, the ensemble demo needs at least two reachable U64s on the
LAN (or one real + a stub).

### Overlay demos

Each overlay file picks the smallest compatible scene as a host.

| File                                  | Overlay                | Host scene             |
|---------------------------------------|------------------------|------------------------|
| `overlay-scrolling_text.toml`         | scrolling_text         | PETSCII webcam         |
| `overlay-marquee.toml`                | marquee                | PETSCII webcam         |
| `overlay-rss.toml`                    | rss                    | PETSCII webcam         |
| `overlay-spectrum_petscii.toml`       | spectrum_petscii       | PETSCII webcam + audio |
| `overlay-clock.toml`                  | clock                  | PETSCII webcam         |
| `overlay-weather.toml`                | weather                | PETSCII webcam         |
| `overlay-callsign.toml`               | callsign               | PETSCII webcam         |
| `overlay-countdown.toml`              | countdown              | PETSCII webcam         |
| `overlay-network.toml`                | network                | PETSCII webcam         |
| `overlay-logo.toml`                   | logo                   | PETSCII webcam         |
| `overlay-big_text.toml`               | big_text               | Blank canvas           |
| `overlay-obs_status.toml`             | obs_status             | PETSCII webcam         |

## Audio-enabled demos

Five files exercise the audio path; the first three require `[audio] enabled = true`:

| File                              | Audio source                                     |
|-----------------------------------|--------------------------------------------------|
| `scene-webcam-audio.toml`         | Mic capture → SID DAC (needs `mic` extra)        |
| `overlay-spectrum_petscii.toml`   | Mic capture + visual 8-band FFT (needs `mic`)    |
| `scene-video.toml`           | Video-file soundtrack (needs `video`)      |
| `scene-waveform.toml`             | Native SID playback of a `.sid` file             |
| `scene-midi.toml`                 | MIDI input → in-process SID synth (needs `midi`) |

The waveform and MIDI scenes drive the U64's SID chip directly and ignore
the `[audio]` section.

## Editing a demo for yourself

Copy any file to your working directory as `c64cast.toml` (or anywhere
and pass `--config PATH`), then edit it. Combining overlays, swapping
display modes, etc. — the kitchen-sink reference at
[`c64cast.example.toml`](../c64cast.example.toml) covers every
option a single demo doesn't.
