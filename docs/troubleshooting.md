# Troubleshooting

Symptom-first index — find what you're seeing, follow the link to the
cause. Most of these are documented in [caveats.md](caveats.md); this
file is the "I saw X, what now?" companion.

**Start here when a config won't load or run:** `c64cast
--doctor --config your.toml` validates every scene/overlay/orchestrator,
checks which optional install extras are present, and pings each
system's U64 — all without starting the stream. See
[the Programmer's Reference Guide, "Validation"](reference/02-config-rules.md#validation). Most
"why won't it start" questions answer themselves from the doctor report.

If your problem isn't here, run with `-vv` (debug logging) and check
the stats line printed every 10 s — `errors/s > 0` usually points
at the right corner of the system.

## Audio symptoms

### "Audio sounds robotic / metallic / quantized"

Mostly working as intended — the SID's `$D418` DAC is inherently low-fi, and
that character *is* the sound of a real C64. But if reaching for `[audio]
sample_rate` is your instinct, that's the wrong lever; here's the accurate
picture and the knobs that actually help:

- **Rate isn't the quality knob.** The default is 12 kHz. The C64-side NMI
  period is derived *from* `sample_rate` (it programs the CIA #2 Timer A
  latch), so raising the rate keeps pitch correct and lifts the Nyquist
  modestly — 12 kHz already carries the fricatives/sibilants 8 kHz lost. But
  the NMI handler has a fixed cycle budget, so there's little headroom above
  the default: the live pipeline starts underrunning around ≈12.5 kHz, and
  rates past the isolated-handler ceiling (≈13.6 kHz NTSC) are *rejected at
  load* (`c64.nmi_rate_safety`).
- **Bit depth is `[audio] dac_curve`, not the rate.** The default `"auto"`
  already lifts the U64's (deterministic emulated) SID to the Mahoney 8-bit
  `$D418` technique — ≈6-7 effective bits, not 4 — and `--calibrate-dac`
  extends that to a physical SID. Only an uncalibrated physical/unknown chip
  falls back to the classic 4-bit linear path.
- **`--calibrate-dac` says "capture device N is not carrying the calibration
  ring"** (exit 3). It recorded from an input that doesn't have the C64's audio
  on it — by far the most common cause is the auto-picked device being the
  machine's own microphone, which records room noise and measures like a dead
  chip. The message lists every input; pass the right one with
  `--audio-device N` (index or name substring). The capture has to be the input
  the C64's audio actually arrives on: an HDMI capture stick, a Cam Link, or a
  line-in fed from the AV port. If the device *is* right, check that HDMI audio
  is enabled and the input gain isn't at zero. A warning line earlier in the run
  ("doesn't look like a video-capture input") flags the same thing before the
  measuring starts.
- **`--calibrate-dac` says "REJECTED — the volume-0 self-test is off by N%"**
  (and exits 4 if every SID failed). The measurement came back internally
  inconsistent: codes `$h0` set the master volume to 0, so they *must* measure
  as silence, and on this run they didn't. No table is written and playback
  keeps the previous curve — deliberately, because a wrong table sounds worse
  than none. A healthy run lands near 1%, so a large miss points at the capture
  rather than the chip: check that the capture device is actually fed by the SID
  output, that nothing else is mixed into it, and that the level isn't clipping.
  The raw levels are saved in the calibration file for diagnosis; `metrics`
  there also carries per-ring pass spreads (`pass_spread_p95_frac`, which the
  trust gate reads, and `pass_spread_frac`, the worst single slot), which are
  large when the capture couldn't be read reliably at all.
- **`--calibrate-dac` says "the calibration ring is playing and is being
  recorded, but the passes disagree…"**. Each capture plays the same ladder
  several times over and compares the passes; one whose passes disagree
  broadly is refused, because a table fitted to levels that move is worse
  than none. The input is right — the message says so up front — and it also
  says which *kind* of unsteady it saw, because the two have opposite fixes.
  A **level drift** means the ring replayed faithfully while the level it was
  measured through moved: let the machine play a few seconds before
  calibrating, and check nothing in the capture path applies AGC or its own
  level control. Passes that **differ in shape** mean something besides the
  ring is reaching the output: another SID, a sampler channel or a drive
  still up in the machine's mixer, or a tune still playing — over a link
  with no config API nothing is muted for you. Nothing is written and
  playback keeps the previous curve; the refused capture is saved under
  `calibration/unusable/` in the data directory with its diagnostics, and
  the failure names the exact path.
- **On the U64, video audio isn't on the `$D418` DAC at all by default.**
  `[audio] backend = "auto"` uses the off-bus Ultimate Audio PCM sampler,
  far higher fidelity than any `$D418` path. Persistent robotic *video*
  audio usually means the DAC backend was forced (`backend = "dac"`) or the
  sampler wasn't available.

See [caveats.md → "Audio is intentionally lo-fi"](caveats.md#audio-is-intentionally-lo-fi-the-4-bit-d418-dac).

### "Constant hiss under everything on a TeensyROM+"

If the TeensyROM+ is in the cartridge port of a C64U / Ultimate 64, check the
**Ultimate's** setting before any c64cast one: **F2 → Cartridge and ROM Settings
→ Bus Operation Mode**, which defaults to `Quiet`. Set it to `Writes` (or
`Dyn. & Writes`), back out with <kbd>RUN/STOP</kbd> and save. Nothing under
`[audio]` or `[dsp]` clears it, and c64cast can't set it for you — the run's
connection is to the TeensyROM+, not to the Ultimate.

See [caveats.md → "A TeensyROM+ in an Ultimate needs Bus Operation Mode set to
Writes"](caveats.md#a-teensyrom-in-an-ultimate-needs-bus-operation-mode-set-to-writes).

### "Audio cuts in and out / drops to a steady `writes=4/s` trickle"

The audio worker can't get fresh samples onto the U64's ring buffer fast
enough, so it pads with neutral samples — audible as dropouts. (There is
no client-side write queue to watch under Socket DMA; the TCP send buffer
is the only buffer, and `--profile` reports `u64 dma latency` rather than
a queue percentage.) Run with `-v` first: on the `$D418` DAC path the worker
logs a short health line every few seconds — underruns, late ring sub-writes,
write and consumer rates — which tells a fault that is present throughout
from one that appears part-way in, and the stop summary reports the run's
late-write share. Possible causes:

- LAN saturated by something else (other streaming, large transfers).
  Move the U64 onto wired Ethernet.
- DMA latency is spiking — run `--profile` and check the
  `u64 dma latency` line. Sustained values well above ≈5 ms mean the
  network or U64 is congested.
- `[audio] sample_rate` pushed near the ceiling. The default 12 kHz already
  sits just below the ≈12.5 kHz streaming-underrun onset, so if you raised
  it, nudge it back toward the default. (Rates past ≈13.6 kHz NTSC are
  rejected at load outright, so this only bites in the 12.5–13.6 kHz band.)
- For a `video` scene stuck at `writes=4/s bytes=4KiB/s` for
  minutes after the clip should have ended, the demuxer hit EOF but the
  video buffer never cleared — `AVFileSource.current_frame` handles that
  EOF edge, so a run still showing it is not on current code.
- U64 firmware older than 3.x. Update.

### "No audio at all, mic is enabled"

1. `c64cast --list-devices` — is your mic listed under
   "Audio input devices"? If not, your OS denied microphone permission.
2. Your mic level is low and the noise-floor cleanup is squelching
   everything. By default the `[dsp]` chain is ON and its downward expander
   handles the floor — lower `[dsp] expander_threshold_db` (default `-45`)
   or raise `[audio] mic_sensitivity` to test. (`[audio] noise_gate` only
   applies when `[dsp] enabled = false`; on that path, lower it from
   `0.05` toward `0.01`.)
3. You don't have the `mic` extra — without it the audio path silently
   disables itself with one warning. `c64cast --doctor` lists every extra it
   can see; reinstall with `uv tool install --force 'c64cast[all]'` (extras
   don't accumulate, so name them all at once).

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

### "The preview window (or the recording) is black, but the C64 looks fine"

Expected, and not a bug in the scene: the preview is a host-side
reconstruction of the writes c64cast sends, so any frame that reaches the
VIC by a route other than host DMA is invisible to it.

1. **A bitmap scene on the staged or double-buffered path** — the common
   case, because both are on by default. `[video].use_reu_staged = "auto"`
   stages hires/mhires frames through the REU on a REU-enabled U64, and
   `double_buffer = "auto"` page-flips them on a TeensyROM; neither routes
   through the write listener the shadow watches, and the renderer reads a
   fixed `$2000`/`$0400` without modeling `$DD00` banking. Set
   `[video].use_reu_staged = false` to bring the picture back into the
   window — at the cost of the tear-free path on the C64 itself.
2. **A `launcher` scene** — the `.prg`/`.crt` draws on the Commodore with no
   host-side pixel writes at all, so there is nothing to reconstruct. This
   one has no workaround.

Neither is visual verification of what the VIC drew; that needs a capture
device. Full list of blind spots:
["Preview window fidelity + limits"](caveats.md#preview-window-fidelity--limits).

### "Multi-line preview window or recording, but they don't match what the U64 shows"

Two known causes:

1. **No character ROM yet** — the preview falls back to a built-in 8×8
   ASCII font, which renders PETSCII line-art as garbage. See
   ["Text or the scrolling text looks blocky or wrong"](#text-or-the-scrolling-text-looks-blocky-or-wrong)
   below.
2. **You changed display modes mid-frame** — the framebuffer shadow
   follows API writes but doesn't model bank/mode switches as
   precisely as the real VIC. The next full frame paint corrects it.

### "`[preview] enabled = true` but no window appears"

Check the log for `preview disabled: cannot open a window`. That means the
installed opencv has no GUI support — either a headless wheel
(`opencv-python-headless`, often pulled in transitively) is the one occupying
the `cv2` namespace, or there is no desktop session at all (ssh without X, a
container).

`c64cast --doctor` names the build that actually loaded and flags it as
headless, which distinguishes the two causes. If it is headless, the fix is an
environment that does not contain a headless wheel at all — find what pulled it
in (`uv pip tree`) and install without that, rather than installing a GUI wheel
on top: co-installing both just makes the winner depend on write order.

Reinstalling `c64cast[all]` is *not* the fix — see
["--doctor says two opencv distributions share the `cv2` namespace"](#--doctor-says-two-opencv-distributions-share-the-cv2-namespace)
below for why.

Note the window is drawn by the main thread while the playlist renders on a
worker thread, so a wedged playlist leaves the window up but frozen.

### "`--doctor` says two opencv distributions share the `cv2` namespace"

Every opencv wheel — `opencv-python`, `opencv-contrib-python`, and the
`-headless` variants of each — unpacks into the same `site-packages/cv2/`
directory, but they are separate distributions as far as the installer is
concerned, so it will happily install several. Only one set of files can
survive. Whichever was written last is the one that loads, and nothing in
`uv.lock` or `uv pip list` records which that was.

With the `vision` extra this is expected: mediapipe depends on
`opencv-contrib-python`, so `c64cast[vision]` and `c64cast[all]` both end up
with two. It is also harmless there — contrib is a superset of the plain
build, and c64cast uses nothing outside it. This is why reinstalling with
`[all]` does not cure an opencv problem: `[all]` includes `vision`, so it is
the install that creates the situation.

If you need the version c64cast pins to be the one that loads, install
without `vision` and give up hand-gesture control:

```bash
uv tool install --force \
  'c64cast[video,mic,control,obs,midi,logging,tr,wizard,camera,yt,wled]'
