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

`av_open` / `_HTTP_RECONNECT_OPTIONS`. A yt-dlp-resolved YouTube URL is a single progressive `googlevideo` CDN link that the CDN throttles (see its `cps=`/`ratebypass` query params) and periodically drops mid-stream; that surfaces as `OSError: [Errno 5] Input/output error` out of `container.demux()`, which the demux loop's broad `except` catches, logs as "crashed", and ends playback on. The fix is to open remote inputs with FFmpeg's http-protocol reconnect options (`reconnect`, `reconnect_streamed`, `reconnect_on_network_error`, `reconnect_delay_max=5`) so FFmpeg transparently re-establishes the connection and resumes from the current byte offset instead of erroring. `av_open(path)` wraps `av.open` and injects these **only for `http(s)://` inputs** (`_is_remote_url`) — they're http-protocol-only options, so scoping them keeps FFmpeg from warning about unrecognized options on a local/file input. Every `av.open` site in the module (playback, audio-full decode, peak scan, color pre-scan) routes through it, and so does `AudioFileSource` (probe and decode). Reconnect fires on an error or EOF, never on silence, and FFmpeg's socket timeouts default to none, so a server that accepts and then stops answering used to block the opening thread forever — for an audio-file scene that is the playlist's own thread, in `setup()`. `av_open` therefore also passes PyAV's `timeout=(_REMOTE_OPEN_TIMEOUT_S, _REMOTE_READ_TIMEOUT_S)` (20 s / 30 s) for remote inputs — and for every input FFmpeg would not open with its `file` protocol (`_is_local_file`), because an audio-file entry such as `tcp://…/x.wav` or `rtsp://…` reaches `av_open` on its extension alone and blocks on a silent peer the same way: PyAV's interrupt callback bounds `avformat_open_input` and `find_stream_info` separately by the first, and each blocking `av_read_frame` in `demux()` by the second, so a slow-but-flowing stream is unaffected and the 5 s reconnect backoff cap fits under the read bound. A tripped bound surfaces as `av.error.ExitError`, which every caller's existing `except` already handles. **PyAV does not bound a seek, so `_seek` does.** PyAV arms the read bound only for the life of a `demux()` generator (and restarts it only before each `av_read_frame`), and `set_timeout`/`start_timeout` are `cfunc`s with no Python hook. A `container.seek` on a remote input outside a generator therefore waits, on a server that stops answering the HTTP range request the seek opens, for as long as FFmpeg's own per-IO timeout allows (none by default; `_protocol_options` below) (Matroska, AVI and TS seek with IO; MP4 only looks up its index), and one inside a generator is timed against that generator's last read, so a generator held open across a pause fails a healthy server at once. Every seek in the module — the `start_s` seek in `AVFileSource.__init__`, the peak scan's, the color pre-scan's keyframe seeks and the transport seek — goes through `_seek(container, path, offset)`, which seeks a local file directly and runs any other seek on a daemon `av-seek` worker joined for `_REMOTE_READ_TIMEOUT_S`. Past that it raises `RemoteSeekStalled` and leaves the container to the worker, which closes it if the seek ever returns: closing it from the caller would free the context FFmpeg is still reading through, and there is no way to interrupt that read. A stalled `start_s` seek fails the open; the peak scan falls back to unity gain; the pre-scan is skipped; a stalled transport seek ends playback with an error log. `AVFileSource.close()` cannot wait out a transport seek either — it joins the demux thread for only 1 s — so it releases the container through a `_ContainerCloser`, which closes it once the owner has released it and no `_seek` worker is still inside it, whichever comes last; the demux thread checks `_closed` before opening the next pass, since that worker may have closed the container under it. Nothing interrupts the abandoned seek, so `av_open` also passes FFmpeg's own per-IO `rw_timeout` (`_protocol_options`; an `rtsp://` input also gets it as the RTSP demuxer's own `timeout`, because that demuxer opens its control connection without `rw_timeout`), at twice the read bound so PyAV's bound still fires first inside `demux()`. That is what ends the worker, its socket and its container: without it they lasted as long as the server held the connection open, one more per stall over a looping show. With the reconnect options each timed-out IO is retried until the backoff passes its cap, so the worker can still outlive the scene by a few of those timeouts. Two choices here are deliberate: the 30 s bound covers the whole seek, not each read, so a seek still receiving data past it fails too; and the setup steps against one dead server each wait out their own bound (pre-scan, then the `start_s` seek), about a minute before the scene fails. Pinned by `tests/test_video.py::RemoteStallBoundTest` and `::RemoteSeekBoundTest` against a loopback server that goes silent.

