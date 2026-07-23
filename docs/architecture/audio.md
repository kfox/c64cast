# Audio output

Getting sound out of a C64: the NMI-driven 4-bit `$D418` DAC, the U64's off-bus FPGA PCM sampler, and the host-side DSP that makes 4 bits listenable.

Part of the [architecture reference](../architecture.md). For end-user configuration see [usage.md](../usage.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`audio.py` — AudioStreamer](#audiopy--audiostreamer)
* [`sampler.py` — UltimateAudioSampler (U64 "Ultimate Audio" FPGA PCM)](#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm)
* [`dsp.py` — host-side audio DSP for the 4-bit DAC path](#dsppy--host-side-audio-dsp-for-the-4-bit-dac-path)
* [`audio_features.py` — audio-input music features (reactive visuals from live input)](#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input)

---

## `audio.py` — AudioStreamer

An NMI-driven 4-bit SID DAC, writing the `$D418` volume nibble. This is the only approach that works on a real C64 with active video output.

**Why not PWM.** `$D402` PWM was tested and rejected on two counts:

* At an 8 kHz NMI rate the PWM carrier sits 9 dB *above* the audio signal (confirmed by spectral capture).
* At 16 kHz, VIC-II badlines — 40 stolen cycles in a 63-cycle period — make the NMI handler overrun and queue back-to-back, stretching samples. A 440 Hz test tone came out at 421 Hz.

### Sample rate and the overrun ceiling

The default is **12 kHz**, up from 8 → 10.5 → 11.6 kHz over time and bumped after the 2026-07-02 hardware sweeps below. That lifts Nyquist to ≈6.0 kHz, so fricatives and sibilants survive and speech is clearer.

`c64.nmi_rate_safety` is the single source of truth for the safe ceiling. `config.validate_nmi_sample_rate` rejects overrunning rates at load, and `--doctor` reports them.

There are **two different ceilings**, and the shipped default respects the lower one.

*Isolated-handler ceiling — ≈13.6 kHz NTSC / ≈13.1 kHz PAL.* The handler-cycle budget in `c64.py` is directly hardware-measured, not estimated. A ring-prefill tone sweep on a real NTSC C64 (`scripts/diags/tr_nmi_rate_ceiling.py`) found the effective consumer rate tracks the configured rate cleanly through 14 kHz (73-cycle period), slips ≈1% at 15 kHz (68-cycle overrun onset), and plateaus ≈15.3 kHz. Worst-case handler completion is therefore ≈68 cycles, not the 88 previously estimated. `NMI_SAFE_MIN_PERIOD_CYCLES = 75` keeps margin above that for PAL and unit variation. Because the sweep prefills the ring and runs with **no host feed**, this figure is independent of the backend and of TeensyROM firmware.

*Live-pipeline ceiling — ≈12.5 kHz.* The full streaming pipeline overruns lower. A pitch A/B sweep on a real NTSC U64-II (`scripts/diags/nmi_rate_sweep_ab.py`) plays one clip at a range of rates and recovers the played pitch by log-spectrum cross-correlation against the source; a rate whose pitch drops below the low-rate floor is the handler queuing. Results:

| Rate | Pitch vs source |
| --- | --- |
| 11600, 12000 | ≈+0.15 % (clean floor) |
| 13000, 13500 | ≈−0.4 % |
| 15000 | −1.1 % (positive control) |

The onset was **identical in char (petscii) and bitmap (mhires) modes**, which identifies the cause: the host-DMA audio-ring writes themselves halt the 6510 and steal handler cycles. It is not video bus load. So the default stays below ≈12.5 kHz even though the isolated handler could go higher.

Char and light scenes hold cleanly near the ceiling — petscii video plus host-DMA DAC verified clean with no underruns at both 11.6 and 13.5 kHz on a TeensyROM+. Bitmap+digi historically carried heavier bus-halt loss (≈10 KB/frame re-upload), but the bitmap+digi fps cap and the REU-staged double-buffer have since driven that near zero (see host-DMA pitch compensation below).

> **Not the same problem:** forcing the DAC path on **bitmap** video also shows a rate-independent tempo *stretch* — correct pitch, ≈12 % slow. That is the servo under-draining the ring under bitmap DMA load, and it is fixed by bitmap+DAC tempo compensation (see the [`video.py`](video-color.md#videopy--webcamsource-shared-broker--avfilesource-pyav) notes), which pre-compresses the content. It affects neither char modes nor the default U64 sampler path.

### Input modes

* `start_mic(device, sens, gate)` — sounddevice capture; `mic_callback` pushes into the queue.
* `start_for_external_source()` — no input thread; the caller (the PyAV demuxer) pushes via `push_samples(int16)`.
* `start_listen(device, sens, *, sample_rate=None)` — **analysis-only capture**: opens the input, feeds `analysis_sink` from `_listen_callback`, and stops there. No NMI, no worker thread, no DAC/SID writes, so nothing reaches the C64 — the input drives reactive visuals only (the `audio_source = "listen"` VJ case). Because nothing downstream is bound to the DAC rate, it opens at `sample_rate` when given — the listen path passes a higher rate (44.1 kHz) for full-bandwidth analysis (real hi-hat energy above the DAC's 6 kHz Nyquist, cleaner onsets). A `_listen_mode` flag makes `stop()` short-circuit its DAC teardown to a bare stream-close; the other `start_*` methods clear it (the streamer is reused across scenes). See [`audio_features.py`](#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input).

### The worker thread and its pacing

The worker drains the queue at `chunk_size / sample_rate` — the NMI consumption rate — so it can never lap the NMI read pointer and overwrite real audio with neutral padding. Each iteration:

1. Collects up to `chunk_size` bytes by the pace deadline. There is no grace period: the pace deadline *is* the collect deadline.
2. Pads with `NEUTRAL_SAMPLE=7` only on a real underrun, meaning the deadline expired with nothing queued.
3. Uploads to the ring buffer at `$4000-$5FFF`.

After `PREBUFFER_CHUNKS * chunk_size` bytes of prebuffer it starts the CIA #2 timer (`$DD04/05`). The BASIC clear-loop is kicked once at session startup, not per scene.

Pacing is **strict absolute** — `next_write_time + chunk_period` — and never snaps forward to wall-clock on overrun. That matters: an earlier snap-forward variant let DMA round-trip and Python wakeup overhead shrink the effective sample rate below NMI consumption. Every chunk then got NEUTRAL padding, producing audible chunk-rate AM sidebands (≈−5 dB at the carrier) and ≈16 dB of overall level loss on video audio. The 8 KB ring (≈1 s at 8 kHz) absorbs occasional pace overshoots.

### Why the ring lives at `$4000`

`$4000-$5FFF` is VIC bank 1, chosen over `$8000-$9FFF` so the ring stays out of VIC banks 0 and 2 — the two banks with kernal char-ROM mapped (at `$1000` and `$9000`), which the REU-staged char display modes use as their off-screen swap target. The 6510 NMI handler sees `$4000` as ordinary main RAM regardless of VIC bank.

Three patch offsets in the NMI routine bytes — read address HI, end-compare HI, wrap-reset HI — come from `RING_BUFFER_HI` / `RING_BUFFER_END_HI`, so relocating is a one-line change. Bitmap modes that want VIC bank 1 for themselves would need that relocation; PETSCII never selects bank 1.

### `[audio].use_reu_pump` — REU-staged mic streaming

Setting this on a webcam scene, or any scene that calls `start_mic`, opts the mic path into REU-staged streaming. The mic callback REUWRITEs encoded samples into a 64 KB REU ring at offset `$100000` — bus-clean, with no SID perturbation — and a C64-side IRQ handler at `$C100` drains that ring into the audio ring at the matched CIA #1 rate.

The handler reloads the REU source registers (`$DF04`/`$DF05`/`$DF06`) from a 3-byte tracker in main RAM at `$C200` on every IRQ, rather than trusting `$DF06` read-back. This is not defensive coding — the U64's REU returns garbage in the upper bits of `src_hi`, which made the handler's wrap check (`CMP #reu_end_hi`) always succeed. The source reset to the start of the prefilled NEUTRAL block every time, and the result was pure silence.

Two pinned BCC displacements (+15 src wrap, +10 dst wrap) must land on instruction boundaries; wrong values stomp either the tracker or the REU registers.

Bootstrap latency is `REU_MIC_BOOTSTRAP_BYTES / sample_rate`, ≈200 ms at 8 kHz. The one `use_reu_pump` flag covers both the video (`start_for_reu_staged`) and mic (`start_mic`) paths — `AudioStreamer` picks the matching bring-up from whichever start method was called.

### `[ultimate64].auto_reu` — automatic REU provisioning

Default `true`, so the REU paths that hard-require it work without the manual F2 enable step.

*When it fires.* Only when the config **hard**-requires the REU: `[audio].use_reu_pump`, or an explicit `[video].use_reu_staged = true`. This is the same `_wants_reu` condition the doctor checks. The `"auto"` default is deliberately excluded, because it self-heals to the host-DMA double-buffer path, which is also tear-free.

*What it does.* `cli.build_stack` calls `doctor.provision_reu(api, cfg)` after the probe and **before** `_resolve_reu_available`, so that probe sees the now-enabled REU. It enables `"RAM Expansion Unit"` and grows `"REU Size"` to `16 MB` via `api.put_config_item` (`PUT /v1/configs/<cat>/<item>?value=…`, verified live and no-reboot in the firmware's `effectuate_settings`). 16 MB covers every c64cast REU offset — the audio ring near 1 MB, the video staging region near 14 MB — and is both the maximum and FPGA-backed, so it costs nothing.

*Restoring.* The change is live and **volatile**, never saved to flash, so it reverts on the next power-cycle even if teardown's restore is missed. `teardown_stack` calls `doctor.restore_reu` while the REST session is still open to put the originals back; those originals ride on `SystemStack.reu_restore`, which survives SIGHUP and control-plane reloads since they reuse the same `api`.

*When it is skipped.* No-REU backends (`profile.supports_reu`, i.e. TeensyROM); under `--skip-probe`, since we never write config we could not first read back; and when `auto_reu = false`, meaning you manage the REU yourself. It is best-effort throughout — any REST failure logs a warning and leaves the existing doctor/probe degradation in place.

### `--doctor`: REU enable check

When the config opts into a REU-staged path as a **hard** requirement (`[audio].use_reu_pump`, or `[video].use_reu_staged = true`; the `"auto"` default is excluded, as above), the connectivity probe also GETs `/v1/configs/C64 and Cartridge Settings` to confirm `"RAM Expansion Unit": "Enabled"`.

If it is disabled the severity depends on `auto_reu`:

* `auto_reu` on → **ok**, because the run will provision it live.
* `auto_reu = false` → **error**. Without it the staged paths silently produce silent audio or unchanged video, with no host-side error at all: REUWRITE succeeds and the REU→main DMA simply reads zeroes.

The hint points at both `auto_reu` and the F2 menu path. `doctor.reu_is_enabled(api)` and `read_reu_config(api)` are the shared REST queries, also feeding cli.py's `"auto"` resolution and the provisioner.

### `--doctor`: REST-probe severity

`Ultimate64API.probe()` is a pure liveness check — `GET base_url + "/"` returns `HTTP <status>` for any response, and `None` only when `requests` raises. So `None` means the REST/web server itself is unreachable (port 80 refused or timed out), which is distinct from the DMA socket on port 64. They are separate firmware services; on the retail C64 Ultimate, REST is the **Web Remote Control Service** toggle, its own switch beside the DMA Service.

Because the DMA socket can be up while REST is down, `_probe_connectivity` grades that case by what the config actually needs from REST:

* **error** — `_wants_rest_runner(cfg)` is true: a `waveform`, `launcher`, or `generative` + `audio_source = "sid"` scene. These *start* via the REST `run_prg`/`run_crt` endpoint (`run_sid_player` / `launch_program`) and cannot run at all without it.
* **warn** — everything else (video, slideshow, webcam, blank, midi, generative without SID). These paint entirely over DMA and merely degrade: no physical-keyboard reads, no machine reset.

`reset()` is REST-only on the Ultimate but is caught and non-fatal — the picture still paints — so it never escalates on its own. The TR backend has no REST surface and is handled on its own probe branch; its SID player and launcher use pure-DMA vector-swap and LaunchFile.

### Optional TPDF dither

`[audio].dither` applies ±1 LSB triangular dither during sample encoding. It is **default false** after real-6581 A/B testing: at 4 bits the noise floor is already high enough that the added hiss outweighs the buzz reduction, and the user consistently preferred it off.

Turn it on if your hardware or source material disagrees. It converts signal-correlated rounding distortion into smooth white-noise hiss, which can sound better on already-noisy sources.

### `[audio].digi_boost` (experimental, default off)

Initializes all three SID voices with a locked pulse waveform (control `$49` = gate+pulse+test, sustain `$F0`) so the ADSR envelope D/As feed a steady DC offset into the master mixer.

The C=Hacking #20 digi article documents this as mandatory on 8580s and emulated SIDs, where `$D418`-only playback is near-silent because the volume DAC has nothing to scale. On a real 6581 the residual ADSR offset suffices on its own, but digi-boost still raises output level meaningfully — roughly 3× with all three voices stacked.

It stays marked experimental until tested across more hardware variants; enable it per-system in TOML to A/B.

### `[audio].dac_curve` — Mahoney 8-bit `$D418` companding

Default `"auto"`. `"linear"` keeps the classic 4-bit path (float → volume nibble 0..15), bit-identical to before this existed.

`"mahoney_ultisid"` switches the encoder to Pex 'Mahoney' Tufvesson's 8-bit technique. `_enable_mahoney_env` — branched in `_upload_nmi_and_buffers`, and mutually exclusive with `digi_boost` — parks all three voices as steady DC sources (control `$49` = pulse+TEST+GATE, AD `$0F`, SR `$FF`) with voices 1 and 2 routed through the analog filter (`$D415/$D416 = $FF`, `$D417 = $03`). That is the white-paper §XIV environment.

In that environment the **full `$D418` byte** written per NMI sample — volume nibble, filter HP/BP/LP mode bits, and the voice-3-OFF bit — selects one of ≈256 distinct, strongly non-linear output levels. That is ≈6-7 *effective* bits (Wothke), not 16.

Cost is unchanged: still one `STA $D418` per sample. Only the ring byte values differ, spanning 0..255 rather than 0..15, since the NMI routine applies no nibble mask.

The mapping is a 256-entry amplitude→`$D418` table. `encode_floats_to_dac(..., curve)` maps `float[-1,1]` to an 8-bit amplitude index centered on 128, then through `sidtable[idx]`. Dither folds in at the index domain, and exact zero maps to index 128 (silence, dither skipped). The ring rest byte `_neutral_byte`, used for prefill and underrun/EOF padding, becomes `sidtable[128]` when a curve is active.

`AudioStreamer` receives the resolved table via its `dac_table` parameter — the CLI resolves the system-aware name first — so its `dac_curve` string is only a label for logs.

### Table selection, `"auto"`, and per-system calibration

Implemented in [c64cast/dac_calibration.py](../../c64cast/dac_calibration.py).

Only the **emulated-UltiSID** table ships baked into [c64cast/dac_curves.py](../../c64cast/dac_curves.py). Hardware measurement (2026-07-02, Cam Link) showed the U64 FPGA UltiSID curve is deterministic across units, and that the 6581/8580 model knob is irrelevant — byte-identical output — so one table generalises.

Physical chips do not generalise. 6581/8580 variation is enormous chip-to-chip, dominated by the analog filter: two 6581s correlated only 0.74, and swapping their tables cost ≈29% RMS level error. SID replacements (ARM2SID, SwinSID, FPGASID) differ again. No baked table can serve them, hence calibration:

`c64cast -u <target> --calibrate-dac` (`cli` → `dac_calibration.run_calibration`) measures the connected SID's signed transfer curve. It toggles a 500 Hz ref↔code square wave through the NMI ring, captures it off the Cam Link, and takes the FFT amplitude at 500 Hz as the output step. Measuring each code against both `$00` and `$0F` resolves the bipolar sign.

#### Identity keys

The file is keyed by a **stable device identity** (`resolve_calibration_key`), not the connection target, so a DHCP re-lease or USB replug doesn't orphan it: a U64/U2+'s REST `unique_id` (`Ultimate64API.get_device_info` → `GET /v1/info`, e.g. `"5D327C"`) → `ultimate-5D327C`; a TeensyROM serial device's USB serial number (`teensyrom_dma.usb_serial_number`, re-scans `list_ports.comports()`) → `tr-<serial>`; falling back to the pre-existing host/device-path key when there's no live backend to query (offline `--doctor --skip-probe`) or the live lookup fails. `[audio].dac_calibration_profile` overrides all of that with a user-chosen name (`profile-<name>`) — the escape hatch for a roaming TeensyROM+, which has no config API and can be moved between physical C64s: its own USB serial identifies the *cartridge*, not whichever machine's SID it's plugged into right now, so a calibration keyed off it would silently apply the wrong table after a move. A user who moves a TR+ around names each host's calibration once (`--calibrate-dac --dac-calibration-profile my-breadbin`) and passes the same name on every playback run against that host.

#### Multi-socket U64/U2+

A real U64 can carry two physical SID sockets, each potentially a different chip. `run_calibration` queries the live config (`sid_hw_config.detect_sockets` — `"SID Detected Socket N"`) and, for every socket reporting a real chip, isolates it to `$D400` (the fixed address the NMI DAC handler's `STA $D418` reaches) via `_isolate_socket` — reusing the "chip 0 must land at `$D400`" trick from [c64cast/asid_sidmap.py](../../c64cast/asid_sidmap.py)'s multi-SID address planner: that socket's address → `$D400` + enabled, the other socket → disabled, both UltiSID cores → unmapped, auto-mirroring off — measures it independently, then restores the original `SID Addressing`/`SID Sockets Configuration` (`sid_hw_config.snapshot_sid_config`/`restore_sid_config`) once every socket is done. This is purely config-driven, no U64-vs-U2+ model check: a U2+ with one socket + one UltiSID core measures just that socket; a bare-UltiSID board or a backend with no config API (TeensyROM) falls back to one unlabeled measurement of whatever SID currently answers `$D400`, as before.

#### The calibration file

It lives under `paths.calibration_dir()` — the canonical `<data root>/calibration/dac/`, `$C64CAST_DATA_DIR`-overridable and resolved at use time (see [`paths.py`](config.md#pathspy)); gitignored at the legacy repo location. Writes go through `transport.atomic_write_text`.

Schema 2 holds one 256-entry sidtable per measured SID, keyed `"1"`/`"2"` by socket number, or `"default"` for the single-measurement fallback, plus a `"device"` provenance block.

At playback, `load_calibrated_table` picks the entry matching whichever socket is *currently* live-mapped to `$D400` — `_active_socket_at_d400` does a live `SID Addressing` / `SID Sockets Configuration` read. That is what stops a calibrated physical-chip table from being misapplied when an UltiSID core actually owns `$D400`. With no live backend, or when the file has no socket-keyed entries, it falls back to the `"default"` entry, or to the lone entry if there is exactly one.

Resolution: `resolve_dac_curve_for_backend(cfg, be=...)` maps `"auto"` to the applicable calibrated table if present, else `mahoney_ultisid` on the Ultimate, else `linear`. It yields to an explicit `digi_boost` by staying linear. `"calibrated"` forces the table and raises if it is absent.

#### How `--doctor` reports calibration

Three code paths, deliberately non-overlapping:

* `cli.build_stack` threads the already-probed `api` through, so **playback** resolution is precise.
* `doctor._probe_dac_calibration_status` — wired into `_probe_connectivity`, category `connectivity`, subject `"{name} (DAC calibration)"` — is equally precise for a live run.
* `doctor._validate_dac_curve_cfg` (category `audio`, always runs) only flags an unknown name or a `digi_boost` conflict. These are genuinely offline, hardware-identity-independent checks.

The "resolves to X" reporting lives in `doctor._validate_dac_curve_resolution`. `validate_load_result` calls it **after** `_probe_connectivity` (when `probe_u64`), and only for systems not already covered by a `"(DAC calibration)"` diagnostic from that live probe. So a live `--doctor` run reports calibration resolution exactly once, precisely, under `CONNECTIVITY`; the `audio` section's `dac_curve` line appears only for systems that got no live answer — `--skip-probe`, or a system whose connectivity probe failed.

This ordering fixes a real, user-visible bug. An earlier revision ran the offline resolution unconditionally alongside the live one, so even a successfully-probed run got a redundant and sometimes *contradictory* `audio`-section line. Since `--doctor`'s AUDIO section is the first thing people read, it disagreed with CONNECTIVITY in exactly the place that mattered.

#### Why the offline check hedges

`_validate_dac_curve_resolution` cannot read a live device identity. On the Ultimate and on a serial TeensyROM — where `dac_calibration.offline_key_is_authoritative` is False, meaning no `dac_calibration_profile` override and no TCP TR — a miss against its host/path fallback key does **not** prove no calibration applies, because the live `unique_id` or USB-serial key may resolve to a different file.

Rather than assert a false "resolves to `mahoney_ultisid`", or a hard `calibrated`-missing error, it consults `dac_calibration.list_calibration_files(backend)` for any file on disk recorded for this backend and downgrades accordingly:

* `"auto"` stays **ok** — it degrades safely either way.
* `"calibrated"` drops from **error** to **warn**, since a live run might yet find a match.

A profile override or a TCP TeensyROM key needs no hedge: those keys are identical with or without a live connection, so a miss there is a real miss.

#### Scope and migration

This shapes the `$D418` DAC only — TeensyROM+ audio, and mic/webcam audio everywhere. It does **not** touch the U64's default video path, which uses the off-bus Ultimate Audio sampler and never writes `$D418`.

`config.validate_dac_curve_cfg` rejects an unknown name, or an explicit non-linear `dac_curve` combined with `digi_boost`, at load time.

Old pre-multi-socket calibration files used both a different schema and a different host-based key, so there is no migration path — they are simply orphaned. Re-run `--calibrate-dac` once after upgrading.

> **Follow-up:** the per-code capture is ≈256×2 measurements per SID. A time-multiplexed fast pass could cut it to ≈1–2 min.

### Host-DMA pitch compensation — now default OFF

Three knobs, and understanding why two of them are off matters more than the knobs themselves.

**The original problem.** The host-DMA worker paces ring writes to wall-clock, so the write head W advances at `sample_rate`. The NMI reader R historically lost NMI ticks to video bus-halts. W therefore out-produced R, lapped the ring after ≈26 s (an audible echo), and playback ran slow.

**`host_dma_servo` — default on, kept.** A pure host-side PI controller (`_servo_period`) reads R once per chunk and stretches or shrinks the worker's sleep so the ring gap parks near half a ring. This is orthogonal to pitch and stays on.

**`nmi_rate_adaptive` and `pitch_mult_*` — now off / 1.0.** Playback pitch is `R / sample_rate`, and both of these tried to force R back up to `sample_rate`: the adaptive loop (`_nmi_rate_step`) shrinks the CIA #2 latch from a measured-R estimate, and the static per-mode `pitch_mult_*` multipliers do the same open-loop.

They are off because the loss they corrected for is gone. Hardware measurement (2026-07-02, `scripts/diags/nmi_pitch_ab.py` — full-pipeline capture, pitch via log-spectrum cross-correlation against the source, robust to avfoundation's chunk-drops) showed the bitmap+digi fps cap, `VideoScene` frame dedup, and the REU-staged double-buffer have driven bus-halt loss to **≈0**. DAC-path mhires video, with no compensation at all, plays at +0.07 % on a near-static clip and −0.01 % on a high-motion one.

With the loss gone, compensation only injects error:

* Static `pitch_mult_mhires = 1.015` overcorrects to **+1.36 % high**.
* The adaptive loop is worse. Its dR/dt estimator reads ≈12 % high — a torn DMA read-back of the `$C025/$C026` read pointer over REST — so it drives the latch the wrong way. One clip measured **−8.5 % slow**, and the error is content-dependent and non-deterministic.

So the DAC path now runs at the nominal latch, dead-on, with the servo still centering the ring. Both knobs are kept for platforms that may still lose ticks (PAL at 50 fps, the lower-latency TeensyROM+ backend) — but the adaptive estimator's bias would need fixing first.

Unaffected: the U64's default video path uses the off-bus Ultimate Audio sampler, which never writes `$D418` and takes its pitch from `sampler_clock_hz`.

### `position_seconds()`

The audio-master clock: `(pushed - queued) / sample_rate`. The C64-side ring buffer adds ≈1 s of constant latency beyond this, which is harmless for relative sync.

### `flush(*, silence_output=False)` — transport resync

Added for MIDI live-tune Phase 4. Drops everything queued but not yet ring-written, **without moving `position_seconds()`**.

The sequence: bump `_flush_epoch`; `get_nowait`-drain the queue via `_drain_queue_samples`; then, under `_count_lock`, subtract the drained sample count from *both* `_pushed_count` and `_queued_samples`. That paired subtract is what keeps `position = pushed − queued` exactly invariant. `VideoScene`'s resync splice calls this after `request_seek`, so pre-splice audio never plays past the cut-over.

**Why the epoch counter exists.** A bare queue drain cannot cover the steady-state races. In practice `_encode_and_enqueue` is usually blocked in its backpressure spin, and `_worker` usually holds an in-hand chunk (`leftover` + `from_queue`). Both capture the epoch, and when it has changed they discard their bytes — counted as never-pushed via the same paired subtract — instead of landing them right behind the drain.

**No DAC ring stomp at seeks or loop wraps.** The servo-held ≈4096-byte ring gap (≈0.5 s) is accepted constant output latency, so flush-only makes each splice a constant-latency crosscut: the not-yet-heard approach to the splice point finishes while fresh audio lands behind it. No silence hole, no mid-phrase chop.

**`silence_output=True` (pause only)** sets `_stomp_requested`. The *worker thread* — which owns `write_addr`, so no ring DMA races the servo — then NEUTRAL-fills the unplayed region `_stomp_spans(R+STOMP_GUARD_BYTES, W)` on its next iteration. The guard deliberately leaves ≈16 ms of stale tail un-stomped so the fill can never race the read head.

`flush()` is a no-op in REU-pump mode: that path owns its own C64-side timeline, and it is force-disabled under transport anyway.

## `sampler.py` — UltimateAudioSampler (U64 "Ultimate Audio" FPGA PCM)

The U64 firmware exposes a 7-channel **FPGA PCM sampler** at `$DF20-$DFFF` ("Ultimate Audio", Gideon's register API v0.2). It plays 8/16-bit PCM up to 48 kHz **straight out of REU SDRAM with zero SID / `$D418` / NMI / CPU / turbo involvement** — so it's immune to the bus-halt / badline problems the 4-bit DAC fights, and is **vastly higher fidelity**. It's the **default video-audio backend on the U64** ([audio].backend = "auto"); the 4-bit `$D418` DAC stays for TeensyROM (no sampler) and as an opt-in lo-fi path. Mic/webcam audio always uses the DAC.

### Module shape

Two halves.

**Pure register helpers**, all unit-testable:

* `divider_for_rate(rate)` = `round(6_250_000 / rate)`
* `control_byte(...)` — gate b0, repeat b1, irq b2, mode b4-5 (`00` = 8-bit, `01` = 16-bit LE)
* `pack_pcm(int16, bits)` — signed 8-bit, or int16-LE
* `channel_register_writes(...)` — the big-endian register byte layout: start `$01000000`+REU offset, length, rate divider, and repeat A/B as **byte positions in the sample**
* `program_channel` / `gate_off`

**`UltimateAudioSampler`** is the scene-facing object, mirroring the slice of `AudioStreamer` that scenes actually call: `sample_rate`, `position_seconds`, `push_samples`, `get_recent_samples`, `stop`, plus no-op `set_pre_emphasis` / `mark_eof`, and `is_sampler=True`.

### The streaming REU ring

Channel 0 is programmed as an A↔B loop over `[ring_base, ring_base+ring_size)`. The base is `$200000` — above the mic ring at `$110000`, below video staging at `$E00000` — so it coexists with REU-staged bitmap video. Default size is 1 MiB.

`start()` prefills the ring with NEUTRAL silence plus a prebuffer of real PCM, gates the loop on, and records `gate_time`. A writer thread then REUWRITEs decoded PCM **ahead of a wall-clock-computed read head**:

```
read = (monotonic - gate_time) * actual_rate * bps   (mod ring_size)
```

It wraps at the boundary and NEUTRAL-pads only past a low watermark (`_lead_panic`), which signals a genuine producer underrun — not merely a briefly-empty queue.

**Prebuffer and lead target are separate knobs.** `DEFAULT_PREBUFFER_SECONDS` (0.5 s) is seeded before gating so playback starts promptly; `DEFAULT_LEAD_SECONDS` (1.0 s) is then ramped up to at runtime. The lead is *buffer depth, not A/V latency* — video tracks the read head — so a deeper target only buys resilience against heavier PyAV decode stalls. Measured on hardware: a 4K h264 clip's lead floor doubled from ≈9 KB to ≈21 KB going 0.5 s → 1.0 s.

**No servo, no governor, no NMI.** The read head is computed and never read back; the loop is fully open-loop.

`sample_rate` is set to the FPGA's `REF/divider`, and `AVFileSource` resamples to it. `position_seconds()` is `clamp(monotonic - gate_time, 0, total)` — the same contract as the REU-pump branch, so `VideoScene._clock_s` works unchanged.

### Reference-clock calibration

Config: `ref_clock_hz` / `[audio].sampler_clock_hz`.

**The requirement.** The open-loop design is only drift-free if the FPGA's *real* sample rate equals our computed `REF/divider`.

**The design value.** The firmware (`sampler2.vhd`) uses a fractional prescaler to normalize every platform clock to an effective 50 MHz, giving a 6.25 MHz rate base (50 MHz / 8). So `SAMPLER_REF_CLOCK = 6_250_000` is the design value, kept as the divider-table base and pinned by tests.

**The reality.** The U64 FPGA actually clocks the sampler ≈1.44 % slow — real effective REF ≈ 6.16 MHz. Since video is paced off the host monotonic clock (`position_seconds`) while audio clocks out of the FPGA, that gap makes audio drift *behind* video by seconds over a few minutes. The symptom is the beep sliding off the flash in an A/V-sync test, worsening toward the end.

**Why it went unnoticed.** The earlier de-risk concluded "FPGA rate agrees < 0.3 %" — but computed against the *same* assumed clock. The lead telemetry cannot see the true rate either, since there is no read-back, and the sampler's end-of-sample/wrap IRQ is not DMA-readable. There is no host-only runtime signal to self-calibrate against; this is hardware-proven, see `scripts/diags/sampler_irq_clock_probe.py`.

**Why it ships as a constant, not a per-unit calibration.** The offset is a firmware/FPGA-derivation property — identical across U64 units on the same firmware, not chip-to-chip variation. So it is measured once and shipped as `SAMPLER_REF_CLOCK_DEFAULT = 6_160_000`, threaded into `[audio].sampler_clock_hz`, rather than stored per-unit the way `$D418` DAC calibration is.

`divider_for_rate`, `actual_rate_for_divider`, `program_channel`, and `UltimateAudioSampler` all take a `ref_clock`, so the programmed divider and the resample target shift together. Heard speed is `real_ref / assumed_ref` — the divider cancels, making the setting monotonic.

#### The measurement

`scripts/diags/sampler_av_align_calib.py` is definitive. At each interval it emits two tones into one captured stream, plus a border flash for visual A/V confirmation:

* a **SID tone**, clocked by the accurate C64 system crystal — a true wall-clock marker;
* a **sampler tone**, riding the FPGA clock.

Fitting each band's onset-time-vs-index slope and taking their **ratio** cancels the capture-side time compression. That compression is real and large: the avfoundation/Cam Link path drops samples under heavy host DMA load (here the sampler's REU-streaming writes; elsewhere bitmap re-uploads), uniformly compressing the recorded timeline.

Critically, its magnitude is **DMA-load-dependent**, not a fixed capture-clock property. SID reference markers fired at exact 5.000 s wall-clock landed at a captured factor of ≈0.90 under a light click-train load, but ≈0.77–0.87 under the sampler's streaming DMA. Any absolute-timing method is therefore unusable — which is why the older `sampler_clock_calib.py` fell back to pitch plus ear-tuning — while the per-run differential measures the factor and cancels it whatever its value.

Results on a U64-II:

* Nominal-driven run: ratio 0.9852 → 1.48 % slow → 6.157 MHz, r²≈0.9999 over 36 markers.
* Confirmation runs driven at the candidate converged to ≈6.16 MHz.
* A run at 6,160,000 showed residual drift of only **−1.3 ms per 5 s** — 17× better than nominal. Verdict `ALIGNED`.

Re-measure and bump `SAMPLER_REF_CLOCK_DEFAULT` after any firmware release that changes sampler timing; the diag prints the new value. Hardware or firmware that clocks the sampler correctly can set `[audio].sampler_clock_hz` back to 6.25 MHz.

The sampler was hardware-de-risked before integration (gapless, no dropouts over 3 min); the rate offset only surfaced later, on the A/V-sync test clip.

### `flush(*, silence_output=False)` — transport resync

Added for MIDI live-tune Phase 4. Cuts the ring over to post-splice audio:

1. Bump `_flush_epoch`.
2. Drain the queue.
3. Under `_io_lock`, NEUTRAL-rewrite the unconsumed lead from `consumed + FLUSH_GUARD_S·rate` up to the old `_written`, and pull `_written` back to that point. One formula covers both the normal rewrite-the-lead case and the rare lead < margin case, which blanks the lap-stale skip region.
4. Clear the `_eof` latch.

`position_seconds()` is wall-based and therefore unaffected — the computed read head keeps advancing, and we only change what it reads.

**`FLUSH_GUARD_S` (0.15 s)** is the margin between the computed read head and the first rewritten byte. It has to cover open-loop consumed-estimate jitter, REUWRITE latency (so the FPGA never fetches a byte mid-write), and the calibrated-ref residual drift. It is also the audible splice latency: old content plays at most this long past the splice point.

**Epoch checks.** `_writer_loop` discards a chunk dequeued just before the splice rather than writing it past the cut-over. `push_samples` drops the chunk of a producer parked in the Full-retry loop, and increments `_pushed_samples` only after a successful put, so a dropped chunk cannot inflate the EOF clamp. The writer's write+advance and `flush`'s read-modify-rewrite are both serialized under `_io_lock`.

**`silence_output=True` (pause)** additionally writes channel volume 0 to `$DF21` via `_write_volume` — one live DMA write, giving instant silence independent of ring content and REUWRITE latency — and sets `_output_silenced`. The next plain `flush()`, from resume's splice, restores the channel volume.

The DAC has no volume-0 equivalent: its NMI re-writes `$D418` from the ring at 8-12 kHz, so a one-shot zero is overwritten within ≈100 µs. The worker ring stomp remains the DAC's pause silencer.

The gate is never touched. Gate 0→1 restarts playback from the sample start, and NEUTRAL PCM already *is* silence, so there is no reason to.

### Backend resolution and frame rate

`[audio].backend` (`"auto"` | `"dac"` | `"sampler"`) resolves per video scene in `config.build_scene` via `resolve_audio_backend(setting, *, supports_sampler, sampler_available)`, mirroring `resolve_use_reu_staged`:

* `"auto"` → sampler if both flags are true, else dac.
* explicit `"sampler"` → warns and falls back to dac when unavailable.

The sampler is constructed as the scene's audio object; `VideoScene` drives it polymorphically, with `setup()` branching on `isinstance(audio, UltimateAudioSampler)`. `sampler_sample_rate` (default 44100) and `sampler_bits` (default 16) are validated by `config.validate_sampler_cfg`.

**Frame rate.** Because the sampler is off the C64 bus — and its presence forces the bus-clean REU-staged video path — sampler-audio bitmap video gets neither the 4-bit DAC's 20 fps cap nor the muted half-rate cap. `_frame_push_default_fps(..., off_bus_audio=True)` returns the full system rate (60 NTSC / 50 PAL) as the poll *ceiling*.

Since `VideoScene` dedups, re-pushing only on a genuinely new source frame, the effective push rate equals the source video's own fps: a 24 fps clip pushes 24/s, a 60 fps clip 60/s. That is source-rate playback capped at the VIC refresh, with no artificial cap. Hardware-verified: real ≤30 fps content pushes at source rate with no added shimmer, and audio stayed clean at a genuine 60/s push.

> Continuous-motion shimmer scales with push rate and appears only on true >30 fps sources. That is the separate unsynced-bank-swap-timing issue, not a consequence of this fps default.

### Provisioning

`doctor.provision_sampler` / `restore_sampler`, gated on `profile.supports_sampler`, not `--skip-probe`, and `_wants_sampler`. It enables `Map Ultimate Audio $DF20-DFFF` if disabled, and unmutes `Vol Sampler L`/`R` to `" 0 dB"` if OFF. Both changes are live and volatile, restored at teardown via the composite-keyed `SystemStack.sampler_restore`.

Because the ring lives in REU SDRAM, `_wants_sampler` also pulls the REU into `_wants_reu`, so `provision_reu` enables the REU at 16 MB for a sampler run. A useful side effect: that makes `"auto"` video resolve to the tear-free REU bank-swap path. The sampler installs no `$0314` IRQ, so REU-staged video and the sampler coexist with no IRQ contention.

`doctor.sampler_is_available(api)` — map enabled and a channel audible — feeds `cli._resolve_sampler_available`, and `_probe_sampler_status` reports the state in `--doctor`.

## `dsp.py` — host-side audio DSP for the 4-bit DAC path

Pure-numpy DSP that runs on float samples in `[-1, 1]` **before** `audio.encode_floats_to_dac` quantizes them. The premise: the SID volume DAC is 4 bits — 16 levels, ≈24 dB of usable range — so a raw line/mic signal wastes most of it (quiet passages collapse into a handful of codes, audible as buzz/chop). The same reasoning that makes AM radio and telephony lean on heavy compression applies here, only harder. The job of this module is to hand the encoder a signal that already lives in the loud, narrow band 4 bits can represent. Config surface is `[dsp]` (`config.DSPCfg`, which builds the pure `dsp.DSPParams` this module consumes); **scope is the `$D418` DAC path only** — the U64's default video audio goes through the off-bus Ultimate Audio sampler at 16 bits and never touches this.

Five stateful processors, wired by `AudioDSP` in a source-appropriate order: **pre-emphasis → (AGC, mic only) → expander → compressor → limiter**. The order is load-bearing — pre-emphasis shapes first; AGC normalizes gross mic level; the expander cleans the noise floor *before* the compressor's makeup gain would raise it; the compressor evens dynamics; the limiter is the final ceiling. A disabled chain (`enabled=False`) is an exact identity, and `AudioDSP.active` reports whether any processor will actually run.

* **`PreEmphasis`** — first-order HF boost (`y[n] = x[n] + amount*(x[n]-x[n-1])`), so a DC signal is unchanged and only high frequencies lift. `pre_emphasis = None` means **source-aware auto**, resolved in `AudioDSP.__init__`: `PRE_EMPHASIS_MIC_DEFAULT` (0.7) vs `PRE_EMPHASIS_LINE_DEFAULT` (0.6). Pure voice benefits most from the consonant/upper-formant boost, while line content (videos = speech + music) wants a gentler lift so music doesn't get over-bright; both HW-A/B-tuned on a real 6581 (2026-06-12).
* **`Expander`** — downward expander with **hysteresis**, and it exists specifically because it *replaced a hard noise gate that chattered*: a signal hovering at the threshold toggled the gate rapidly. The gate opens at `threshold_db` but only closes once the level falls `hysteresis_db` below it, and gain changes are attack/release-smoothed (fast open, slow close).
* **`Compressor`** — soft-knee feed-forward, attack/release-smoothed peak detector, static dB curve. The headline win: it's what lets quiet detail survive quantization. `makeup_db=None` (the default) auto-computes makeup as `-threshold_db * (1 - 1/ratio)` so a signal *at* the threshold exits near unity.
* **`Limiter`** — instant-attack peak detector + release-smoothed recovery + a final hard clip against intra-sample overshoot. Transparent below the ceiling.
* **`AGC`** — slow broadband gain for the mic path only (line/video audio is already peak-normalized upstream). **Known limitation, measured** (2026-06-12, Kaggle speech-noise set, `scripts/diags/dsp_noise.py`): being level-based, AGC cannot distinguish a −30 dB noise floor from −30 dB quiet speech. `noise_floor_db` is the only "this is just noise" signal and it is *absolute*, so setting it below the real floor means sustained noise gets boosted toward target during long pauses. A VAD (or a tuned expander ahead of it) is the real fix; for noisy mics prefer the chatter-free expander, or raise `noise_floor_db` and accept that genuinely quiet speech won't be lifted.

**Streaming contract (the invariant to preserve when editing).** Every processor is stateful and fed arbitrary-sized blocks from realtime callbacks, so processing a signal split across blocks **must** match processing it in one shot — the recursive smoothers carry envelope/gain state across `process()` calls. `tests/test_dsp.py` asserts this continuity per processor. Note `AGC` deliberately smooths per-*sample* rather than per-block for exactly this reason: a per-block gain trajectory would depend on the callback block size and break the property.

**Performance.** `_ar_envelope` (the attack/release follower) and the expander/AGC loops are genuinely recursive — per-sample state with an attack≠release branch — so they use Python loops rather than a vectorized form (no scipy in the dep set). At DAC sample rates with realtime mic blocks (hundreds of samples) this is negligible; the offline video pre-encode runs it once over the whole track (≈1 s for a 2.5-min clip), acceptable for one-time scene setup.

## `audio_features.py` — audio-input music features (reactive visuals from live input)

The **second producer** of `modulation.MusicModulation`, alongside [`music_features.SidFeatureStream`](sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene). The SID stream reads envelope/gate/frequency out of a host-side 6502 running the same tune the chip plays; this one analyzes **actual audio samples**, so a generative scene reacts to music c64cast has no symbolic knowledge of — an instrument or mixer feed through an audio interface, a phone into an iRig, a mic in the room.

Everything downstream of `MusicModulation` was already source-agnostic (`generators.py`, the effect chain, `wled_sync.py`), so this module *is* the whole feature: an analyzer, a ring the audio path pushes into, and a poll thread between them. `MicAudioSource.features()` — which returned `None` for years with a "a future audio-tap feature source could light this up" comment — now returns its snapshot.

### Why a separate pre-DSP tap (the non-obvious constraint)

`AudioStreamer.get_recent_samples()` already exposes a 2048-sample mono float ring — the one `overlays/spectrum_petscii.py` FFTs. Reusing it would have been free, and it is the wrong tap: it is filled inside `_encode_and_enqueue` **after** `_apply_dsp`, and `[dsp].enabled` defaults **True**. That puts AGC + compressor + limiter ahead of it on the mic path — stages that exist precisely to flatten dynamics into the 4-bit DAC's ~24 dB, which is exactly the information an onset detector reads. A compressed kick barely moves the spectral flux.

So `AudioStreamer.analysis_sink` is a separate hook, invoked from `_mic_callback`, `_mic_callback_reu`, `push_samples`, and the listen-only `_listen_callback` right after the mono downmix × `sensitivity` and **before** the noise gate and the DSP chain. It is `None` unless a reactive source installs one, so a non-reactive run pays a single attribute load per callback. `_push_to_analysis` wraps the call in `try/except`: the first failure logs once and clears the sink — a failing analyzer must never take down a realtime sounddevice callback, and losing the visuals' reactivity is a far better outcome than losing the audio.

### `mic` vs `listen` — the DAC copy, and the sample rate

`audio_source = "mic"` streams the input to the 4-bit DAC **and** analyzes it; `audio_source = "listen"` analyzes it and plays **nothing** on the C64. Listen is the VJ case: the real music is on a PA, and only the visuals track it. `MicAudioSource` covers both — a `listen_only` flag routes `setup()` to `start_listen` instead of `start_mic` (see [`audio.py` input modes](#input-modes)). `build_scene` builds the listen source from the shared streamer directly, so it is **never ensemble-suppressed** (it holds no audio spotlight) and ignores the per-scene `audio` DAC toggle; the `SourceScene` carries no DAC audio (`audio=None`).

The two paths analyze at **different sample rates on purpose**. The mic path opens at the streamer's DAC rate (~12 kHz, 6 kHz Nyquist), because the analyzer should see what the DAC actually plays. The listen path is freed from that — it opens (and builds its `AudioFeatureStream`) at `[audio_features].listen_sample_rate` (44.1 kHz by default), handing the analyzer full-bandwidth audio: real hi-hat/cymbal energy above 6 kHz and cleaner transient timing. The analyzer's feature math is sample-rate-agnostic (band edges are bin-index based, every decay rate is derived from wall-clock `dt`), so the only wiring needed is to build the stream with the matching rate — `MicAudioSource.setup` passes the same rate to both `start_listen` and the `AudioFeatureStream`. The one visible shift is per-bin frequency content: at 44.1 kHz a 1024-sample window spans 0–22 kHz (bin 1 ≈ 43 Hz) versus 0–6 kHz at 12 kHz (bin 1 ≈ 12 Hz) — a net win for treble/onset detection.

The spectrum overlay's tap is deliberately left alone. The two want different signals for good reasons: the overlay visualizes *what the C64 is actually playing* (post-DSP is correct), the analyzer needs the dynamics of *what came in*.

### The analyzer

`AudioFeatureAnalyzer.update(window, now)` → `snapshot() -> MusicModulation`. Pure numpy — no threads, no I/O — so the entire feature math is testable with synthetic signals (`tests/test_audio_features.py`). Every decay rate is derived from the *measured* elapsed time between calls, not the nominal poll period, so a stuttering poll thread degrades smoothly instead of changing the feel.

* **`level`** — block RMS through a one-pole attack/release follower (10 ms attack so a transient is on screen the frame it happens, 150 ms release so brightness breathes rather than flickers), normalized against a rolling peak that decays toward `_PEAK_FLOOR` over ~2 s. That makes `level` *relative* loudness: a quiet feed still reaches full scale within a couple of seconds, while true silence reads 0 rather than being amplified into noise. Per-**block** deliberately — `dsp._ar_envelope` is a per-sample Python loop and is the wrong tool at 60 blocks/sec.
* **`bands`** — Hann → `np.fft.rfft` → mean magnitude over log-spaced edges → `log1p` compression, clipped to [0, 1]. The band-edge function and the `log1p(mag * 100)` curve are **shared with `spectrum_petscii`** (moved here, the overlay imports them), so the bars it draws and the bands the analyzer reports describe identical frequency ranges — one definition, not two that can drift.
* **`onset`** — spectral flux: the sum of positive per-band deltas in log magnitude against the previous frame, compared to an adaptive threshold (running median of ~1 s of flux history × `_THRESH_MULT`, plus an absolute `_FLUX_FLOOR`). The floor matters: with a median near zero, any numerical dust would read as a crossing. A separate `_SILENCE_LEVEL` guard suppresses onsets entirely below a floor level, which is what stops a silent room from growing a phantom tempo. On a crossing, `onset` latches to 1.0; otherwise it decays by `exp(-dt/0.18)` — **the same τ as `SidFeatureStream._ONSET_TAU_S`**, so a pulse looks identical to the SID path after 16-color quantization. Flux is computed on the *unclipped* log magnitudes so a loud transient isn't hidden by the [0, 1] clip the consumers see.
* **`bpm` / `beat_phase`** — delegated to `modulation.TempoEstimator` (below). The BPM also feeds the process-wide performance beat grid when `[performance].tempo_source = "audio"`: `Playlist` forwards the active scene's `features().bpm` into its `TempoClock.audio_drive` each frame, so the detected beat drives launch quantization, `mod_source = "clock"` effects and WLED tempo (see the [`tempo.py` audio drive mode](control.md#tempopy--process-wide-musical-beat-grid-live-djvj-phase-1)).
* **`voice_freqs` / `voice_gates`** — zeros/False. They are SID-specific with no audio-input analogue, so the two generators that read them (moire, kaleidoscope) fall back to their base geometry and react through level/onset/beat_phase/bands like everything else.

### `modulation.TempoEstimator` — one tempo implementation, two producers

Lifted verbatim (logic and constants) out of `SidFeatureStream`, which was the only producer until this module needed exactly the same math: EMA the inter-onset interval, fold near-simultaneous onsets into one beat, re-anchor across long rests, clamp to a plausible BPM band, and integrate `bpm/60` into `beat_phase` so a jittery estimate never causes a phase discontinuity. It lives in `modulation.py` because that module is stdlib-only by design — the one place both the py65-backed SID stream and the numpy-backed audio analyzer can import without dragging in each other's deps. `SidFeatureStream` now delegates to it (a behavior-identical refactor; `tests/test_music_features.py` guards it).

`MusicModulation` also gained **`bands: tuple[float, ...] = ()`** plus `bass`/`mid`/`treble` properties that fold whatever band count the analyzer was configured for into thirds. It is defaulted, so every pre-existing construction site is untouched, and it stays empty on the SID path — which is what keeps the SID look bit-for-bit unchanged: `generators._reactive_value`'s bass term and `_reactive_hue_offset`'s treble term both evaluate to exactly 0.0 there.

**Why bass→brightness and treble→hue** (and not saturation): the 16-color quantizer handles a desaturated hue badly — it lands in the greys — so the spectral split rides the two axes that survive quantization. A kick punches the value, a hi-hat pattern shimmers the hue, and they read as different events.

### The stream + tap

`AnalysisTap` is a small lock-protected mono float ring with the same wrap arithmetic as `AudioStreamer._push_to_tap`/`get_recent_samples` — lifted rather than shared, because the writer here is a realtime callback on a streamer that may not exist yet (the tap outlives any single `start_mic`). `push()` is nothing but a couple of slice assignments under a short-lived lock.

`AudioFeatureStream` is the `PollThread` between them, modelled directly on `SidFeatureStream`: `start()` / `stop()` / `features()`, a `_lock` around the snapshot, `features()` returning `None` before the first tick, and `_process_tick` split out so tests drive it over a hand-filled tap with no thread. The FFT runs outside the lock; only the snapshot swap takes it.

### Config + wiring

`[audio_features]` (`config.AudioFeaturesCfg`): `bands` (8), `onset_sensitivity` (1.0), `poll_hz` (60.0), `fft_size` (1024), `listen_sample_rate` (44100). `onset_sensitivity` divides the flux threshold and is the one knob worth turning in practice — dense, heavily-compressed material reads as continuous transients at high values; sparse material needs a push. `listen_sample_rate` is the `audio_source = "listen"` capture rate (ignored by `mic`, which analyzes at the DAC rate).

`MicAudioSource` gained `reactive` (default True), `listen_only` (default False) + `features_cfg`. `setup()` installs the tap **before** `start_mic`/`start_listen` so the first callbacks already reach the analyzer; `teardown()` clears `analysis_sink` **before** `audio.stop()` so no callback can push into a tap whose thread is going away. A startup failure degrades to non-reactive with the audio intact — the same contract as `SidFileAudioSource.setup`.

The analyzer taps the capture callback, so `reactive = true` with `audio_source = "mic"`/`"listen"` needs `[audio].enabled` (the shared streamer owns the capture); `_validate_generative` **warns** rather than failing (`reactive` defaults True, so someone who only wanted silent generative visuals shouldn't have to opt out explicitly). Listen additionally warns on `reactive = false`, since a listen source exists only to drive the visuals — with reactivity off it opens nothing.

Demo config: `config/examples/audio-reactive-input.toml`.