```

That is every extra except `vision` — `all` minus the one that brings the
second opencv.

### "Preview window scale is too small / too big"

`[preview] scale = 3` → window is 3× the C64's 320×200. Drop to 2 for
a smaller window, raise to 4 for a giant one. Scaling is integer +
nearest-neighbor so C64 pixels stay square and crisp; non-integer
values would alias badly, so the field is an int.

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
reinstall with `uv tool install --force 'c64cast[all]'`. The control plane
silently disables itself with one warning if FastAPI isn't installed.

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

You didn't install the `video` extra (`uv tool install --force
'c64cast[all]'`). The loader emits "Found N video files
but PyAV is not installed; skipping videos" and continues
without videos.

### "A streaming/YouTube video stops partway with `OSError: [Errno 5] Input/output error`"

The demuxer logged `demux <url> crashed` with an `Input/output error`
traceback out of `container.demux()`. A yt-dlp-resolved YouTube URL is a
single `googlevideo` CDN stream that the CDN throttles and periodically
drops mid-playback. `AVFileSource` opens remote (`http(s)://`) inputs
with FFmpeg's reconnect options, so a transient drop resumes automatically
instead of crashing. If a stream still fails to the end, the URL may have
expired (yt-dlp URLs carry an `expire=` timestamp) — re-run to re-resolve
it, or play a local copy of the file.

