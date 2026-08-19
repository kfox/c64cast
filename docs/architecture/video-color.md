# Video input & the color pipeline

Turning arbitrary video into VIC-II output: frame sources, the display-mode hierarchy, and every stage of the color pipeline (shaping, dither, quantization, forced palettes).

Part of the [architecture reference](../architecture.md). For end-user configuration see [the Programmer’s Reference Guide](../reference/README.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`video.py` — WebcamSource (shared broker) + AVFileSource (PyAV)](#videopy--webcamsource-shared-broker--avfilesource-pyav)
* [`modes/` — DisplayMode hierarchy](#modes--displaymode-hierarchy)
* [`modes_irq.py` — C64-side IRQ handlers + REU push helpers](#modes_irqpy--c64-side-irq-handlers--reu-push-helpers)
* [`palette.py` — which 16 colors the machine emits (`[hardware].host_palette`)](#palettepy--which-16-colors-the-machine-emits-hardwarehost_palette)
* [`rolling_palette.py` + `palette.py` — forced-palette remap](#rolling_palettepy--palettepy--forced-palette-remap)
* [Framerate pacing & frame-dropping](#framerate-pacing--frame-dropping)
* [`framebuffer.py` + `preview.py` — the software mirror behind preview and recording](#framebufferpy--previewpy--the-software-mirror-behind-preview-and-recording)

---

## `video.py` — WebcamSource (shared broker) + AVFileSource (PyAV)

### `WebcamSource` — the shared camera broker

An always-on broker. A single `cv2.VideoCapture` is single-consumer (every `.read()` consumes the next device frame; concurrent reads from two threads aren't safe), so one background grab thread owns the capture, continuously reads the newest frame, and `read()` hands out an independent **copy** of the latest frame. That lets the webcam scene (when active) and the always-on vision controller (`vision.py`) share **one** physical camera with no contention — and keeps the live-webcam path low-latency (always the freshest frame, stale ones overwritten). `WebcamScene._read_frame()` just calls `source.read()`. The camera is opened once per stack in `cli.py` when `needs_webcam or cfg.vision.enabled`, stored on `SystemStack.source`, released at teardown.

`WebcamSource.__init__` takes `device: int | str` and resolves it through `camera.resolve_camera_index` (see the `camera.py` note below): a plain int stays a cv2 index opened with the default `CAP_ANY`, while a **string** — a camera name substring or USB `VID:PID` — is matched against enumerated cameras and opened with the *matched backend* (`cv2.VideoCapture(index, backend)`), because the enumerated index is only valid for the apiPreference it was enumerated with. The string form is what makes a roaming USB capture stick (e.g. a Cam Link) selectable by identity instead of by a reboot-unstable index.

### `AVFileSource` — video playback

The playback source. The demuxer thread reads packets from one container, pushes resampled mono int16 audio straight through to AudioStreamer, and queues decoded video frames keyed by PTS. Consumers call `current_frame(audio_position_s)` which returns the latest frame whose PTS ≤ the clock and drops anything behind. **Drift can't accumulate** because the audio clock IS the reference — *as long as a fresh frame exists when the clock asks for it*.

### HTTP reconnect for remote streams

`av_open` / `_HTTP_RECONNECT_OPTIONS`. A yt-dlp-resolved YouTube URL is a single progressive `googlevideo` CDN link that the CDN throttles (see its `cps=`/`ratebypass` query params) and periodically drops mid-stream; that surfaces as `OSError: [Errno 5] Input/output error` out of `container.demux()`, which the demux loop's broad `except` catches, logs as "crashed", and ends playback on. The fix is to open remote inputs with FFmpeg's http-protocol reconnect options (`reconnect`, `reconnect_streamed`, `reconnect_on_network_error`, `reconnect_delay_max=5`) so FFmpeg transparently re-establishes the connection and resumes from the current byte offset instead of erroring. `av_open(path)` wraps `av.open` and injects these **only for `http(s)://` inputs** (`_is_remote_url`) — they're http-protocol-only options, so scoping them keeps FFmpeg from warning about unrecognized options on a local/file input. Every `av.open` site in the module (playback, audio-full decode, peak scan, color pre-scan) routes through it.

### Decode-time downscale

Config: `decode_target_size` / `_plan_decode_size`.

**The gap this closes.** The frame-selection model above is correct, but only works if the demuxer can produce frames in real time. It does not cover **supply**: when the decoder can't keep up, `current_frame` returns the newest frame it *has*, which falls progressively further behind the audio clock. Video lags and appears to drift, worst on heavy 4K clips.

**Why the decode size is the lever.** Converting every frame to BGR at **full source resolution** (`frame.to_ndarray("bgr24")`) and leaving the downscale to the display mode's `cv2.resize` to ≤320px costs ≈40 ms/frame for a 4K source on the U64 host — over the ≈33 ms budget at 29.97 fps, before codec decode is even counted. The pixels thrown away by the resize are paid for twice: once to convert, once to discard.

**The fix.** `VideoScene` passes the display mode's `frame_target_size` — the only resolution it actually consumes — as `decode_target_size`. The demux loop plans a decode size once from the first frame (`_plan_decode_size`) and downscales **during** the yuv→bgr swscale pass, via `av.VideoFrame.reformat(w, h, "bgr24")`.

Measured ≈40 ms → ≈4 ms/frame, a 9× speedup on a 4K sync clip. The conversion, the center-crop, the auto_fit accumulator, and the final resize then all work on a ≈640px frame.

Two guards in `_plan_decode_size`:

* Post-crop dims stay ≥ `DECODE_HEADROOM` (2×) the target in **both** axes, mirroring `scenes._crop_to_aspect` so the anamorphic MHires target — where height > width — is honored.
* It never upscales. A source already small enough returns None, falling back to a plain full-res convert.

The same downscale applies to the one-shot color pre-scan (`scan_video_samples`), since color statistics are distribution-based.

### Seek-sampled color pre-scan (`scan_video_samples`)

The auto_fit and force_palette pre-scan needs a representative frame sample across the *whole* source, not real-time playback.

**Why not sequential decode.** Decoding every frame is decode-bound and scales with file length. Striding the loop doesn't help: an `if i % stride: continue` skips accumulation, not decode, so the cost is unchanged.

| Clip | Sequential decode |
| --- | --- |
| 61 s, 1080p h264 | 0.56 s |
| 266 s, 4K AV1 | **14.6 s** |

That is a startup pause growing without bound.

**The fix.** Seek to `max_samples` evenly spaced timestamps — midpoints of `[0, duration)` — and decode **one keyframe at each** (`_seek_sample_frames`, with `backward=True` landing on the keyframe ≤ target).

Keyframe-only is exactly right here: color stats are distribution-based, so a keyframe near each timestamp represents its region as well as an exact frame would. And it makes the scan roughly **constant-time regardless of length or codec**:

| Clip | Seek-sampled |
| --- | --- |
| 61 s, 1080p h264 | ≈0.9 s |
| 266 s, 4K AV1 | ≈3.1 s |

Short clips pay a small per-seek overhead — an accepted trade for bounding the worst case.

**Duration and fallback.** Duration comes from the stream (`v_stream.duration × time_base`), else the container (`container.duration / av.time_base`). When neither is known (a live or unbounded stream), or seeking raises (a non-seekable input), it re-opens and falls back to the original sequential-decode stride (`_decode_sample_frames`), so nothing regresses on sources that can't seek.

Both paths share `_frame_to_scan_bgr`, the decode-time downscale above, and one decode pass serves force_palette and auto_fit alike.

**Progress reporting.** `scan_video_samples(..., on_progress=)` feeds the setup progress bar ([scenes/setup_progress.py](scenes.md#setup_progresspy--the-video-setup-progress-bar)) without touching either sampling function: the hook is implemented as `_SampleProgressTap`, just another accumulator appended to the list, counting `add()` calls against `max_samples`. The sequential fallback can sample fewer frames than planned, so the reported fraction may end short of 1.0 — the caller marks its own completion (`SegmentedProgress.complete`).

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

generalizes rebase-to-zero. An untouched scene leaves the anchor at `0.0`, so its `start_s` seek rebases exactly as an unconditional rebase would; a transport seek's first post-seek frame rebases to land exactly on `target_s`. This is the mechanism behind "the clock **is** file position once touched" — design decision 2 of the transport plan, which avoids any separate `file_offset_s` bookkeeping.

**`set_muted(bool)`** latches a flag that `_emit_audio` checks first. Once muted, packets are dropped before gain and noise-gate, permanently for that scene. This is the `loop_audio = "mute"` escape valve; note that nothing already queued downstream in `AudioStreamer` or `UltimateAudioSampler` is retracted.

**`duration_s`** is read from `container.duration` once at construction, or `None` if PyAV reports none. It drives absolute-jog mapping and seek/loop clamping.

### Transport audio resync (MIDI live-tune Phase 4)

The default `loop_audio = "on"` keeps audio playing across every transport splice instead of muting. Two small `AVFileSource` additions serve it.

**The `_emit_audio` seek guard.** It early-returns while `self._pending_seek is not None`. Audio decoded from the stale pre-seek read position must not reach the consumer, or it would play *after* the splice's downstream `flush()` had already retracted the queue.

That `_pending_seek` read is unlocked — racy, but benign. The consumer-side flush epoch closes the residual one-blob window: a chunk slipping through right as the seek lands is discarded by `AudioStreamer` / `UltimateAudioSampler`'s epoch check.

**`seek_pending`** is a `_lock`-guarded property that `VideoScene`'s resync loop-wrap reads, so it does not re-fire `transport_seek(A)` every frame until the demux thread clears the pending slot. Each re-fire would flush the first fresh post-A audio.

The actual queue retraction lives in `AudioStreamer.flush()` / `UltimateAudioSampler.flush()` — see the [`audio.py` and `sampler.py`](audio.md#audiopy--audiostreamer) notes. `flush()` drops everything queued without moving `position_seconds()`, and a flush-epoch counter on both backends discards stale audio held by a pusher blocked mid-commit or by a consumer mid-write.

No other demux-side change is needed: `_apply_pending_seek` already clears `_eof`, rebuilds the resampler and atempo graph, and re-anchors PTS to the target.

## `modes/` — DisplayMode hierarchy

Each mode does VIC register setup + frame quantization + push to the right addresses. All uploads go through `write_region` so the delta cache applies.

**Package layout** (split from the single `modes.py`, 2026-08): one module per mode (`petscii`/`blank`/`mcm`/`hires`/`mhires`) over two mid-bases (`char.py` — `CharDisplayMode` + `clear_char_screen`; `bitmap.py` — `engage_bitmap_mode` + `BitmapDisplayMode`), with the compose-buffer TypedDicts, cell-color pickers, palette-mode shaping helpers and the `DisplayMode` base in `base.py`. `__init__.py` re-exports the whole public surface, so `from c64cast.video.modes import X` resolves exactly as before — but its submodule import order is `isort: off`-guarded because it **is** `DisplayMode.__subclasses__()` creation order, which introspect's live-target walk, the MIDI-setup wizard's pick lists, and generated reference appendix F all render in. Two things to know when editing: the live-tunable pick knobs (`PALETTE_PICK_EMA_ALPHA`, the `PERCELL_*` trio) are rebindable **on `modes.base` only** — the mode classes read them as `base.<NAME>` at call time so a runtime retune (the `mhires_ema_ghost_ab.py` diag) takes effect, while the `modes.<NAME>` re-exports are import-time value snapshots; and the helpers that went public in the split (`pick_cell_colors`, `ema_counts`, `fade_nibbles`, `clear_char_screen`, the `validate_*`/`*_palette_*` family) did so because the split made them cross-module — don't re-privatize them.

### `frame_target_size`

Each mode's `(width, height)` — the only resolution it downscales a source frame to in `compose`/`render` (`(40,25)` PETSCII, `(80,50)` MCM, `(320,200)` Hires, `(160,200)` MHires; `None` for `BlankDisplayMode`, which renders no source frame). `compose` sources its `cv2.resize` target from this attribute (not a literal), and `VideoScene` reads it as `AVFileSource`'s `decode_target_size` — so it's the **single source of truth** for both the compose resize and the video decoder's downscale-during-decode plan, and the two can't drift (a stale decode plan would under/over-decode). See the `video.py` decode-time-downscale note above.

### Bitmap engage clean-field (`engage_bitmap_mode`)

The hires/mhires VIC bring-up is one shared module-level primitive, `engage_bitmap_mode(api, *, d011, d018, d016, …)`. It is called by **both** the single-buffer `HiresDisplayMode`/`MultiHiresDisplayMode` `setup()` **and** `voice_scope.VoiceScopeRenderer._apply_vic_hires_bank`, the waveform/midi oscilloscope — so the engage invariant and the VIC-register set live in exactly one place and cannot drift apart. Two copies drift in one particular direction — one of them ends up clearing *after* its `$D011` flip instead of before, which is precisely the garbage field the invariant exists to prevent.

**The invariant.** Zero both the `$2000` bitmap **and** screen RAM (`$0400`) *before* flipping `$D011` into bitmap mode, and write `$D018`/`$D016` first as well. The window between the mode flip and the first composed frame then shows solid black, rather than uninitialized-RAM garbage or a color ghost of the prior scene.

**Why `$0400` too — the non-obvious part.** A zeroed bitmap makes every pixel select its cell's *background* color. In hires, that background is the **low nibble of the `$0400` byte**, not `$D021`. So leaving stale `$0400` — say the previous interstitial's PETSCII codes — paints a 40×25 color ghost on engage. Zeroing `$0400` pins every cell's background to black.

**Why border and bg0 are pinned on every path.** `$D020`/`$D021` are set to `0x00` everywhere, including REU-staged mhires. The REU bank-swap IRQ only starts writing `$D021` from the first *real* swap, since the frame tracker's ready flag starts zeroed (see `modes_irq.install_bank_swap_irq`). Without the setup-time write, every frame until that first swap showed whatever `$D021` the previous scene left behind — observed on hardware as a black border over a stale-blue screen. The setup write covers exactly that gap; the IRQ still owns `$D021` from the first real frame onward.

**Per-caller differences are arguments, not forks:**

* `dd00` plus `bitmap_base`/`screen_base`/`d018` let the scope **relocate the VIC bank**, switching bank 0↔2 according to the SID footprint.
* `clear_region_ids` selects the **delta-cached `write_region`** clear — used by the scope, which reuses stable region IDs to also blank its spacer rows — versus the **`write_memory_file`** bulk clear, the display modes' one-time clear that bypasses the cache the first `push` rebuilds.
* `clear=False` lets the REU and host-DMA double-buffer paths take only the register pokes, since they zero both VIC *banks* themselves during setup.

### Char engage clean-field (`_clear_char_screen`)
The char-mode sibling of the invariant above: `PETSCIIDisplayMode`/`BlankDisplayMode`/`MCMDisplayMode` `setup()` all clear `$0400` (to `SC_SPACE` for PETSCII/Blank, `0x00` for MCM — whose 2-bit sub-cell code selects bg slot 0) + `$D800` to black BEFORE the `$D018`/`$D016`/border-register pokes, and flip `$D011` LAST, so a mode switch — especially away from a bitmap scene, whose `$0400` holds nibble-packed colors rather than glyph codes — never reveals stale screen content as garbled characters. MCM additionally pins `$D020`-`$D023` (border + bg0-2) to black at setup so its cleared screen (code `0x00` = bg slot 0) is actually black rather than whatever the previous scene's bg registers held; PETSCII/Blank instead push their own style/configured border+background immediately, since those are already fully determined at setup.

### Scene fade (dim toward black)
Every compose-based mode supports a setup/teardown fade driven by the Playlist (`[playlist].fade_duration_s`, 0 disables). The C64 has no global brightness register and its 16 palette indices aren't luminance-ordered, so the fade is a **palette remap**: `palette.build_fade_lut(alpha)` returns a 16-entry LUT mapping each color to the palette index nearest (in the quantizer's weighted-BGR space) to `C64_PALETTE_BGR[c] * alpha` — identity at `alpha ≥ 1`, all-black at `alpha = 0`, black always → black, memoized on a 1/256-quantized alpha. `DisplayMode.apply_fade(buffers)` applies that LUT to a mode's **color-bearing** fields only and leaves the **bitmap pixel-selectors** untouched, so dimming the cell colors fades the picture while black pixels stay black: PETSCII/Blank dim color RAM (FG); MCM dims the shared bg0/bg1/bg2 registers + the per-cell multicolor FG (via a 0..7-constrained LUT so the dimmed value stays a legal multicolor color and bit 3 is preserved); Hires dims both screen-byte nibbles (fg/bg) via `_fade_nibbles` + the bg/border scalar; MultiHires adds color RAM (c3). `apply_fade` never mutates its input — `_render_with_overlays` caches the full-brightness, post-overlay buffers as `display_mode.last_buffers`, then dims a copy before push; `repush_faded(api, alpha)` re-dims that pristine cache and re-pushes, which is how the freeze+dim fade-out replays the last frame at decreasing alpha without re-composing (the unchanged bitmap delta-skips, so it's cheap). Non-compose scenes (waveform/midi oscilloscope, native launcher — all `display_mode = None`) are untouched. The Playlist timeline + CTRL-skip abort are in the `scenes.py`/playlist note below.

### Persistent brightness dim (`user_dim`)
Alongside the transient `fade_alpha`, every mode carries a `user_dim ∈ (0, 1]` (default 1.0) — the WLED bridge's `bri` slider as a *real* output dim. `apply_fade` feeds `build_fade_lut` the **product** `fade_alpha * user_dim` (the `DisplayMode._fade_lut_alpha` property; the LUT memo cache already keys on the combined alpha), so a fade-out from a dimmed scene ramps down from the dimmed level, not from full. `repush_faded` still toggles only `fade_alpha`, so the freeze+dim replay inherits the dim for free. The `_render_with_overlays` dim guard is widened to `fade_alpha < 1.0 or user_dim < 1.0`, and because every compose-based scene composes every frame there's no repush machinery for the static case — a `user_dim` change lands on the next frame (same non-compose/launcher limitation as the fade). `user_dim` lives on the per-scene mode instance, so `Playlist.user_dim` owns the persistent value and `safe_setup` re-stamps it onto each fresh scene's mode — a dim set via the app survives playlist auto-advance. The bridge (`wled_device._apply_dim`) writes both `pl.user_dim` and the current mode's `user_dim` for an instant-plus-durable effect.

### Key vectorization tricks


* `palette.quantize_distances()` returns the full (N, 16) distance matrix via the `(x-p)²` expansion — avoids the (N, 16, 3) broadcast tensor the naive form would build.
* `MCMDisplayMode` reuses one distance matrix across both the bg-color picker and the per-cell FG search, and vectorizes the original 8-iteration Python loop into one `argmin`.
* `MultiHiresDisplayMode` has two render paths. The **global-4** path (cheap/vivid/grayscale palette modes) uses a 16-entry LUT to remap every palette index to the nearest of the 4 globally-chosen colors (in weighted BGR space); zero-defaulting the unused indices to bg0 instead is the cheaper option and it silently bleeds large patches of background into the image. The **per-cell** path (default `palette_mode = "percell"`) uses VIC-II MCBM's per-cell `c1`/`c2`/`c3` capacity: picks `bg0` globally, then for every 4×8 cell picks its own top-3 non-bg colors by population and resolves each of the 32 cell pixels against {bg0, c1_cell, c2_cell, c3_cell}. Frames carry up to `bg0 + 3×1000 = 3001` distinct colors instead of 4 — the capacity VIC-II MCBM was designed around, and which the global path leaves almost entirely unused.
* `PETSCIIDisplayMode` delegates glyph + color selection to a `PetsciiStyle` from `petscii_styles.py` (see below). The default style is the original luma → 11-char ramp + per-cell quantized color; cycling via SHIFT swaps in increasingly abstract alternatives (halftone blocks, random graphics glyphs, letter rain, etc.).

### `palette_mode` — per-cell slot allocation

`MCMDisplayMode` and `MultiHiresDisplayMode` accept a `palette_mode` constructor argument (configurable per-scene via `palette_mode = "percell"|"cheap"|"vivid"|"grayscale"` in TOML, default `"percell"`):

* **`"percell"`** — MultiHires only. MCM treats it as an alias for `"cheap"`, since MCM already picks its fg per cell. See the detailed breakdown below.
* `"cheap"` — global-4. HSV saturation boost (`boost_saturation`, factor 1.8) before quantization plus a `make_gray_penalty` bias added to the per-pixel distance matrix. The penalty pushes the 5 gray-axis palette entries + cyan (which sits at the pale-chromatic boundary and over-selects on warm-gray skin) far enough that borderline pixels flip to a chromatic neighbor. Top-N slot picks go through `_ema_counts` (EMA-smoothed bincount, `PALETTE_PICK_EMA_ALPHA = 0.25`) and are then sorted by palette index, so the chosen SET only flips on sustained scene changes and a stable SET always lands in a stable slot ORDER — without this the picks flickered between e.g. cyan and orange every few frames as borderline counts tied differently, rewriting screen + color RAM + bg registers and producing a visible palette flash. Still the default for MCM.
* `"vivid"` — same biases, plus the 3 (MCM) / 4 (MultiHires) global slots are picked by `pick_diverse_top_n` instead of raw frequency: the most-populated index always wins slot 0, then each subsequent slot prefers a populated entry whose hue is at least 45° away from already-chosen chromatic picks. Falls back to most-populated when no diverse candidate exists. Use when a scene keeps reducing to two-or-three near-shades.
* `"grayscale"` — restricts every quantization decision to the 5 gray-axis palette entries (black, white, dark gray, gray, light gray). Skips the saturation boost (wasted work on gray-only output) and uses `make_gray_penalty(chromatic_strength=GRAYSCALE_CHROMATIC_PENALTY=1e10)` so every chromatic entry is dominated in the per-pixel argmin. Global slot picking is **fixed** (not adaptive) in luminance order: MHires uses `(0, 11, 12, 15)` = black, dark gray, gray, light gray (pure white is dropped for better mid-tone resolution); MCM uses bgs `(11, 12, 15)` with FG resolving to `{0, 1}` for full 5-level coverage per screen. The MHires LUT is precomputed once at `__init__`. Adaptive picking from only 5 gray entries is a perf trap: per-frame tie-break shuffles flip the slot order, which rebuilds the LUT, which remaps every pixel to a different slot in the 8 KB bitmap, which busts the chunked-delta cache and forces full bitmap + screen RAM + color RAM uploads every frame — ≈13 fps. Pinning the order costs nothing visually and holds the same "old TV broadcast" aesthetic at the full system frame rate (60 NTSC / 50 PAL). Note that in MCM only black (0) and white (1) survive into the FG slot (color RAM bit 3 = multicolor flag steals the high bit, so FG is restricted to indices 0..7).

#### How `"percell"` works

**Choosing `bg0`.** Globally, as the EMA-smoothed most-populated palette index — **stabilized by relative hysteresis** (`BG0_HYSTERESIS_MARGIN`). bg0 only changes when a challenger's smoothed count beats the incumbent by the margin.

That hysteresis is why near-tied dominants — mostly-black video with a bright moment, or pillarbox/letterbox bars — stop strobing `$D021`. Without it the background and bars flash a different color every frame. Note this is a single instant register change, *not* a write tear, and it is especially visible on a slow transport like TeensyROM serial where the rest of the frame lags behind. A sustained dominant-color shift still moves bg0, and an old bg0 that vanishes (smoothed count → ≈0) is never sticky.

**Choosing each cell's 3 colors.** For every 4×8 cell, the top 3 non-bg colors by population, using a per-cell bincount on the same `(N,16)` distance matrix the global path uses — or an alternate [`[color].cell_strategy`](#colorcell_strategy--which-3-colors-fill-a-cell).

Picks are sorted by palette index for delta-cache stability, and bg0 is excluded from the per-cell search so the cell's effective palette stays at 4.

**The bg0 poison-filler guard.** A cell with fewer than 3 distinct non-bg0 colors present — mostly-bg0 cells, which are the norm under a small forced palette — **pads its surplus slots with bg0**, not with an arbitrary zero-count palette index.

Padding with an arbitrary zero-count index leaks an out-of-palette color — green into a `[0,4,6,14]` cast, say — and churns slot order frame to frame. The VIC renders that briefly during the non-atomic screen/color/bitmap write tear, which on a slow transport like TeensyROM serial reads as green-square flicker and flashing letterbox edges. bg0 in a filler slot is a harmless duplicate, since the `%00` code already reaches it.

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

`MultiHiresDisplayMode._compose_percell` and MCM's per-cell `fa` computation still pick each cell's *candidate set* — `{bg0, c1, c2, c3}` and `{bg0, bg1, bg2, fg}` respectively — through the same EMA-smoothed histograms, dithering or not. Dithering replaces only the final per-pixel-within-cell code assignment: `d_cand.argmin` becomes `error_diffuse_cells(pixels_cell, candidates_bgr, method, strength)`. That loops over the small in-cell pixel count (32 for mhires, 4 for MCM) while staying vectorized across all 1000 cells at each step, rather than looping cell by cell.

Hires — 2 colors, a global `bg` plus a per-8×8-cell sampled `fg` — gets the same treatment over 8×8 blocks.

**No hysteresis on this path.** Each cell re-diffuses independently every frame with no persisted state, so the per-pixel code hysteresis (`PERCELL_CODE_HYSTERESIS_BONUS`) is skipped — there is no meaningful "previous code" to blend toward.

That is precisely why `"auto"` never picks these for a motion scene: independently-diffused frames read as shimmer even though any single frame looks great. The ordered family's fixed pattern does not have this problem.

**Coverage differences.** MCM has no separate percell-vs-global `palette_mode` branch — `fa` is computed the same way regardless — so its FS/Atkinson dithering applies unconditionally. mhires' only fires under `palette_mode = "percell"`; the global-4 `_compose_global` path has no per-cell candidate structure to dither against, though it still gets the ordered-family offset for free, applied upstream in `flat`.

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

**Perceptual swaps only the distance space, not the shaping.** `channel_boost` and `gray_penalty` still apply, and this is load-bearing. Dropping them as weighted-BGR crutches is the tempting move and it is wrong: hardware A/B shows flat desaturated regions — a pale sky — fragmenting into drab gray under the accurate-but-neutral Lab match. The gray penalty is what keeps those regions chromatic, and `channel_boost` holds the C64-friendly hues.

The gray penalty and the percell code/quant hysteresis bonuses are all d²-space quantities, so they are scaled by `palette.PERCEPTUAL_DIST_SCALE` (≈1/3, the Lab-vs-weighted-BGR magnitude ratio for equal physical gaps). Their tuned strength therefore carries over.

**Reach.** petscii threads the metric through `petscii_styles._quantize_color` / `_quantize_to_spectrum`. The force-palette remap is unaffected — its pixels are already exact palette colors, so every metric returns the same index.

**`"auto"` resolution.** `config.resolve_color_match(setting, display_mode_name)`, inside the single construction funnel `_build_display_mode`, picks perceptual on every quantizing mode (`_COLOR_MATCH_AUTO_PERCEPTUAL`) and rgb on the non-color-picking ones (blank, hires_edges). `validate_color_match_cfg` and `doctor._validate_color_match` report the resolved metric per scene.

**Hardware A/B on the U64**, with the default `auto_fit` saturation lift in play: MCM improves clearly, with smoother skin gradients and far less per-cell color speckle. mhires, hires, and petscii range from a wash to a marginal win, because `auto_fit` already dominates their color decision. But perceptual never regressed once the shaping was kept, so `auto` chooses it everywhere it applies.

Cost is one extra `cvtColor` per frame on the small downscaled `flat` (≤64k px) — negligible.

### `[color].cell_strategy` — which 3 colors fill a cell

Implemented in `modes._pick_cell_colors`. Selects *which* 3 of a cell's present colors fill the per-cell `c1`/`c2`/`c3` slots on the mhires `percell` path.

No-op everywhere else: MCM already picks a single fg per cell by error, and the global-4 modes have no per-cell pick at all. It is orthogonal to `palette_mode` (percell vs global), `dither` (the per-pixel fill, decided *after* these 3 colors), and `color_match` (the distance space).

**The four strategies:**

* **`"frequency"`** — the default. The 3 most-populated non-bg0 colors, ranked on the EMA-smoothed per-cell histogram. Temporally stable.
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

### `[color].hires_cell_pick` — which color fills a hires cell

`"error-min"` (default) `| "sample"`. Hires gets two colors per 8×8 cell and one of them is spent on the global background, so the single remaining choice — which foreground the cell takes — decides most of the frame. This selects how it is made. Only the `"normal"` style picks color at all; the two `edges` styles are fixed 2-color, so the knob is inert there, exactly like `color_match`.

**`"sample"`** reads one pixel per cell (`quantized[4::8, 4::8]`). Cheap, and the historical default.

**`"error-min"`** (`HiresDisplayMode._errmin_fg`) picks the entry minimizing that cell's own reconstruction error. Because every pixel ends up showing whichever of `{bg, fg}` is nearer, a candidate's cost for a cell is exactly that elementwise minimum averaged over its 64 pixels — so there is no search, just one `argmin` over the 16 entries of a `(1000, 64, 16)` view of the distance matrix **the quantizer already built**. It reuses `quantize_distances_for`'s output rather than recomputing anything, which is why the whole change costs ≈0.8 ms/frame.

**Why it replaced the sample as the default.** The sample was kept on the grounds that it costs less *and holds still better*, and the second half does not survive measurement. Against `"sample"` on a noisy static subject, error-min scores **−34 % mean Lab error** and drops per-frame screen churn to **zero** (`"sample"` sits at ≈33 bytes/frame), because a one-pixel read tracks sensor noise directly while a whole-cell mean averages it out. It is the more accurate pick and the stabler one at once. The cost half of the claim is real but small, so `"sample"` stays available for tight CPU budgets.

**Where the gain comes from.** Entirely from intra-cell variance — the two only diverge when a cell's own pixels disagree, and the advantage tracks that almost linearly:

| intra-cell std dev | example content | error-min vs sample |
|---|---|---|
| ≈1 | smooth gradient | ±0 % |
| ≈4 | soft/blurred | ±0 % |
| ≈14 | flat color patches | −13 % |
| ≈73 | high-frequency detail | −32 % |

On the repo's photo set it lands at **−24 %**, consistently across every `dither_method` (−26 % to −33 %). A flat or smoothly graded test fixture asserts nothing about it, which is what `tests/test_hires_cell_pick.py`'s `textured_frame` exists to avoid.

**Hysteresis.** `HIRES_CELL_HYSTERESIS_BONUS` (2000, d² space, scaled by `PERCEPTUAL_DIST_SCALE` under the Lab metric like base.py's percell bonuses) keeps a cell's previous pick unless this frame beats it by that margin. Well below the per-pixel 5000 because the quantity differs: this thresholds a *mean* over 64 pixels, which has already averaged most of the noise out. Swept on noisy static and panning sequences — 2000 takes static churn to zero for +0.06 Lab on the panning case, and everything above only buys lag (5000 → +0.28, 15000 → +1.05, 50000 → +6.6). Since it is a decision hysteresis and not a smoother, over-damping shows up directly as motion inaccuracy, so it sits at the knee. `set_cell_pick` drops the state on a live swap — the strategies choose by different criteria, so a carried-over "previous pick" would hold the old strategy's answers for a frame.

### `[color].flicker_tolerance` — temporal colour blending

Off by default (`flicker_tolerance = "off"`), hires `"normal"` style only. Holds **two** screen pages over one shared bitmap and alternates `$D018` between them every video field, so the eye fuses each cell's pair of hardware colours into a shade the VIC cannot draw — the Dragon Breed / Mayhem in Monsterland trick. Colour side in [`video/flicker.py`](../../c64cast/video/flicker.py), C64 side in `modes_irq.FLICKER_SWAP_IRQ_HANDLER` — whose bank-swap commit is held to the [raster gate](#the-raster-gate--why-a-vblank-irq-is-not-enough) while the `$D018` alternation itself is not.

**The frame rate does not come from the link.** This is the thing that makes it practical: the alternation is owned by a C64-side raster IRQ and free-runs at the VIC field rate no matter how slowly the host pushes. The host only uploads the *pair*. Both fields share one bitmap — the fg/bg mask must be identical or the flicker would be geometry rather than colour — so a frame costs one extra 1000-byte page, not a second frame: **≈26.0 ms vs 20.8 ms** on the Ultimate link (`HardwareProfile.write_cost_s`), comfortably inside the 30 fps bitmap cap. Compose adds ≈1.3 ms. No REU and no sampler involved; it works on the TeensyROM too.

#### Eligibility: a safety cap, then a table of what was actually seen

`flicker.blend_pairs(max_luma_delta, tolerance=)` admits a pair when three things hold: the **absolute difference in linear luminance** between its two colours is under the luma cap, the pair carries a scored tier no worse than the tolerance allows, and the fused colour lands ≥4 Lab from all 16 solids — below that it duplicates a solid and costs a page write for nothing.

**The cap is a photosensitivity control, and that is all it is.** A pair is seen at 25 Hz (PAL) / 30 Hz (NTSC), inside the ITU-R BT.1702 risk band, where the hazard scales with luminance modulation depth. Hence `flicker_max_luma_delta = 0.075` by default, a hard `MAX_ALLOWED_LUMA_DELTA = 0.12` clamp set below the 20%-of-peak-white level the guidance is written around, a warning past `WARN_LUMA_DELTA = 0.10`, and the feature opt-in.

**Two rules were fitted here and a blind run refuted both.** The first derived 0.075 from six flat bands bracketing a solid/flicker transition, leaving a 0.106-wide unsampled hole that the interesting behaviour turned out to live inside. Scoring the pairs the default admits put ΔY's correlation with the verdicts at r=+0.33 with two clean refutations, so the branch then reached for colour instead: every pair containing Red (2), Purple (4), Orange (8) or Light Red (10) had scored high, and `flicker_max_warmth` capped a Lab chroma projection onto a red-orange axis to exclude them.

That rule was then scored against a run it had not been fitted to — all 33 pairs the hard clamp admits, positions shuffled, pools separated, seven hidden solid negative controls, key withheld — and it did not survive:

| predictor | r vs scored rating | AUC, moderate-or-worse |
|---|---|---|
| warmth (max of pair) | +0.32 | 0.714 |
| ΔY | +0.26 | 0.680 |
| Δchroma, max chroma, mean luminance | +0.04 … +0.08 | — |

Best multi-term fit: adjusted R² **0.179** over n=33. Two things killed the warm rule specifically. All seven solid controls scored *none*, Red, Orange and Brown among them — so warm colours do not flicker on their own, and the effect is fusion failure rather than composite chroma crawl. And warm+warm pairs are among the steadiest scored: Red+Purple, Red+Orange and Purple+Orange all read *very mild* while Red+Dark Gray reads *intense*. What the earlier session had picked up was warm against **neutral**, and the cap was excluding five of the eight quietest pairs to catch it.

**So the eligible set is a recording, not a rule.** `flicker.SCORED_FLICKER` holds one tier per pair on the five-point scale the sitting used, and `[color].flicker_tolerance` is a cut across it:

| tolerance | admits | pairs (U64, cap 0.12) | effective palette |
|---|---|---|---|
| `off` (default) | nothing | 0 | 16 |
| `clean` | none + very mild | 8 | 24 |
| `subtle` | + mild | 14 | 30 |
| `visible` | + moderate | 23 | 39 |
| `strobe` | + intense | 33 | 49 |

The tolerance values are named apart from the tier names on purpose: one pair scored `none`, which a tolerance called `"none"` would have to include and exclude at once.

**A pair with no tier is never admitted, at any tolerance.** On the Ultimate 64 table that costs nothing — the scored set is exactly what the hard clamp allows, so coverage is total at every legal setting. The VIC-II rendering shifts luminances enough to bring five unscored pairs under the clamp, and one of them is Cyan+Yellow, which this module's own docstring calls as violent a flicker as anything on the chart and which ΔY refused on the U64. Excluding the unscored is what stops a palette swap admitting it. `scripts/diags/flicker_score_grid.py` is how the table grows; a test pins the recorded distribution so a tier cannot drift silently.

**What the table does not carry.** One observer, one sitting, one rating per pair, and that observer put the mild/moderate and moderate/intense boundaries at ±1. `"clean"` is the only cut that rests on neither. The tiers are also applied to whatever `host_palette` is active, which is an extrapolation from the Ultimate 64 they were collected on.

**Why absolute ΔY and not a contrast ratio.** Michelson contrast was the first rule and it is wrong in the one place it matters. Dividing by the pair's own mean luminance makes the metric maximally pessimistic where the eye is least sensitive: black against anything scores 1.0 by construction, so Black+Blue, Black+Brown and Black+Dark Gray — all under 0.07 ΔY, all of which fuse cleanly — could never qualify at any setting. In the other direction it admitted Cyan+Yellow, which on an Ultimate 64 is 0.26 ΔY. Against the emitted palette the two rules agree on only 9 of ~20 pairs. Weber contrast and a Ferry-Porter frequency term were tried against the same six bands and both degraded the separation; a chroma-swing term did too, which is the expected result — chroma flicker fuses at a far lower rate than luminance flicker, so it is not the binding constraint.

The 8-bit `PALETTE_LUMA` delta is also wrong here, for a different reason: it is Rec.601 on gamma-encoded values, so it overstates separation at the dark end exactly where these pairs live.

**Eligibility is per machine.** ΔY is measured against the active palette, so which pairs are even candidates follows [`host_palette`](#palettepy--which-16-colors-the-machine-emits-hardwarehost_palette) — what fuses is a statement about the light one machine emits, not about "the C64 palette". `flicker.py` registers an `on_palette_change` listener rather than computing its tables at import, because a stale table would admit pairs that flicker on the machine in front of you, which is the single failure this module exists to prevent.

**The safety cap binds before the tolerance does.** Three of `"clean"`'s eight pairs sit between 0.075 and the 0.12 clamp, so the shipping default holds it to five:

| cap | `clean` | `subtle` | `visible` | `strobe` |
|---|---|---|---|---|
| 0.05 | 5 | 7 | 8 | 12 |
| **0.075 (default)** | **5** | **9** | **13** | **21** |
| 0.10 (warns above) | 7 | 13 | 19 | 28 |
| 0.12 (clamp) | 8 | 14 | 23 | 33 |

(Ultimate 64. The VIC-II table is smaller throughout and flat in `clean` at 3.) Raising the cap to reach the other three is a photosensitivity decision, not a quality one, and should read that way in any recommendation.

Fusion is the **linear-light** average, not the sRGB one — the eye integrates emitted light over the two fields, so mixing the encoded values instead makes every blend read too dark, worst where the gamma curve is steepest.

#### What it is actually for

Gradient banding, not a general palette upgrade — spatial dither already synthesises intermediate colours wherever there is texture to hide them in, so blending is largely redundant on photographic content and only pays where dither has little to work with. Measured against the plain path (perceptual metric):

| content | VIC-II palette | Ultimate 64 palette |
|---|---|---|
| chromatic gradient (blue→cyan) | **−33.8 %** | **−26.8 %** |
| vertical dusk gradient | −20.4 % | −14.8 % |
| luminance ramp (black→white) | −9.1 % | −15.7 % |
| warm sky gradient | −8.2 % | −1.0 % |
| soft radial glow | −1.8 % | −0.5 % |
| photograph | −1.3 % | −0.9 % |

Two columns because eligibility is per machine, and the two tables do not gain the same colours: the ramp improves twice as much on an Ultimate 64 (its dark end holds more near-equal pairs), the warm gradients less.

**Those figures admit every eligible pair**, which is `flicker_tolerance = "strobe"` today. Isolating the palette from the cell fit — per-pixel nearest-colour Lab error against the widened table, so not the same quantity as the compose measurement above, but it tracks it within a point or two — shows what each cut is actually worth at the 0.12 cap:

| content | `clean` | `subtle` | `visible` | `strobe` |
|---|---|---|---|---|
| chromatic gradient (U64) | −11.5 % | −23.3 % | −29.3 % | −29.3 % |
| chromatic gradient (VIC-II) | −12.9 % | −31.8 % | −34.0 % | −34.0 % |
| vertical dusk gradient (U64) | −11.7 % | −14.5 % | −14.5 % | −14.6 % |
| luminance ramp (U64) | −2.3 % | −16.4 % | −17.2 % | −17.2 % |

The last column is the finding worth acting on: **`"strobe"` measures the same as `"visible"`** to within 0.1 % everywhere. The ten pairs scored *intense* add no reconstruction accuracy at all — whatever they cover, a quieter pair or a solid already covers about as well. So `"strobe"` is never the right answer to "I want more colours"; it exists only for when the alternation itself is the intended effect.

**It requires the perceptual metric**, and forces it. Blending is *defined* perceptually — linear-light fusion, Lab-measured gaps — so fitting cells in weighted-BGR optimises a different space than the one the extra entries live in. That mismatch is not academic: under the BGR metric the widened palette measures **worse** than the 16 solids on a photo (+2.5 %) and on a luminance ramp (+6.3 %), where the same frames improve under Lab. `color_match`'s own default already resolves to perceptual here, so the force only fires when a config explicitly asked for `"rgb"`, and `set_cell_pick`'s sibling `set_color_match` pins it live.

Blending also **forces the error-min cell pick** regardless of [`hires_cell_pick`](#colorhires_cell_pick--which-color-fills-a-hires-cell): a blend entry sits between its two constituent solids, so a single-pixel sample lands on one of them more or less at random, and the widened palette then scores worse than the 16 solids. The cell fit is what makes the second page pay for itself.

#### Mechanism

`FLICKER_SWAP_IRQ_HANDLER` (53 bytes at `$C500`) is the host-DMA page-flip handler plus an unconditional per-field toggle of the `$D018` screen-matrix nibble between `D018_HIRES_PAGE_A` (`$18`, matrix offset `$0400`) and `_B` (`$38`, offset `$0C00`), bitmap pinned at the `$2000` offset in both. Those values are **bank-relative**, so one pair is correct in bank 0 and bank 2 alike and the alternation survives a `$DD00` double-buffer swap untouched.

The toggle sits deliberately *ahead* of the ready-flag check — the alternation is the C64's job and must free-run whatever the host is doing, which is precisely why this needs no 50-60 fps link. Only the double-buffer commit (`$DD00` + `$D021`) waits on a staged frame, and that commit is additionally gated on landing in **phase 0**, so a swap arriving on an odd field can never transpose the A/B page roles — invisible on a still frame, a colour shift on motion. `X` carries the page index and is not saved: kernal `$FF48` pushed A/X/Y before vectoring through `$0314`.

Tracker at `$C700`, 6 bytes: `[bg0, bank, ready, phase, d018_a, d018_b]`. `phase` is handler-owned, so `_arm_flicker_swap` writes only the first three — re-sending the rest would restart the alternation from page A on every staged frame and stall the blend. `install_bank_swap_irq`'s `tracker_init` seeds the page pair before the raster source is armed, since zeros there would point VIC at the `$0000` matrix offset for the field or two before the first frame stages.

`$0C00` rather than `$0800` because `$0801` is where `run_prg` drops a PRG. It is the same page [`overlays/big_text.py`](control.md) page-flips its own strip into, for the same reason — which is why the two cannot be live at once.

#### Gating

`scene_factory.resolve_flicker_tolerance` is opt-in, so there is no `"auto"`; it only decides where an explicit tolerance can be honoured, returning `"off"` where it cannot. An unrecognised value raises rather than degrading to `"off"`, which would silently disable the feature on a typo. Four structural gates: hires only (mhires' c3 lives in un-banked colour RAM at `$D800`, which `$D018` does not select, so only part of its picture could alternate — and the char modes keep per-cell colour there too); `"normal"` style only; no buffer-painting text overlay (the `$0C00` collision); and not while the REU mic pump owns `$0314`. `force_host_dma` gates it as well, for the reason it gates the others — a SID-audio scene's player owns `$0314`.

Where it engages it takes the double-buffer slot and pushes REU staging aside, extending the mutual exclusion those two already have, because the REU bank-swap handler has no `$D018` phase toggle. A `display = "random"` slideshow re-resolves it per concrete mode, alongside the other two.

The border cannot blend: `$D020` is a single register the field IRQ does not manage, so it takes the field-A component. Widening the handler to alternate it would buy a blended frame *around* the picture at the cost of bytes in the one routine that must fit inside vblank.

### `[color].motion_smoothing` — temporal smoothing / after-images

Range 0..1, default 0.25. A single dial over the mhires `percell` path's two *temporal* flicker-suppression buffers. No-op on every other mode and palette_mode — only percell carries them.

**The two buffers:**

1. The per-cell color-count EMA (`_smoothed_cell_counts`, blended each frame with `PERCELL_PICK_EMA_ALPHA = 0.15`), which stabilizes *which* colors a cell offers.
2. The per-pixel/per-cell decision hysteresis (`PERCELL_QUANT_HYSTERESIS_BONUS` / `PERCELL_CODE_HYSTERESIS_BONUS`, each 5000 in d²-space, further scaled by `PERCEPTUAL_DIST_SCALE` under Lab matching), which keeps a pixel on its previous palette index or bitmap code unless the new frame beats it by the bonus.

**The tradeoff.** Both exist to stop per-frame color churn reading as shimmer on noisy video. Both buy that by trading motion-tracking for stability — so on a hard shot cut they hold structure from the *previous* shot for a moment, and an outline lingers as an after-image while the buffers decay.

**What the dial does.** `motion_smoothing` scales both together at construction time:

| `s` | Behavior |
| --- | --- |
| `1.0` | Full smoothing: `_ema_alpha = PERCELL_PICK_EMA_ALPHA`, full hysteresis. Most stable, ghostiest. |
| `0.0` | `_ema_alpha = 1.0` (new frame fully replaces count history) and both hysteresis bonuses zeroed. Tracks the source frame-exactly — no after-image, but grainy content can flicker. |
| between | Lerps both: `_ema_alpha = 1 - s·(1-0.15)`, `hyst = base·s·penalty_scale`. |

Threaded `ColorCfg.motion_smoothing` → `_build_display_mode` → `MultiHiresDisplayMode.__init__`; `compose()` reads `self._ema_alpha` rather than the module constant.

**Why one dial and not an EMA-only knob.** An offline stateless-vs-stateful A/B (`scripts/diags/mhires_ema_ghost_ab.py`, measuring how far the stateful render deviates from a fresh-mode render of the same frame) isolated the contributions:

* The **hysteresis dominates** — killing it alone removes ≈60 % of the deviation.
* The EMA is secondary, ≈30 %.
* `s=0` plus no hysteresis tracks the stateless ground truth exactly.

Since neither buffer accounts for the ghost on its own, a combined dial is the correct control.

**Why 0.25.** Picked by an on-hardware flicker/ghost A/B on the U64 — WarGames hard cuts for the after-image, grainy dark footage for flicker — as the lowest value where flicker stays acceptable. It is a large ghost reduction against the `1.0` row above.

`validate_motion_smoothing_cfg` and `doctor._validate_motion_smoothing` bound it 0..1 and note a non-default value on the mhires percell scenes it affects. Orthogonal to `cell_strategy` (which 3 colors), `dither` (per-pixel fill), and `color_match` (distance space).

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

**Where `reu_available` comes from.** Computed once in `cli._resolve_reu_available` — gated on `"auto"`, `api.profile.supports_reu`, and not `--skip-probe`, via `hw_provision.reu_is_enabled` — then stashed on `SystemStack.reu_available` and threaded through `scenes_from_config`/`build_scene`, including SIGHUP/control-plane reloads and ensemble-follower rebuilds. A `display = "random"` slideshow stores the raw tri-state plus `reu_available` and re-resolves per concrete mode at each setup.

Any uncertainty — no REU, a failed query, `--skip-probe`, a non-REU backend — degrades to host-DMA, so video never silently freezes.

#### The two REU pipelines

**Char modes (PETSCII/Blank) — single-buffer.** `push()` calls `modes_irq.push_screen_via_reu(api, screen_bytes, $0400)`: REUWRITE the 1000-byte screen to `REU_VIDEO_SCREEN_BASE = $E00000` (bus-clean), configure REC `$DF02`/`$DF04`/`$DF07` for a one-shot REU→main DMA, then trigger via `$DF01 = $91`. Color RAM at `$D800` is not VIC-banked, so it stays on the delta-cached DMAWRITE path.

**Bitmap modes (Hires/MultiHires) — double-buffer.** Bitmap and screen are REUWRITE-staged, then DMA'd into the *off-screen* VIC bank. A C64-side raster IRQ at `$0314` flips `$DD00` at vblank for a tear-free swap — this is what eliminates the scene-cut whole-screen flashes.

**Coexistence with the REU audio pump** is fine on any scene: the bank-swap installer picks a **merged** `$0314` dispatcher whose non-raster branch JMPs to the audio pump at `$C100`, servicing both IRQ sources through one hook. That merged dispatcher is why `use_reu_staged` and `use_reu_pump` need no mutual exclusion in `validate_scene_cfg`.

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

`setup()` zeroes both VIC banks' bitmap and screen, pins bank 0, and installs `HOSTDMA_SWAP_IRQ_HANDLER` — a 45-byte minimal handler at `$C500` with a 3-byte tracker `[bg0, bank, ready]` at `$C700` — via the shared `modes_irq.install_bank_swap_irq`.

`push()` writes bitmap and screen into the *off-screen* bank via `write_region`, using **per-bank** `RegionID`s: `BITMAP`/`SCREEN` for bank 0, `BITMAP_BANK2`/`SCREEN_BANK2` for bank 2. Each bank therefore diffs against its own prior content, not the other's. It then arms the tracker, and the next vblank IRQ that reaches the handler in time flips `$DD00` and `$D021` for a whole, tear-free frame — see [the raster gate](#the-raster-gate--why-a-vblank-irq-is-not-enough) for what "in time" costs and why it is not automatic.

**MHires color-RAM residual.** `$D800` is not VIC-banked, so the c3 slot still tears in a brief ≈9 ms window before each flip — color RAM is written last, just before arming. Bitmap and screen (the structure plus c1/c2) do go tear-free. Hires has no color RAM, and static-palette mhires (cheap, grayscale) does not churn it, so both are fully tear-free.

NMI audio lives on the `$FFFA` vector, independent of this `$0314` raster IRQ, so the two coexist with no REU pump on the TR. The handler chains to `$EA31`, so kernal keyboard scan (`$028D`) keeps the pollers live.

## `modes_irq.py` — C64-side IRQ handlers + REU push helpers

Everything the tear-free bitmap pipelines upload to C64 RAM, split out of `modes.py` (2026-08) so the 6502 layer lives apart from the `DisplayMode` hierarchy that drives it: the `$C500` bank-swap raster IRQ handlers (hires 61 B, mhires 83 B, the chunked mhires+audio merged dispatcher 176 B, and the 45 B host-DMA page-flip sibling for no-REU backends, plus its 63 B flicker variant), the `$C700` frame-tracker layouts each handler reads at vblank, the REU staging addresses near 14 MB (`REU_VIDEO_*`), the merged-dispatcher builder `_make_merged_handler`, and the `install_bank_swap_irq` / `uninstall_bank_swap_irq` bring-up/teardown plus the per-frame `push_screen_via_reu` / `push_bitmap_via_reu` / `push_mhires_via_reu` helpers.

The module is pure Python over `C64Backend` — no numpy, no cv2 — which is what qualifies it for `mypy --strict` (it's in the pyproject strict-files list; the `modes/` renderers stay out for those import reasons). The two `[video]` subsections above (`use_reu_staged`, `double_buffer`) describe when each pipeline engages; the byte-level rationale (branch-offset asserts, the NMI-collapse chunking math, the Cam Link FFT history behind the merged dispatcher) lives with the bytes in the module's own comments. Coverage: `tests/test_reu_video.py` and `tests/test_bitmap_compose.py` verify the handler bytes, tracker packing, and install/teardown sequences against `FakeAPI`'s write log — nothing about them changed in the split.

### The raster gate — why a vblank IRQ is not enough

Both host-DMA swap handlers ask `$D012` where the raster actually is before committing, and decline to commit outside `[248, 255] ∪ [0, 45]`. Without that check a "vblank" IRQ is only nominally in vblank.

**Why.** A host DMA write halts the 6510 at ~1.02 µs/byte, so an 8000-byte bitmap push stalls it ~8.2 ms ≈ 128 raster lines. A raster IRQ that fires during a halt does not run until the halt ends, and its `STA $DD00` then lands deep in the visible picture: the top band still shows the previous frame while the rest shows the new one. Measured over HDMI at 60 fps while sustaining ~231 KiB/s, before the gate: **5.3% of flicker frames torn (seam at a median 30% of picture height), 1.2% of plain double-buffer frames (median 36%)**. The predicted seam from ~4000 bytes left on an average mid-flight catch is ~25%, which is what identifies the halt as the cause rather than something in the host's frame pacing.

**Why the host cannot fix it.** Scheduling writes to avoid the swap window needs the host to know the raster phase. Reading `$D012` means REST polling during playback, which wedges the machine; extrapolating from a clock reference drifts past a whole field within seconds. Without phase knowledge, "chunk only the writes that would straddle the swap" degenerates into chunking *every* write — which does work (0.0% torn under a 900-byte cap) but costs ~26 → ~15 fps. The decision has to be made where the information is, which is on the C64, in the handler, at the moment it runs.

**Skip, don't commit late.** Out of window the handler acks the IRQ and returns **without clearing the ready flag**, so the staged frame commits on a later field instead. A deferred frame holds the previous one a field longer; it never shows two at once. Freezing briefly is the better artifact — a tear is a broken picture, a repeated field is a slow one.

**What it measures after the gate.** Same diag, same load, 1796 scored frames per phase: plain double-buffer **0 torn frames of 1796**, flicker `0.28%` (5 of 1796), seams scattered at 9 / 12 / 40 / 48% of picture height. Throughput held at ~235 KiB/s and 61 writes/s with `clock/wall = 1.0000`, so the gate is free — which is the half of the result that separates it from the write cap.

The residual being **flicker-only is what identifies it**: anything on the display side would hit both phases, and plain reads exactly zero. The handler reads `$D012`, checks, then writes `$DD00` a few cycles later, and a halt that begins *in that gap* passes the check and still commits late. Flicker is the more exposed of the two — its phase-0 gate lets a staged frame wait a whole extra field before it is even eligible, so it is likelier to be pending when an 8000-byte push starts, and its handler is longer. Scattered seam positions fit a race; a fixed line would not. Closing it means removing the halt rather than dodging it, which is REU staging.

**The window, and the 8-bit aliasing.** `RASTER_COMMIT_LAST_SAFE_LINE = 45` sits below the first badline (51 at the default YSCROLL), where the VIC starts fetching the frame's video matrix. The safe set wraps through 0, so the handler adds 8 first — rotating `[248, 255] ∪ [0, 45]` into a contiguous `0..53` — and the check costs one `CMP` and one branch. `$D012` cannot distinguish line *n* from *n*+256, but every line that aliases into the window really is in vblank on both systems (NTSC 256-261 and PAL 256-301 read back as 0-45); PAL 302-311 alias onto 46-55 and are conservatively rejected, forgoing a commit opportunity and nothing else. No line in the picture (46-247) can alias in, since none exceed 255. One formulation is correct for PAL and NTSC.

**Flicker defers twice as far.** Its commit is additionally gated on phase 0, so a rejected commit waits for the next phase-0 field: worst case 2 fields = 33.4 ms against a ~38.5 ms host frame period at 26 fps. That is also the likely reason flicker tore ~4.4× more often than plain before the gate — half as many commit opportunities per second, so a halt is likelier to have covered all of them — and why the whole of the post-gate residual is on the flicker side. The attribution is inferred from the handler shape, not measured.

The `$D018` phase toggle is deliberately **not** gated — it sits ahead of the check and free-runs at the field rate whatever the host is doing, which is what makes flicker independent of link speed. Gating it would drop fields out of the fusion cadence, a worse artifact than a late page flip: the flip mistimes only the blended cells' colours, where a dropped field breaks the blend itself.

**Coverage.** `tests/test_raster_gate.py` runs both handlers' real bytes under py65 across both window edges, the wrap through 0, and the aliased line sets for 262- and 312-line systems. On hardware, `scripts/diags/flicker_tear_ab.py` is the acceptance test — it reports percent torn *and* seam position, and the run has to hold throughput, since a fix that buys cleanliness with frame rate is the write cap in disguise.

## `palette.py` — which 16 colors the machine emits (`[hardware].host_palette`)

Every color decision in the pipeline is a distance measured against a table of 16 BGR triples, so that table has to be the colors the display will actually show. It is not one table: a real VIC-II and an Ultimate 64's FPGA reimplementation are **~25 counts per channel apart on average, 60 at worst** (Orange), which is not a rounding difference.

**Measured, not assumed.** Captured off a U64's HDMI output, the firmware's own `default_colors` table (`software/u64/u64_config.cc`) comes back within **4 counts per channel** — and the residual is a uniform ~2-count black-level offset in the capture chain, present on Black too, so the table is exact. `U64_PALETTE_BGR` transcribes it; `PEPTO_PALETTE_BGR` is the classic VIC-II rendering that was previously the only table.

**What aiming at the wrong one costs.** Quantizing against a table the machine doesn't use is not a uniform tint that a viewer's eye discounts — the quantizer picks *indices* by distance, so a wrong table changes which color a pixel becomes. Measured over `assets/pictures/` at 320×200, against a U64 it costs **+12.9% mean Lab error** and sends **18.8% of pixels to a different index**, concentrated in the grays and warm colors (of all pixels: Dark Gray 4.2%, White 3.2%, Black 3.0%, Orange 2.4%, Light Red 2.0%). Per image it ranges from +4.4% to +30%, worst where the source is saturated.

**Resolution** is `hw_provision.resolve_palette`, a sibling of `resolve_system` and running from the same place for the same reason: what the machine reports about itself can only be read once the backend exists. `"auto"` reads the Ultimate's `Palette Definition` field and takes its built-in table; everything else is a real C64 (an Ultimate II+ and a TeensyROM+ both *drive* one, and neither has a palette of its own — the TR+ is a cartridge and emits no colors at all), so the VIC-II table is assumed. A machine carrying a custom `.vpl` is detected and warned about but cannot be read: the file lives in the Ultimate's flash and the REST API will not serve it, so `host_palette` takes a path to a local copy instead (`parse_vpl`).

**The swap mutates in place.** `set_host_palette` writes through `C64_PALETTE_BGR[:]` rather than rebinding the name, because half the render pipeline — `framebuffer.py`, the display modes, `flicker.py` — binds the array at import time and a rebind would leave all of them painting the old colors. Modules with their own palette-derived tables register a rebuild hook (`on_palette_change`); `flicker.py` does, because which two colors fuse is a statement about emitted luminance and a stale table there would admit pairs that visibly flicker on that machine.

The active palette is **process-wide**. An ensemble driving machines that render the 16 colors differently would need it per-system; threading a palette through every quantizer, dither buffer and fade LUT to serve that case costs far more than the case is worth, so `resolve_palette` keeps the first and warns — the same trade the frame profiler makes for its per-scene timings.

## `rolling_palette.py` + `palette.py` — forced-palette remap

**Forced-palette remap** (`[color].force_palette` / `force_palette_colors`) is the opt-in FALSE-COLOR stage.

**What it does.** k-means the source into N Lab clusters, assign each to a **distinct** C64 color via a min-Lab-error bijection, and bake a BGR→index LUT (`palette.ColorMapAccumulator` → `ColorMap`). A gamut-clustered source — TRON, which is essentially black plus dark blue — then uses all N colors instead of rendering near-monochrome.

Applied per frame as a single LUT gather in `ColorMap.apply` on mcm and mhires, the modes built with `_force_palette=True`. It is a no-op echo elsewhere.

**Two derivation paths, by source kind:**

* **Pre-scan** — `VideoScene` and `SlideshowScene`. One `prescan_source_color` pass fixes the map before the first frame.
* **Rolling** — live sources that cannot pre-scan: webcam, the `wled` sink, and generative.

**The rolling path** ([c64cast/video/rolling_palette.py](../../c64cast/video/rolling_palette.py): `RollingForcePalette` + `palette.RollingColorMapAccumulator`) runs a worker thread sampling the latest frame at ≈1 Hz into a sliding ≈30 s Lab window, re-baking a `ColorMap`. Three mechanisms let it adapt to changing content **without popping**:

1. **Warm-start k-means** — init labels are the nearest previous center (`KMEANS_USE_INITIAL_LABELS`).
2. **Assignment hysteresis** — keep the previous cluster→C64-index bijection unless the optimal beats it by more than `ROLLING_HYSTERESIS`, mirroring the percell hysteresis.
3. **A swap policy** — only re-install a baked map when the C64 color *set* actually changed, so a stable scene stops re-installing and therefore stops shimmering; or when a **shot cut** fired, detected by HSV-histogram correlation, which clears the window so the new shot's palette is fresh and hides the snap behind the cut.

**Ownership.** `WebcamScene` and `SourceScene` own the driver: `_maybe_start_rolling_palette` gates on `getattr(mode, "_force_palette", False)`, and `_apply_rolling_palette` submits the clean frame and installs any polled map before quantization. k-means costs ≈15-60 ms and stays on the worker, so the render thread never stutters.

Hardware-verified on the U64: a `generative plasma` run with `force_palette=8` rendered live in a forced 8-color set, errors 0/s.

`--suggest-palette FILE` ranks a good `force_palette_colors` set for a given source.

## Framerate pacing & frame-dropping

`Playlist.run` uses deadline-based pacing: each frame advances a `next_deadline` by `frame_time` (resolved per-scene by `_frame_time_for(scene)`). If the wall clock has fallen more than two frame_times behind the deadline, the deadline snaps forward — dropping the missed frames — instead of bursting to catch up. All built-in scenes follow the system rate except the lower-rate defaults above (bitmap frame-pushing scenes, `WaveformScene`, `MidiScene`). Animation logic that uses `current_time` keeps tracking wall-clock time correctly across dropped frames.

`_crop_to_aspect()` is the shared aspect-correction primitive. `_apply_aspect(img, aspect_mode)` dispatches over it: `"crop"` → `_crop_to_aspect` (center-crop to fill — what webcam/video always use and slideshow's default), `"fit"` → `_fit_to_aspect` (letterbox/pillarbox, black pad), `"stretch"` → identity (the mode's resize distorts to fill). Only `SlideshowScene` reads the `aspect_mode` config field today.

## `framebuffer.py` + `preview.py` — the software mirror behind preview and recording

The `[preview]` window and the `[recording]` MP4 both need host-side pixels, and the render path already sends every byte the screen is made of — so the mirror costs no bus traffic at all. `Framebuffer` reconstructs the display from that outbound stream: `cli._build_preview_and_recording` registers `on_write` as a backend write listener (synchronous and exception-isolated — [the shared write path](hardware-io.md#backendpy--the-c64backend-duck-type-hardware-profiles-and-the-shared-write-path)), a 64 KB shadow absorbs every host-DMA write, and `render()` snapshots the shadow under its lock, dispatches on the shadowed `$D011`/`$D016` mode bits, and paints one of exactly the four modes c64cast renders to — standard text, MCM, hires, mhires. It is a reconstruction, not a capture; what that costs (REU-staged scenes preview black, launcher scenes blank, no `$DD00` bank modeling) is user-facing and lives in [caveats.md → "Preview window fidelity + limits"](../caveats.md#preview-window-fidelity--limits). The shadow starts from the machine's post-reset state — VIC registers at their reset values, color RAM light blue — so the mirror agrees with the C64 even about the screen nothing has written to yet.

Text modes need the 2 KB charset, and resolution goes through [`char_rom.py`](hardware-io.md#char_rompy--reading-the-character-rom-off-the-machine) so the window shows the same glyphs the C64 does. A configured-but-unreadable `[preview].charset_path` degrades to the built-in font with a warning instead of failing the run — the window is a mirror, and killing a session over a mistyped preview path would be a spectacularly bad trade. The built-in fallback (`_builtin_charset`, a cv2-rendered ASCII font) mirrors the real ROM's reverse-video upper half — `$80-$FF` as the bitwise complement of `$00-$7F` — because the codes c64cast leans on hardest live up there: `big_text` paints its glyph pixels with `$A0`, the `blocks` PETSCII style fills every cell with it, and the shading ramp is mostly `$E0-$F2`; before #187 they all rendered as nothing.

**`PreviewWindow` is not self-driving, and must never become a thread.** cv2's HighGUI may only create and service a window on the process's main thread (a hard Cocoa requirement on macOS — an off-thread `namedWindow` raises "Unknown C++ exception from OpenCV code"), and every playlist runs on a worker thread; the main thread, otherwise parked in `join()`, is both the only legal place to pump a window and the one with nothing else to do. Hence `open()`/`pump()`/`close()`, driven by `session._pump_previews_until_done` from [the run loop's other side](config.md#playlistpy--the-run-loop-scene-walk-pacing-crash-tolerance). The predecessor proved the point: the pygame implementation ran its blit loop on a daemon thread and therefore never worked on macOS at all — #165's cv2 rewrite is when the feature started existing there, and it retired pygame (and the `preview` extra it lived in) entirely, because the window was the only thing pygame did and cv2 is already a hard dependency.

The pump mechanics carry three non-obvious rules. `pump()` re-renders no faster than `fps` but calls `cv2.waitKey(1)` on every invocation — `waitKey` is what actually services HighGUI's event loop (without it the window never paints and the OS marks it unresponsive), and its ~1 ms block is what paces the main-thread loop off a busy-spin. User-close detection polls `WND_PROP_VISIBLE`, because HighGUI has no event queue to read. And `close()` follows `destroyWindow` with one more `waitKey(1)`, because destroy only queues the teardown. Every failure is deliberately non-fatal — on the main thread an escaping exception takes the whole session with it — so a draw blowup logs and disables the window, a headless opencv build never opens one, and the user closing the window logs "session continues" (closing it is not a stop signal). `WINDOW_AUTOSIZE` plus the module's own integer `INTER_NEAREST` upscale keeps C64 pixels crisp instead of letting HighGUI interpolate them; and because HighGUI keys windows by *title*, an ensemble gets one window per system by folding the system name into it — something pygame's one-display-surface-per-process model could never do.

`StreamRecorder`, the other half, *is* self-driving — a `PollThread(manual=True)` grabbing `render()`s at `fps` into a `cv2.VideoWriter` — precisely because it has no window and therefore no main-thread constraint. That asymmetry is the point of the module docstring's warning: "simplifying" the pair to match means re-threading the window, which is the pygame mistake again. When the writer falls behind (a slow disk), the loop snaps its deadline forward rather than bursting to catch up — the same drop-don't-burst policy as [the frame pacing above](#framerate-pacing--frame-dropping). The per-system output-path derivation, and why `[recording].path` never cascades in an ensemble, is [`config.py`'s story](config.md#configpy).
