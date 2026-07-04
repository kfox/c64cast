# Troubleshooting

Symptom-first index — find what you're seeing, follow the link to the
cause. Most of these are documented in [caveats.md](caveats.md); this
file is the "I saw X, what now?" companion.

**Start here when a config won't load or run:** `python -m c64cast
--doctor --config your.toml` validates every scene/overlay/orchestrator,
checks which optional install extras are present, and pings each
system's U64 — all without starting the stream. See
[usage.md "Validating a config"](usage.md#validating-a-config). Most
"why won't it start" questions answer themselves from the doctor report.

If your problem isn't here, run with `-vv` (debug logging) and check
the stats line printed every 10 s — `errors/s > 0` usually points
at the right corner of the system.

## Audio symptoms

### "Audio sounds robotic / metallic / quantized"

Working as intended. The SID DAC streaming path is 4-bit @ 8 kHz; that
*is* the format a real C64 plays through. Raising `[audio] sample_rate`
does NOT improve quality — the C64-side NMI is sized for 8 kHz and
nothing in the pipeline resamples; other rates play at the wrong
pitch. See [caveats.md → "Audio is intentionally lo-fi"](caveats.md#audio-is-intentionally-lo-fi).

### "Audio cuts in and out / drops to a steady `writes=4/s` trickle"

The audio worker can't get fresh samples onto the U64's ring buffer fast
enough, so it pads with neutral samples — audible as dropouts. (There is
no client-side write queue to watch under Socket DMA; the TCP send buffer
is the only buffer, and `--profile` reports `u64 dma latency` rather than
a queue percentage.) Possible causes:

- LAN saturated by something else (other streaming, large transfers).
  Move the U64 onto wired Ethernet.
- DMA latency is spiking — run `--profile` and check the
  `u64 dma latency` line. Sustained values well above ~5 ms mean the
  network or U64 is congested.
- `[audio] sample_rate` raised past 8 kHz. Set it back (the C64-side NMI
  is sized for 8 kHz; nothing resamples).
- For a `video` scene stuck at `writes=4/s bytes=4KiB/s` for
  minutes after the clip should have ended, the demuxer hit EOF but the
  video buffer never cleared — that's a known edge handled in
  `AVFileSource.current_frame`; update to a build that includes the fix.
- U64 firmware older than 3.x. Update.

### "No audio at all, mic is enabled"

1. `python -m c64cast --list-devices` — is your mic listed under
   "Audio input devices"? If not, your OS denied microphone permission.
2. Check `[audio] noise_gate` — at 0.05 it cuts only background hum,
   but if your mic level is low everything gets gated. Lower it to
   0.01 to test.
3. `pip install -e .[mic]` — without the `mic` extra the audio path
   silently disables itself with one warning.

### "Mic capture works but I hear my own voice loud over the speakers"

You probably have a desktop mic + speakers without echo cancellation,
and the C64 is just playing back what you said. Use headphones for
talking and the C64 for ambient/music.

## Video symptoms

### "Webcam shows but everything is solid black / one color"

For `hires_edges`: the Canny edge detector found no edges. Try
`display = "hires"` to confirm capture is working, then re-enable
edges in better light or with `mic_sensitivity` raised (doesn't apply
to video — wrong knob; the right fix is more contrast in the scene).

For other modes: the quantizer landed on a single dominant color.
Usually means the scene is genuinely monochrome (point camera away
from a white wall).

### "Webcam doesn't appear in --list-devices on macOS"

Grant Terminal (or whichever app is running Python) Camera permission
under **System Settings → Privacy & Security → Camera**, then quit and
relaunch Terminal. OpenCV's AVFoundation backend will then enumerate.

### "Multi-line preview window or recording, but they don't match what the U64 shows"

Two known causes:

1. **CHARGEN ROM missing** — the preview falls back to a built-in 8×8
   ASCII font, which renders PETSCII line-art as garbage. Drop a real
   `characters.901225-01.bin` in `assets/roms/` (see
   [assets/roms/README.md](../assets/roms/README.md)) and the preview
   will match.
2. **You changed display modes mid-frame** — the framebuffer shadow
   follows API writes but doesn't model bank/mode switches as
   precisely as the real VIC. The next full frame paint corrects it.

### "Preview window scale is too small / too big"

`[preview] scale = 3` → window is 3× the C64's 320×200. Drop to 2 for
a smaller window, raise to 4 for a giant one. The preview uses pygame
at integer scaling so non-integer values would alias badly.

## Playlist + control

### "Pressing the Commodore key does nothing"

The C= → pause path needs the kernal IRQ to be running because it
reads `$028D` which is only updated by the kernal keyboard scan. If
some scene installed an IRQ handler at `$0314` and didn't chain back
to `$EA31` on the way out, `$028D` stops updating and pause/skip stop
responding mid-scene.

The bundled scenes shouldn't trigger this — `WaveformScene`'s player
chains to `$EA31` after every PLAY call, and the audio NMI path uses
the NMI vector (`$0318`), not the IRQ vector. If you've added a custom
scene that hooks `$0314`, make sure it preserves the chain.

The CTRL key (skip) also relies on `$028D`, so the same applies.

### "Playlist freezes between scenes"

Usually the new scene's `setup()` is blocking — the U64 might be
unreachable. Run with `-vv` to see the per-write debug log; you'll
see retries piling up if so. Eventually the scene gives up and the
playlist advances to the next interstitial.

### "POST /skip returns 200 but nothing happens"

The skip event fires on the next frame, after `process_frame` returns.
If the current scene is in a `time.sleep()` or blocked on a network
read, it won't see the skip until that finishes. Worst-case wait is
the scene's `target_fps` period (33 ms at 30 fps).

If skip never works at all, you're missing the `control` extra —
`pip install -e .[control]`. The control plane silently disables
itself with one warning if FastAPI isn't installed.

Also: skip is intentionally a no-op in **single-scene mode** (when the
config defines exactly one scene). Look for `skip ignored — single-scene
mode` in `-vv` logs. See
[caveats.md → "Single-scene mode"](caveats.md#single-scene-mode-is-automatic-not-opt-in).

### "Interstitial never appears between scenes"

If your config defines exactly one scene, the Playlist enters
single-scene mode and the interstitial path is bypassed entirely. Add a
second scene to bring it back. Same applies if `[playlist] interleave_videos`
is your only source of additional scenes — single-scene mode short-
circuits video interleaving (you'll see `interleave_videos skipped: single-scene
playlist` in the logs).

## Scenes

### "`video` scene type is rejected at load time"

You didn't install the `video` extra
(`pip install -e .[video]`). The loader emits "Found N video files
but PyAV is not installed; skipping videos" and continues
without videos.

### "A streaming/YouTube video stops partway with `OSError: [Errno 5] Input/output error`"

The demuxer logged `demux <url> crashed` with an `Input/output error`
traceback out of `container.demux()`. A yt-dlp-resolved YouTube URL is a
single `googlevideo` CDN stream that the CDN throttles and periodically
drops mid-playback. `AVFileSource` now opens remote (`http(s)://`) inputs
with FFmpeg's reconnect options, so a transient drop resumes automatically
instead of crashing. If a stream still fails to the end, the URL may have
expired (yt-dlp URLs carry an `expire=` timestamp) — re-run to re-resolve
it, or play a local copy of the file.