### "`waveform` scene plays for 180 s and stops, but the tune is longer"

Default duration is 180 s when no SongLengths DB is configured or
auto-detected. Fixes:

1. Set `duration_s = <seconds>` on the scene explicitly.
2. Configure `[playlist] songlengths_file = "assets/sids/C64Music/DOCUMENTS/Songlengths.md5"`
   and the loader will look up the tune's real length.
3. Unpack HVSC under `assets/sids/` — the loader auto-detects
   `Songlengths.md5` there with no config needed (this is what quick
   playback, which has no `[playlist]` section, relies on).

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

### "Text or the scrolling text looks blocky or wrong"

Almost always: **c64cast has no character ROM**, so it is drawing C64 text
with a built-in ASCII substitute font instead of the real C64 glyphs. It is
most obvious on a bitmap display mode (`hires`/`mhires`), where every text
overlay — `scrolling_text`, `marquee`, `corner_text`, `logo` — goes through
that font, and on anything using PETSCII graphics characters (which the
substitute renders as blanks).

Check what's in use:

```bash
c64cast --doctor --skip-probe        # ENVIRONMENT → "character ROM"
```

If it says *not installed*, connect your C64 and let a normal run read it
(it happens automatically on the first run), or do it explicitly:

```bash
c64cast --dump-char-rom -u u64://192.168.2.64
```

