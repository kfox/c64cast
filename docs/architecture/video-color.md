# Video input & the color pipeline

Turning arbitrary video into VIC-II output: frame sources, the display-mode hierarchy, and every stage of the color pipeline (shaping, dither, quantization, forced palettes).

Part of the [architecture reference](../architecture.md). For end-user configuration see [usage.md](../usage.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`video.py` — WebcamSource (shared broker) + AVFileSource (PyAV)](#videopy--webcamsource-shared-broker--avfilesource-pyav)
* [`modes.py` — DisplayMode hierarchy](#modespy--displaymode-hierarchy)
* [`rolling_palette.py` + `palette.py` — forced-palette remap](#rolling_palettepy--palettepy--forced-palette-remap)
* [Framerate pacing & frame-dropping](#framerate-pacing--frame-dropping)

---

## `video.py` — WebcamSource (shared broker) + AVFileSource (PyAV)

### `WebcamSource` — the shared camera broker

An always-on broker. A single `cv2.VideoCapture` is single-consumer (every `.read()` consumes the next device frame; concurrent reads from two threads aren't safe), so one background grab thread owns the capture, continuously reads the newest frame, and `read()` hands out an independent **copy** of the latest frame. That lets the webcam scene (when active) and the always-on vision controller (`vision.py`) share **one** physical camera with no contention — and keeps the live-webcam path low-latency (always the freshest frame, stale ones overwritten). `WebcamScene._read_frame()` is unchanged — it still calls `source.read()`. The camera is opened once per stack in `cli.py` when `needs_webcam or cfg.vision.enabled`, stored on `SystemStack.source`, released at teardown.

`WebcamSource.__init__` takes `device: int | str` and resolves it through `camera.resolve_camera_index` (see the `camera.py` note below): a plain int stays a cv2 index opened with the default `CAP_ANY` (byte-identical to the historical behavior), while a **string** — a camera name substring or USB `VID:PID` — is matched against enumerated cameras and opened with the *matched backend* (`cv2.VideoCapture(index, backend)`), because the enumerated index is only valid for the apiPreference it was enumerated with. The string form is what makes a roaming USB capture stick (e.g. a Cam Link) selectable by identity instead of by a reboot-unstable index.

### `AVFileSource` — video playback

The playback source. The demuxer thread reads packets from one container, pushes resampled mono int16 audio straight through to AudioStreamer, and queues decoded video frames keyed by PTS. Consumers call `current_frame(audio_position_s)` which returns the latest frame whose PTS ≤ the clock and drops anything behind. **Drift can't accumulate** because the audio clock IS the reference — *as long as a fresh frame exists when the clock asks for it*.

### HTTP reconnect for remote streams

`_av_open` / `_HTTP_RECONNECT_OPTIONS`. A yt-dlp-resolved YouTube URL is a single progressive `googlevideo` CDN link that the CDN throttles (see its `cps=`/`ratebypass` query params) and periodically drops mid-stream; that surfaces as `OSError: [Errno 5] Input/output error` out of `container.demux()`, which the demux loop's broad `except` catches, logs as "crashed", and ends playback on. The fix is to open remote inputs with FFmpeg's http-protocol reconnect options (`reconnect`, `reconnect_streamed`, `reconnect_on_network_error`, `reconnect_delay_max=5`) so FFmpeg transparently re-establishes the connection and resumes from the current byte offset instead of erroring. `_av_open(path)` wraps `av.open` and injects these **only for `http(s)://` inputs** (`_is_remote_url`) — they're http-protocol-only options, so scoping them keeps FFmpeg from warning about unrecognized options on a local/file input. Every `av.open` site in the module (playback, audio-full decode, peak scan, color pre-scan) routes through it.

### Decode-time downscale

Config: `decode_target_size` / `_plan_decode_size`.

**The gap this closes.** The frame-selection model above is correct, but only works if the demuxer can produce frames in real time. It does not cover **supply**: when the decoder can't keep up, `current_frame` returns the newest frame it *has*, which falls progressively further behind the audio clock. Video lags and appears to drift, worst on heavy 4K clips.

**Root cause.** The demux loop converted every frame to BGR at **full source resolution** (`frame.to_ndarray("bgr24")`), and only then did the display mode `cv2.resize` it down to ≤320px. For a 4K source that convert-then-downscale costs ≈40 ms/frame on the U64 host — over the ≈33 ms budget at 29.97 fps, before codec decode is even counted.

**The fix.** `VideoScene` passes the display mode's `frame_target_size` — the only resolution it actually consumes — as `decode_target_size`. The demux loop plans a decode size once from the first frame (`_plan_decode_size`) and downscales **during** the yuv→bgr swscale pass, via `av.VideoFrame.reformat(w, h, "bgr24")`.

Measured ≈40 ms → ≈4 ms/frame, a 9× speedup on a 4K sync clip. The conversion, the center-crop, the auto_fit accumulator, and the final resize then all work on a ≈640px frame.

Two guards in `_plan_decode_size`:

* Post-crop dims stay ≥ `DECODE_HEADROOM` (2×) the target in **both** axes, mirroring `scenes._crop_to_aspect` so the anamorphic MHires target — where height > width — is honored.
* It never upscales. A source already small enough returns None, falling back to a plain full-res convert.

The same downscale applies to the one-shot color pre-scan (`_scan_video_samples`), since color statistics are distribution-based.

### Seek-sampled color pre-scan (`_scan_video_samples`)

The auto_fit and force_palette pre-scan needs a representative frame sample across the *whole* source, not real-time playback.

**The problem.** Decoding every frame — an earlier `if i % stride: continue` skipped only accumulation, not decode — is decode-bound and scales with file length:

| Clip | Old scan time |
| --- | --- |
| 61 s, 1080p h264 | 0.56 s |
| 266 s, 4K AV1 | **14.6 s** |

That is a startup pause growing without bound.

**The fix.** Seek to `max_samples` evenly spaced timestamps — midpoints of `[0, duration)` — and decode **one keyframe at each** (`_seek_sample_frames`, with `backward=True` landing on the keyframe ≤ target).

Keyframe-only is exactly right here: color stats are distribution-based, so a keyframe near each timestamp represents its region as well as an exact frame would. And it makes the scan roughly **constant-time regardless of length or codec**:

| Clip | New scan time |
| --- | --- |
| 61 s, 1080p h264 | ≈0.9 s |
| 266 s, 4K AV1 | ≈3.1 s |

Short clips pay a small per-seek overhead — an accepted trade for bounding the worst case.

**Duration and fallback.** Duration comes from the stream (`v_stream.duration × time_base`), else the container (`container.duration / av.time_base`). When neither is known (a live or unbounded stream), or seeking raises (a non-seekable input), it re-opens and falls back to the original sequential-decode stride (`_decode_sample_frames`), so nothing regresses on sources that can't seek.

Both paths share `_frame_to_scan_bgr`, the decode-time downscale above, and one decode pass serves force_palette and auto_fit alike.

### A/V-lag telemetry

`current_frame` records the chosen frame's rebased PTS (`last_frame_pts`) and exposes `video_buffer_depth`; `VideoScene._record_av_lag` logs `audio_clock − displayed_frame_pts` per displayed frame. Small + lag (≤ one source-frame interval) is healthy frame selection; a lag that climbs while the buffer sits near 0 is the decoder failing real time. This is **software-side and artifact-free** — the right way to measure A/V drift on this project (Cam Link audio capture uniformly time-compresses the recording under host DMA load — a load-dependent factor, not the sampler — so it can't measure absolute drift). Live line at `-vv` (every `AV_LAG_LOG_INTERVAL_S`); per-scene min/avg/max summary at teardown (`-v`, mirrors the sampler's write-ahead-lead line).

### Start offset (`start_s`)

`AVFileSource(..., start_s=N)` seeks the container to the keyframe at/just-before N (whole-container `seek` in AV_TIME_BASE microseconds, `backward=True`) before the demux thread starts, and the peak-scan container seeks too so normalization covers only the played portion. Because the playback clock starts at 0 (audio samples / wall-clock) while post-seek frame PTS are ≈N, the demux loop rebases every video frame's PTS by the **first decoded frame's PTS** (`_pts_offset`) so video restarts at ≈0 and tracks the clock — the no-seek path is unchanged (offset ≈ 0). Post-seek audio packets are interleaved near the same byte offset, so A/V stay aligned to sub-GOP precision; accuracy is keyframe-granular (exact-to-the-second start via decode-and-discard is a future refinement). Carried by `SceneCfg.start_s` (video-only; rejected on other types, negative rejected) → `VideoScene` → here. Quick playback fills it from a URL timestamp; a `[[scenes]]` video can set it directly.

### Bitmap + `$D418`-DAC tempo compensation (`tempo_scale`)

**The symptom.** On the host-DMA 4-bit DAC path (`[audio].backend = "dac"`) over a **bitmap** display mode, everything plays ≈12 % slow — at correct pitch.

**The cause.** The audio worker shares the single socket-DMA link with heavy REU bank-swap bitmap writes. Under that load the host-DMA servo reads the ring pointer biased and throttles the worker by ≈12 %. Video is slaved to the audio drain clock (`position_seconds` → `_clock_s`), so both play at ≈1/`s`.

Pitch survives because the `$D418` *output* rate stays ≈ `sample_rate` — a pure tone reads ≈993 Hz for a nominal 1000. The ring under-fills and the NMI re-reads samples, which is a pitch-preserving time stretch.

**Why not fix the servo.** There is no free lunch on the host side: servo on is smooth but slow, open-loop has correct tempo but skips, and the REU pump is wobbly. No tuning gives both speed and smoothness.

**The fix — pre-compress the content.** Compress the content in the time domain by the inverse factor, so the system's own stretch nets back to real time.

`config.build_scene` resolves `tempo_scale = s`, the observed speed fraction, from `[audio].dac_bitmap_tempo_hires` / `_mhires`. It is gated to `backend == "dac"` **and** `isinstance(mode, BitmapDisplayMode)` **and** not `use_reu_pump`; anything else gets 1.0, since the off-bus sampler, the REU pump, char modes, and muted scenes do not stretch. It threads through `VideoScene._tempo_scale` into `AVFileSource`.

There, when `tempo_scale < 1.0`:

* `__init__` builds a one-stage `atempo` filter graph (`abuffer → atempo=1/s → abuffersink`), fed by the existing s16/mono/`target_sr` resampler output.
* `_demux_loop` pushes each resampled frame through it and drains the time-compressed result (`_drain_atempo`).
* At EOF, `_flush_atempo` pushes `None` and drains the buffered tail — without this the last fraction of a second is lost.
* Each rebased video PTS is multiplied by `s`.

The existing drain-clock A/V sync, which reads ≈`s`, then lands both compressed streams at real time, in sync, with pitch intact.

**What deliberately does not change.** `position_seconds` and `_clock_s` are untouched, and `clock/wall` telemetry still reads ≈`s` **by design** — it measures the drain rate, and the compensation makes the *content* real-time, not the drain clock. `decode_audio_full` (REU pre-encode) and `_scan_audio_peak` are off this path entirely, since the gate holds `tempo_scale` at 1.0 for them.

**Bounds.** `atempo` spans 0.5..2.0 per stage, so `validate_dac_bitmap_tempo_cfg` bounds `s` to 0.5..1.0 — keeping `1/s ≤ 2.0` in one stage.

**Where the defaults come from.** mhires 0.88 and hires 0.89 are the measured U64-II NTSC fractions. Hardware run 2026-07-02 gave `clock/wall` drain fractions of petscii ≈0.976, hires ≈0.906, mhires ≈0.894. The *mode ratio* is clean; the ≈2 % absolute offset is fixed startup latency. So the defaults are anchored on the ear-validated mhires `s=0.88`, with hires scaled by the measured 1.013× faster drain.

Other platforms — U64+PAL, U2P, TR+ PAL/NTSC — differ. Measure per platform with `scripts/diags/mhires_tempo_clock_ab.py`, which reads the `clock/wall` A/V-lag gauge, and set the field.

> This is **orthogonal** to the `pitch_mult_*` NMI-rate multipliers, which correct pitch — a tempo-blind axis.

### EOF handling

`current_frame` normally keeps the chosen frame in `_video_buf` so a clock stall doesn't black-frame the display. After demux EOFs (`self._eof = True`) that stall-protection becomes a trap — the buffer stays size-1 forever, `finished` (which checks `_eof and not _video_buf`) never flips, `VideoScene.process_frame` never returns False, and the audio worker pads NEUTRAL indefinitely (visible as a 3-min `writes=4/s bytes=4KiB/s` streak in audio logs). The fix is in `current_frame`: when `_eof` is set AND the consumed index is the last buffered frame, clear the buffer entirely so `finished` can flip on the next check.

### Transport seek/mute (MIDI live-tune Phase 2)

Three additions serving `VideoScene`'s DJ-style transport surface — see the [`scenes.py`](scenes.md#scenespy--scene-state-machine) and [`midi_control.py`/`transport.py`](control.md#midi_controlpy--process-wide-midi-control-surface-optional-live-performance) notes.

**`request_seek(target_s)`** sets `self._pending_seek` and clears `_video_buf` immediately, both under `self._lock`. The clear matters as much as the flag: it unblocks a demuxer currently spin-waiting on a full buffer, since the backpressure loop's capacity check passes again right away.

**`_apply_pending_seek()`** is demux-thread-only, and is checked in *two* places — at the top of `_demux_loop`'s packet loop **and** inside the backpressure wait. The packet already in flight when a seek lands was fetched from the pre-seek read position and is therefore always stale, so it is discarded via `continue`/`break` rather than buffered. That double check is what stops a seek being silently ignored while the demuxer is mid-decode.

When it fires it re-seeks the container, rebuilds the resampler and atempo graph so no stale samples carry across the jump, clears `_eof`, and re-derives `_pts_offset` from a **new anchor**: `_pts_anchor_target`, set to `target_s` rather than the ordinary `0.0`.

That anchor is the whole trick. The existing line

```python
if self._pts_offset is None:
    self._pts_offset = pts - self._pts_anchor_target
```

already generalizes the old always-rebase-to-zero behavior. An untouched scene's `start_s` seek is bit-for-bit unchanged, because the anchor stays `0.0`; a transport seek's first post-seek frame rebases to land exactly on `target_s`. This is the mechanism behind "the clock **is** file position once touched" — design decision 2 of the transport plan, which avoids any separate `file_offset_s` bookkeeping.

**`set_muted(bool)`** latches a flag that `_emit_audio` checks first. Once muted, packets are dropped before gain and noise-gate, permanently for that scene. This is the `loop_audio = "mute"` escape valve; note that nothing already queued downstream in `AudioStreamer` or `UltimateAudioSampler` is retracted.

**`duration_s`** is read from `container.duration` once at construction, or `None` if PyAV reports none. It drives absolute-jog mapping and seek/loop clamping.

### Transport audio resync (MIDI live-tune Phase 4)

The default `loop_audio = "on"` keeps audio playing across every transport splice instead of muting. Two small `AVFileSource` additions serve it.

**The `_emit_audio` seek guard.** It now early-returns while `self._pending_seek is not None`. Audio decoded from the stale pre-seek read position must not reach the consumer, or it would play *after* the splice's downstream `flush()` had already retracted the queue.

That `_pending_seek` read is unlocked — racy, but benign. The consumer-side flush epoch closes the residual one-blob window: a chunk slipping through right as the seek lands is discarded by `AudioStreamer` / `UltimateAudioSampler`'s epoch check.

**`seek_pending`** is a `_lock`-guarded property that `VideoScene`'s resync loop-wrap reads, so it does not re-fire `transport_seek(A)` every frame until the demux thread clears the pending slot. Each re-fire would flush the first fresh post-A audio.

The actual queue retraction lives in `AudioStreamer.flush()` / `UltimateAudioSampler.flush()` — see the [`audio.py` and `sampler.py`](audio.md#audiopy--audiostreamer) notes. `flush()` drops everything queued without moving `position_seconds()`, and a flush-epoch counter on both backends discards stale audio held by a pusher blocked mid-commit or by a consumer mid-write.

No other demux-side change is needed: `_apply_pending_seek` already clears `_eof`, rebuilds the resampler and atempo graph, and re-anchors PTS to the target.

## `modes.py` — DisplayMode hierarchy

Each mode does VIC register setup + frame quantization + push to the right addresses. All uploads go through `write_region` so the delta cache applies.

### `frame_target_size`

Each mode's `(width, height)` — the only resolution it downscales a source frame to in `compose`/`render` (`(40,25)` PETSCII, `(80,50)` MCM, `(320,200)` Hires, `(160,200)` MHires; `None` for `BlankDisplayMode`, which renders no source frame). `compose` sources its `cv2.resize` target from this attribute (not a literal), and `VideoScene` reads it as `AVFileSource`'s `decode_target_size` — so it's the **single source of truth** for both the compose resize and the video decoder's downscale-during-decode plan, and the two can't drift (a stale decode plan would under/over-decode). See the `video.py` decode-time-downscale note above.

### Bitmap engage clean-field (`engage_bitmap_mode`)

The hires/mhires VIC bring-up is one shared module-level primitive, `engage_bitmap_mode(api, *, d011, d018, d016, …)`. It is called by **both** the single-buffer `HiresDisplayMode`/`MultiHiresDisplayMode` `setup()` **and** `voice_scope.VoiceScopeRenderer._apply_vic_hires_bank`, the waveform/midi oscilloscope — so the engage invariant and the VIC-register set live in exactly one place and cannot drift. They previously did: the scope was left clearing *after* its `$D011` flip.

**The invariant.** Zero both the `$2000` bitmap **and** screen RAM (`$0400`) *before* flipping `$D011` into bitmap mode, and write `$D018`/`$D016` first as well. The window between the mode flip and the first composed frame then shows solid black, rather than uninitialized-RAM garbage or a colour ghost of the prior scene.

**Why `$0400` too — the non-obvious part.** A zeroed bitmap makes every pixel select its cell's *background* colour. In hires, that background is the **low nibble of the `$0400` byte**, not `$D021`. So leaving stale `$0400` — say the previous interstitial's PETSCII codes — paints a 40×25 colour ghost on engage. Zeroing `$0400` pins every cell's background to black.

**Why border and bg0 are pinned on every path.** `$D020`/`$D021` are set to `0x00` everywhere, including REU-staged mhires. The REU bank-swap IRQ only starts writing `$D021` from the first *real* swap, since the frame tracker's ready flag starts zeroed (see `_install_bank_swap_irq`). Without the setup-time write, every frame until that first swap showed whatever `$D021` the previous scene left behind — observed on hardware as a black border over a stale-blue screen. The setup write covers exactly that gap; the IRQ still owns `$D021` from the first real frame onward.

**Per-caller differences are arguments, not forks:**

* `dd00` plus `bitmap_base`/`screen_base`/`d018` let the scope **relocate the VIC bank**, switching bank 0↔2 according to the SID footprint.
* `clear_region_ids` selects the **delta-cached `write_region`** clear — used by the scope, which reuses stable region IDs to also blank its spacer rows — versus the **`write_memory_file`** bulk clear, the display modes' one-time clear that bypasses the cache the first `push` rebuilds.
* `clear=False` lets the REU and host-DMA double-buffer paths take only the register pokes, since they zero both VIC *banks* themselves during setup.

### Char engage clean-field (`_clear_char_screen`)
The char-mode sibling of the invariant above: `PETSCIIDisplayMode`/`BlankDisplayMode`/`MCMDisplayMode` `setup()` all clear `$0400` (to `SC_SPACE` for PETSCII/Blank, `0x00` for MCM — whose 2-bit sub-cell code selects bg slot 0) + `$D800` to black BEFORE the `$D018`/`$D016`/border-register pokes, and flip `$D011` LAST, so a mode switch — especially away from a bitmap scene, whose `$0400` holds nibble-packed colors rather than glyph codes — never reveals stale screen content as garbled characters. MCM additionally pins `$D020`-`$D023` (border + bg0-2) to black at setup so its cleared screen (code `0x00` = bg slot 0) is actually black rather than whatever the previous scene's bg registers held; PETSCII/Blank instead push their own style/configured border+background immediately, since those are already fully determined at setup.

### Scene fade (dim toward black)
Every compose-based mode supports a setup/teardown fade driven by the Playlist (`[playlist].fade_duration_s`, 0 disables). The C64 has no global brightness register and its 16 palette indices aren't luminance-ordered, so the fade is a **palette remap**: `palette.build_fade_lut(alpha)` returns a 16-entry LUT mapping each color to the palette index nearest (in the quantizer's weighted-BGR space) to `C64_PALETTE_BGR[c] * alpha` — identity at `alpha ≥ 1`, all-black at `alpha = 0`, black always → black, memoized on a 1/256-quantized alpha. `DisplayMode.apply_fade(buffers)` applies that LUT to a mode's **color-bearing** fields only and leaves the **bitmap pixel-selectors** untouched, so dimming the cell colors fades the picture while black pixels stay black: PETSCII/Blank dim color RAM (FG); MCM dims the shared bg0/bg1/bg2 registers + the per-cell multicolor FG (via a 0..7-constrained LUT so the dimmed value stays a legal multicolor color and bit 3 is preserved); Hires dims both screen-byte nibbles (fg/bg) via `_fade_nibbles` + the bg/border scalar; MultiHires adds color RAM (c3). `apply_fade` never mutates its input — `_render_with_overlays` caches the full-brightness, post-overlay buffers as `display_mode._last_buffers`, then dims a copy before push; `repush_faded(api, alpha)` re-dims that pristine cache and re-pushes, which is how the freeze+dim fade-out replays the last frame at decreasing alpha without re-composing (the unchanged bitmap delta-skips, so it's cheap). Non-compose scenes (waveform/midi oscilloscope, native launcher — all `display_mode = None`) are untouched. The Playlist timeline + CTRL-skip abort are in the `scenes.py`/playlist note below.

### Persistent brightness dim (`user_dim`)
Alongside the transient `fade_alpha`, every mode carries a `user_dim ∈ (0, 1]` (default 1.0) — the WLED bridge's `bri` slider as a *real* output dim. `apply_fade` feeds `build_fade_lut` the **product** `fade_alpha * user_dim` (the `DisplayMode._fade_lut_alpha` property; the LUT memo cache already keys on the combined alpha), so a fade-out from a dimmed scene ramps down from the dimmed level, not from full. `repush_faded` still toggles only `fade_alpha`, so the freeze+dim replay inherits the dim for free. The `_render_with_overlays` dim guard is widened to `fade_alpha < 1.0 or user_dim < 1.0`, and because every compose-based scene composes every frame there's no repush machinery for the static case — a `user_dim` change lands on the next frame (same non-compose/launcher limitation as the fade). `user_dim` lives on the per-scene mode instance, so `Playlist.user_dim` owns the persistent value and `_safe_setup` re-stamps it onto each fresh scene's mode — a dim set via the app survives playlist auto-advance. The bridge (`wled_device._apply_dim`) writes both `pl.user_dim` and the current mode's `user_dim` for an instant-plus-durable effect.

### Key vectorization tricks


* `palette.quantize_distances()` returns the full (N, 16) distance matrix via the `(x-p)²` expansion — avoids the (N, 16, 3) broadcast tensor the naive form would build.
* `MCMDisplayMode` reuses one distance matrix across both the bg-color picker and the per-cell FG search, and vectorizes the original 8-iteration Python loop into one `argmin`.
* `MultiHiresDisplayMode` has two render paths. The legacy global-4 path (cheap/vivid/grayscale palette modes) uses a 16-entry LUT to remap every palette index to the nearest of the 4 globally-chosen colors (in weighted BGR space) rather than zero-defaulting unused indices to bg0 — that older behavior silently bled large patches of background into the image. The new per-cell path (default `palette_mode = "percell"`) uses VIC-II MCBM's per-cell `c1`/`c2`/`c3` capacity: picks `bg0` globally, then for every 4×8 cell picks its own top-3 non-bg colors by population and resolves each of the 32 cell pixels against {bg0, c1_cell, c2_cell, c3_cell}. Frames carry up to `bg0 + 3×1000 = 3001` distinct colors instead of 4, which is what VIC-II MCBM was designed to support; the older global path was leaving most of that capacity unused.
* `PETSCIIDisplayMode` delegates glyph + color selection to a `PetsciiStyle` from `petscii_styles.py` (see below). The default style is the original luma → 11-char ramp + per-cell quantized color; cycling via SHIFT swaps in increasingly abstract alternatives (halftone blocks, random graphics glyphs, letter rain, etc.).

### `palette_mode` — per-cell slot allocation

`MCMDisplayMode` and `MultiHiresDisplayMode` accept a `palette_mode` constructor argument (configurable per-scene via `palette_mode = "percell"|"cheap"|"vivid"|"grayscale"` in TOML, default `"percell"`):

* **`"percell"`** — MultiHires only. MCM treats it as an alias for `"cheap"`, since MCM already picks its fg per cell. See the detailed breakdown below.
* `"cheap"` — legacy global-4. HSV saturation boost (`boost_saturation`, factor 1.8) before quantization plus a `make_gray_penalty` bias added to the per-pixel distance matrix. The penalty pushes the 5 gray-axis palette entries + cyan (which sits at the pale-chromatic boundary and over-selects on warm-gray skin) far enough that borderline pixels flip to a chromatic neighbor. Top-N slot picks go through `_ema_counts` (EMA-smoothed bincount, `PALETTE_PICK_EMA_ALPHA = 0.25`) and are then sorted by palette index, so the chosen SET only flips on sustained scene changes and a stable SET always lands in a stable slot ORDER — without this the picks flickered between e.g. cyan and orange every few frames as borderline counts tied differently, rewriting screen + color RAM + bg registers and producing a visible palette flash. Still the default for MCM.
* `"vivid"` — same biases, plus the 3 (MCM) / 4 (MultiHires) global slots are picked by `pick_diverse_top_n` instead of raw frequency: the most-populated index always wins slot 0, then each subsequent slot prefers a populated entry whose hue is at least 45° away from already-chosen chromatic picks. Falls back to most-populated when no diverse candidate exists. Use when a scene keeps reducing to two-or-three near-shades.
* `"grayscale"` — restricts every quantization decision to the 5 gray-axis palette entries (black, white, dark gray, gray, light gray). Skips the saturation boost (wasted work on gray-only output) and uses `make_gray_penalty(chromatic_strength=GRAYSCALE_CHROMATIC_PENALTY=1e10)` so every chromatic entry is dominated in the per-pixel argmin. Global slot picking is **fixed** (not adaptive) in luminance order: MHires uses `(0, 11, 12, 15)` = black, dark gray, gray, light gray (pure white is dropped for better mid-tone resolution); MCM uses bgs `(11, 12, 15)` with FG resolving to `{0, 1}` for full 5-level coverage per screen. The MHires LUT is precomputed once at `__init__`. Adaptive picking from only 5 gray entries was a perf disaster: per-frame tie-break shuffles flipped the slot order, which rebuilt the LUT, which remapped every pixel to a different slot in the 8 KB bitmap, which busted the chunked-delta cache and forced full bitmap + screen RAM + color RAM uploads every frame. Result is the same "old TV broadcast" aesthetic but at the full system frame rate (60 NTSC / 50 PAL) instead of ≈13 fps. Note that in MCM only black (0) and white (1) survive into the FG slot (color RAM bit 3 = multicolor flag steals the high bit, so FG is restricted to indices 0..7).

#### How `"percell"` works

**Choosing `bg0`.** Globally, as the EMA-smoothed most-populated palette index — **stabilized by relative hysteresis** (`BG0_HYSTERESIS_MARGIN`). bg0 only changes when a challenger's smoothed count beats the incumbent by the margin.

That hysteresis is why near-tied dominants — mostly-black video with a bright moment, or pillarbox/letterbox bars — stop strobing `$D021`. Without it the background and bars flash a different color every frame. Note this is a single instant register change, *not* a write tear, and it is especially visible on a slow transport like TeensyROM serial where the rest of the frame lags behind. A sustained dominant-color shift still moves bg0, and an old bg0 that vanishes (smoothed count → ≈0) is never sticky.

**Choosing each cell's 3 colors.** For every 4×8 cell, the top 3 non-bg colors by population, using a per-cell bincount on the same `(N,16)` distance matrix the global path uses — or an alternate [`[color].cell_strategy`](#colorcell_strategy--which-3-colors-fill-a-cell).

Picks are sorted by palette index for delta-cache stability, and bg0 is excluded from the per-cell search so the cell's effective palette stays at 4.

**The bg0 poison-filler guard.** A cell with fewer than 3 distinct non-bg0 colors present — mostly-bg0 cells, which are the norm under a small forced palette — **pads its surplus slots with bg0**, not with an arbitrary zero-count palette index.

The old padding leaked an out-of-palette color, for instance green into a `[0,4,6,14]` cast, and churned slot order frame to frame. The VIC briefly rendered that during the non-atomic screen/color/bitmap write tear, which on TeensyROM serial showed up as green-square flicker and flashing letterbox edges. bg0 in a filler slot is a harmless duplicate, since the `%00` code already reaches it.

**Resolving pixels.** Each of the cell's 32 pixels resolves directly against `{bg0, c1_cell, c2_cell, c3_cell}` via `take_along_axis` on the `(1000, 32, 16)` cell-shaped distance tensor. There is no LUT step, because there is no global slot remap to apply.

**Memory layout.** Screen RAM (`$0400`) carries `(c1<<4)|c2` per cell; color RAM (`$D800`) carries `c3` per cell. Both are per-cell content rather than one repeated byte, so they bust the delta cache more often — still well under the DMA budget.

**What it buys.** Black-dominated content benefits most: cells that don't contain bg0 stop wasting one of their 4 slots on it, and regional content — a laptop screen, a kid's sweater, monitor glow — keeps its colors instead of collapsing to the global dominant pick.

### `[color].dither` — spatial dither

Implemented in `dither.py`. Adds a spatial-dither stage to mhires/mcm/hires, ahead of nearest-palette quantization. Two families, chosen by `dither_method` (`"none"` (default resolves to a concrete value via `config.resolve_dither_method` — see below) `| "ordered" | "blue_noise" | "floyd-steinberg" | "atkinson"`), threaded into each mode's constructor alongside `channel_boost`/`hue_corrections`:

#### The ordered family — `"ordered"` / `"blue_noise"`

A fixed, position-deterministic threshold offset added to every BGR channel of `flat` — the same pixel array `channel_boost`/`hue_corrections` already produced — *before* `quantize_distances`/`quantize_flat` runs.

Nothing structural changes downstream: candidate selection, EMA/hysteresis, and per-cell picking are untouched. It only nudges which side of a quantization boundary a pixel lands on.

* `"ordered"` (`dither.bayer_offset(h, w, strength)`) tiles the classic 8×8 Bayer threshold matrix, normalized to a zero-mean ±0.5 range and scaled by `strength * 64`.
* `"blue_noise"` (`dither.blue_noise_offset`) tiles a 64×64 mask generated offline by void-and-cluster (`scripts/diags/gen_blue_noise.py`), baked into `dither._BLUE_NOISE_B64` as a base64 uint16 blob and **not** regenerated at runtime. It is normalized and scaled identically, so `dither_strength` means the same thing for both.

Both are a single vectorized array op over the whole frame, so they hold realtime frame rates, and both are constant at a given screen position — a static source dithers identically frame to frame, and motion sources gain no shimmer.

Blue noise additionally has no low-frequency structure, so it drops the regular grid/cross-hatch pattern Bayer's 8×8 tiling shows at C64 resolution — same cost, same stability. See the module docstring for the full property breakdown.

Both are skipped when a force-palette remap (`ColorMap.apply`) is active: those pixels are already exact chosen colors, and dithering would fight the assignment. Modes dispatch through `modes._ORDERED_DITHER_OFFSET_FNS`, a lookup shared by the three `compose()` call sites (MCM, Hires, MultiHires).

#### The error-diffusion family — `"floyd-steinberg"` / `"atkinson"`

A per-pixel scan pushing each pixel's quantization error onto its yet-unvisited neighbors: `dither.error_diffuse` for a single region, `dither.error_diffuse_cells` for N independent regions run in lockstep.

* **Floyd-Steinberg** — 4 neighbors, 7/3/5/1 × 1/16.
* **Atkinson** — 6 neighbors × 1/8, deliberately dropping 1/4 of the error for punchier contrast.

**Why they are integrated differently.** Both are Python-level loops, not vectorizable across pixels, since each depends on its predecessors' diffused error. So they are a **final-step replacement** rather than a `flat`-level perturbation.

`MultiHiresDisplayMode._compose_percell` and MCM's per-cell `fa` computation still pick each cell's *candidate set* — `{bg0, c1, c2, c3}` and `{bg0, bg1, bg2, fg}` respectively — exactly as before, through the existing EMA-smoothed histograms. Dithering replaces only the final per-pixel-within-cell code assignment: `d_cand.argmin` becomes `error_diffuse_cells(pixels_cell, candidates_bgr, method, strength)`. That loops over the small in-cell pixel count (32 for mhires, 4 for MCM) while staying vectorized across all 1000 cells at each step, rather than looping cell by cell.

Hires — 2 colors, a global `bg` plus a per-8×8-cell sampled `fg` — gets the same treatment over 8×8 blocks.

**No hysteresis on this path.** Each cell re-diffuses independently every frame with no persisted state, so the per-pixel code hysteresis (`PERCELL_CODE_HYSTERESIS_BONUS`) is skipped — there is no meaningful "previous code" to blend toward.

That is precisely why `"auto"` never picks these for a motion scene: independently-diffused frames read as shimmer even though any single frame looks great. The ordered family's fixed pattern does not have this problem.

**Coverage differences.** MCM has no separate percell-vs-global `palette_mode` branch — `fa` is computed the same way regardless — so its FS/Atkinson dithering applies unconditionally. mhires' only fires under `palette_mode = "percell"`; the legacy global-4 `_compose_global` path has no per-cell candidate structure to dither against, though it still gets the ordered-family offset for free, applied upstream in `flat`.

#### `"auto"` resolution

`config.resolve_dither_method(dither_setting, scene_type)` resolves the default at `build_scene` time, via `_display_mode_for_scene` — the single funnel webcam, video, slideshow, and generative scenes share.

* **Static** scenes (`slideshow`) → `"floyd-steinberg"`. Composed once per image, so the per-pixel cost is a non-issue and it is the highest-quality method.
* **Everything else** (webcam, video, generative — recomposed every frame) → `"blue_noise"`. Strictly better than `"ordered"` at the same realtime, no-shimmer cost.

`"ordered"` remains available as an explicit choice for the classic Bayer look. Any explicit non-`"auto"` value passes through unchanged for every scene type, so you can force floyd-steinberg or atkinson onto video and accept the shimmer (see [caveats.md](../caveats.md)).

PETSCII is not wired up — its bg/fg-per-character-cell selection is not a raw pixel grid in the same way.

### `[color].color_match` — the distance space

Implemented in `palette.py`. Selects the *color space* the nearest-palette decision runs in, for every quantizing mode (mcm, mhires, hires, petscii).

**The default metric** is a brightness-weighted BGR distance (`quantize_distances`, weights `[2,4,3]`). It is fast but over-weights luminance, so a warm mid-gray — skin — can land nearer a gray-axis entry than orange or brown.

**`color_match = "perceptual"`** swaps in a CIE-Lab distance (`quantize_distances_lab`). The 16 palette colors are precomputed once in OpenCV 8-bit Lab (`_PALETTE_LAB`) with the transposed/norm-squared matmul precompute (`_PAL_LAB_T` / `_PAL_LAB_NORMSQ`). Each frame's shaped `flat` is converted BGR→Lab by `_bgr_to_lab` — a clip and uint8 round, then `cv2.cvtColor` — and matched by the same `(x-p)²` expansion the weighted path uses.

The swap is fully contained in `quantize_distances_for(flat, perceptual=…)` / `quantize_flat_for`. Every downstream compose decision — per-pixel argmin, bg/fg picks, per-cell candidate resolution, error-diffusion candidate distances — operates on the returned `(N,16)` distance matrix, so the modes call those instead of the fixed pair and nothing else in the pipeline changes shape.

**Perceptual swaps only the distance space, not the shaping.** `channel_boost` and `gray_penalty` still apply, and this is load-bearing. An earlier revision dropped both, on the reasoning that they were weighted-BGR crutches. Hardware A/B then showed flat desaturated regions — a pale sky — fragmenting into drab gray under the accurate-but-neutral Lab match. The gray penalty is what keeps those regions chromatic, and `channel_boost` holds the C64-friendly hues.

The gray penalty and the percell code/quant hysteresis bonuses are all d²-space quantities, so they are scaled by `palette.PERCEPTUAL_DIST_SCALE` (≈1/3, the Lab-vs-weighted-BGR magnitude ratio for equal physical gaps). Their tuned strength therefore carries over.

**Reach.** petscii threads the metric through `petscii_styles._quantize_color` / `_quantize_to_spectrum`. The force-palette remap is unaffected — its pixels are already exact palette colors, so every metric returns the same index.

**`"auto"` resolution.** `config.resolve_color_match(setting, display_mode_name)`, inside the single construction funnel `_build_display_mode`, picks perceptual on every quantizing mode (`_COLOR_MATCH_AUTO_PERCEPTUAL`) and rgb on the non-color-picking ones (blank, hires_edges). `validate_color_match_cfg` and `doctor._validate_color_match` report the resolved metric per scene.

**Hardware A/B on the U64**, with the default `auto_fit` saturation lift in play: MCM improves clearly, with smoother skin gradients and far less per-cell color speckle. mhires, hires, and petscii range from a wash to a marginal win, because `auto_fit` already dominates their color decision. But perceptual never regressed once the shaping was kept, so `auto` chooses it everywhere it applies.

Cost is one extra `cvtColor` per frame on the small downscaled `flat` (≤64k px) — negligible.

### `[color].cell_strategy` — which 3 colors fill a cell

Implemented in `modes._pick_cell_colors`. Selects *which* 3 of a cell's present colors fill the per-cell `c1`/`c2`/`c3` slots on the mhires `percell` path.

No-op everywhere else: MCM already picks a single fg per cell by error, and the global-4 modes have no per-cell pick at all. It is orthogonal to `palette_mode` (percell vs global), `dither` (the per-pixel fill, decided *after* these 3 colors), and `color_match` (the distance space).

**The four strategies:**

* **`"frequency"`** — the historical behavior. The 3 most-populated non-bg0 colors, ranked on the EMA-smoothed per-cell histogram. Temporally stable.
* **`"luminance"`** — darkest, median, and brightest present color by `palette.PALETTE_LUMA` (a Rec.601 luma per palette entry), so a cell's full tonal span survives even when one tone dominates the count.
* **`"contrast"`** — the two luma extremes, plus the present color whose minimum luma-distance to both extremes is largest. A farthest-point pick maximizing tonal spread.
* **`"error-min"`** — the trio minimizing the cell's summed per-pixel reconstruction error against `{bg0,c1,c2,c3}`.

All four keep the **absent-slot → bg0 poison-filler guard**, and the caller still sorts the 3 picks by palette index for delta-cache stability. So the flicker-suppression and tear-safety properties of the frequency path carry over unchanged.

**How error-min stays realtime.** It is vectorized across all 1000 cells: bound each cell's candidate pool to its top-`ERROR_MIN_POOL_SIZE` (6) present colors, then evaluate every `C(6,3)=20` position-trio at once — a per-pixel min over `{bg0}+trio` on the `(1000,32,K)` gathered distance tensor, summed over the 32 pixels, argmin over trios. That is near-optimal, and exactly optimal when a cell holds ≤6 meaningfully-populated colors.

It also carries a guarantee: since the frequency top-3 is always one of the 20 trios error-min scores, error-min's reconstruction error **can never exceed** frequency's on the same cell. The tests assert this invariant.

**`"auto"` resolution.** `config.resolve_cell_strategy(setting, scene_type)` picks:

* `error-min` for **static** scenes (`slideshow`) — composed once, so the trio search cost is paid a single time in exchange for the best reconstruction.
* `frequency` for **motion** scenes (video, webcam, generative) — the per-frame recompose makes temporal stability the right call, since the tonal-extreme strategies re-rank on noisier raw content and churn slots frame to frame.

It threads through `_build_display_mode` / `_display_mode_for_scene` alongside `dither_method`. `validate_cell_strategy_cfg` and `doctor._validate_cell_strategy` report the resolved strategy per mhires-percell scene.

**How much it matters in practice.** On natural photographic content the strategies rarely diverge — most cells hold ≤3 post-quantization colors, so every strategy picks the same set. They separate on busy, high-detail images.

Hardware A/B on the U64 (busy slideshow, Cam Link): error-min holds high-detail regions subtly better than frequency, with no regression. luminance and contrast can add off-color speckle in near-flat regions, because they force a tonal extreme onto a lone outlier pixel. Hence `auto` only ever selects error-min or frequency, leaving the other two as opt-in creative controls.

### `[color].motion_smoothing` — temporal smoothing / after-images

Range 0..1, default 0.25. A single dial over the mhires `percell` path's two *temporal* flicker-suppression buffers. No-op on every other mode and palette_mode — only percell carries them.

**The two buffers:**

1. The per-cell colour-count EMA (`_smoothed_cell_counts`, blended each frame with `PERCELL_PICK_EMA_ALPHA = 0.15`), which stabilizes *which* colours a cell offers.
2. The per-pixel/per-cell decision hysteresis (`PERCELL_QUANT_HYSTERESIS_BONUS` / `PERCELL_CODE_HYSTERESIS_BONUS`, each 5000 in d²-space, further scaled by `PERCEPTUAL_DIST_SCALE` under Lab matching), which keeps a pixel on its previous palette index or bitmap code unless the new frame beats it by the bonus.

**The tradeoff.** Both exist to stop per-frame colour churn reading as shimmer on noisy video. Both buy that by trading motion-tracking for stability — so on a hard shot cut they hold structure from the *previous* shot for a moment, and an outline lingers as an after-image while the buffers decay.

**What the dial does.** `motion_smoothing` scales both together at construction time:

| `s` | Behavior |
| --- | --- |
| `1.0` | Legacy values: `_ema_alpha = PERCELL_PICK_EMA_ALPHA`, full hysteresis. Most stable, ghostiest. |
| `0.0` | `_ema_alpha = 1.0` (new frame fully replaces count history) and both hysteresis bonuses zeroed. Tracks the source frame-exactly — no after-image, but grainy content can flicker. |
| between | Lerps both: `_ema_alpha = 1 - s·(1-0.15)`, `hyst = base·s·penalty_scale`. |

Threaded `ColorCfg.motion_smoothing` → `_build_display_mode` → `MultiHiresDisplayMode.__init__`; `compose()` reads `self._ema_alpha` rather than the module constant.

**Why one dial and not an EMA-only knob.** An offline stateless-vs-stateful A/B (`scripts/diags/mhires_ema_ghost_ab.py`, measuring how far the stateful render deviates from a fresh-mode render of the same frame) isolated the contributions:

* The **hysteresis dominates** — killing it alone removes ≈60 % of the deviation.
* The EMA is secondary, ≈30 %.
* `s=0` plus no hysteresis tracks the stateless ground truth exactly.

Since neither buffer accounts for the ghost on its own, a combined dial is the correct control.

**Why 0.25.** Picked by an on-hardware flicker/ghost A/B on the U64 — WarGames hard cuts for the after-image, grainy dark footage for flicker — as the lowest value where flicker stays acceptable. It is a large ghost reduction over the old always-on 1.0 behavior.

`validate_motion_smoothing_cfg` and `doctor._validate_motion_smoothing` bound it 0..1 and note a non-default value on the mhires percell scenes it affects. Orthogonal to `cell_strategy` (which 3 colours), `dither` (per-pixel fill), and `color_match` (distance space).

### `petscii_styles.py`

Registers the styles in `STYLE_NAMES` (default, halftone, random_glyph, letter_rain, neon, inverse_pop, hatch, color_only). Each subclass owns its own char ramp + color policy and declares its preferred border + background; the mode pokes those on setup and on every SHIFT cycle. The `random` config sentinel is resolved at scene `setup()` to a concrete style — subsequent cycles proceed from there in declared order, so SHIFT behavior stays predictable instead of re-randomizing each press. New styles are one PetsciiStyle subclass + a registry entry away (no PETSCIIDisplayMode change needed).

### `BlankDisplayMode`

A standard PETSCII char mode with no video input — every cell is `SC_SPACE` (0x20) with FG = `background`, so the canvas reads as solid color until an overlay paints over it. Takes `border` and `background` palette indices (masked to 4 bits). `is_petscii_compatible = True` (class flag, parallel to `PETSCIIDisplayMode`), so every overlay that writes PETSCII screen codes works on blank scenes too. Used as a clean foundation for demo-scene title cards via the `big_text` overlay. `BlankScene` (in `scenes.py`) is the matching no-source Scene subclass.

### `[video].use_reu_staged`

Routes video pushes through the REU. Tri-state `true | false | "auto"`, default `"auto"`.

**Resolution.** `config.resolve_use_reu_staged(setting, display, reu_available)` resolves per scene's display mode at build time. `"auto"` yields True only when *all three* hold:

1. The mode is a bitmap mode (`_REU_BITMAP_MODES` = hires, hires_edges, mhires).
2. The startup probe confirmed the REU is on.
3. The scene has no buffer-painting (text) overlay.

Char modes (petscii, blank) stay on host-DMA under auto, because their delta cache makes a full per-frame REU→main DMA a net regression.

**Why bitmap + text overlay also stays on host-DMA.** Determined by `has_buffer_overlays`, computed from the scene's overlay types via `overlays.paints_into_buffers`. The bank-swap's `$DD00` swap fires only *after* the ≈9000-cycle REU→bank DMA inside the vblank IRQ, which pushes the swap past vblank into the visible rows. Fine high-contrast glyphs in the bottom rows then shimmer — hardware-confirmed. Host-DMA renders them crisply, and overlay-free bitmap video keeps the tear-free REU pipeline.

Explicit `true`/`false` ignore both the probe and the overlay check; `true` deliberately opts into the shimmer in exchange for tear-free cuts.

**Where `reu_available` comes from.** Computed once in `cli._resolve_reu_available` — gated on `"auto"`, `api.profile.supports_reu`, and not `--skip-probe`, via `doctor.reu_is_enabled` — then stashed on `SystemStack.reu_available` and threaded through `scenes_from_config`/`build_scene`, including SIGHUP/control-plane reloads and ensemble-follower rebuilds. A `display = "random"` slideshow stores the raw tri-state plus `reu_available` and re-resolves per concrete mode at each setup.

Any uncertainty — no REU, a failed query, `--skip-probe`, a non-REU backend — degrades to host-DMA, so video never silently freezes.

#### The two REU pipelines

**Char modes (PETSCII/Blank) — single-buffer.** `push()` calls `modes._push_screen_via_reu(api, screen_bytes, $0400)`: REUWRITE the 1000-byte screen to `REU_VIDEO_SCREEN_BASE = $E00000` (bus-clean), configure REC `$DF02`/`$DF04`/`$DF07` for a one-shot REU→main DMA, then trigger via `$DF01 = $91`. Color RAM at `$D800` is not VIC-banked, so it stays on the delta-cached DMAWRITE path.

**Bitmap modes (Hires/MultiHires) — double-buffer.** Bitmap and screen are REUWRITE-staged, then DMA'd into the *off-screen* VIC bank. A C64-side raster IRQ at `$0314` flips `$DD00` at vblank for a tear-free swap — this is what eliminates the scene-cut whole-screen flashes.

**Coexistence with the REU audio pump** is fine on any scene: the bank-swap installer picks a **merged** `$0314` dispatcher whose non-raster branch JMPs to the audio pump at `$C100`, servicing both IRQ sources through one hook. This is what lifted the earlier `validate_scene_cfg` mutex against `use_reu_staged + use_reu_pump`, which no longer exists.

MCM does not support staging yet.

### `[video].double_buffer`

The host-DMA page-flip sibling of `use_reu_staged` — tear-free bitmap video without needing a REU at all. Tri-state `true | false | "auto"`, default `"auto"`.

**Resolution.** `config.resolve_double_buffer(setting, display, *, use_reu_staged, backend_supports_reu, has_buffer_overlays, audio_reu_pump_active)` enables it only for a bitmap mode (`_REU_BITMAP_MODES`), and only when `use_reu_staged` resolved False — the two are mutually exclusive, since both flip `$DD00`.

Under `"auto"` it fires when REU staging offers no tear-free alternative for the scene, which is either:

* The backend has **no REU at all** (`not api.profile.supports_reu`) — TeensyROM serial and TCP, both ≈106 KiB/s, so the bus rather than the link is the wall.
* The scene has a buffer-painting text overlay (`has_buffer_overlays`).

**The overlay case is the U64 path**, and it is the interesting one. `resolve_use_reu_staged` turns the REU bank-swap *off* for bitmap+text to dodge the swap shimmer — which would otherwise leave single-buffer host-DMA that tears on scene cuts. Host-DMA double-buffer gives those scenes tear-free frames **and** crisp text. Overlay-free bitmap video on a REU backend stays on the REU path, the better tear-free option there.

Explicit `true`/`false` pass through, still scoped to bitmap modes.

**Why it renders text crisply.** The swap IRQ does *no* in-IRQ DMA — it only writes `$D021` (bg0) and flips `$DD00` from a 3-byte tracker. So the swap lands cleanly inside vblank with no past-vblank overrun, hence no shimmer. That is precisely the advantage over the REU path, and why it is the right pick for overlaid bitmap.

**When it is gated off.** When the REU mic pump is active (`audio_reu_pump_active`) — they share `$0314`, and unlike the REU bank-swap path there is no merged dispatcher for this pair — and by `force_host_dma`, for SID-audio scenes whose SID player owns `$0314` for PLAY.

`backend_supports_reu`, `has_buffer_overlays`, and `audio.use_reu_pump` are threaded from `build_scene`; a `display = "random"` slideshow re-resolves per concrete mode at setup.

#### Mechanism

`setup()` zeroes both VIC banks' bitmap and screen, pins bank 0, and installs `HOSTDMA_SWAP_IRQ_HANDLER` — a ≈35-byte minimal handler at `$C500` with a 3-byte tracker `[bg0, bank, ready]` at `$C700` — via the shared `_install_bank_swap_irq`.

`push()` writes bitmap and screen into the *off-screen* bank via `write_region`, using **per-bank** `RegionID`s: `BITMAP`/`SCREEN` for bank 0, `BITMAP_BANK2`/`SCREEN_BANK2` for bank 2. Each bank therefore diffs against its own prior content, not the other's. It then arms the tracker, and the next vblank IRQ flips `$DD00` and `$D021` for a whole, tear-free frame.

**MHires color-RAM residual.** `$D800` is not VIC-banked, so the c3 slot still tears in a brief ≈9 ms window before each flip — color RAM is written last, just before arming. Bitmap and screen (the structure plus c1/c2) do go tear-free. Hires has no color RAM, and static-palette mhires (cheap, grayscale) does not churn it, so both are fully tear-free.

NMI audio lives on the `$FFFA` vector, independent of this `$0314` raster IRQ, so the two coexist with no REU pump on the TR. The handler chains to `$EA31`, so kernal keyboard scan (`$028D`) keeps the pollers live.

## `rolling_palette.py` + `palette.py` — forced-palette remap

**Forced-palette remap** (`[color].force_palette` / `force_palette_colors`) is the opt-in FALSE-COLOR stage.

**What it does.** k-means the source into N Lab clusters, assign each to a **distinct** C64 color via a min-Lab-error bijection, and bake a BGR→index LUT (`palette.ColorMapAccumulator` → `ColorMap`). A gamut-clustered source — TRON, which is essentially black plus dark blue — then uses all N colors instead of rendering near-monochrome.

Applied per frame as a single LUT gather in `ColorMap.apply` on mcm and mhires, the modes built with `_force_palette=True`. It is a no-op echo elsewhere.

**Two derivation paths, by source kind:**

* **Pre-scan** — `VideoScene` and `SlideshowScene`. One `prescan_source_color` pass fixes the map before the first frame.
* **Rolling** — live sources that cannot pre-scan: webcam, the `wled` sink, and generative.

**The rolling path** ([c64cast/rolling_palette.py](../../c64cast/rolling_palette.py): `RollingForcePalette` + `palette.RollingColorMapAccumulator`) runs a worker thread sampling the latest frame at ≈1 Hz into a sliding ≈30 s Lab window, re-baking a `ColorMap`. Three mechanisms let it adapt to changing content **without popping**:

1. **Warm-start k-means** — init labels are the nearest previous center (`KMEANS_USE_INITIAL_LABELS`).
2. **Assignment hysteresis** — keep the previous cluster→C64-index bijection unless the optimal beats it by more than `ROLLING_HYSTERESIS`, mirroring the percell hysteresis.
3. **A swap policy** — only re-install a baked map when the C64 color *set* actually changed, so a stable scene stops re-installing and therefore stops shimmering; or when a **shot cut** fired, detected by HSV-histogram correlation, which clears the window so the new shot's palette is fresh and hides the snap behind the cut.

**Ownership.** `WebcamScene` and `SourceScene` own the driver: `_maybe_start_rolling_palette` gates on `getattr(mode, "_force_palette", False)`, and `_apply_rolling_palette` submits the clean frame and installs any polled map before quantization. k-means costs ≈15-60 ms and stays on the worker, so the render thread never stutters.

Hardware-verified on the U64: a `generative plasma` run with `force_palette=8` rendered live in a forced 8-color set, errors 0/s.

`--suggest-palette FILE` ranks a good `force_palette_colors` set for a given source.

## Framerate pacing & frame-dropping

`Playlist.run` uses deadline-based pacing: each frame advances a `next_deadline` by `frame_time` (resolved per-scene by `_frame_time_for(scene)`). If the wall clock has fallen more than two frame_times behind the deadline, the deadline snaps forward — dropping the missed frames — instead of bursting to catch up. All built-in scenes follow the system rate except the lower-rate defaults above (bitmap frame-pushing scenes, `WaveformScene`, `MidiScene`). Animation logic that uses `current_time` keeps tracking wall-clock time correctly across dropped frames.

`_crop_to_aspect()` is shared aspect-correction logic; previously inlined in three places. `_apply_aspect(img, aspect_mode)` dispatches over it: `"crop"` → `_crop_to_aspect` (center-crop to fill — what webcam/video always use and slideshow's default), `"fit"` → `_fit_to_aspect` (letterbox/pillarbox, black pad), `"stretch"` → identity (the mode's resize distorts to fill). Only `SlideshowScene` reads the `aspect_mode` config field today.