**A remote 4xx is re-raised naming expiry.** Reconnect options handle a *drop*; they cannot help when the URL itself has stopped being valid. `scene_factory._resolve_video_source` resolves a page URL through yt-dlp at **build** time, `session.build_stack` calls `scenes_from_config` once at startup, and `Playlist.scenes` is that same list looped forever — so a playlist holds whatever signed stream URL it got at startup and replays it on every pass. Those signatures expire (YouTube's within a few hours), and a show that ran fine all afternoon starts 403ing with a bare `HTTPForbiddenError` that says nothing about why. `av_open` catches `av.error.HTTPClientError` on a remote open and raises a `RuntimeError` naming the likely cause and the remedy: reloading the playlist (SIGHUP, or `POST /reload`) rebuilds the scenes and therefore re-resolves the URL. The message deliberately does **not** quote the URL back — it can carry a signature or credential, and each caller's own log line already names the path it was opening. That intent needs `_remote_refusal_message` to be a real function rather than an f-string, because **PyAV appends the filename it was opening to an `FFmpegError`'s `str()`**: interpolating the exception is exactly how the signed URL gets quoted back. `strerror` is the same explanatory text without the filename, and it is what the message is built from. The expiry advice is also scoped to the `HTTPUnauthorizedError`/`HTTPForbiddenError` pair a stale signature actually answers with — a 404 is a pulled video and a 429 is rate limiting, and sending the operator to reload the playlist for either points them at the wrong thing.

A TTL-based automatic re-resolve at `setup()` was considered and **not** built: it would put a multi-second blocking network fetch between every scene on every loop pass and make the whole show network-dependent, to fix a failure mode that only bites multi-hour looping URL shows. If those become a real use case, stamp the resolve time on the scene and re-resolve past ~60 minutes — not unconditionally per pass.

### `probe_container_title` — a name for the interstitial card, before `setup()` opens the file for real

`MediaFileMixin.prepare_next()` picks a pool's next file (and sets `.name` from it) *before* the "UP NEXT" card is built — well before `setup()` opens `AVFileSource` for actual playback (see `docs/reference/03-vocabulary.md` → "Between One Scene and the Next"). `VideoScene` overrides the mixin's `_display_name_for` hook to call `probe_container_title(filepath)` at that same moment: a second, throwaway `av_open` that reads only the container's `metadata` dict (PyAV parses headers without decoding any frames) and closes immediately, returning the `title` tag if the file carries one. Local files only — `_is_remote_url` short-circuits a URL before either `ensure_pyav()` or the open, since a resolved stream's real title already arrives from yt-dlp at scene-build time (see `config.md`'s `_video_source` note) and probing a CDN link here would be real network I/O spent on a display name. Any open/read failure returns `None` silently; `setup()`'s own open, moments later, is what actually reports a bad file. `SlideshowScene`/`LauncherScene` get the mixin's unmodified basename-only default — this is scoped to video because a container title tag is the one piece of file-embedded naming metadata that's both common and cheap to check.

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

`current_frame` records the chosen frame's rebased PTS (`last_frame_pts`) and exposes `video_buffer_depth`; `VideoScene._record_av_lag` logs `audio_clock − displayed_frame_pts` per displayed frame. Small + lag (≤ one source-frame interval) is healthy frame selection; a lag that climbs while the buffer sits near 0 is the decoder failing real time. This is **software-side and artifact-free** — the right way to measure A/V drift on this project (Cam Link audio capture uniformly time-compresses the recording under host DMA load — a load-dependent factor, not the sampler — so it can't measure absolute drift). Live line at `-v` (every `AV_LAG_LOG_INTERVAL_S`); per-scene min/avg/max summary at teardown (INFO, so no flag needed — mirrors the sampler's write-ahead-lead line).

### Start offset (`start_s`)

`AVFileSource(..., start_s=N)` seeks the container to the keyframe at/just-before N (whole-container `seek` in AV_TIME_BASE microseconds on the stream timestamps, `backward=True`, from the stream origin `_origin_s` so a file whose streams start after 0 lands on its file position) before the demux thread starts, and the peak-scan container seeks too so normalization covers only the played portion. The playback clock starts at 0 (audio samples / wall-clock) at file position N, so the **content timeline** is the stream timestamp less `_origin_s + N` (`_pts_offset`, set in `__init__`, and by `pin_timeline_origin` for the REU preload) — not the first decoded timestamp, which is the keyframe before N. What the seek decoded before N is dropped, never shown or played: pictures by `_admit_frame` (below) and sound by `place_audio_frame` against the audio fed starting at 0, so both start on N to within a frame and `AUDIO_ALIGN_TOLERANCE_S`. A file reporting neither stream's start has no origin to fix the offset with, and falls back to the old rebase: the pass's first decoded timestamp from either stream (`_content_time`) becomes 0, so the start is keyframe-granular. Audio shares the origin; see "Audio on the picture's timeline" below. Carried by `SceneCfg.start_s` (video-only; rejected on other types, negative rejected) → `VideoScene` → here. Quick playback fills it from a URL timestamp; a `[[scenes]]` video can set it directly.


### Audio on the picture's timeline

The sink's clock counts the samples it has played, and the picture follows that clock. Audio fed back to back as it was read therefore drifted from the picture by every stretch the file leaves without sound (#606): a sound that starts late, or comes back after a gap, played as soon as the demuxer read it, up to a whole video buffer ahead of its picture. And a stretch with no audio stopped the clock while the demuxer waited on a full video buffer for the clock to drain it, so the scene stalled for good. The demuxer now feeds audio at its place on the content timeline (rebased, unscaled seconds), and `_audio_fed_s` tracks where the audio fed so far ends.

* **Each decoded audio frame is placed before it is fed** (`_align_audio_frame`). A frame that starts more than `AUDIO_ALIGN_TOLERANCE_S` (30 ms) after `_audio_fed_s` is preceded by silence up to its start (`_feed_silence`, through the atempo graph when tempo compensation is on, in pieces the sink's backpressure takes one at a time). A frame that starts that much before it loses the overlap: `_audio_trim` samples are dropped from the front of the resampled output (`_emit_resampled`). Within the tolerance a frame follows on, so the samples a resampler holds back or a muxer rounds do not split it. A frame with no timestamp follows on. The placement is `place_audio_frame`, and `_audio_fed_s` advances by what was fed rather than to the frame's own end: set to the frame's end, each gap or overlap under the tolerance was forgotten as soon as the next frame was placed, so a file that drops single audio frames (21 ms each in AAC) or whose timestamps drift against its sample count moved the sound from its picture without bound. Carried instead, they add up until one correction is due.
* **A full buffer with no audio coming is filled with silence** (`_fill_dry_stretch`, from `_enqueue_frame`'s backpressure wait). The target is the newest buffered frame less `DRY_FILL_INTERLEAVE_S` (0.5 s), or less the furthest this file has been seen to write a packet of audio behind its picture since the last seek (`_audio_lag_s`, reset by a seek: one stray packet stamped far behind widened the margin for the rest of the file) if that is more: audio for anything earlier would already have been read, so a fill there trims nothing. Audio that does come later is aligned as usual.
* **A stall takes the sink's lead.** Each sink holds audio back before its clock moves (the DAC's `PREBUFFER_CHUNKS` x `CHUNK_SIZE` = 6144 B prebuffer plus a ring lead the servo steers toward `HOST_DMA_SERVO_TARGET_GAP` = 4096 B, about 0.85 s at its 12 kHz default and 1.28 s at 8 kHz; the sampler's `DEFAULT_LEAD_SECONDS`, 1.0 s), so in a buffer spanning less than that plus the interleave allowance, a fill short of the newest frame never brings the clock to the oldest frame, or brings it there only just: the picture then crawls, a frame drained now and then, with no one frame waiting long. `_watch_dry_pace` therefore judges pace over a window spanning frames, each `DRY_FILL_STALL_S` (1 s), by the frames the consumer took off the buffer (`_frames_taken`, counted in `current_frame`) against the file's frame rate: the stamps would not do, because tempo compensation runs them at `tempo_scale` of real time in healthy playback (down to the 0.5 it accepts) and a file can step them back. Only the sink's stall is judged: the consumer must have read the clock during the window (`_clock_read`) and found it short of the frame due next (the one after the frame shown, or the oldest itself while the clock is short of it, which on a file whose stamps step back is the later of the two). A consumer that stopped asking (a stalled render, a link redial) or a clock already past the next frame is not the sink holding audio back, and a lead taken for it only trimmed the next sound, at shipped settings too. Under `DRY_FILL_STALL_PACE` (half) of the frame rate, `_dry_stall_level` goes to 1 and the fill reaches at least `DRY_FILL_MIN_LEAD_S` (1.5 s) past the oldest frame, never past the newest. A picture that then takes no frame at all, with the clock read at the same position as when the window opened, has a sink holding back more than the whole buffer spans, as the DAC's prebuffer alone does at a low rate (1.5 s at 4 kHz) behind a short buffer: each further window raises the level, and the stall reaches `DRY_FILL_PAST_NEWEST_STEP_S` (0.25 s) further past the newest frame of a full buffer per level, up to `DRY_FILL_MAX_PAST_NEWEST_S` (20 s). Silence past the newest frame read would cover sound not yet read, whether the file writes it late or on time, and a sound coming back there loses as much of its start. So the reach is frames read instead: `_dry_extra_frames` lets the buffer take that many frames past `max_video_buffer`, counted at the spacing of the stamps buffered (`_frame_spacing_s`) rather than the nominal frame rate, so a variable-frame-rate file reaches as far as the level asks; the fill stays within them, as far as they reach by their stamps (`_extra_reach_s`), though from the first level on it no longer stops the interleave allowance short of the newest frame: a file that writes its audio behind its picture still loses up to that lag of a sound coming back in a stall. The growth stops at `max_video_buffer` more frames, which bounds the memory a stall can take; only past that does the fill go past the newest frame, by what the extra frames do not cover. The steps are small and stop once the clock moves; a crawl never goes past the newest, because the lead within the frames read already moves it, and neither does a clock running between the frames of a low or variable frame rate, or one a consumer stalled mid-window last read short of where it now is. Read off the stamps, a file whose video timestamps step back looked held while its picture moved, and raised the level. The level is kept until audio comes again, a seek or a mute, rather than recomputed per wait, because each drained frame resets the wait and the picture stalled again a frame later. Nothing is judged, and nothing filled, without an audio sink to fill or while the source is muted (`_dry_fill_applies`): a pause or transport's mute path drops every push, and its clock is a frozen anchor or the wall rather than the sink holding audio back, so a pause read as a held picture raised the level and grew the buffer by up to `max_video_buffer` frames. A test checks that the first level covers both sinks at their shipped rates, so a stall there costs one window.

The buffer holds 240 frames, so the allowance and the lead bind only above about 120 fps; there a silent stretch plays with short stalls rather than in real time, and audio written more than the buffer's span behind its picture is trimmed.

**A jump in the audio timestamps is followed on, not filled** (`_audio_frame_start`). Placed at its stamp, a single packet stamped 1e6 s ahead was preceded by that much silence, fed at the sink's real-time pace (or at full CPU with the sink stopped or the source muted), so the scene never ended; one stamped far behind had every later frame trimmed whole, and the rest of the pass was mute. A frame is a discontinuity when it starts more than `AUDIO_DISCONTINUITY_S` (30 s) past the newest picture read this pass (`_video_read_s`, or the pass's anchor before one is read), or that far behind `_audio_fed_s`. The forward bound is measured from the picture rather than from the audio fed, because a file that steps each packet a little under the bound past the last never trips a bound measured from the audio fed and adds up silence without end; measured from the picture, the silence never takes the audio fed more than the bound past the newest picture read. 30 s is far past any muxer's interleave, and a sound that starts late or comes back after a gap has its picture read up to it first, so the bound trips only on a stamp the picture never reaches. On a discontinuity the frame follows on (it starts at `_audio_fed_s`), and `_audio_shift_s` takes the jump, so the pass's later frames are placed relative to the new stretch rather than each one jumping again. `_audio_lag_s` is computed from the adjusted start, so a jump does not widen the fill's allowance for the rest of the file. The first discontinuity of a pass logs one warning (`_audio_jump_warned`); later ones in the same pass are silent, so a file that jumps on every packet does not flood the log.

The REU-staged preload (`[audio].use_reu_pump`) gets no audio from the demuxer, so it places its own: `decode_audio_full` puts each frame through `place_audio_frame` too, with sample 0 at the origin `AVFileSource.pin_timeline_origin` fixes on the source before its demuxer starts — the earliest start either stream reports, since the pump plays the whole track from the picture's clock 0 and no pass's first timestamp exists yet. Under a `start_s` seek the pinned origin is `start_s` further in: content 0 is the file position the scene starts at. The preload decodes from the file's start, so the sound before that origin reaches `place_audio_frame` behind the audio placed and is trimmed (`is_audio_discontinuity`'s `floor_s` keeps more than the bound of it from reading as a jump back), and the pinned `_pts_offset` makes the picture's frames from the keyframe before `start_s` stamp before 0, where `_admit_frame` holds only the newest of them back for the frame at 0 (below). Both paths share one predicate, `is_audio_discontinuity`. The preload follows on past a backward jump the same way, so the sound after it is not trimmed whole, and past a forward one measured from the newest picture packet it has read (`decode_audio_full` demuxes the picture's packets alongside the audio and reads their stamps without decoding them): a packet stamped more than the bound past the picture, after a gap, otherwise put silence ahead of it up to the cap and the rest of the clip played silent. A soundtrack that runs on past its picture without a gap is no jump, since the bound applies only to a frame that also starts after the audio placed. The one forward jump the audio-placed bound alone does follow is a return: once a backward jump has shifted the stamps later, a frame more than the bound past the audio placed undoes that shift, never past the frame's own stamp; that check comes first, so a return beyond the picture is an undo and not a fresh jump. Without it, a single stray packet stamped far behind left the rest of the track that far behind its picture, with that much silence ahead of it. Instead `decode_audio_full(..., max_samples=)` caps its output and stops decoding once the cap is reached, and `_preencode_audio_for_reu` passes `REU_AUDIO_MAX_BYTES` (one byte per sample in the REU, so the region could hold no more). Uncapped, the silence ahead of a packet stamped 1e6 s out asked for an allocation of about 24 GB at the DAC's 12 kHz default.

A seek starts the timeline over: `_apply_pending_seek` clears `_audio_fed_s`, `_audio_trim`, `_video_read_s`, `_audio_lag_s`, the stall level and its pace window, and the discontinuity shift and its warning flag, and sets the shared origin to the file position (`_pts_offset`), with the pass starting at the seek target. Kept, the old pass's fed position put the target's first audio behind it: trimmed away on a seek back, or behind silence on a seek forward.

### Bitmap + `$D418`-DAC tempo compensation (`tempo_scale`)

**The symptom.** On the host-DMA 4-bit DAC path (`[audio].backend = "dac"`) over a **bitmap** display mode, everything plays ≈12 % slow — at correct pitch.

**The cause.** The audio worker shares the single socket-DMA link with heavy REU bank-swap bitmap writes. Under that load the host-DMA servo reads the ring pointer biased and throttles the worker by ≈12 %. Video is slaved to the audio drain clock (`position_seconds` → `_clock_s`), so both play at ≈1/`s`.

Pitch survives because the `$D418` *output* rate stays ≈ `sample_rate` — a pure tone reads ≈993 Hz for a nominal 1000. The ring under-fills and the NMI re-reads samples, which is a pitch-preserving time stretch.

**Why not fix the servo.** There is no free lunch on the host side: servo on is smooth but slow, open-loop has correct tempo but skips, and the REU pump is wobbly. No tuning gives both speed and smoothness.

**The fix — pre-compress the content.** Compress the content in the time domain by the inverse factor, so the system's own stretch nets back to real time.

`scene_factory.build_scene` resolves `tempo_scale = s`, the observed speed fraction, from `[audio].dac_bitmap_tempo_hires` / `_mhires`. Both default to unset, which asks the connected backend (`C64Backend.dac_bitmap_tempo`): `s` depends on how the link's writes halt the NMI player, so no one number fits every machine. The ABC returns the U64-II NTSC figures, 0.89 hires / 0.88 mhires, which a TeensyROM writing with WriteC64Mem also matches; a TR+ writing sliced returns 0.97 ([teensyrom_api.py](hardware-io.md#teensyrom_apipy--the-teensyrom-backend)). An explicit value always wins and stays fixed. Unset, the backend's figure is only where the scene starts: `s` moves with the content — a mostly static clip drains near 1.0 on either link, so a fixed `s` made it play fast (11% at 0.88, 2.5% at 0.97) — and with the commit rate, so the scene follows the drain it measures (below). It is gated to `backend == "dac"` **and** `isinstance(mode, BitmapDisplayMode)` **and** not `use_reu_pump`; anything else gets 1.0, since the off-bus sampler, the REU pump, char modes, and muted scenes do not stretch. It threads through `VideoScene._tempo_scale` into `AVFileSource`.

There, when `tempo_scale < 1.0`, or when the scene follows the drain (`tempo_follow`, so a run that starts from a followed 1.0 can still be retuned):

* `__init__` builds a one-stage `atempo` filter graph (`abuffer → atempo=1/s → abuffersink`), fed by the existing s16/mono/`target_sr` resampler output.
* `_demux_loop` pushes each resampled frame through it and drains the time-compressed result (`_drain_atempo`).
* At EOF, `_flush_resampler` first pushes `None` through the resampler, whose filter holds back a few milliseconds until flushed, and then `_flush_atempo` pushes `None` and drains the graph's buffered tail — without these the last fraction of a second is lost. `decode_audio_full` (the REU-staged preload) and `AudioFileSource` flush their resamplers the same way.
* Each rebased video PTS `c` is stamped `offset + c × s` (`_rebase_pts`); `offset` is 0 until a retune.

The existing drain-clock A/V sync, which reads ≈`s`, then lands both compressed streams at real time, in sync, with pitch intact.

**Following the drain** (`VideoScene._follow_drain`, `AVFileSource.request_tempo_scale` / `_apply_pending_tempo`). With the field unset (`tempo_follow`), the scene reads clock/wall over the last `TEMPO_FOLLOW_WINDOW_S` of displayed frames, from `TEMPO_FOLLOW_WARMUP_S` after it starts (the prebuffer and the start's catch-up read as a drain that is not there), and asks the source for that `s` when it is `TEMPO_FOLLOW_DEADBAND` or more off the one in force. The DAC clock also stops when nothing lands in the ring — a stalled link, or a producer that ran dry — and clock/wall over that window is not the drain. Followed, a 2 s stall asked for `s`=0.5 for about 4 s after it, and a source that supplies content no faster than real time (a live stream) ratcheted down to 0.5 for good, since each lower `s` asks it for more content and it ran dry again (simulated clock). So a window restarts, keeping the `s` in force, at an underrun (`AudioStreamer.stats()`; a sink without that telemetry is never followed), at a `delivery_epoch` move, and at two frames `TEMPO_FOLLOW_STALL_S` or more apart across which the clock ran at under half the `s` in force. What gets past those is bounded: a retune moves `s` by `TEMPO_FOLLOW_MAX_STEP` (0.05) at most, `TEMPO_FOLLOW_RETUNE_S` (1 s) after the last, and `s` stays within `TEMPO_FOLLOW_MAX_DROP` (0.15) of the starting figure, not of the followed one, so the bound does not ratchet across runs either. That costs 0.88 → 0.79 one second (two steps, at 8 s and 9 s after the start rather than one at 8 s), and puts a dry producer the counters miss at 0.73, not 0.5. The next run of the scene starts from the `s` last followed only when it picks the same file: a spec that picks at random would otherwise start a busy clip from a static one's drain. Following measures on `time.monotonic()`: an NTP step or a sleep moves `time.time()` without the drain moving. The fixed figure was measured at about ten committed frames a second; the clock-between-landings change (#600) roughly doubled the commit rate on mhires, the drain fell to ≈0.79, and the fixed 0.88 played the content about 10% slow. The demux thread applies a retune at its next packet: the atempo filter's `tempo` command changes the ratio mid-stream, and the PTS map pivots at the content time the audio fed so far reaches — `offset` moves so that point's stamp stays put, and the picture stamped from there on follows the new ratio without a jump. A picture decoded ahead of the audio by a fraction of a second keeps a stamp off by that fraction times the change, a few milliseconds for a retune of a few percent. The transport's `clock_to_content` / `content_to_clock` read the source's map rather than dividing by a ratio. Following stops once transport is touched, since the clock is a transport anchor from then on. The touch also freezes the source's map (`freeze_tempo`, and `request_seek` drops a pending retune too): a retune still pending when a seek lands was applied after it, so the seek target converted with one map was stamped with another, seconds off the sound by `target × change`. For the same reason a retune with no audio or picture read yet in the pass pivots at the pass's anchor (`_pts_anchor_target`), not at 0.

**What deliberately does not change.** `position_seconds` and `_clock_s` are untouched, and `clock/wall` telemetry still reads ≈`s` **by design** — it measures the drain rate, and the compensation makes the *content* real-time, not the drain clock. `decode_audio_full` (REU pre-encode) and `_scan_audio_peak` are off this path entirely, since the gate holds `tempo_scale` at 1.0 for them.

**Bounds.** `atempo` spans 0.5..2.0 per stage, so `validate_dac_bitmap_tempo_cfg` bounds `s` to 0.5..1.0 — keeping `1/s ≤ 2.0` in one stage.

**Where the defaults come from.** mhires 0.88 and hires 0.89 are the measured U64-II NTSC fractions. Hardware run 2026-07-02 gave `clock/wall` drain fractions of petscii ≈0.976, hires ≈0.906, mhires ≈0.894. The *mode ratio* is clean; the ≈2 % absolute offset is fixed startup latency. So the defaults are anchored on the ear-validated mhires `s=0.88`, with hires scaled by the measured 1.013× faster drain.

Other platforms — U64+PAL, U2P, TR+ PAL/NTSC — differ. Measure per platform with `scripts/diags/mhires_tempo_clock_ab.py`, which reads the `clock/wall` A/V-lag gauge, and set the field.

> This is **orthogonal** to the `pitch_mult_*` NMI-rate multipliers, which correct pitch — a tempo-blind axis.

### EOF handling

`current_frame` normally keeps the chosen frame in `_video_buf` so a clock stall doesn't black-frame the display. After demux EOFs (`self._eof = True`) that stall-protection becomes a trap — the buffer stays size-1 forever, `finished` (which checks `_eof and not _video_buf`) never flips, `VideoScene.process_frame` never returns False, and the audio worker pads NEUTRAL indefinitely (visible as a 3-min `writes=4/s bytes=4KiB/s` streak in audio logs). The fix is in `current_frame`: when `_eof` is set AND the consumed index is the last buffered frame, clear the buffer entirely so `finished` can flip on the next check.

**A pass that reaches EOF parks the demux thread instead of ending it.** The demuxer reads up to `max_video_buffer` frames (240, ≈8 s at 30 fps) ahead of playback, and a paused scene's frozen clock lets it run the whole way, so it reaches EOF while the scene still has seconds to show. A thread that returned there left any later `request_seek` — the resume splice, an A/B loop wrap landing in that window, a jog back — with nothing to apply it: resume near the end of a clip ended the scene, and a wrap held a frozen frame. `_demux_loop` therefore runs one `_demux_pass` per seek target; at EOF it flushes the resampler and atempo tails, sets `_eof`, and `_await_seek_after_eof` waits on the `_wake` condition (notified by `request_seek` and `close`); `close` returns the thread. A seek always ends a pass, and the next one opens a fresh `container.demux()`: a generator that has read EOF yields only flush packets, which would drain the decoders the seek just reset. The seek is applied between passes, with no generator live, through `_seek` (above), which bounds it on a remote input. `finished` stays False while a seek is pending, since `request_seek` empties the buffer before the thread has woken, and `_apply_pending_seek` clears `_eof` in the same critical section that retires the request, so no read sees neither. Once the thread has returned for good (closed or crashed, `_demux_exited`), a pending seek no longer holds `finished` off.

**Each EOF ends the sink's input.** `start(audio_push, audio_end)` takes the sink's `end_input`, and a pass that reaches EOF calls it after the resampler and atempo tails, just before parking. Both sinks wait for a prebuffer (about 0.5 s) before they play, so a clip whose audio is shorter than that never started the DAC's NMI, the playback clock never moved and the scene did not end with the clip; the sampler held `setup()` for its 2 s prebuffer timeout. Under an A/B loop the end is not final, which is why the sinks reopen their input on the next accepted push rather than this source holding the call back ([audio.md](audio.md#audiopy--audiostreamer)). The one exception is a pass that reaches EOF with a seek already pending: it skips the call, because the splice's cut, which reopens the input, may already have been taken, and an end marked after it would cut the post-seek input; the next pass ends the input at its own EOF. The check and the call share `_lock` with `request_seek`, which takes the cut, so a post-seek pass that reaches EOF before the splice's flush runs keeps its end. A demuxer that crashes mid-stream ends the input too, under the same check, since nothing more will be pushed.

### Transport seek/mute (MIDI live-tune Phase 2)

Three additions serving `VideoScene`'s DJ-style transport surface — see the [`scenes.py`](scenes.md#scenespy--scene-state-machine) and [`midi_control.py`/`transport.py`](control.md#midi_controlpy--process-wide-midi-control-surface-optional-live-performance) notes.

**`request_seek(target_s, *, unmute=False, on_request=None)`** sets `self._pending_seek` and clears `_video_buf` immediately, both under `self._lock`. The clear matters as much as the flag: it unblocks a demuxer currently spin-waiting on a full buffer, since the backpressure loop's capacity check passes again right away. `on_request` runs in the same critical section and its result is returned: the resync splice takes the audio sink's cut there (below). `unmute` lifts `set_muted` there too, for a resume.

**`_apply_pending_seek()`** is demux-thread-only, and runs in one place: `_demux_loop`, between passes, with no `demux()` generator live. A pending seek ends the current `_demux_pass` — checked at the top of each packet, and the backpressure wait in `_enqueue_frame` returns as soon as one lands so the pass reaches that check. The packet in flight when a seek lands was read from the pre-seek position, so its frames are dropped rather than buffered. Applying the seek outside a live `demux()` is deliberate: a generator that has read EOF yields only flush packets, which would drain the decoders the seek just reset, and a seek inside one is timed against its stale last read (see `_seek`).

When it fires it re-seeks the container (to `_origin_s + target_s`, `_seek_us`), rebuilds the resampler and atempo graph so no stale samples carry across the jump, clears `_eof`, and puts the content timeline on the file position: `_pts_offset` becomes `_origin_s`, and `_pts_anchor_target`, where the pass starts, becomes `target_s` rather than the ordinary `0.0`.

**An approximate seek (`request_seek(..., exact=False)`) skips the decode up to the target.** The exact seek decodes from the keyframe to the target on every call, which on a long GOP (measured on a 120 s H.264 clip, one keyframe per 250 frames) takes 0.1 s to 0.4 s at 720p and 0.2 s to 0.8 s at 1080p, and a held FF/RW or jog seeks every tick, each seek interrupting the pass before it gets there; the picture would then land only after the last step, that long after the release. An approximate seek leaves `_pts_offset` unset instead, so the first timestamp read, the keyframe's, becomes the target (`_content_time`) and its picture is shown at once, as before exact seeking. `TransportSession` makes the steps of a held rw/ff and a jog approximate (`transport_scrub`) and the position they end on exact, `_SCRUB_SETTLE_S` after the last step (`transport_settle`, which seeks exactly to `position()` unless an exact seek has come since). A single seek, a `seek` event, the A/B loop wrap, a loop-slot recall and `start_s` stay exact.

The container lands on the keyframe before the target, so the pass decodes up to a GOP of content before it, and that is dropped rather than rebased onto the target (rebased, the keyframe's picture was shown labeled as the target, up to a GOP early, with the sound that far off from where the transport said it was). A picture before `_pts_anchor_target` is not converted: `_admit_frame` holds the newest one (`_pre_target`) until a picture at or after the target arrives, and then queues the held one first, stamped before the target, so `current_frame` at the target shows the picture that is on screen at that position, as a still when the clock is held there (a paused seek, a splice's hold) — dropping it too would show nothing until the next picture's stamp. The held picture is also queued once a packet of any stream has been read `PRE_TARGET_RELEASE_S` (1 s) past the target (`_release_held_picture`): a stream whose pictures are seconds apart (a screen capture, a slide show) would otherwise keep the last position's picture on screen until the demuxer, throttled by the sink, read as far as the next one. A picture before the target that decodes after that (the decoder's delay) is queued only if it is newer than the released one (`_released_s`). At the end of the stream a held picture is queued, so a seek past the last picture shows it. Sound before the target is trimmed by `place_audio_frame` against the audio fed, which starts at the target (a packet ending before it whole, the straddling packet by its part), and `is_audio_discontinuity`'s `floor_s` stops more than `AUDIO_DISCONTINUITY_S` of it (a long GOP) from reading as a jump back. `_audio_lag_s` counts only sound at or after the target. The rebase in `_content_time`

```python
if self._pts_offset is None:
    self._pts_offset = pts_s - self._pts_anchor_target
```

remains for a file with no stream origin, where the first post-seek timestamp lands on the target and nothing before it is dropped. This is the mechanism behind "the clock **is** file position once touched" — design decision 2 of the transport plan, which avoids any separate `file_offset_s` bookkeeping: with the absolute mapping the position the transport reports is the position the picture and sound are at, so a loop mark or a stored loop slot (`LoopPresetStore`, which holds positions) keeps meaning the content at that position; one saved from a position read just after a seek under the old rebase labeled content up to a keyframe interval earlier.

**`set_muted(bool)`** latches a flag that `_emit_audio` checks first. Once muted, packets are dropped before gain and noise-gate, permanently for that scene. This is the `loop_audio = "mute"` escape valve; note that nothing already queued downstream in `AudioStreamer` or `UltimateAudioSampler` is retracted.

**`duration_s`** is read from `container.duration` once at construction, or `None` if PyAV reports none. It drives absolute-jog mapping and seek/loop clamping.

### Transport audio resync (MIDI live-tune Phase 4)

The default `loop_audio = "on"` keeps audio playing across every transport splice instead of muting. Two small `AVFileSource` additions serve it.

**The `_emit_audio` seek guard.** It early-returns while `self._pending_seek is not None`. Audio decoded from the stale pre-seek read position must not reach the consumer, or it would play after the splice.

**Each push carries the sink's flush epoch, read under the seek lock.** `start(..., audio_epoch=)` takes the sink's `current_flush_epoch`, and `_emit_audio` reads it under `_lock` together with the closed, muted and pending-seek checks, then pushes outside the lock with `epoch=`. The splice takes the sink's cut (`cut()`, which bumps that epoch and reads the anchor) inside `request_seek`'s critical section, so no push can straddle it. A push decided before the seek was requested carries the retired epoch and the sink drops it, however late it lands. A push decided after the demuxer applied the seek carries the new one and is kept, even when it lands before the splice's `flush()` has run. Before this, both sinks lost such audio (#620): the DAC's flush drained the queue, taking the target's first audio with it, and the sampler tagged each push with the epoch read at its own entry, which the flush then retired. A seek therefore lost the start of its target, and a post-seek pass shorter than that window lost all of it. The pending-seek read used to be unlocked, with the flush epoch left to catch what slipped through; with the epoch read under the lock there is nothing to slip.

`_emit_audio` also drops everything once `close()` has set `_closed`. `close()` joins the demux thread for at most a second, and a scene reuses its `UltimateAudioSampler` across activations: the next `setup()` re-arms it, so a demux thread that outlived the join would otherwise push the last lap's audio into the new lap's prebuffer.

**`seek_pending`** is a `_lock`-guarded property that `VideoScene`'s resync loop-wrap reads, so it does not re-fire `transport_seek(A)` every frame until the demux thread clears the pending slot. Each re-fire would flush the first fresh post-A audio.

The actual queue retraction lives in `AudioStreamer.flush()` / `UltimateAudioSampler.flush()` — see the [`audio.py` and `sampler.py`](audio.md#audiopy--audiostreamer) notes. `flush()` drops everything queued without moving `position_seconds()`, and a flush-epoch counter on both backends discards stale audio held by a pusher blocked mid-commit or by a consumer mid-write.

No other demux-side change is needed: `_apply_pending_seek` already clears `_eof`, rebuilds the resampler and atempo graph, and re-anchors PTS to the target, and a seek that arrives after EOF restarts the parked demux thread ([EOF handling](#eof-handling)).

## `modes/` — DisplayMode hierarchy

Each mode does VIC register setup + frame quantization + push to the right addresses. All uploads go through `write_region` so the delta cache applies.

**Package layout** (split from the single `modes.py`, 2026-08): one module per mode (`petscii`/`blank`/`mcm`/`hires`/`mhires`) over two mid-bases (`char.py` — `CharDisplayMode` + `clear_char_screen`; `bitmap.py` — `engage_bitmap_mode` + `BitmapDisplayMode`), with the compose-buffer TypedDicts, cell-color pickers, palette-mode shaping helpers and the `DisplayMode` base in `base.py`. `__init__.py` re-exports the whole public surface, so `from c64cast.video.modes import X` resolves exactly as before — but its submodule import order is `isort: off`-guarded because it **is** `DisplayMode.__subclasses__()` creation order, which introspect's live-target walk, the MIDI-setup wizard's pick lists, and generated reference appendix F all render in. Two things to know when editing: the live-tunable pick knobs (`PALETTE_PICK_EMA_ALPHA`, the `PERCELL_*` trio) are rebindable **on `modes.base` only** — the mode classes read them as `base.<NAME>` at call time so a runtime retune (the `mhires_ema_ghost_ab.py` diag) takes effect, while the `modes.<NAME>` re-exports are import-time value snapshots; and the helpers that went public in the split (`pick_cell_colors`, `ema_counts`, `fade_nibbles`, `clear_char_screen`, the `validate_*`/`*_palette_*` family) did so because the split made them cross-module — don't re-privatize them.

### Per-scene color override (`[scenes.color]`)

`[color]` is a show-wide default; any `[[scenes]]` block may override any subset
of its fields with its own `[scenes.color]` sub-table (`config.SceneCfg.color`).
`config.scene_color(cfg, s)` is the one resolution point: it returns `cfg.color`
unchanged when the scene has no override (same object, no copy — the common
case), otherwise a deep copy of the global section with the scene's authored
keys applied over it. `scene_factory._display_mode_for_scene` calls it and
threads the result everywhere a builder used to read `cfg.color` directly (the
seven `color=` constructor kwargs, plus `dither`/`cell_strategy`/
`flicker_tolerance` resolution) — so a scene's effective color is what its
`DisplayMode` actually gets, the same funnel the global section already went
through.

**Why the override is stored as the raw authored keys, not a merged
`ColorCfg`.** The obvious alternative — a nested `ColorCfg` field on `SceneCfg`,
resolved the way `apply_master_defaults` resolves the machine-settings cascade
("a field differs from `ColorCfg()`'s default → it was authored") — breaks the
one case per-scene overrides exist for: a scene can never override a field
*back to* its dataclass default. With `[color] force_palette = true` show-wide,
a scene wanting `force_palette = false` would test as "not authored" under that
scheme and silently inherit `true`. Storing the scene's `color` as a sparse
`dict[str, Any]` of exactly what TOML wrote makes "unset" precisely "key
absent," with no ambiguity against the default. `hue_corrections` is an
all-or-nothing replace when a scene sets it (not an extend of the global's
list) — the same shape distinction `hue_corrections_replace_defaults` already
draws between the *built-in* purple-rescue band and user bands.

`scene_factory.effective_colors(cfg)` is the loop every `validate_*_cfg` guard
(dither/color_match/cell_strategy/motion_smoothing) and doctor's per-aspect
probes now run: the global section, plus one more effective `ColorCfg` per
scene that carries an override, each labeled for its error/report messages
(`"[color]"` vs. `"[[scenes]][i].color"`). `validate_scene_cfg` rejects a
non-empty `color` on a scene type that paints no frame (waveform/midi/asid/
launcher/blank), mirroring the existing `effect`/`start_s` type-scoping.

Live-tuning follows the same scene-vs-global split: a `mode.*` target whose
config home is a `_MODE_FIELD_TO_COLOR` field (`transport.py`) saves into
`[color]` as before UNLESS the scene playing when it was tuned carries its own
override for that field, in which case the save-back writes that scene's
`[scenes.color]` block instead — `LiveTuneTracker._scene_overrides` is the
check, and `write_live_tune_row` (shared by the CLI exit flow and the web
console's `_restamp`) is where the write actually lands. The web console's
nested field-edit form addresses a `[scenes.color]` key as
`{scene: i, subsection: "color", field, value}` — deliberately not
`{section: "color", scene: i}`, which `config_store._apply_edit` already
refuses (naming both a section and a scene is nonsensical for every other
field, so the ambiguity is resolved with a distinct key rather than
overloading that pair).

### `frame_target_size`

Each mode's `(width, height)` — the only resolution it downscales a source frame to in `compose`/`render` (`(40,25)` PETSCII, `(80,50)` MCM, `(320,200)` Hires, `(160,200)` MHires; `None` for `BlankDisplayMode`, which renders no source frame). `compose` sources its `cv2.resize` target from this attribute (not a literal), and `VideoScene` reads it as `AVFileSource`'s `decode_target_size` — so it's the **single source of truth** for both the compose resize and the video decoder's downscale-during-decode plan, and the two can't drift (a stale decode plan would under/over-decode). See the `video.py` decode-time-downscale note above.

### Bitmap engage clean-field (`engage_bitmap_mode`)

The hires/mhires VIC bring-up is one shared module-level primitive, `engage_bitmap_mode(api, *, d011, d018, d016, …)`. It is called by **both** the single-buffer `HiresDisplayMode`/`MultiHiresDisplayMode` `setup()` **and** `voice_scope.VoiceScopeRenderer._apply_vic_hires_bank`, the waveform/midi oscilloscope — so the engage invariant and the VIC-register set live in exactly one place and cannot drift apart. Two copies drift in one particular direction — one of them ends up clearing *after* its `$D011` flip instead of before, which is precisely the garbage field the invariant exists to prevent.

**The invariant.** Zero both the `$2000` bitmap **and** screen RAM (`$0400`) *before* flipping `$D011` into bitmap mode, and write `$D018`/`$D016` first as well. The window between the mode flip and the first composed frame then shows solid black, rather than uninitialized-RAM garbage or a color ghost of the prior scene.

**Why `$0400` too — the non-obvious part.** A zeroed bitmap makes every pixel select its cell's *background* color. In hires, that background is the **low nibble of the `$0400` byte**, not `$D021`. So leaving stale `$0400` — say the previous interstitial's PETSCII codes — paints a 40×25 color ghost on engage. Zeroing `$0400` pins every cell's background to black.

**Why border and bg0 are pinned on every path.** `$D020`/`$D021` are set to `0x00` everywhere, including REU-staged mhires. The REU bank-swap IRQ only starts writing `$D021` from the first *real* swap, since the frame tracker's ready flag starts zeroed (see `modes_irq.install_bank_swap_irq`). Without the setup-time write, every frame until that first swap showed whatever `$D021` the previous scene left behind — observed on hardware as a black border over a stale-blue screen. The setup write covers exactly that gap; the IRQ still owns `$D021` from the first real frame onward.

**Per-caller differences are arguments, not forks:**

* `dd00` plus `bitmap_base`/`screen_base`/`d018` let the scope **relocate the VIC bank**, moving between bank 0, bank 2 and bank 1 according to the SID footprint (`waveform._DISPLAY_BANKS` carries all three, in that preference order; the bank-1 exception is written up in [hardware-io.md](hardware-io.md)).
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

Implemented in `dither.py`. Adds a spatial-dither stage to mhires/mcm/hires, ahead of nearest-palette quantization. Two families, chosen by `dither_method` (`"none"` (default resolves to a concrete value via `scene_factory.resolve_dither_method` — see below) `| "ordered" | "blue_noise" | "floyd-steinberg" | "atkinson"`), threaded into each mode's constructor alongside `channel_boost`/`hue_corrections`:

#### The ordered family — `"ordered"` / `"blue_noise"`

A fixed, position-deterministic threshold offset added to every BGR channel of `flat` — the same pixel array `channel_boost`/`hue_corrections` already produced — *before* `quantize_distances`/`quantize_flat` runs.

Nothing structural changes downstream: candidate selection, EMA/hysteresis, and per-cell picking are untouched. It only nudges which side of a quantization boundary a pixel lands on.

* `"ordered"` (`dither.bayer_offset(h, w, strength)`) tiles the classic 8×8 Bayer threshold matrix, normalized to a zero-mean ±0.5 range and scaled by `strength * 64`.
* `"blue_noise"` (`dither.blue_noise_offset`) tiles a 64×64 mask generated offline by void-and-cluster (`scripts/diags/gen_blue_noise.py`), baked into `dither._BLUE_NOISE_B64` as a base64 uint16 blob and **not** regenerated at runtime. It is normalized and scaled identically, so `dither_strength` means the same thing for both.

Both are a single vectorized array op over the whole frame, so they hold realtime frame rates, and both are constant at a given screen position — a static source dithers identically frame to frame, and motion sources gain no shimmer.

Blue noise additionally has no low-frequency structure, so it drops the regular grid/cross-hatch pattern Bayer's 8×8 tiling shows at C64 resolution — same cost, same stability.

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

`scene_factory.resolve_dither_method(dither_setting, scene_type)` resolves the default at `build_scene` time, via `_display_mode_for_scene` — the single funnel webcam, video, slideshow, and generative scenes share.

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

**`"auto"` resolution.** `scene_factory.resolve_color_match(setting, display_mode_name)`, inside the single construction funnel `_build_display_mode`, picks perceptual on every quantizing mode (`_COLOR_MATCH_AUTO_PERCEPTUAL`) and rgb on the non-color-picking ones (blank, hires_edges). `validate_color_match_cfg` and `doctor._validate_color_match` report the resolved metric per scene.

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

**`"auto"` resolution.** `scene_factory.resolve_cell_strategy(setting, scene_type)` picks:

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

### `[color].flicker_tolerance` — temporal color blending

Off by default (`flicker_tolerance = "off"`); hires `"normal"` style and mhires `percell`. Holds **two** screen pages over one shared bitmap and alternates `$D018` between them every video field, so the eye fuses each cell's pair of hardware colors into a shade the VIC cannot draw — the Dragon Breed / Mayhem in Monsterland trick. Color side in [`video/flicker.py`](../../c64cast/video/flicker.py), C64 side in `modes_irq.FLICKER_SWAP_IRQ_HANDLER` — whose bank-swap commit is held to the [raster gate](#the-raster-gate--why-a-vblank-irq-is-not-enough) while the `$D018` alternation itself is not.

**The frame rate does not come from the link.** This is the thing that makes it practical: the alternation is owned by a C64-side raster IRQ and free-runs at the VIC field rate no matter how slowly the host pushes. The host only uploads the *pair*. Both fields share one bitmap — the fg/bg mask must be identical or the flicker would be geometry rather than color — so a frame costs one extra 1000-byte page, not a second frame: **≈26.5 ms vs 21.3 ms** on the Ultimate link (`HardwareProfile.write_cost_s`), comfortably inside the 30 fps bitmap cap. Compose adds ≈1.3 ms. No REU and no sampler involved; it works on the TeensyROM too.

#### Eligibility: a safety cap, then a table of what was actually seen

`flicker.blend_pairs(max_luma_delta, tolerance=)` admits a pair when three things hold: the **absolute difference in linear luminance** between its two colors is under the luma cap, the pair carries a scored tier no worse than the tolerance allows, and the fused color lands ≥4 Lab from all 16 solids — below that it duplicates a solid and costs a page write for nothing.

**The cap is a photosensitivity control, and that is all it is.** A pair is seen at 25 Hz (PAL) / 30 Hz (NTSC), inside the ITU-R BT.1702 risk band, where the hazard scales with luminance modulation depth. Hence `flicker_max_luma_delta = 0.075` by default, a warning past `WARN_LUMA_DELTA = 0.10`, a second warning past `FLASH_CRITERION_LUMA_DELTA = 0.12` where modulation approaches the 20%-of-peak-white level the guidance is written around, and the feature opt-in.

**It advises; it does not refuse.** An earlier build clamped to 0.12, which put a computed threshold above pairs a person had looked at and accepted — the same mistake the fitted eligibility rules made, in the one place where being wrong withholds something already verified. It was not hypothetical: against the VIC-II rendering, five of the eight pairs scored as fusing cleanly sit above 0.12 (Red+Purple 0.161, Red+Orange 0.284, Cyan+Light Gray 0.366, Purple+Orange 0.123, Orange+Medium Gray 0.152), so the clamp held `"clean"` to 3 of 8 there and no setting could recover them. Both thresholds now log and proceed. Nothing is lost on safety grounds that the scored table does not already cover: admission is bounded by the tier data, so however wide the cap is set it cannot reach a pair nobody has judged.

**Two rules were fitted here and a blind run refuted both.** The first derived 0.075 from six flat bands bracketing a solid/flicker transition, leaving a 0.106-wide unsampled hole that the interesting behavior turned out to live inside. Scoring the pairs the default admits put ΔY's correlation with the verdicts at r=+0.33 with two clean refutations, so the branch then reached for color instead: every pair containing Red (2), Purple (4), Orange (8) or Light Red (10) had scored high, and `flicker_max_warmth` capped a Lab chroma projection onto a red-orange axis to exclude them.

That rule was then scored against a run it had not been fitted to — all 33 pairs the hard clamp admits, positions shuffled, pools separated, seven hidden solid negative controls, key withheld — and it did not survive:

| predictor | r vs scored rating | AUC, moderate-or-worse |
|---|---|---|
| warmth (max of pair) | +0.32 | 0.714 |
| ΔY | +0.26 | 0.680 |
| Δchroma, max chroma, mean luminance | +0.04 … +0.08 | — |

Best multi-term fit: adjusted R² **0.179** over n=33. Two things killed the warm rule specifically. All seven solid controls scored *none*, Red, Orange and Brown among them — so warm colors do not flicker on their own, and the effect is fusion failure rather than composite chroma crawl. And warm+warm pairs are among the steadiest scored: Red+Purple, Red+Orange and Purple+Orange all read *very mild* while Red+Dark Gray reads *intense*. What the earlier session had picked up was warm against **neutral**, and the cap was excluding five of the eight quietest pairs to catch it.

**So the eligible set is a recording, not a rule.** `flicker.SCORED_FLICKER` holds one tier per pair on the five-point scale the sitting used, and `[color].flicker_tolerance` is a cut across it:

| tolerance | admits | pairs (U64, cap 0.12) | effective palette |
|---|---|---|---|
| `off` (default) | nothing | 0 | 16 |
| `clean` | none + very mild | 8 | 24 |
| `subtle` | + mild | 14 | 30 |
| `visible` | + moderate | 23 | 39 |

The tolerance values are named apart from the tier names on purpose: one pair scored `none`, which a tolerance called `"none"` would have to include and exclude at once.

**No tolerance admits the `intense` tier.** Those ten pairs stay in the table because they are what was seen, and dropping them would make "scored but excluded" indistinguishable from "never scored" — which is the distinction the whole admission rule turns on. But there is no setting for them, because measured against the plain palette they buy nothing: see the reconstruction table below, where admitting them moves the error by under 0.1 % on every fixture. A setting that trades visible flicker for zero accuracy is not a choice worth offering.

**A pair with no tier is never admitted, at any tolerance.** On the Ultimate 64 table that costs nothing — the scored set is exactly what the hard clamp allows, so coverage is total at every legal setting. The VIC-II rendering shifts luminances enough to bring five unscored pairs under the clamp, and one of them is Cyan+Yellow — as violent a flicker as anything on the chart, and one ΔY refused on the U64. Excluding the unscored is what stops a palette swap admitting it. `scripts/diags/flicker_score_grid.py` is how the table grows; a test pins the recorded distribution so a tier cannot drift silently.

**The scoring path is not bounded by the table it feeds.** Filtering by tier is right for playback and exactly wrong for the tool that produces the tiers. A pair scored `intense` is in no blend table, so it cannot be put on screen — which would make a wrong tier permanent, since re-judging it requires rendering it. The same blocks scoring a palette nobody has scored: its unscored pairs are in no table either. `[color].flicker_score_pairs` takes an explicit list (`"Blue+Brown"`, or `"6+9"` — the shape `BlendTable.describe` prints, so a pair copies straight out of an arming log) and replaces the eligible set outright, ignoring both the tier data and the luma cap. `flicker_score_grid.py` writes it per page, so each page's table holds exactly that page's patches and `verify_page` becomes an exact check rather than an approximate one.

Two properties keep it from being a back door to the tier it bypasses. It cannot enable blending on its own — `flicker_tolerance` must still be set and every structural gate still applies — so a stray key cannot start the screen alternating. And arming logs a warning naming it as the scoring path, because a pair reachable only this way was excluded on evidence.

**What the table does not carry.** One observer, one sitting, one rating per pair, and that observer put the mild/moderate and moderate/intense boundaries at ±1. `"clean"` is the only cut that rests on neither. The tiers are also applied to whatever `host_palette` is active, which is an extrapolation from the Ultimate 64 they were collected on.

**Why absolute ΔY and not a contrast ratio.** Michelson contrast was the first rule and it is wrong in the one place it matters. Dividing by the pair's own mean luminance makes the metric maximally pessimistic where the eye is least sensitive: black against anything scores 1.0 by construction, so Black+Blue, Black+Brown and Black+Dark Gray — all under 0.07 ΔY, all of which fuse cleanly — could never qualify at any setting. In the other direction it admitted Cyan+Yellow, which on an Ultimate 64 is 0.26 ΔY. Against the emitted palette the two rules agree on only 9 of ~20 pairs. Weber contrast and a Ferry-Porter frequency term were tried against the same six bands and both degraded the separation; a chroma-swing term did too, which is the expected result — chroma flicker fuses at a far lower rate than luminance flicker, so it is not the binding constraint.

The 8-bit `PALETTE_LUMA` delta is also wrong here, for a different reason: it is Rec.601 on gamma-encoded values, so it overstates separation at the dark end exactly where these pairs live.

**Eligibility is per machine.** ΔY is measured against the active palette, so which pairs are even candidates follows [`host_palette`](#palettepy--which-16-colors-the-machine-emits-hardwarehost_palette) — what fuses is a statement about the light one machine emits, not about "the C64 palette". `flicker.py` registers an `on_palette_change` listener rather than computing its tables at import, because a stale table would admit pairs that flicker on the machine in front of you, which is the single failure this module exists to prevent.

**The safety cap binds before the tolerance does.** Three of `"clean"`'s eight pairs sit between 0.075 and the 0.12 clamp, so the shipping default holds it to five:

| cap | `clean` | `subtle` | `visible` |
|---|---|---|---|
| 0.05 | 5 | 7 | 8 |
| **0.075 (default)** | **5** | **9** | **13** |
| 0.10 (warns above) | 7 | 13 | 19 |
| 0.12 (warns above) | 8 | 14 | 23 |

Ultimate 64; every scored pair sits under 0.12 there, so a wider cap adds nothing. The VIC-II table is the opposite case — flat at `clean` = 3 all the way to 0.12 and only complete at ~0.37, because that rendering spreads the same pairs much further apart in luminance. Widening the cap is a photosensitivity decision, not a quality one, and should read that way in any recommendation.

Fusion is the **linear-light** average, not the sRGB one — the eye integrates emitted light over the two fields, so mixing the encoded values instead makes every blend read too dark, worst where the gamma curve is steepest.

#### What it is actually for

Gradient banding, not a general palette upgrade — spatial dither already synthesizes intermediate colors wherever there is texture to hide them in, so blending is largely redundant on photographic content and only pays where dither has little to work with. Measured against the plain path (perceptual metric):

| content | VIC-II palette | Ultimate 64 palette |
|---|---|---|
| chromatic gradient (blue→cyan) | **−33.8 %** | **−26.8 %** |
| vertical dusk gradient | −20.4 % | −14.8 % |
| luminance ramp (black→white) | −9.1 % | −15.7 % |
| warm sky gradient | −8.2 % | −1.0 % |
| soft radial glow | −1.8 % | −0.5 % |
| photograph | −1.3 % | −0.9 % |

Two columns because eligibility is per machine, and the two tables do not gain the same colors: the ramp improves twice as much on an Ultimate 64 (its dark end holds more near-equal pairs), the warm gradients less.

**Those figures admit every eligible pair**, which is wider than any `flicker_tolerance` now offers. Isolating the palette from the cell fit — per-pixel nearest-color Lab error against the widened table, so not the same quantity as the compose measurement above, but it tracks it within a point or two — shows what each cut is actually worth at the 0.12 cap:

| content | `clean` | `subtle` | `visible` | + `intense` |
|---|---|---|---|---|
| chromatic gradient (U64) | −11.5 % | −23.3 % | −29.3 % | −29.3 % |
| chromatic gradient (VIC-II) | −12.9 % | −31.8 % | −34.0 % | −34.0 % |
| vertical dusk gradient (U64) | −11.7 % | −14.5 % | −14.5 % | −14.6 % |
| luminance ramp (U64) | −2.3 % | −16.4 % | −17.2 % | −17.2 % |

The last column is why there is no setting for it: admitting the ten pairs scored *intense* moves the error by under 0.1 % anywhere. Whatever they cover, a quieter pair or a solid already covers about as well, so the tier is recorded and never offered.

**It requires the perceptual metric**, and forces it. Blending is *defined* perceptually — linear-light fusion, Lab-measured gaps — so fitting cells in weighted-BGR optimizes a different space than the one the extra entries live in. That mismatch is not academic: under the BGR metric the widened palette measures **worse** than the 16 solids on a photo (+2.5 %) and on a luminance ramp (+6.3 %), where the same frames improve under Lab. `color_match`'s own default already resolves to perceptual here, so the force only fires when a config explicitly asked for `"rgb"`, and `set_cell_pick`'s sibling `set_color_match` pins it live.

Blending also **forces the error-min cell pick** regardless of [`hires_cell_pick`](#colorhires_cell_pick--which-color-fills-a-hires-cell) or [`cell_strategy`](#colorcell_strategy--which-3-colors-fill-a-cell): a blend entry sits between its two constituent solids, so counting how many pixels landed on each entry splits a cell's population across neighbors and no slot wins on merit. In hires the widened palette then scores worse than the 16 solids outright; in mhires it costs less but still turns two of seven photographic fixtures very slightly negative, which the fit turns into an improvement or a tie on all seven. The cell fit is what makes the second page pay for itself.

This override cuts directly against `auto`'s own video-content rule ([`cell_strategy`](#colorcell_strategy--which-3-colors-fill-a-cell) above): error-min is skipped for motion content because it re-ranks on noisier raw input and churns slots frame to frame, yet blending forces it on unconditionally, motion or not. Unlike frequency (which picks off the EMA-smoothed cell_counts, so it inherits `motion_smoothing`'s stability for free), error-min's trio search scores each frame's raw, unsmoothed `d_cell` — the pool it searches is smoothed, but the winner is not. A pair's fused color sitting deliberately close to a solid or another pair (that's what makes it worth blending) means two candidate trios routinely land within noise of each other, and on video that near-tie flips the argmin every frame: cells whose slot is a demoted-to-solid pick (`_solid_last`, below) visibly flash between the two near-tied colors — reproduced offline at up to ~500 of 1000 cells/frame on a synthetic near-tied cell under realistic per-frame noise, and observed on hardware as cell *backgrounds* flipping unpredictably on video playback, distinct from the intended per-field flicker fusion.

`ERROR_MIN_HYSTERESIS_MARGIN` (`base.py`, 0.25 — the same value as `BG0_HYSTERESIS_MARGIN`) fixes this the same way bg0 is kept sticky: `pick_cell_colors`/`_pick_cell_colors_error_min` take the previous frame's trio and keep it unless a challenger's summed error is at least that fraction lower, so a genuine near-tie stops flip-flopping while a real content change (whose error improvement is overwhelming, not a near-tie) still wins on a single frame. Unscaled by `motion_smoothing` (`self._error_min_margin`, set alongside `_ema_alpha`/`_quant_hysteresis`/`_code_hysteresis` in the setter, but not derived from `s` like they are) — scaling it down left the margin at ~0.06 under the real-world default of `motion_smoothing = 0.25`, too weak to suppress the flicker it exists to fix, and unlike the other three buffers it costs nothing in motion-tracking responsiveness: a genuinely-better trio's error improvement clears the margin on a single frame regardless.

The margin itself is gated to blend-armed scenes at the `_compose_percell` call site, not applied unconditionally: the near-tie rate above was only ever measured for a blend pair's fused color sitting close to a solid or another pair. A user-selected `cell_strategy = "error-min"` with no `flicker_tolerance` armed was never profiled the same way, so it keeps the original raw per-frame argmin rather than silently inheriting stickiness through the margin.

#### mhires: two of four slots

A mode blends exactly the colors it keeps in the **screen matrix**, because that is the only memory `$D018` re-points. Hires keeps both of a cell's colors there, so the page flip reaches all of them. MCBM spreads a cell's four across three places, and only one of them alternates:

| slot | bit pair | lives in | blends? |
|---|---|---|---|
| bg0 | `%00` | `$D021`, one register | no |
| c1 | `%01` | screen byte, high nibble | **yes** |
| c2 | `%10` | screen byte, low nibble | **yes** |
| c3 | `%11` | color RAM `$D800` | no |

`$D800` is not VIC-banked and no register selects an alternate copy, so both fields read the one byte; a pair parked there would show only half of itself. `$D021` is a single register the swap handler writes once per *committed frame*, not once per field.

**bg0 was measured rather than assumed.** Alternating `$D021` too is cheap in principle — one more indexed store in the handler — but across the fixture set the frame's most-populated entry came out a solid every time, so the widened bg0 pick was bit-identical to the restricted one. A solid wins the dominant slot precisely because it owns a larger region of color space than any pair squeezed between two of them. The handler is therefore the hires one **unmodified**, which is also why `$D021` still carries bg0 at all: hires ignores that write and mhires needs it.

`_solid_last` enforces the c3 rule after the pick rather than constraining the pick. The picks arrive sorted ascending and the widened table lists all 16 solids before any pair, so a cell that picked a solid at all has it at position 0 and the fix is a rotation. Only a cell whose three picks are *all* pairs has to give one up, and it gives up the one with the smallest `BlendTable.demotion_cost` — the pair whose fused color was closest to a real color anyway, so the one buying least. That case fires on **0.14 % of cells**, measured; ranking by pixel count instead was rejected because EMA jitter would reshuffle slots on a static cell, where the cost ranking is content-independent.

**Blending pays far better here than in hires**, and on photographs rather than only on gradients — four colors across a 4-pixel-wide cell leaves spatial dither much less room to synthesize intermediates than hires' 8-pixel cells do. Reconstruction error against the source, per-cell fit isolated from the color-shaping stage, at the 0.075 default cap:

| content | VIC-II `clean` | `subtle` | U64 `clean` | `subtle` |
|---|---|---|---|---|
| chromatic gradient (blue→cyan) | −12.7 % | −31.2 % | −4.2 % | −20.8 % |
| photograph | −7.5 % | −13.1 % | −3.6 % | −31.8 % |
| vertical dusk gradient | −5.2 % | −5.2 % | −6.6 % | −10.5 % |
| luminance ramp (black→white) | −1.4 % | −8.2 % | −0.7 % | −14.8 % |

Compare the hires table above, where a photograph moves ~1 %.

**Only `percell` blends.** The global-4 palette modes pick one set for the whole frame, so no cell has a decision for a pair to win, and `grayscale`'s slots are fixed on purpose (see the class docstring). Those modes still *arm* — the handler alternates whatever the two pages hold — so `compose` hands `push` a page B that is a real copy of page A rather than whatever the last blended frame left there. The forced-palette remap is held identical for a different reason: it exists to emit exactly the colors it was given, and a blend is not one of them. Arming warns when the palette_mode cannot use it.

#### Mechanism

`FLICKER_SWAP_IRQ_HANDLER` (53 bytes at `$C500`) is the host-DMA page-flip handler plus an unconditional per-field toggle of the `$D018` screen-matrix nibble between `D018_HIRES_PAGE_A` (`$18`, matrix offset `$0400`) and `_B` (`$38`, offset `$0C00`), bitmap pinned at the `$2000` offset in both. Those values are **bank-relative**, so one pair is correct in bank 0 and bank 2 alike and the alternation survives a `$DD00` double-buffer swap untouched.

The toggle sits deliberately *ahead* of the ready-flag check — the alternation is the C64's job and must free-run whatever the host is doing, which is precisely why this needs no 50-60 fps link. Only the double-buffer commit (`$DD00` + `$D021`) waits on a staged frame, and that commit is additionally gated on landing in **phase 0**, so a swap arriving on an odd field can never transpose the A/B page roles — invisible on a still frame, a color shift on motion. `X` carries the page index and is not saved: kernal `$FF48` pushed A/X/Y before vectoring through `$0314`.

Tracker at `$C700`, 6 bytes: `[bg0, bank, ready, phase, d018_a, d018_b]`. `phase` is handler-owned, so `_arm_flicker_swap` writes only the first three — re-sending the rest would restart the alternation from page A on every staged frame and stall the blend. `install_bank_swap_irq`'s `tracker_init` seeds the page pair before the raster source is armed, since zeros there would point VIC at the `$0000` matrix offset for the field or two before the first frame stages.

`$0C00` rather than `$0800` because `$0801` is where `run_prg` drops a PRG. It is the same page [`overlays/big_text.py`](control.md) page-flips its own strip into, for the same reason — which is why the two cannot be live at once.

#### Gating

`scene_factory.resolve_flicker_tolerance` is opt-in, so there is no `"auto"`; it only decides where an explicit tolerance can be honored, returning `"off"` where it cannot. An unrecognized value raises rather than degrading to `"off"`, which would silently disable the feature on a typo. Four structural gates: the two bitmap modes only (the char modes keep all their per-cell color in `$D800`, which `$D018` does not select, so they have nothing to alternate); hires `"normal"` style only; no buffer-painting text overlay (the `$0C00` collision); and not while the REU mic pump owns `$0314`. `force_host_dma` gates it as well, for the reason it gates the others — a SID-audio scene's player owns `$0314`.

Where it engages it takes the double-buffer slot and pushes REU staging aside, extending the mutual exclusion those two already have, because the REU bank-swap handler has no `$D018` phase toggle. A `display = "random"` slideshow re-resolves it per concrete mode, alongside the other two.

The border cannot blend: `$D020` is a single register the field IRQ does not manage, so it takes the field-A component. Widening the handler to alternate it would buy a blended frame *around* the picture at the cost of bytes in the one routine that must fit inside vblank.

### `[color].motion_smoothing` — temporal smoothing / after-images

Range 0..1, default 0.25. A single dial over the mhires `percell` path's two *temporal* flicker-suppression buffers. No-op on every other mode and palette_mode — only percell carries them.

**The two buffers:**

1. The per-cell color-count EMA (`_smoothed_cell_counts`, blended each frame with `PERCELL_PICK_EMA_ALPHA = 0.15`), which stabilizes *which* colors a cell offers.
2. The per-pixel/per-cell decision hysteresis (`PERCELL_QUANT_HYSTERESIS_BONUS` / `PERCELL_CODE_HYSTERESIS_BONUS`, each 5000 in d²-space, further scaled by `PERCEPTUAL_DIST_SCALE` under Lab matching), which keeps a pixel on its previous palette index or bitmap code unless the new frame beats it by the bonus.

    The code hysteresis is what the long-capture profile pointed at: its most-flickery cells ran 80-90 % bitmap-byte transition rates with **zero** screen + color RAM changes, i.e. pure per-pixel code oscillation inside a stable cell palette.

    Both are calibrated against measured webcam sensor noise rather than picked round. 5000 in d² space (√5000 ≈ 71 in L2 BGR) suppresses up to ≈10 LSB per channel of sensor noise, which moves d² by ≈3000 for a typical near-boundary pixel, while a 25-LSB real color change (d² shift ≈22000) still releases on a single frame. `PERCELL_QUANT_HYSTERESIS_BONUS` was raised from an initial 2000 because residual rug-style flicker on textured static subjects under ≈8 LSB of noise was still crossing the threshold. It is a *decision* hysteresis rather than an input-frame EMA, so it costs no motion smear: real motion exceeds the threshold on the frame it happens.

    They also have to work together. The code hysteresis operates only in the cell's 4-entry `{bg0, c1, c2, c3}` space *after* the top-3 picks, so an unstable per-pixel argmin pushes the cell's histogram around, shifts the top-3 picks, and trips the cand-changed gate that disarms it. Stabilizing the per-pixel argmin upstream is what keeps the per-cell histograms, the top-3 picks and therefore the code hysteresis all stable.

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

**Resolution.** `scene_factory.resolve_use_reu_staged(setting, display, reu_available)` resolves per scene's display mode at build time. `"auto"` yields True only when *both* hold:

1. The mode is a bitmap mode (`_REU_BITMAP_MODES` = hires, hires_edges, mhires).
2. The startup probe confirmed the REU is on.

Char modes (petscii, blank) stay on host-DMA under auto, because their delta cache makes a full per-frame REU→main DMA a net regression.

**Text overlays take the REU path too.** A buffer-painting overlay (`overlays.paints_into_buffers`) folds fine high-contrast glyphs into the bitmap, and a bank-swap flip that lands past vblank makes the bottom rows shimmer. The dispatchers flip inside the raster window (see [the two REU pipelines](#the-two-reu-pipelines)), so the glyphs render as crisply as on the host-DMA page flip — hardware-confirmed on `hires` and `mhires` — and `"auto"` does not look at overlays.

Explicit `true`/`false` ignore the probe.

**Where `reu_available` comes from.** Computed once in `cli._resolve_reu_available` — gated on `"auto"`, `api.profile.supports_reu`, and not `--skip-probe`, via `hw_provision.reu_is_enabled` — then stashed on `SystemStack.reu_available` and threaded through `scenes_from_config`/`build_scene`, including SIGHUP/control-plane reloads and ensemble-follower rebuilds. A `display = "random"` slideshow stores the raw tri-state plus `reu_available` and re-resolves per concrete mode at each setup.

Any uncertainty — no REU, a failed query, `--skip-probe`, a non-REU backend — degrades to host-DMA, so video never silently freezes.

#### The two REU pipelines

**Char modes (PETSCII/Blank) — single-buffer.** `push()` calls `modes_irq.push_screen_via_reu(api, screen_bytes, $0400)`: REUWRITE the 1000-byte screen to `REU_VIDEO_SCREEN_BASE = $E00000` (bus-clean), configure REC `$DF02`/`$DF04`/`$DF07` for a one-shot REU→main DMA, then trigger via `$DF01 = $91`. Color RAM at `$D800` is not VIC-banked, so it stays on the delta-cached DMAWRITE path. Those are four separate DMA writes the C64 can interrupt, so this pipeline cannot share the REC with the REU audio pump: with `[audio].use_reu_pump` on, `scene_factory.resolve_use_reu_staged` keeps petscii and blank on host DMA, even for an explicit `true` (see [the pump note in audio.md](audio.md#audio_handlerspy--the-6502-machine-code-layer)).

**Bitmap modes (Hires/MultiHires) — double-buffer.** Bitmap and screen are REUWRITE-staged, then DMA'd into the *off-screen* VIC bank. A C64-side raster IRQ at `$0314` flips `$DD00` at vblank for a tear-free swap — this is what eliminates the scene-cut whole-screen flashes.

The dispatcher (`modes_irq._bank_swap_dispatcher`, one per mode with and without the audio pump) copies on one raster IRQ and flips on a later one. Four things have to hold for every shown frame to be one whole frame, and each was a visible failure on hardware before it did:

* **The flip waits for vblank.** The copy takes several fields — an mhires frame is about 40 ms of chunks at the 12 kHz default — so a flip at the end of it landed at whatever line the copy finished on, and every scene change showed one torn frame: old picture above the line, new below. The commit now goes through [the raster gate](#the-raster-gate--why-a-vblank-irq-is-not-enough), and out of the window the copied frame waits a field.
* **The C64 picks the bank.** The host alternated its target per pushed frame, but a frame is shown only at the vblank after its copy, and a host that staged the next frame first aimed it at the bank on screen. The dispatcher keeps the displayed bank's `$DD00` value in its own state at `BANK_SWAP_STATE_ADDR` (`$C718`), copies into the other bank by replacing bit 7 of each banked destination's high byte, and flips from that byte. The tracker's destinations are bank 0's. Its old bank byte carries the border on `hires` (byte 14, below) and is reserved on `mhires` (byte 22).
* **A copied frame is not discarded by the next one.** "Copied, waiting for vblank" is the dispatcher's own flag, not the host's ready flag. At 20 fps the host re-arms about every third field — roughly what a copy plus the wait takes — and a re-arm that restarted the copy left the picture updating a few times a second.
* **The copy reads one frame.** The host rotates through `REU_VIDEO_SLOTS` (16) REU staging slots, enough that at 60 fps it does not refill a slot until well after the C64 has copied and committed from it — with one slot, frames committed with one frame's top rows over the next one's bottom rows. And the dispatcher snapshots the tracker when it starts a copy, retrying if the host's tracker write lands mid-snapshot, so the commit's bg0 and color RAM come from the copied frame rather than whatever the host staged since.

Color RAM is not banked, so `mhires` copies it at the commit, right after the flip: before it, the new colors would sit under the old bitmap for a field. A 40-byte chunk costs about 100 cycles with NMIs taken, against the ~500 the beam spends on one 40-cell row, so the copy stays ahead of every row it changes, provided the commit starts by `MHIRES_COMMIT_LAST_SAFE_LINE` (see [where the window closes](#the-raster-gate--why-a-vblank-irq-is-not-enough)) so that the first chunk beats row 0.

The `hires` border goes with its frame the same way (#668). The host used to write `$D020` when it pushed a frame, which on hardware changed the border about five fields before the picture at every cut. The tracker's byte 14 now carries it, and the commit writes it right after the flip. The commit skips the write when the value matches what it last wrote, kept at `BORDER_SHOWN_ADDR` in the dispatcher's state. Otherwise every frame would erase the red border the transport pokes while a loop is armed. The host marks that memo stale (bit 7 set), under the `VIC_D020` dirty-cache region, whenever it would once have written `$D020` itself: on a change of color, after `invalidate_region`, or after a lost write. A commit on the IRQ's own line writes it in the bottom border, so on a cut the border lines below it change one field before the picture; a commit deferred past line 255 writes it in the top border, and the lines above it change one field after. The host-DMA and flicker page flips still write `$D020` from the host, now just before they arm the flip, so the border leads the picture by the wait for that flip rather than by the whole frame write: under a field on the host-DMA flip, and up to two on the flicker flip, which commits only on its phase-0 field.

Because the copy runs for fields at a time from `$C500`, `uninstall_bank_swap_irq` waits `_REU_SLOT_MAX_IN_USE_S` after masking both IRQ sources and before it restores the vector and `$DD00`, when the mode tearing down is REU-staged; the host-DMA and flicker page flips copy nothing and skip the wait. A copy already in flight finishes inside the code that started it, rather than running on into whatever the next scene's setup writes over `$C500`, or into bank 0 after teardown has put it on screen. The step's position is pinned by `test_an_in_flight_copy_drains_before_the_handler_is_released`.

**Coexistence with the REU audio pump** is fine on any scene: the bank-swap installer picks a **merged** `$0314` dispatcher (one each for `hires` and `mhires`) whose non-raster branch JMPs to the audio pump at `$C100`, servicing both IRQ sources through one hook. Every dispatcher, merged or not, splits each per-frame REU→main DMA into `BANK_SWAP_CHUNK_SIZE` (40) byte pieces so that no bus halt spans a CIA #2 underflow at the shortest NMI period the streamer can arm (75 cycles): two underflows inside one halt latch as one NMI, and the sample the reader skips plays the sound slow and flat. A one-piece DMA, which the `hires` dispatcher used to issue, and 100-byte pieces, sized for the 125-cycle period at 8 kHz, both did that at the 12 kHz default; REU-pump video audio on `mhires` played about 17 % slow against the picture (#661). The pump half of that fix, and the measurements, are in [the audio notes](audio.md#audio_handlerspy--the-6502-machine-code-layer). That merged dispatcher is why `use_reu_staged` and `use_reu_pump` need no mutual exclusion in `validate_scene_cfg`. `install_bank_swap_irq` masks CIA #1 and the raster source before it uploads anything: a teardown whose `$0314` restore never landed leaves the vector on `$C500`, and an IRQ taken mid-upload would run a half-written handler. The double-buffer setups go further and call `mask_irq_sources` with its REU drain before they clear both banks and pin bank 0: a leaked dispatcher already inside a copy keeps writing a bank after the masks land, and one still reachable would flip `$DD00` back after the pin (`tests/test_irq_install_order.py`). Before it hooks `$0314`, it puts a `JMP $EA31` stub at `$C100` and a lone `RTS` at `$C180`, because the merged dispatchers JSR `$C180` themselves. Until the audio streamer uploads its pump, a CIA #1 tick then reaches the kernal and pumps nothing, rather than running power-on RAM or a previous scene's pump body (#551). The video and mic pumps both upload a `$C180` body behind the same tracked `$C100` entry, so both work under either dispatcher.

MCM does not support staging yet.

### `[video].double_buffer`

The host-DMA page-flip sibling of `use_reu_staged` — tear-free bitmap video without needing a REU at all. Tri-state `true | false | "auto"`, default `"auto"`.

**Resolution.** `scene_factory.resolve_double_buffer(setting, display, *, use_reu_staged, backend_supports_reu, audio_reu_pump_active)` enables it only for a bitmap mode (`_REU_BITMAP_MODES`), and only when `use_reu_staged` resolved False — the two are mutually exclusive, since both flip `$DD00`.

Under `"auto"` it fires when REU staging offers no tear-free alternative for the scene: the backend has **no REU at all** (`not api.profile.supports_reu`) — TeensyROM serial and TCP, both ≈106 KiB/s, so the bus rather than the link is the wall. Bitmap video on a REU backend stays on the REU path, the better tear-free option there.

Explicit `true`/`false` pass through, still scoped to bitmap modes.

**Why it renders text crisply.** The swap IRQ does *no* in-IRQ DMA — it only writes `$D021` (bg0) and flips `$DD00` from a 3-byte tracker, held to the raster gate. So the swap lands inside vblank, hence no shimmer.

**When it is gated off.** When the REU mic pump is active (`audio_reu_pump_active`) — they share `$0314`, and unlike the REU bank-swap path there is no merged dispatcher for this pair — and by `force_host_dma`, for SID-audio scenes whose SID player owns `$0314` for PLAY.

`backend_supports_reu` and `audio.use_reu_pump` are threaded from `build_scene`; a `display = "random"` slideshow re-resolves per concrete mode at setup.

#### Mechanism

`setup()` zeroes both VIC banks' bitmap and screen, pins bank 0, and installs `HOSTDMA_SWAP_IRQ_HANDLER` — a 45-byte minimal handler at `$C500` with a 3-byte tracker `[bg0, bank, ready]` at `$C700` — via the shared `modes_irq.install_bank_swap_irq`.

`push()` writes bitmap and screen into the *off-screen* bank via `write_region`, using **per-bank** `RegionID`s: `BITMAP`/`SCREEN` for bank 0, `BITMAP_BANK2`/`SCREEN_BANK2` for bank 2. Each bank therefore diffs against its own prior content, not the other's. It then arms the tracker, and the next vblank IRQ that reaches the handler in time flips `$DD00` and `$D021` for a whole, tear-free frame — see [the raster gate](#the-raster-gate--why-a-vblank-irq-is-not-enough) for what "in time" costs and why it is not automatic.

**MHires color-RAM residual.** `$D800` is not VIC-banked, so the c3 slot still tears in a brief ≈9 ms window before each flip — color RAM is written last, just before arming. Bitmap and screen (the structure plus c1/c2) do go tear-free. Hires has no color RAM, and static-palette mhires (cheap, grayscale) does not churn it, so both are fully tear-free.

NMI audio lives on the `$FFFA` vector, independent of this `$0314` raster IRQ, so the two coexist with no REU pump on the TR. The handler chains to `$EA31`, so kernal keyboard scan (`$028D`) keeps the pollers live.

## `modes_irq.py` — C64-side IRQ handlers + REU push helpers

Everything the tear-free bitmap pipelines upload to C64 RAM, split out of `modes.py` (2026-08) so the 6502 layer lives apart from the `DisplayMode` hierarchy that drives it: the `$C500` bank-swap raster IRQ handlers (the four REU dispatchers — hires and mhires, each with and without the audio pump, 199 to 255 B, all assembled by `_bank_swap_dispatcher` through `hw/asm6502.py` — and the 45 B host-DMA page-flip sibling for no-REU backends, plus its 63 B flicker variant), the `$C700` frame-tracker layouts each handler reads, the REU dispatchers' state (displayed bank, copied flag, last border written) and tracker snapshot at `$C718`, the REU staging slots near 14 MB (`REU_VIDEO_*`), and the `install_bank_swap_irq` / `uninstall_bank_swap_irq` bring-up/teardown plus the per-frame `push_screen_via_reu` / `push_bitmap_via_reu` / `push_mhires_via_reu` helpers.

**One unhook sequence for every `$0314` raster handler.** `uninstall_bank_swap_irq`, `big_text`'s teardown and the interstitial card all take a handler off `$0314` through `hw/irq_unhook.unhook_raster_irq`, whose module docstring states the order and the retry rules. They used to carry a sequence each, and the copies drifted: one restored `$0314` only after its raster disable landed, another restored it regardless and wrote the disable again behind it. The shared steps run under `run_teardown_steps`, so a link hiccup on any one cannot starve the rest ([config.md](config.md#_teardownpy--a-teardowns-steps-are-independent-guarantees)). But they are not all independent promises. Both IRQ sources are masked first. A caller whose handler copies (a REU dispatcher) passes a drain that waits `_REU_SLOT_MAX_IN_USE_S` behind the masks, because a copy already in flight keeps running from `$C500`. The restore runs whether or not the masks confirmed: the 6510 reads `$0314` only on IRQ entry, so a restore landing mid-copy is harmless, while a handler left hooked stays reachable as the next setup writes over it. A mask that never confirmed is written again behind the restore. The kernal handler at `$EA31` never acks `$D019`, so a `$D01A` raster source left live there re-enters the IRQ on every RTI. The CIA #1 mask is retried only while `$0314` is still hooked, since the unmask re-arms Timer A once it is not. The drain is repeated in two cases: when a mask never confirmed but the restore did, since an IRQ up to the restore could start a copy the first drain never saw; and when the restore failed but a retry masked the last live source, since a copy the handler started before then may still be running. The last step, re-enabling CIA #1 Timer A, runs only once `$0314/$0315` is back at `$EA31`. With the vector still on the `$C500` handler, re-arming the jiffy IRQ hands every IRQ to it. `$D019`'s raster flag latches regardless of `$D01A`, so the handler sees the raster bit and re-flips `$DD00` to bank 2 on the next frame, and once the next scene writes over `$C500` it jumps into whatever is there. Leaving Timer A masked costs the kernal keyboard scan and the C= / CTRL / SHIFT poller, the lesser failure. The masks, the restore and the unmask go through `hw/delivery.write_confirmed`, because a write the link loses moves the loss mark without raising, and an unconfirmed restore counts as failed for the unmask unless it reads back. A redial during the restore moves the loss mark whether or not the write landed, and counted as lost it left CIA #1 masked and the keyboard dead with the vector already on the kernal. So once the link answers (`link_answers`, which also puts every earlier write ahead of the read), one REST read of `$0314/$0315` decides it: `$EA31` counts as restored, and anything else, or a read that cannot be made, leaves it failed (`tests/test_irq_unhook.py`). Each caller adds its own steps: the bank-swap teardown pins VIC bank 0 (confirmed) between the ack and the unmask, the card drains as a REU teardown does and pins the bank there too, and `big_text` resets its shadows when the handler stays hooked. `tests/test_bank_swap_teardown.py` pins the retries and drains, and `tests/test_reu_video.py`'s `BankSwapIrqTeardownGuardTest` the unmask's dependence on the restore. A bitmap mode calls the uninstall only for a handler its own `setup()` hooked: `BitmapDisplayMode._install_bank_swap_irq` records the hook before the install's first write, so an install the link cuts short is still undone, and `BitmapDisplayMode.teardown` reads that record rather than the mode's options. Gated on the options, tearing down a scene that was built but never set up unhooked the handler of the scene on screen, and its frames went on staging with nothing to copy or flip them.

The module is pure Python over `C64Backend` — no numpy, no cv2 — which is what qualifies it for `mypy --strict` (it's in the pyproject strict-files list; the `modes/` renderers stay out for those import reasons). The two `[video]` subsections above (`use_reu_staged`, `double_buffer`) describe when each pipeline engages; the byte-level rationale (the host-DMA handlers' branch-offset asserts, the NMI-collapse chunking math) lives with the bytes in the module's own comments. Coverage: `tests/test_reu_video.py`'s `BankSwapDispatcherExecutionTest` runs every REU dispatcher under py65 across a sequence of IRQs with the REU modeled — the copy into the hidden bank, the raster window, alternation, a restage while a frame waits, a tracker write mid-snapshot, the pump between families — and it and `tests/test_bitmap_compose.py` verify tracker packing and install/teardown sequences against `FakeAPI`'s write log.

### The raster gate — why a vblank IRQ is not enough

Every swap handler asks `$D012` where the raster actually is before committing — the host-DMA ones and the REU dispatchers alike — and decline to commit outside `[251, 255] ∪ [0, 43]` (REU `hires`: `[0, 42]`; mhires: `[0, 38]`). Without that check a "vblank" IRQ is only nominally in vblank.

**Why.** A host DMA write halts the 6510 at ~1.02 µs/byte, so an 8000-byte bitmap push stalls it ~8.2 ms ≈ 128 raster lines. A raster IRQ that fires during a halt does not run until the halt ends, and its `STA $DD00` then lands deep in the visible picture: the top band still shows the previous frame while the rest shows the new one. Measured over HDMI at 60 fps while sustaining ~231 KiB/s, before the gate: **5.3% of flicker frames torn (seam at a median 30% of picture height), 1.2% of plain double-buffer frames (median 36%)**. The predicted seam from ~4000 bytes left on an average mid-flight catch is ~25%, which is what identifies the halt as the cause rather than something in the host's frame pacing.

**Why the host cannot fix it.** Scheduling writes to avoid the swap window needs the host to know the raster phase. Reading `$D012` means REST polling during playback, which wedges the machine; extrapolating from a clock reference drifts past a whole field within seconds. Without phase knowledge, "chunk only the writes that would straddle the swap" degenerates into chunking *every* write — which does work (0.0% torn under a 900-byte cap) but costs ~26 → ~15 fps. The decision has to be made where the information is, which is on the C64, in the handler, at the moment it runs.

**Skip, don't commit late.** Out of window the handler acks the IRQ and returns **without clearing the ready flag**, so the staged frame commits on a later field instead. A deferred frame holds the previous one a field longer; it never shows two at once. Freezing briefly is the better artifact — a tear is a broken picture, a repeated field is a slow one.

**What it measures after the gate.** Same diag, same load, 1796 scored frames per phase: plain double-buffer **0 torn frames of 1796**, flicker `0.28%` (5 of 1796), seams scattered at 9 / 12 / 40 / 48% of picture height. Throughput held at ~235 KiB/s and 61 writes/s with `clock/wall = 1.0000`, so the gate is free — which is the half of the result that separates it from the write cap.

The residual being **flicker-only is what identifies it**: anything on the display side would hit both phases, and plain reads exactly zero. The handler reads `$D012`, checks, then writes `$DD00` a few cycles later, and a halt that begins *in that gap* passes the check and still commits late. Flicker is the more exposed of the two — its phase-0 gate lets a staged frame wait a whole extra field before it is even eligible, so it is likelier to be pending when an 8000-byte push starts, and its handler is longer. Scattered seam positions fit a race; a fixed line would not. Closing it means removing the halt rather than dodging it, which is REU staging.

**The window, and the 8-bit aliasing.** `RASTER_COMMIT_LAST_SAFE_LINE = 43` sits below the first badline (51 at the default YSCROLL), where the VIC starts fetching the frame's video matrix. The window opens at `RASTER_VBLANK_LINE = 251`, the first line below the picture: line 248 is past the last badline (243), but the bottom cell row still draws through line 250, so a commit there put the next frame's bitmap in its last two pixel lines for a field. The safe set wraps through 0, so the handler adds 5 first — rotating `[251, 255] ∪ [0, 43]` into a contiguous `0..48` — and the check costs one `CMP` and one branch. `$D012` cannot distinguish line *n* from *n*+256, but every line that aliases into the window really is below the picture on both systems (NTSC 256-262 and PAL 256-294 read back as 0-38 and pass even the mhires window, PAL up to 299 as 0-43); PAL 300-311 alias onto 44-55 and are conservatively rejected, forgoing a commit opportunity and nothing else. No line in the picture (51-250) can alias in, since none exceed 255. One formulation is correct for PAL and NTSC.

**Where the window closes, measured.** The window's end has to leave room for whatever the commit must finish before the first badline at 51: the flip on most handlers, on REU `hires` the border it writes 14 cycles after the flip (the picture's first line has a side border), and on mhires the first color-RAM chunk too, since its commit copies color RAM after the flip and the chunk carrying cell row 0's colors has to land before row 0's badline fetches them — or that frame shows the new bitmap under the previous frame's colors in the top row. The worst case stacks audio NMIs at the fastest rate the streamer arms (41 cycles each on the routine's short path, one every 75) and one host DMA halt as long as the streamer's longest ring write (147 bytes, at that rate: the NMI-period quantum raised by the link's write-rate floor; at slower rates `_halt_quantum` stops the NMI-period part at that same 147, so no piece is longer). From the raster read, the flip lands within 436 cycles, the hires border within 449 and the mhires row-0 chunk within 741 (165 of them the dispatcher's own). Read at the end of the line, that puts `RASTER_COMMIT_LAST_SAFE_LINE` at 43, `HIRES_COMMIT_LAST_SAFE_LINE` at 42 and `MHIRES_COMMIT_LAST_SAFE_LINE` at 38; the old 45, sized for a bare `STA $DD00`, let an mhires commit finish row 0's colors around line 57. The host-DMA page flips' own frame writes are outside this budget — an 8000-byte write that starts between the gate and the flip is the residual described above. `tests/test_commit_window.py` computes every handler's budget from its assembled bytes, `NMI_ROUTINE` and the streamer's own write sizing, and fails if a window outruns it.

**Flicker defers twice as far.** Its commit is additionally gated on phase 0, so a rejected commit waits for the next phase-0 field: worst case 2 fields = 33.4 ms against a ~38.5 ms host frame period at 26 fps. That is also the likely reason flicker tore ~4.4× more often than plain before the gate — half as many commit opportunities per second, so a halt is likelier to have covered all of them — and why the whole of the post-gate residual is on the flicker side. The attribution is inferred from the handler shape, not measured.

The `$D018` phase toggle is deliberately **not** gated — it sits ahead of the check and free-runs at the field rate whatever the host is doing, which is what makes flicker independent of link speed. Gating it would drop fields out of the fusion cadence, a worse artifact than a late page flip: the flip mistimes only the blended cells' colors, where a dropped field breaks the blend itself.

**Coverage.** `tests/test_raster_gate.py` runs both handlers' real bytes under py65 across both window edges, the wrap through 0, and the aliased line sets for 262- and 312-line systems. On hardware, `scripts/diags/flicker_tear_ab.py` is the acceptance test — it reports percent torn *and* seam position, and the run has to hold throughput, since a fix that buys cleanliness with frame rate is the write cap in disguise.

## `palette.py` — which 16 colors the machine emits (`[hardware].host_palette`)

Every color decision in the pipeline is a distance measured against a table of 16 BGR triples, so that table has to be the colors the display will actually show. It is not one table: a real VIC-II and an Ultimate 64's FPGA reimplementation are **~25 counts per channel apart on average, 60 at worst** (Orange), which is not a rounding difference.

**Measured, not assumed.** Captured off a U64's HDMI output, the firmware's own `default_colors` table (`software/u64/u64_config.cc`) comes back within **4 counts per channel** — and the residual is a uniform ~2-count black-level offset in the capture chain, present on Black too, so the table is exact. `U64_PALETTE_BGR` transcribes it; `PEPTO_PALETTE_BGR` is the classic VIC-II rendering that was previously the only table.

**What aiming at the wrong one costs.** Quantizing against a table the machine doesn't use is not a uniform tint that a viewer's eye discounts — the quantizer picks *indices* by distance, so a wrong table changes which color a pixel becomes. Measured over `assets/pictures/` at 320×200, against a U64 it costs **+12.9% mean Lab error** and sends **18.8% of pixels to a different index**, concentrated in the grays and warm colors (of all pixels: Dark Gray 4.2%, White 3.2%, Black 3.0%, Orange 2.4%, Light Red 2.0%). Per image it ranges from +4.4% to +30%, worst where the source is saturated.

**Resolution** is `hw_provision.resolve_palette`, a sibling of `resolve_system` and running from the same place for the same reason: what the machine reports about itself can only be read once the backend exists. Under `"auto"`, an Ultimate is **asked for its live palette** (see below) and otherwise falls back to the built-in `U64_PALETTE_BGR`; everything else is a real C64 (an Ultimate II+ and a TeensyROM+ both *drive* one, and neither has a palette of its own — the TR+ is a cartridge and emits no colors at all), so the VIC-II table is assumed.

**Reading the live palette** goes through the [Ultimate Command Interface](hardware-io.md#ucipy--the-ultimate-command-interface-at-df1c-df1f), not REST, because REST will not serve it: `CTRL_CMD_GET_PALETTE` answers with the 16 RGB triples the machine is currently driving — including a `.vpl` loaded from flash, which the config API names but has no endpoint to return. It costs ~110 single-byte reads, once, at provisioning time (a drain spends a control read per byte as well as the data read). Ultimate 64 firmware 3.15a added it; older firmware answers `21,UNKNOWN COMMAND` and falls through.

This is best-effort in the same sense as the rest of `hw_provision`: firmware without the command, registers that are not the interface, and any failed or timed-out read all answer None and drop through to the built-in table, with the custom-`.vpl` warning preserved for exactly that case. The status string is checked and not just the reply length — a bus reading back a constant `$80` otherwise passes a 48-byte length check with 48 identical bytes, and 16 identical colors installed process-wide is the silent failure this whole section exists to prevent. A live-read table is named `u64-live:<digest>` rather than `u64` so that the ensemble warning below can name the two palettes it is comparing; the comparison itself is by color, since a stock machine reaches the same 16 by both routes.

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

## `hardware_palette.py` — pushing a scene's own 16 colors (`[color].hardware_palette`)

Everything above picks *among* the machine's 16 colors. `hardware_palette = "source"` picks the 16, over the UCI `SET_PALETTE` command that Ultimate 64 firmware 3.15a added ([uci.py](hardware-io.md#ucipy--the-ultimate-command-interface-at-df1c-df1f)). Measured over `assets/pictures/` at 320×200, the per-pixel nearest-color Lab error against a fitted palette is 4.4–11.1, against 14.5–27.2 for the U64's own table: about a third, before dither and the per-cell limits.

**The derivation** is `palette.derive_hardware_palette`. Its input is the source **after the display mode's own shaping**, through `DisplayMode.quantizer_input`: the auto-fit, saturation, hue corrections and channel boost, the same `shape_for_quantize` chain mcm and mhires run in `compose`. A palette fitted to the raw source would be missed by every pixel the boost moved. The gray axis (indices 0, 1, 11, 12, 15) stays the machine's own: fades end on index 0, cards and overlays draw in 0 and 1, and the gray penalty and `grayscale` mode are written against those five. The other eleven are a Lab k-means with the five grays as fixed centers, so no cluster is spent on a color a gray already reproduces, seeded by k-means++ from those centers with a fixed RNG so the same source always gets the same palette. A min-cost bijection then puts each cluster on the free index whose own color it is nearest, which keeps the color names and multicolor char mode's 0–7 per-cell range meaningful. A source whose every pixel sits on one of the five grays gets the machine's table back unchanged. Every frame is fitted from a `FrameSampler` copy at the pre-scan width rather than at its own resolution, and `_sample_lab` strides before converting rather than after.

**Which scenes.** Only the two that can see their content before they paint it. `VideoScene` runs its blocking pre-scan (the one `force_palette` uses, deriving the auto-fit in the same pass and keeping the sampled frames in a `FrameSampler`), installs the fit, then fits and pushes. `SlideshowScene` pushes per image: at `setup()`, and at each advance, but not from `prepare_next`, which runs while the "UP NEXT" card is on screen. The image's own time starts once its push returns, so the push does not come out of `image_duration_s`. A scene's teardown only `release()`s: the quantizer goes back to the run's base palette and the machine is left showing the fitted one, because nothing renders between a teardown and the next setup. `Playlist.safe_setup` calls `settle_for` before every setup, which pushes the machine's palette back unless the incoming scene `pushes_hardware_palette`; the controller tracks what the machine shows (`_on_machine`) and skips a push of the table already there. A looping clip or a playlist lap that sets the same scene up again therefore costs no push, and `VideoScene` keeps its last pre-scan (keyed on source, decode size and `auto_fit`) so it does not decode the source again either. What it keeps is the pixel sample the fit reads (`FrameSampler.pixels`, at most 60000 pixels) rather than the frames, since a playlist holds one per video. The first setup fits that same sample, so every setup of a source fits the same pixels; the result is not always the palette the frames themselves would give, because OpenCV's HSV conversions (saturation, hue corrections, the auto-fit's saturation lift) round a pixel up to 3 levels differently in a one-pixel-wide column than in a full frame, and the k-means can settle elsewhere on that. It is re-shaped and re-fitted on each setup, so a live shaping change still moves the palette. Every other scene type shows the machine's own palette, which `wanting_scene_types` names once at startup.

**The pipeline follows the push.** A successful push is followed by `set_host_palette(pushed)`, so quantization, dither, `color_match` and fades target exactly what the machine emits. That makes `set_host_palette` a mid-run call. The module tables rebuild through it as they always did, but two per-instance caches did not: mhires's `_pal_pairwise` (and the grayscale LUT built from it) now rebuild when `palette.palette_generation()` moves, and `InversePopStyle`'s LUT clears through `on_palette_change`. Two stages are refused alongside it at config load (`scene_factory.hardware_palette_cfg_error`, run over every `effective_colors` entry, which includes a performance clip's `color`): `force_palette`, which re-chooses the same colors, and a blending `flicker_tolerance`, whose pairs are a table of which of the *machine's* colors fuse without visible flicker.

**Lifecycle**, in `hardware_palette.HardwarePalette`:

* **Provisioning** reads the machine's palette over UCI, after the stack's startup reset and clear loop, so the snapshot is the palette the machine's own configuration puts back. That read is both the capability probe and the restore snapshot, and it only happens when some scene or clip asks for a pushed palette. Firmware without the command (before 3.15a, and the C64 Ultimate as of 1.1.0) answers `21,UNKNOWN COMMAND`, and the run renders exactly as it would without the setting, after one warning. It is skipped, with a warning, on anything but an Ultimate 64, under `--skip-probe`, and in an ensemble, because the color pipeline's palette is process-wide.
* **Restores push the snapshot.** The firmware's `RESET_PALETTE` is `set_palette_rgb(default_colors)`, the built-in table, so on a machine with a `.vpl` loaded it would replace the user's palette rather than restore it. Neither `RESET_PALETTE` nor `SET_PALETTE_COLOR` is used.
* **Every C64 reset reverts the push.** The firmware's reset task re-applies `Palette Definition` (`effectuate_settings`), and measured on a U64-II on 3.15a that includes the `run_prg` resets as well as `machine:reset`. `Ultimate64API` therefore runs reset listeners after `reset()`, the clear-loop `run_prg` and `_post_prg` (the SID player, the character-ROM dump, the launcher), and the controller re-pushes whatever a scene is showing. A push issued 0 ms after the reset PUT returned stuck, so the firmware's reset task has finished by then.
* **Teardown** pushes the snapshot before the final reset, and does so even after pushing was given up on, because a push whose answer was lost may still have landed.
* **Failure.** Each push gets two attempts, because the DMA client redials a connection the firmware dropped but cannot resend the commands it may have discarded ([`socket_dma.py`](hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient)). A push that fails twice turns pushing off for the rest of the run, with a warning, and the quantizer goes back to the run's base palette. A scene's push that fails also pushes the machine's own palette back, since the previous scene's teardown may have left its table on the machine.

**Cost.** A `SET_PALETTE` is 50 single-byte DMA writes plus the status drain over REST, about 0.45–0.75 s measured; the provisioning read is about 2.5 s. That puts per-frame or per-color animation out of reach, and is why a slideshow push happens once per image.

## Framerate pacing & frame-dropping

`Playlist.run` uses deadline-based pacing: each frame advances a `next_deadline` by `frame_time` (resolved per-scene by `_frame_time_for(scene)`). If the wall clock has fallen more than two frame_times behind the deadline, the deadline snaps forward — dropping the missed frames — instead of bursting to catch up. All built-in scenes follow the system rate except the lower-rate defaults above (bitmap frame-pushing scenes, `WaveformScene`, `MidiScene`). Animation logic that uses `current_time` keeps tracking wall-clock time correctly across dropped frames.

`_crop_to_aspect()` is the shared aspect-correction primitive. `_apply_aspect(img, aspect_mode)` dispatches over it: `"crop"` → `_crop_to_aspect` (center-crop to fill — what webcam/video always use and slideshow's default), `"fit"` → `_fit_to_aspect` (letterbox/pillarbox, black pad), `"stretch"` → identity (the mode's resize distorts to fill). Only `SlideshowScene` reads the `aspect_mode` config field today.

## `framebuffer.py` + `preview.py` — the software mirror behind preview and recording

The `[preview]` window and the `[recording]` MP4 both need host-side pixels, and the render path already sends every byte the screen is made of — so the mirror costs no bus traffic at all. `Framebuffer` reconstructs the display from that outbound stream: `cli._build_preview_and_recording` registers `on_write` as a backend write listener (synchronous and exception-isolated — [the shared write path](hardware-io.md#backendpy--the-c64backend-duck-type-hardware-profiles-and-the-shared-write-path)), a 64 KB shadow absorbs every host-DMA write, and `render()` snapshots the shadow under its lock, dispatches on the shadowed `$D011`/`$D016` mode bits, and paints one of exactly the four modes c64cast renders to — standard text, MCM, hires, mhires. It is a reconstruction, not a capture; what that costs (REU-staged scenes preview black, launcher scenes blank) is user-facing and lives in [caveats.md → "Preview window fidelity + limits"](../caveats.md#preview-window-fidelity--limits). The host-DMA double-buffer paths' `$DD00` bank swap is itself a C64-side write the host never issues, so `render()` follows the frame tracker's own pending-bank byte (`Framebuffer._vic_bank_base`) rather than the shadowed (permanently stale) `$DD00` register — see the same caveats section for the one-field bound on how far that can lag the real swap. The shadow starts from the machine's post-reset state — VIC registers at their reset values, color RAM light blue — so the mirror agrees with the C64 even about the screen nothing has written to yet.

Text modes need the 2 KB charset, and resolution goes through [`char_rom.py`](hardware-io.md#char_rompy--reading-the-character-rom-off-the-machine) so the window shows the same glyphs the C64 does. A configured-but-unreadable `[preview].charset_path` degrades to the built-in font with a warning instead of failing the run — the window is a mirror, and killing a session over a mistyped preview path would be a spectacularly bad trade. The built-in fallback (`_builtin_charset`, a cv2-rendered ASCII font) mirrors the real ROM's reverse-video upper half — `$80-$FF` as the bitwise complement of `$00-$7F` — because the codes c64cast leans on hardest live up there: `big_text` paints its glyph pixels with `$A0`, the `blocks` PETSCII style fills every cell with it, and the shading ramp is mostly `$E0-$F2`; before #187 they all rendered as nothing.

**`PreviewWindow` is not self-driving, and must never become a thread.** cv2's HighGUI may only create and service a window on the process's main thread (a hard Cocoa requirement on macOS — an off-thread `namedWindow` raises "Unknown C++ exception from OpenCV code"), and every playlist runs on a worker thread; the main thread, otherwise parked in `join()`, is both the only legal place to pump a window and the one with nothing else to do. Hence `open()`/`pump()`/`close()`, driven by `session._pump_previews_until_done` from [the run loop's other side](config.md#playlistpy--the-run-loop-scene-walk-pacing-crash-tolerance). The predecessor proved the point: the pygame implementation ran its blit loop on a daemon thread and therefore never worked on macOS at all — #165's cv2 rewrite is when the feature started existing there, and it retired pygame (and the `preview` extra it lived in) entirely, because the window was the only thing pygame did and cv2 is already a hard dependency.

The pump mechanics carry three non-obvious rules. `pump()` re-renders no faster than `fps` but calls `cv2.waitKey(1)` on every invocation — `waitKey` is what actually services HighGUI's event loop (without it the window never paints and the OS marks it unresponsive), and its ~1 ms block is what paces the main-thread loop off a busy-spin. User-close detection polls `WND_PROP_VISIBLE`, because HighGUI has no event queue to read. And `close()` follows `destroyWindow` with one more `waitKey(1)`, because destroy only queues the teardown. Every failure is deliberately non-fatal — on the main thread an escaping exception takes the whole session with it — so a draw blowup logs and disables the window, a headless opencv build never opens one, and the user closing the window logs "session continues" (closing it is not a stop signal). `WINDOW_AUTOSIZE` plus the module's own integer `INTER_NEAREST` upscale keeps C64 pixels crisp instead of letting HighGUI interpolate them; and because HighGUI keys windows by *title*, an ensemble gets one window per system by folding the system name into it — something pygame's one-display-surface-per-process model could never do.

`StreamRecorder`, the other half, *is* self-driving — a `PollThread(manual=True)` grabbing `render()`s at `fps` into a `cv2.VideoWriter` — precisely because it has no window and therefore no main-thread constraint. That asymmetry is the point of the module docstring's warning: "simplifying" the pair to match means re-threading the window, which is the pygame mistake again. When the writer falls behind (a slow disk), the loop snaps its deadline forward rather than bursting to catch up — the same drop-don't-burst policy as [the frame pacing above](#framerate-pacing--frame-dropping). The per-system output-path derivation, and why `[recording].path` never cascades in an ensemble, is [`config.py`'s story](config.md#configpy).