If c64cast can't read from your setup — an emulator-only rig, or a TeensyROM
on firmware older than v0.7.2.5, which has neither `ReadC64Mem` nor the
IRQ-enabled idle the dump needs — install a dump you already have instead:

```bash
c64cast --install-char-rom /path/to/chargen.bin
```

Both commands verify the bytes really are a charset before writing anything,
so a bad file is refused rather than silently making things worse. See
[the User's Guide, "The Character ROM"](guide/04-setting-up.md#the-character-rom).

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

### "`c64cast: command not found`"

The install worked but its directory isn't on `PATH`. `uv tool update-shell`
then open a new shell. `uv tool run c64cast` works
meanwhile.

### "Install fails: error compiling sounddevice / PyAV"

Every dependency ships wheels for macOS, Linux and Windows, so a *compile* means
the installer couldn't match one to your platform and fell back to source. These
have system-level dependencies (portaudio, ffmpeg headers). On macOS:
`brew install portaudio ffmpeg`. On Debian/Ubuntu:
`apt install portaudio19-dev libavformat-dev`. Then retry.

On Windows there is no equivalent one-liner, and you should not need one — reach
for the narrower extra set below instead, or check that you are not on a Python
release newer than the wheels have caught up with.

If you don't need the feature, install a narrower extra set instead
(`uv tool install 'c64cast[video]'`).

### "A feature says its extra is missing, but I installed it"

Extras don't accumulate: installing `c64cast[midi]` over an existing
`c64cast[video]` leaves you with `midi` only. Name every extra you want in one
command, and use `--force` to overwrite the existing install:

```bash
uv tool install --force 'c64cast[all]'
```

`c64cast --doctor` prints an EXTRAS section listing exactly which ones the
running install can import.

### "ImportError from `c64cast.scenes.overlays`, or mypy / ruff not found"

Both are development-environment symptoms — a stale editable install and
missing dev tooling respectively. Neither can happen to an installed release.
See [CONTRIBUTING.md](../CONTRIBUTING.md) for the checkout setup, and
`make doctor` for its self-check.

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