### "`waveform` scene plays for 180 s and stops, but the tune is longer"

Default duration is 180 s when no SongLengths DB is configured. Two
fixes:

1. Set `duration_s = <seconds>` on the scene explicitly.
2. Configure `[playlist] songlengths_file = "assets/sids/C64Music/DOCUMENTS/Songlengths.md5"`
   and the loader will look up the tune's real length.

See [caveats.md → "WaveformScene duration"](caveats.md#waveformscene-duration).

### "`midi` scene starts but no notes play"

Check the log for `MidiScene: opened MIDI port <name>` — if missing,
the port doesn't exist or the name pattern didn't match. List ports
with:

```bash
python -c 'import mido; print(mido.get_input_names())'
```

Then set `midi_port = "..."` (substring match is fine) in your scene
config.

If the port opens but you still hear nothing: the SID master volume
(`midi_master_volume`) might be 0, or you may have a `waveform` scene
running concurrently that's stomping $D418.

## Overlays

### "Overlay paints PETSCII screen codes and only renders correctly with display = 'petscii'"

Most overlays paint PETSCII glyphs into screen+color RAM ($0400/$D800).
MCM reinterprets color RAM bit 3 as "multicolor cell" and renders pixel
pairs at half horizontal resolution, so PETSCII glyphs come out garbled;
bitmap modes don't expose the character matrix at all. Move the overlay
to a `display = "petscii"` scene.

### "`weather` / `rss` / `obs_status` show '...' forever"

Background fetch failed. Reasons:

- Network down. Check with `curl <url>` from the same host.
- For `weather` `provider = "open-meteo"`: you forgot to set `lat` /
  `lon`. For `wttr.in`: you forgot to set `location`.
- For `rss`: the URL returns non-XML (a 200 OK redirecting to an HTML
  page is common). Try the feed in a browser; if it shows HTML, the
  publisher probably moved the feed.
- For `obs_status`: OBS isn't running, the websocket port is closed,
  or the password is wrong. Check OBS → Tools → WebSocket Server
  Settings.

## Installation / Setup

### "pip install fails: error compiling sounddevice / PyAV / pygame"

These have system-level dependencies (portaudio, ffmpeg headers, SDL2).
On macOS: `brew install portaudio ffmpeg sdl2`. On Debian/Ubuntu:
`apt install portaudio19-dev libavformat-dev libsdl2-dev`. Then
retry the pip install.

If you don't need a specific feature, drop the corresponding extra:
`pip install -e .[mic]` instead of `.[all]`.

### "ImportError: cannot import name 'X' from 'c64cast.overlays'"

You're running against a stale install. From the repo root:

```bash
pip install -e .[all]
```

The `-e` flag means subsequent edits are picked up live, but the
initial install still needs to register the package.

### "mypy / ruff not found"

Install the dev tooling: `uv sync --all-extras` (or, without uv,
`pip install --group dev` — `dev` is a PEP 735 dependency-group, not an
extra, so `pip install -e .[dev]` will not find it). The pre-commit and
CI configurations assume those are present.

### "objc[NNNNN]: Class AVFFrameReceiver is implemented in both ... libavdevice ..."

macOS warning, not an error. Both `opencv-python` and `av` (PyAV) bundle
their own copy of FFmpeg's `libavdevice` dylib, and each registers the
same `AVFFrameReceiver` / `AVFAudioReceiver` Objective-C classes on
import. The runtime warns about the duplicate; the second registration
is ignored. Triggers when a `video` scene loads PyAV after OpenCV
is already imported. In this project neither library uses AVFoundation
capture (OpenCV reads UVC devices, PyAV reads files), so the warning is
harmless. Suppression would require building OpenCV against system
FFmpeg (e.g. Homebrew) instead of using the wheel — usually not worth
the install complexity.

## Performance

### "Heartbeat shows `writes=10/s` even though target_fps=60"

The U64 (or the LAN) can't keep up. Bitmap modes are most expensive —
`HiresDisplayMode` pushes 8 KB per frame. The Playlist drops frames
automatically when it falls more than 2 frame-times behind. If you're
seeing this on a wired LAN, profile with `make bench` and compare.

### "Frame rate is fine, but the heartbeat shows `skipped=N/s` growing"

That's the delta cache doing its job — `skipped` counts frames where
*nothing* changed and the API correctly elided the upload. A high
skip rate is good. (It's only suspicious when paired with visible
movement on the U64, which would mean the cache is suppressing real
updates — call `api.invalidate_cache()` in your scene's `setup()`.)
