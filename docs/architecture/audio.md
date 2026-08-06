# Audio output

Getting sound out of a C64: the NMI-driven 4-bit `$D418` DAC, the U64's off-bus FPGA PCM sampler, and the host-side DSP that makes 4 bits listenable.

Part of the [architecture reference](../architecture.md). For end-user configuration see [the Programmer’s Reference Guide](../reference/README.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`audio.py` — AudioStreamer](#audiopy--audiostreamer)
* [`sampler.py` — UltimateAudioSampler (U64 "Ultimate Audio" FPGA PCM)](#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm)
* [`dsp.py` — host-side audio DSP for the 4-bit DAC path](#dsppy--host-side-audio-dsp-for-the-4-bit-dac-path)
* [`audio_features.py` — audio-input music features (reactive visuals from live input)](#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input)
* [`audio_source.py` — AudioFileSource (audio-file reactive source)](#audio_sourcepy--audiofilesource-audio-file-reactive-source)

---

## `audio.py` — AudioStreamer

An NMI-driven 4-bit SID DAC, writing the `$D418` volume nibble. This is the only approach that works on a real C64 with active video output.

**Why not PWM.** `$D402` PWM was tested and rejected on two counts:

* At an 8 kHz NMI rate the PWM carrier sits 9 dB *above* the audio signal (confirmed by spectral capture).
* At 16 kHz, VIC-II badlines — 40 stolen cycles in a 63-cycle period — make the NMI handler overrun and queue back-to-back, stretching samples. A 440 Hz test tone came out at 421 Hz.

### Sample rate and the overrun ceiling

The default is **12 kHz**, set by the 2026-07-02 hardware sweeps below. That puts Nyquist at ≈6.0 kHz, so fricatives and sibilants survive and speech is clear.

`c64.nmi_rate_safety` is the single source of truth for the safe ceiling. `config.validate_nmi_sample_rate` rejects overrunning rates at load, and `--doctor` reports them.

There are **two different ceilings**, and the shipped default respects the lower one.

*Isolated-handler ceiling — ≈13.6 kHz NTSC / ≈13.1 kHz PAL.* The handler-cycle budget in `c64.py` is directly hardware-measured, not estimated. A ring-prefill tone sweep on a real NTSC C64 (`scripts/diags/tr_nmi_rate_ceiling.py`) found the effective consumer rate tracks the configured rate cleanly through 14 kHz (73-cycle period), slips ≈1% at 15 kHz (68-cycle overrun onset), and plateaus ≈15.3 kHz. Worst-case handler completion is therefore ≈68 cycles. `NMI_SAFE_MIN_PERIOD_CYCLES = 75` keeps margin above that for PAL and unit variation. Because the sweep prefills the ring and runs with **no host feed**, this figure is independent of the backend and of TeensyROM firmware.

*Live-pipeline ceiling — ≈12.5 kHz.* The full streaming pipeline overruns lower. A pitch A/B sweep on a real NTSC U64-II (`scripts/diags/nmi_rate_sweep_ab.py`) plays one clip at a range of rates and recovers the played pitch by log-spectrum cross-correlation against the source; a rate whose pitch drops below the low-rate floor is the handler queuing. Results:

| Rate | Pitch vs source |
| --- | --- |
| 11600, 12000 | ≈+0.15 % (clean floor) |
| 13000, 13500 | ≈−0.4 % |
| 15000 | −1.1 % (positive control) |

The onset was **identical in char (petscii) and bitmap (mhires) modes**, which identifies the cause: the host-DMA audio-ring writes themselves halt the 6510 and steal handler cycles. It is not video bus load. So the default stays below ≈12.5 kHz even though the isolated handler could go higher.

Char and light scenes hold cleanly near the ceiling — petscii video plus host-DMA DAC verified clean with no underruns at both 11.6 and 13.5 kHz on a TeensyROM+. Bitmap+digi is the heaviest case (≈10 KB/frame re-upload), but the bitmap+digi fps cap and the REU-staged double-buffer hold its bus-halt loss near zero (see host-DMA pitch compensation below).

> **Not the same problem:** forcing the DAC path on **bitmap** video also shows a rate-independent tempo *stretch* — correct pitch, ≈12 % slow. That is the servo under-draining the ring under bitmap DMA load, and it is fixed by bitmap+DAC tempo compensation (see the [`video.py`](video-color.md#videopy--webcamsource-shared-broker--avfilesource-pyav) notes), which pre-compresses the content. It affects neither char modes nor the default U64 sampler path.

### `sample_rate` is a request; `effective_rate` is what you get

The NMI is driven by CIA #2 Timer A, which counts an **integer** number of PHI2 cycles — the period is `latch + 1`. So the achievable rates are the grid `PHI2 / (latch+1)`, and `_nmi_latch_value` picks the nearest point to what was asked for. You never get exactly `sample_rate`:

| Requested | System | Latch | Achieved | Error |
| --- | --- | --- | --- | --- |
| 8000 | NTSC | 127 | 7990.05 | −0.124 % |
| **12000** (default) | **NTSC** | **84** | **12032.08** | **+0.267 %** |
| 8000 | PAL | 122 | 8010.15 | +0.127 % |
| 12000 | PAL | 81 | 12015.22 | +0.127 % |

The offset itself is inaudible and does not distort: the servo adjusts the producer's *pace*, not the sample content, so nothing is duplicated or dropped — +0.267 % is a 4.6-cent pitch shift, against a 50-cent quarter tone. It is also common-mode, since video is slaved to `position_seconds()`, so A/V sync doesn't see it. Left uncorrected it would still put a standing bias in every samples→real-time conversion, and hand the host-DMA servo a fixed error to absorb before it could start correcting for anything real.

`AudioStreamer.effective_rate` exposes the achieved value, and it — not `sample_rate` — is the timebase: producer pacing, the adaptive loop's target, `position_seconds()`, and the rate file paths resample/pre-encode content to. A decoded track therefore plays at exactly real time and pitch, and the `clock/wall` gauge that calibrates `[audio].dac_bitmap_tempo_*` reads true.

Deliberately excluded: the mic capture-device open rate (some devices reject an odd rate, and a mic clock that doesn't match the C64 is what the servo is for) and the DSP filter rates (a 0.27 % shift in a corner frequency is nothing).

`UltimateAudioSampler` likewise reports its divider's achieved rate rather than the request, and carries an `effective_rate` of its own, so a scene can read either sink the same way.

**The same quantization limits `pitch_mult_*`.** `_compensated_latch` computes `period = round((nominal+1) / mult)`, so at 12 kHz NTSC (nominal period 85) **one step is ≈1.2 %** and requests snap to it: `1.005` → +0.00 % (a no-op), `1.010` and `1.015` → both +1.19 %, `1.020` → +2.41 %. This is why a requested trim and a measured one disagree: `pitch_mult_mhires = 1.015` measures **+1.36 %** high on hardware, tracking the quantized +1.19 % rather than the +1.5 % asked for. Sub-step pitch trim is not expressible through the latch; a finer correction has to come from the content side, the way `dac_bitmap_tempo_*` fixes tempo.

### Input modes

The `device` argument to the `start_*` methods is an `int | str`: an int index, or a **device name substring** matched case-insensitively against the input-capable devices `sd.query_devices()` reports (the same listing `--list-devices` prints). `resolve_audio_input_device(device)` (module-level in [`audio.py`](../../c64cast/audio.py)) does the coercion at the top of `_resolve_input_device` — the audio analog of [`camera.resolve_camera_index`](control.md#camerapy--camera-enumeration--namevidpid-device-selection-optional-camera-extra) for `[video].device`, minus USB `VID:PID` (PortAudio exposes none). Unlike the camera resolver it never raises: a name that matches nothing (or multiple → first wins) warns and falls back to the system default input (`-1`), matching `_resolve_input_device`'s existing forgiving contract. `-D/--audio-device` and `[audio].device` both flow through it, and `--save-settings` persists the chosen name/index.

* `start_mic(device, sens, gate)` — sounddevice capture; `mic_callback` pushes into the queue.
* `start_for_external_source()` — no input thread; the caller (the PyAV demuxer) pushes via `push_samples(int16)`.
* `start_listen(device, sens, *, sample_rate=None)` — **analysis-only capture**: opens the input, feeds `analysis_sink` from `_listen_callback`, and stops there. No NMI, no worker thread, no DAC/SID writes, so nothing reaches the C64 — the input drives reactive visuals only (the `audio_source = "listen"` VJ case). Because nothing downstream is bound to the DAC rate, it opens at `sample_rate` when given — the listen path passes a higher rate (44.1 kHz) for full-bandwidth analysis (real hi-hat energy above the DAC's 6 kHz Nyquist, cleaner onsets). A `_listen_mode` flag makes `stop()` short-circuit its DAC teardown to a bare stream-close; the other `start_*` methods clear it (the streamer is reused across scenes). See [`audio_features.py`](#audio_featurespy--audio-input-music-features-reactive-visuals-from-live-input).

### The worker thread and its pacing

The worker drains the queue at `chunk_size / sample_rate` — the NMI consumption rate — so it can never lap the NMI read pointer and overwrite real audio with neutral padding. Each iteration:

1. Collects up to `chunk_size` bytes by the pace deadline. There is no grace period: the pace deadline *is* the collect deadline.
2. Pads with `NEUTRAL_SAMPLE=7` only on a real underrun, meaning the deadline expired with nothing queued.
3. Uploads to the ring buffer at `$4000-$5FFF`.

After `PREBUFFER_CHUNKS * chunk_size` bytes of prebuffer it starts the CIA #2 timer (`$DD04/05`). The BASIC clear-loop is kicked once at session startup, not per scene.

#### The ring write is split and spread

Step 3 is not one write. A host `DMAWRITE` halts the 6510 for the whole transfer, and CIA #2 is **edge-triggered** through the NMI line: when one halt spans two Timer A underflows the ICR latches once and the second sample is never fetched at all. So the payload size of a ring write is not a throughput question, it is how many NMIs that write destroys.

The halt costs **≈1 cycle per byte**. That is HW-measured, not modelled — `scripts/diags/audio_fm_probe.py` fills the ring with a tone that tiles it exactly, arms the NMI, and then issues background writes of a controlled size while capturing off HDMI, so nothing can underrun and contaminate the reading. It measured 1.02 µs/byte on the U64 and 0.97 on a TeensyROM+, holding within a few percent from 32 to 1024 bytes.

At the 12 kHz NTSC default the NMI period is 85 cycles ≈ 83 µs, so the old single 1024-byte write froze the CPU for ≈1064 µs — **12.8 NMI periods, ~12 times a second**. `c64.halt_quantum_bytes` sizes each piece from the live latch (`period_cycles − HALT_QUANTUM_MARGIN_CYCLES`, ≈65 B at that default) so a write can never swallow a second underflow, and `_drip_chunk` spreads the pieces evenly across the chunk period.

Measured on hardware against a 376 Hz carrier, U64 plain RAM, matched byte rate:

| payload | halt | FM deviation | 4–20 Hz modulation |
| --- | --- | --- | --- |
| 1024 B (one write) | 1064 µs | 27.3 Hz | 12.00 |
| 256 B | 249 µs | 11.9 Hz | 1.94 |
| 128 B | 104 µs | 6.7 Hz | 1.96 |
| 64 B | 46 µs | 5.3 Hz | 2.41 |

**Splitting and spreading are two separate wins, and it is easy to bank only the first.** The same bytes sent as 64-byte writes *back-to-back* measure 10.8 Hz deviation and 9.44 in the 4–20 Hz band, against 5.3 and 2.41 when spread — because a burst re-concentrates the halts into one low-frequency event. That band is called out on its own because sensitivity to modulation peaks there, and one write per chunk period puts the cadence at ~11.7 Hz, almost exactly the worst rate available. Note the biggest single step in the table is 1024 → 256, where FM deviation only halves but the 4–20 Hz figure drops 6× — the cadence leaving the band, not the halt shrinking. A metric that sums total sideband power is blind to that.

That burst-vs-spread comparison is also the control that says host-side splitting works at all: if the firmware serviced queued commands without resuming the CPU between them, burst and spread would measure identically. They do not.

This is the host-side twin of `modes.BANK_SWAP_CHUNK_SIZE`, which splits C64-side REC DMA for exactly the same reason and took NMI capture from 67 % to 97 %. The host path simply never got the same treatment.

*Why the worker gained a one-chunk pipeline.* Spreading the writes across the period consumes the time the worker used to spend asleep waiting for the pace deadline — which was also the producer's collect window. So the worker now holds the chunk it collected last iteration and drips *that* one out while collecting its successor in the gaps between pieces (`_collect_until` is called once per quantum slot). Collection keeps its full period. The cost is one `chunk_period` of extra latency, and the queued-sample count is deliberately not decremented until the bytes actually land, so `position_seconds()` still reports where the audio really is and A/V sync does not shift.

*There is deliberately no off switch.* The quantum sets the write **rate** — `chunk_size / quantum` writes per chunk period — and the render thread shares the one socket, so `_halt_quantum` floors the halt-derived size by what `BackendProfile.max_write_rate_hz` can carry. That floor already expresses the off case: a link that cannot afford to split gets a quantum of the whole chunk and issues one write, exactly the pre-split behavior. A boolean would only let someone pick worse audio on a link that could afford better.

Getting that budget wrong is not a mild regression. A 65-byte quantum asks for 188 writes/s; on hardware each write then ran past its slot, so every later slot deadline was already expired, collection got no time at all, and the session NEUTRAL-padded 1744 chunks over a *full* queue and lapped the ring. `_collect_until` now drains non-blockingly once past its deadline so a slow write can never again manufacture an underrun out of queued data — but the budget is what keeps it from happening.

Prebuffer writes are deliberately left unsplit: the NMI is not consuming yet, so there is no halt to hide from, and one write fills the ring faster.

#### The video path is deliberately *not* split, and lost ticks are not the hiss

The render thread pushes frames over the same link and those pushes go out whole — a ~1 KB PETSCII frame push spans ~12 NMI periods, so on a 20 fps run video costs roughly 264 of the ~318 ticks/s lost to halts, against ~54 for the audio ring. Splitting it too is the obvious next step. It was built, measured, and removed, and the measurements are worth keeping so nobody rebuilds it:

* **The tick accounting was right.** Loss ≈ (total halt cycles / NMI period) − (number of writes), so each extra write buys back exactly one tick. Splitting video plus raising the TeensyROM write ceiling to 600/s took the consumer-rate deficit from **3.1 % to 1.1 %** — R rising 11661 → 11898 against a 12032 nominal.
* **The link is not the limit it claims to be.** That run sustained **557 writes/s with zero underruns**, against `max_write_rate_hz = 200`. The declared ceiling is a floor nobody has pushed to a wall.
* **And none of it was audible.** Recovering those ticks changed nothing a listener could hear, because `$D418` holds the previous sample across a lost tick: the error is a zero-order hold, not a gap. Meanwhile the ~5× write rate measurably *raised* the noise floor — 3.4 dB on `dac_curve = "calibrated"`, 2.2 dB on `"mahoney_ultisid"`, THD+N moving the same way.

So the split is a net loss on this path, and `max_write_rate_hz` is deliberately left at 200 rather than corrected upward: more writes measure *worse*, so the honest ceiling is not the interesting number.

#### The Mahoney path is the noise floor

The audible static on the 4-bit DAC is not the halt at all. It is the **Mahoney 8-bit companded path**, which measures ~15 dB worse than the plain 4-bit `linear` curve it exists to improve on.

Measured through the real playback path — a generated clip carrying a steady 440 Hz tone over `testsrc2` motion video, `petscii` at 20 fps, captured off HDMI and scored both by a notched noise floor and by THD+N with harmonics included, because a 4-bit path's quantisation error is harmonic and a notched metric alone would flatter it:

| `dac_curve` | noise floor | THD+N re carrier |
| --- | --- | --- |
| `linear` | **−73.9 dB** | **−8.2 dB** |
| `calibrated` | −66.8 | −7.3 |
| `mahoney_ultisid` | −58.6 | −1.3 |

Both metrics agree on the ordering, and so does every listening pass: `mahoney_ultisid` reads as a constant loud buzz, `calibrated` as interference that grows over a run, `linear` as the cleanest. At −1.3 dB THD+N nearly half the output power is not the signal.

This **inverts the assumption `dac_curve = "auto"` encodes** — auto resolves to a Mahoney variant precisely because Mahoney is believed to be the quality option (~6–7 effective bits against 4). On this hardware it is not. Mahoney parks the three voices as DC through the filter and so depends entirely on filter behaviour, which is where to look; the open calibration bugs around sign inference and degraded filter paths are the obvious suspects.

It is **not TeensyROM-specific**: the same clip on the Ultimate with `backend = "dac"` forced measured −73.3 dB against the TR's −76.9, i.e. slightly *worse*. The Ultimate only escapes in normal use because `backend = "auto"` sends it to the off-bus FPGA sampler.

The noise is **episodic**: 0.5–1.5 s bursts that lift the floor 4–10 dB at irregular intervals, which is what listeners describe as plateaus that jump and then hold. A `calibrated` capture also creeps upward across 11 s (−60 → −52 dB). Cause unestablished; a decaying DC park is the obvious candidate and is untested.

This went unmeasured for years because every metric on this path was a *frequency* metric — carrier shift, FM deviation, modulation bands — and the fault is pure amplitude. `audio_fm_probe.py` now carries `noise_floor_db` for exactly this. Note that probe's own conditions use `digi_boost`, i.e. plain 4-bit: it has never exercised the Mahoney env, which is why its earlier results were all null.

*Late slots are the metric that says the spread actually held.* Everything above is a design; whether a given run kept it is a separate question, and one nothing else answers. A sub-write reached after its slot deadline has passed goes out immediately, which leaves every later slot of that chunk expired too — so they fire back-to-back and the run degrades from the spread condition toward the burst condition, continuously and by degree. That is invisible to every other counter: the bytes still land, in order, and no underrun is recorded, because `_collect_until`'s non-blocking drain is doing its job. `_drip_chunk` therefore counts late slots and the worst lateness, `stop()` reports the session share (warning past 10 %), and `_maybe_log_health` emits a per-window line every `AUDIO_HEALTH_LOG_INTERVAL_S`.

That health line is per-**window**, not cumulative, because the artifacts left on this path are the time-varying ones — a fault that appears part-way in, deepens, clears and returns is indistinguishable from a steady one in a session total. It carries the servo gap's excursion, late slots over total, underruns, write rate, the live quantum, the measured consumer rate R and the latch, which is enough to tell a collection stall from a link stall from a servo excursion without a capture rig.

#### Verifying the arm took

Three writes bring the consumer up, and all three are load-bearing: the `$0318` NMI vector, the Timer A latch, and the `$DD0D`/`$DD0E` enable+start. They ride the same transport as everything else, whose `_emit` contract is to **absorb** a failed write rather than raise — right for a lost border poke, wrong here. Any one of them going missing produces the identical symptom: the NMI never fires, the read pointer R (the self-modifying operand at `$C025`) sits at `RING_BUFFER_ADDR` forever, and the session is silent from the first frame to the last. It also runs *fast* — the host-DMA servo is correctly reacting to a reader that never consumes. A dropped vector write is not a milder case: the KERNAL NMI handler stays installed and writes `#$7F` to `$DD0D`, killing CIA #2 interrupts itself.

So `_start_nmi_timer` verifies rather than assumes. It reads R, calls `_arm_nmi_once` (all three writes, plus a read of `$DD0D` — the only way to clear the latched ICR flags, so the next underflow is a clean 0→1 transition), waits `NMI_ARM_VERIFY_DELAY_S` (30 ms ≈ 360 bytes of R travel at the 12 kHz default, so a frozen R is unambiguous), and re-reads. Up to `NMI_ARM_MAX_ATTEMPTS` times; `_nmi_arm_attempts` records what it took. Retries are cheap and only happen on failure — five of them is ≈150 ms against the ≈512 ms prebuffer already in hand. This was found on a machine whose short-board DMA is marginal enough to drop one of the three intermittently; a retry lands it.

A backend that can't read R (`profile.supports_read` false, older TeensyROM firmware) arms exactly once, as before — there is nothing to verify against, and inventing a wait would only add latency.

`_note_r_reading`, called from the servo's per-chunk R read, covers the consumer that dies *mid*-session: `NMI_STALL_WARN_CHUNKS` consecutive identical readings warn once. R moves ~1 KB per chunk period when alive, so identical back-to-back readings are conclusive, not a heuristic. The servo already had this reading in hand and simply never said anything about it.

Pacing is **strict absolute** — `next_write_time + chunk_period` — and never snaps forward to wall-clock on overrun. Snapping forward lets DMA round-trip and Python wakeup overhead shrink the effective sample rate below NMI consumption; every chunk then takes NEUTRAL padding, producing audible chunk-rate AM sidebands (≈−5 dB at the carrier) and ≈16 dB of overall level loss on video audio. The 8 KB ring (≈0.68 s at the 12 kHz default) absorbs occasional pace overshoots.

### Why the ring lives at `$4000`

`$4000-$5FFF` is VIC bank 1, chosen over `$8000-$9FFF` so the ring stays out of VIC banks 0 and 2 — the two banks with kernal char-ROM mapped (at `$1000` and `$9000`), which the REU-staged char display modes use as their off-screen swap target. The 6510 NMI handler sees `$4000` as ordinary main RAM regardless of VIC bank.

Three patch offsets in the NMI routine bytes — read address HI, end-compare HI, wrap-reset HI — come from `RING_BUFFER_HI` / `RING_BUFFER_END_HI`, so relocating is a one-line change. Bitmap modes that want VIC bank 1 for themselves would need that relocation; PETSCII never selects bank 1.

### `[audio].use_reu_pump` — REU-staged mic streaming

Setting this on a webcam scene, or any scene that calls `start_mic`, opts the mic path into REU-staged streaming. The mic callback REUWRITEs encoded samples into a 64 KB REU ring at offset `$100000` — bus-clean, with no SID perturbation — and a C64-side IRQ handler at `$C100` drains that ring into the audio ring at the matched CIA #1 rate.

The handler reloads the REU source registers (`$DF04`/`$DF05`/`$DF06`) from a 3-byte tracker in main RAM at `$C200` on every IRQ, rather than trusting `$DF06` read-back. This is not defensive coding — the U64's REU returns garbage in the upper bits of `src_hi`, which made the handler's wrap check (`CMP #reu_end_hi`) always succeed. The source reset to the start of the prefilled NEUTRAL block every time, and the result was pure silence.

Two pinned BCC displacements (+15 src wrap, +10 dst wrap) must land on instruction boundaries; wrong values stomp either the tracker or the REU registers.

Bootstrap latency is `REU_MIC_BOOTSTRAP_BYTES / sample_rate`, ≈133 ms at the 12 kHz default. The one `use_reu_pump` flag covers both the video (`start_for_reu_staged`) and mic (`start_mic`) paths — `AudioStreamer` picks the matching bring-up from whichever start method was called.

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

Default `"auto"`. `"linear"` is the classic 4-bit path (float → volume nibble 0..15).

`"mahoney_ultisid"` switches the encoder to Pex 'Mahoney' Tufvesson's 8-bit technique. `_enable_mahoney_env` — branched in `_upload_nmi_and_buffers`, and mutually exclusive with `digi_boost` — parks all three voices as steady DC sources (control `$49` = pulse+TEST+GATE, AD `$0F`, SR `$FF`) with voices 1 and 2 routed through the analog filter (`$D415/$D416 = $FF`, `$D417 = $03`). That is the white-paper §XIV environment.

In that environment the **full `$D418` byte** written per NMI sample — volume nibble, filter HP/BP/LP mode bits, and the voice-3-OFF bit — selects one of ≈256 distinct, strongly non-linear output levels. That is ≈6-7 *effective* bits (Wothke), not 16.

Cost is unchanged: still one `STA $D418` per sample. Only the ring byte values differ, spanning 0..255 rather than 0..15, since the NMI routine applies no nibble mask.

The mapping is a 256-entry amplitude→`$D418` table. `encode_floats_to_dac(..., curve)` maps `float[-1,1]` to an 8-bit amplitude index centered on 128, then through `sidtable[idx]`. Dither folds in at the index domain, and exact zero maps to index 128 (silence, dither skipped). The ring rest byte `_neutral_byte`, used for prefill and underrun/EOF padding, becomes `sidtable[128]` when a curve is active.

`AudioStreamer` receives the resolved table via its `dac_table` parameter — the CLI resolves the system-aware name first — so its `dac_curve` string is only a label for logs.

### Table selection, `"auto"`, and per-system calibration

Implemented in [c64cast/dac_calibration.py](../../c64cast/dac_calibration.py).

Only the **emulated-UltiSID** table ships baked into [c64cast/dac_curves.py](../../c64cast/dac_curves.py). Hardware measurement (2026-07-02, Cam Link) showed the U64 FPGA UltiSID curve is deterministic across units, and that the 6581/8580 model knob is irrelevant — byte-identical output — so one table generalizes.

Physical chips do not generalize. 6581/8580 variation is enormous chip-to-chip, dominated by the analog filter: two 6581s correlated only 0.74, and swapping their tables cost ≈29% RMS level error. SID replacements (ARM2SID, SwinSID, FPGASID) differ again. No baked table can serve them, hence calibration:

`c64cast -u <target> --calibrate-dac` (`cli` → `dac_calibration.run_calibration`) measures the connected SID's signed transfer curve, ≈50 s per socket.

#### Picking the capture device

`find_capture_device` resolves it: `--audio-device` if given, else the first input-capable device whose name matches `CAPTURE_NAME_HINTS` (`"cam link"`, `"elgato"`, `"hdmi"`, `"capture"`, `"macrosilicon"`, `"usb video"`, `"av to usb"` — tried in that order, so a rig with both a Cam Link and another HDMI input still picks the Cam Link), else the system default input.

The hint list has to be broad because the fallback is a bad one: the system default input is the on-board microphone on most machines, and a calibration measured off room noise fails in the expensive way (below) rather than the obvious one. So when that fallback does fire, `run_calibration` warns immediately — `looks_like_capture_input` is false for the chosen name, so the log carries the warning and the input list while the run is 5 s old rather than 50.

#### Resolving the capture format

`resolve_capture_format` then probes what the chosen device will actually *open* and returns a `CaptureFormat(channels, samplerate)`.

Hardcoding stereo at `CAP_SR` (48 kHz) — what an Elgato-class capture card presents — is not enough. Two device classes in the field reject it: mono-only UVC inputs, and the cheap MacroSilicon-based HDMI→USB dongles, which are frequently 96 kHz-only. The refusal lands as a raw `sounddevice.PortAudioError: Invalid number of channels [PaErrorCode -9998]` out of `sd.rec`, mid-run, after the machine has already been reset and brought up.

Neither restriction prevents a measurement. The levels are read off a single folded-to-mono channel (`rec.mean(axis=1)`, a no-op at one channel), and `extract_slot_levels` derives every timing constant from the `sr` it is handed, so a capture at any rate reconstructs the same ladder. So the resolver probes `channels ∈ (2, native, 1)` against `rate ∈ (CAP_SR, the device's default_samplerate, 96000, 44100, 32000)` with `sd.check_input_settings` — which validates without opening a stream, so a rejected combination costs nothing. **Rate is the outer loop**: a 48 kHz mono capture is preferred over a 96 kHz stereo one, since folding the channels is free while changing rate is the compromise. The device's own `default_samplerate` is tried directly after `CAP_SR` so an unusual device still lands on its native rate before the static fallbacks.

A device that accepts nothing — or has no input channels at all — raises `CaptureUnavailableError` listing every input-capable device with its channel count, which `cli` catches for a clean exit 3 instead of a traceback.

#### The slot ring: reading signed levels directly

The SID → capture path is AC-coupled (≈8.5 Hz measured), so a static code produces no steady signal and a level can only be read as a *change*. `build_slot_ring` fills the NMI ring with 32-sample slots alternating `[code][ref]`, `ref = $00` (master volume 0 — silence), behind a leading run of `SYNC_SLOTS` reference slots that marks where a pass begins. One ring carries 112 codes, so 256 codes take 3 rings of ~5 s each.

Every code is then measured against the **same baseline inside one capture**, so its signed level comes off the waveform directly and no sign has to be inferred. `extract_slot_levels` does the rest and is pure, so it can be re-run offline against a saved capture:

1. `_boxcar_step` — a matched filter for a level step; `|s|` peaks on every slot boundary.
2. `_find_ring_anchors` — a pass starts at the edge that ends a sync gap. The test is *relative* (within 25% of the longest gap seen), because a run of codes that all sit at the reference level also leaves no edges, and on a partly-dead chip that run can be long. `plan_code_batches` strides rather than slices for the same reason: slicing 0-110 / 111-221 / 222-255 would put all sixteen codes of one upper nibble in consecutive slots.
3. `_track_slot_grid` — the part that has to follow the signal rather than the clock. A slot is 192.24 capture samples, because the NMI runs at 1022727/128 = 7990.05 Hz, not the 8000 Hz it is asked for, and avfoundation drops samples under load on top of that. Stepping a nominal pitch from the marker walks the read window off the boundary into the middle of a sagging plateau within a fraction of a pass — which yields levels that repeat perfectly across passes (so they look trustworthy) and are wrong. So the grid follows the signal: each boundary is matched to the nearest detected edge and an alpha-beta filter folds that into a smoothed offset *and* a drift rate, with edgeless boundaries coasting on the current rate. A synthetic capture with 12% of its samples dropped reads levels to 0.5% either way; with a fixed grid the same capture is off by 56%.
4. `_dc_restore_gain` — undoes the AC coupling so a plateau mean is a level and not a level plus the sag of whatever preceded it. For a one-pole high-pass the inverse is exactly `v = y + cumsum(y)/(τ·fs)`, one unknown scalar, and the restored signal is affine in it — so the total within-plateau variance is a quadratic with a closed-form minimum. τ is fitted from the data rather than assumed; a 2- and 3-pole basis was tried on real captures and did not improve on it.
5. Each code slot is differenced against the reference slots bracketing it, canceling residual slow drift locally.

`pass_spread_frac` is the trust metric: every pass measures the same 256 levels, so disagreement between them is the one symptom that separates a mistracked capture from a real curve. On hardware it is 0.01–0.2%.

#### Refusing a capture that isn't of the ring

`extract_slot_levels` is a *reader*: handed a waveform it reports what it found, and it can only refuse what it cannot parse. But a recording of the **wrong input** parses fine — the peak finder locks onto noise, a sync gap or two turns up, and levels come back near zero with the passes contradicting each other at a `pass_spread_frac` near 100%. Those are numbers, so ungated they reach the table, and the run survives until some later ring happens to yield fewer than two sync markers: a failure both far from its cause and 30 s of measuring too late.

So `read_ring_capture` wraps the extraction in the two judgments that belong to whoever chose the recording, and every ring goes through it:

* **peak < `SILENT_CAPTURE_PEAK`** (0.002 of full scale) — the ring swings the SID between full-scale codes and silence, so any correctly routed input sees far more than this.
* **`pass_spread_frac` > `RING_TRUST_MAX_SPREAD`** (10%) — two orders of magnitude above what hardware reads, so this only fires on levels that are noise.

`capture_ring` writes the ring once and re-records up to `RING_ATTEMPTS` (2) times, so a ring spoiled by a transient costs one capture window instead of the run; a rig that never produces a usable one then fails with `_capture_fault_message`, which names the device it recorded from, how loud that recording was, the three things that cause it (wrong input / audio not routed / NMI never came up), and the input list to pick from. `cli` catches `MeasurementError` alongside `CaptureUnavailableError` for a clean **exit 3** — an unmeasurable rig is a setup problem, not a bug, and neither should traceback.

#### Why every code is measured three times

A 6581's output for a `$D418` byte is not quite a function of that byte alone. Planting one probe code at twelve positions in an otherwise ordinary ring (`scripts/diags/mahoney_slot_ring_probe.py` measured this): a positive code reads **20% lower** at the end of a ring pass than at its start, a negative code 2% *higher*, and the apparent level correlates at |r| ≈ 0.9 with the mean level of the surrounding slots. It is present in the raw waveform before any processing, so it is the chip's operating point sliding with the accumulated signal, not a measurement artifact. The degraded socket-2 chip shows almost none of it (`context_spread_median_frac` 0.24% vs socket 1's 2.2%), which fits: it is the live filter path that moves.

Measure each code at one fixed slot and that bias is baked into the ladder, ordered by code, looking exactly like curve structure. The tell is that the volume ramp within a nibble band stops being monotone — which it must not, since the master volume nibble scales whatever the mode bits produce. So `plan_capture_rounds` measures the whole set `MEASURE_ROUNDS` times, each round rotating every ring's slot order by another fraction of a ring, and `merge_measurements` averages. Every code then carries the same mean context, which is a common scale factor, and the ladder is scale-invariant.

Three rounds was chosen against a six-round reference captured on hardware: max deviation 0.9% of span and rms 0.2% — below one ladder step — versus 5.2% and six non-monotone codes at one round. The finished 3-round measurement has **zero** non-monotone steps across all 32 nibble rows on both sockets, 512 model-free constraints.

`merge_measurements` also rescales each ring onto the mean of its `ANCHOR_CODE` reading (`$0F`, first pair of every ring, always the same slot) before averaging — capture gain is stable within a capture but not guaranteed across them, and that is what lets several rings stand in for the one 256-code ring that does not fit.

#### The volume-0 self-test

The 16 codes `$h0` set the master volume nibble to 0, so their output level is `$00`'s regardless of what the upper nibble does: `L($h0)` must measure zero, for every `h`, with no model assumptions at all. `build_sidtable_from_levels` checks that and returns `(None, metrics)` when the worst deviation exceeds `SELFTEST_TOLERANCE` (10%).

On the socketed 6581 the residual is **1.3%**, and it is not measurement error: it tracks the filter routing bits (LP set → ≈1% of full scale, no filter → ≈0.1%) and does not move when the plateau read window is widened from 8 to 72 capture samples, so it is the chip's filter path leaking a little DC past a volume DAC set to zero.

`run_calibration` logs a rejection with that reasoning, still persists `raw_signed_levels` + `metrics` for the socket, and omits only `sidtable`. `load_calibrated_table` already treats a missing table as "no calibration applies", so playback falls back to `mahoney_ultisid`/`linear` with no reader change. `cli` returns exit code **4** when no measured SID produced a usable table, so a run that measured everything and trusted nothing can't look like a success.

#### Why levels are read directly, not inferred from two references

The cheaper-looking scheme is to toggle each code against two references — `$00` *and* `$0F` — at 500 Hz, take the FFT amplitude at 500 Hz as `|L(code) − L(ref)|`, and infer each sign from whether `p + q` or `q − p` comes closer to `lmax`. It cannot produce consistent output levels, and the obstacle is structural rather than a tuning problem. Measured on the socket-1 chip it misses the volume-0 ground truth by **52%**, and 89 of its 256 codes violate the triangle inequality `p + q ≥ lmax` by up to 51% of `lmax` — so no 1-D embedding of those numbers exists at all, and the sign inference is not ill-conditioned but unfounded. That is also why "add a third reference and least-squares the signs" cannot rescue it: more magnitudes do not restore a geometry that isn't there. Nor is any of it noise. It reproduces exactly (Pearson +0.9992 across captures three weeks apart), is independent of toggle frequency from 500 Hz down to 31.25 Hz, and reproduces *within a single capture* with both toggles interleaved in one ring — ruling out drift, capture gain, clipping and stereo folding, all checked on hardware. Reading signed levels straight off the slot ring sidesteps the whole construction, and is ~7× faster besides: ≈50 s per socket against ≈6 min.

Beware of one trap when validating a candidate reconstruction offline: agreement with the baked `mahoney_ultisid` ordering is **not** a correctness signal. The 2026-07-02 finding is `emu != physical (corr -0.07)`, and the two archived physical curves score −0.21 and −0.61 against it; the only entry scoring +0.90 is the degenerate socket. High agreement with the baked table indicates a chip whose filter path is dead, not a good ladder. The slot-ring curves score −0.32 (socket 1, working) and +0.49 (socket 2, degraded) — the expected pattern.

#### Measured playback A/B

`scripts/diags/dac_curve_playback_ab.py` plays one test tone through the real encoder (`encode_floats_to_dac`) once per curve, captures it, and reports SNDR — the honest comparison, since the curves differ in loudness by several dB and a level-mismatched listening test reads "louder" as "better". On socket 1, at full scale:

| curve | SNDR | THD | captured level |
|---|---|---|---|
| `linear` (4-bit) | 21.80 dB | −24.31 dB | −15.4 dBFS |
| `mahoney_ultisid` (baked, emulated-SID) | 0.09 dB | −1.35 dB | −12.6 dBFS |
| calibrated, two-reference scheme | 16.99 dB | −21.81 dB | −9.9 dBFS |
| calibrated, slot ring | **23.85 dB** | **−24.75 dB** | −9.8 dBFS |

Two things to read off it. The slot-ring table beats the 4-bit path by 2.0 dB while running 5.6 dB louder. And the two-reference table scores 4.8 dB *below* `linear` — a calibration that actively makes playback worse, which is exactly the failure the volume-0 self-test exists to refuse.

At −30 dBFS the ordering changes: the slot-ring table reproduces the tone at −38.9 dBFS (i.e. tracking the input correctly) where `linear` collapses to −73.9 dBFS because 4 bits cannot represent that level at all, but its SNDR there is 2.8 dB against the two-reference table's 5.4 dB. That is a real consequence of the slot ring finding a *wider* true span (−0.656 to +0.461, against the two-reference measurement's −0.394 to +0.317): the same 256 rungs spread over more range are coarser near silence. Full-scale SNDR is the figure that tracks what a listener hears, and it is limited to ≈24 dB by the chip's own context dependence rather than by the ladder, whose rms placement error is 0.37% of span.

#### Identity keys

The file is keyed by a **stable device identity** (`resolve_calibration_key`), not the connection target, so a DHCP re-lease or USB replug doesn't orphan it: a U64/U2+'s REST `unique_id` (`Ultimate64API.get_device_info` → `GET /v1/info`, e.g. `"5D327C"`) → `ultimate-5D327C`; a TeensyROM serial device's USB serial number (`teensyrom_dma.usb_serial_number`, re-scans `list_ports.comports()`) → `tr-<serial>`; falling back to the pre-existing host/device-path key when there's no live backend to query (offline `--doctor --skip-probe`) or the live lookup fails.

The USB-serial lookup only fires when `[teensyrom].serial_port` is set, so `make_backend` writes the **auto-detected** device back into the config after probing by USB VID/PID. Without that writeback an auto-detected link (the `tr://` default) degrades silently to the generic `tr-serial-auto` key and records an empty port in the calibration's provenance — so two different TR+ boards on one host would collide on a single file. Note Windows' generic `usbser.sys` may still expose no serial number at all, in which case the key falls back to the COM port; `dac_calibration_profile` remains the reliable escape hatch there. `[audio].dac_calibration_profile` overrides all of that with a user-chosen name (`profile-<name>`) — the escape hatch for a roaming TeensyROM+, which has no config API and can be moved between physical C64s: its own USB serial identifies the *cartridge*, not whichever machine's SID it's plugged into right now, so a calibration keyed off it would silently apply the wrong table after a move. A user who moves a TR+ around names each host's calibration once (`--calibrate-dac --dac-calibration-profile my-breadbin`) and passes the same name on every playback run against that host.

#### Multi-socket U64/U2+

A real U64 can carry two physical SID sockets, each potentially a different chip. `run_calibration` queries the live config (`sid_hw_config.detect_sockets` — `"SID Detected Socket N"`) and, for every socket reporting a real chip, isolates it to `$D400` (the fixed address the NMI DAC handler's `STA $D418` reaches) via `_isolate_socket` — reusing the "chip 0 must land at `$D400`" trick from [c64cast/asid_sidmap.py](../../c64cast/asid_sidmap.py)'s multi-SID address planner: that socket's address → `$D400` + enabled, the other socket → disabled, both UltiSID cores → unmapped, auto-mirroring off — measures it independently, then restores the original `SID Addressing`/`SID Sockets Configuration` (`sid_hw_config.snapshot_sid_config`/`restore_sid_config`) once every socket is done. This is purely config-driven, no U64-vs-U2+ model check: a U2+ with one socket + one UltiSID core measures just that socket; a bare-UltiSID board or a backend with no config API (TeensyROM) falls back to one unlabeled measurement of whatever SID currently answers `$D400`.

#### The calibration file

It lives under `paths.calibration_dir()` — the canonical `<data root>/calibration/dac/`, `$C64CAST_DATA_DIR`-overridable and resolved at use time (see [`paths.py`](config.md#pathspy)). It is machine-specific captured data, never committed; a `.gitignore` entry only guards against an accidental commit if a dev points `$C64CAST_DATA_DIR` at the checkout. Writes go through `transport.atomic_write_text`.

Schema 2 holds one 256-entry sidtable per measured SID, keyed `"1"`/`"2"` by socket number, or `"default"` for the single-measurement fallback, plus a `"device"` provenance block.

Each entry also carries `raw_signed_levels` — the per-code `[code, level]` pairs the ladder was folded from. This is written **additively under the same schema version**: `load_calibrated_table` only ever requires `sidtable`, so older files keep loading and new files stay readable by older code. A version bump would orphan every calibration on disk for no reader-visible gain. It is a distinct key from the `raw_levels` some files on disk carry — those hold two-reference `[code, p, q]` triples, a different measurement rather than a different encoding of this one.

It is there because a finished table is otherwise undiagnosable, and the failure modes are silent — a badly reconstructed ladder looks exactly like a good one. With the raw levels a suspect calibration can be re-examined, and alternative ladder constructions trialled, entirely offline. That is exactly how the volume-0 self-test was found and validated: the whole diagnosis, including the triangle-inequality violations above, came out of one already-captured file with no hardware attached. A rejected measurement keeps its raw levels for the same reason — the interesting failures are the rejected ones.

#### Quality metrics

`_ladder_metrics` reports `ladder_bits` (ENOB-style: the RMS distance between each of the 256 requested target levels and the level actually achieved, expressed as the equivalent uniform quantizer), `ladder_rms_err_frac`/`ladder_max_err_frac`, and three gap figures: `worst_gap_frac`, `worst_gap_from_zero_frac`, and `crossover_gap_frac`.

Gap *position* is the point of that last pair. The same hole is crossover distortion at the zero crossing and nearly inaudible out at full scale — on the two measured sockets the worst gaps are almost the same size (4.4% vs 4.9% of span) but sit at −0.06 and +0.98 from silence respectively, which is the whole difference.

`ladder_bits` is deliberately **scale-invariant** — a test asserts that a 10× louder capture yields an identical value. Any bit-count defined against the **capture noise floor** instead measures the recording rig rather than the DAC, and inverts the ranking it is meant to give: a quieter capture scores *more* bits, rating a chip degraded to roughly 4 bits above a working one, and an audibly hissing table best of all. On the scale-invariant figure the two sockets read 5.86 and 5.80 — comparable ladders, which is the honest answer. `capture_noise_floor` is recorded separately, under a name that says what it is. Don't compare `ladder_bits` against `effective_bits` in an older file; they measure different things.

At playback, `load_calibrated_table` picks the entry matching whichever socket is *currently* live-mapped to `$D400` — `_active_socket_at_d400` does a live `SID Addressing` / `SID Sockets Configuration` read. That is what stops a calibrated physical-chip table from being misapplied when an UltiSID core actually owns `$D400`. With no live backend, or when the file has no socket-keyed entries, it falls back to the `"default"` entry, or to the lone entry if there is exactly one.

Resolution: `resolve_dac_curve_for_backend(cfg, be=...)` maps `"auto"` to the applicable calibrated table if present, else `mahoney_ultisid` on the Ultimate, else `linear`. It yields to an explicit `digi_boost` by staying linear. `"calibrated"` forces the table and raises if it is absent.

When `"auto"` goes looking and finds no calibration on a **live** run (`be` is a reachable backend — not an offline `--doctor` pass, which reports separately and can't even confirm the identity key), it logs a helpful, actionable line so a missing calibration is never a silent fidelity downgrade: `info` on the Ultimate (the baked `mahoney_ultisid` table is a correct default; the line just points at `--calibrate-dac` for a socketed physical SID), `warning` on any other backend (the 4-bit `linear` fallback is a real downgrade). Curve resolution is the right place for that line because it is the only point that knows the identity key and the applicable table; there is no repo calibration location for `--doctor` to check instead.

#### How `--doctor` reports calibration

Three code paths, deliberately non-overlapping:

* `cli.build_stack` threads the already-probed `api` through, so **playback** resolution is precise.
* `doctor._probe_dac_calibration_status` — wired into `_probe_connectivity`, category `connectivity`, subject `"{name} (DAC calibration)"` — is equally precise for a live run.
* `doctor._validate_dac_curve_cfg` (category `audio`, always runs) only flags an unknown name or a `digi_boost` conflict. These are genuinely offline, hardware-identity-independent checks.

The "resolves to X" reporting lives in `doctor._validate_dac_curve_resolution`. `validate_load_result` calls it **after** `_probe_connectivity` (when `probe_u64`), and only for systems not already covered by a `"(DAC calibration)"` diagnostic from that live probe. So a live `--doctor` run reports calibration resolution exactly once, precisely, under `CONNECTIVITY`; the `audio` section's `dac_curve` line appears only for systems that got no live answer — `--skip-probe`, or a system whose connectivity probe failed.

The gating is what keeps the report self-consistent. Run the offline resolution unconditionally alongside the live one and a successfully-probed run picks up a redundant, sometimes *contradictory* `audio`-section line — and since `--doctor`'s AUDIO section is the first thing people read, it would disagree with CONNECTIVITY in exactly the place that matters.

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

A file written before the slot ring keeps loading (its `sidtable` is all a reader needs) but its table came from the two-reference scheme described above, which measured 4.8 dB *worse* than no calibration at all on the one chip it was compared on. Re-run `--calibrate-dac` once; it takes ≈50 s per socket.

### Host-DMA pitch compensation — why two of the three knobs default off

Three knobs, and understanding why two of them are off matters more than the knobs themselves.

**What they compensate for.** The host-DMA worker paces ring writes to wall-clock, so the write head W advances at `sample_rate`, while the NMI reader R advances only as fast as the 6510 actually services NMIs. Let video bus-halts steal ticks from R and W out-produces it, laps the ring after ≈26 s (an audible echo), and playback runs slow.

**`host_dma_servo` — default on.** A pure host-side PI controller (`_servo_period`) reads R once per chunk and stretches or shrinks the worker's sleep so the ring gap parks near half a ring. This is orthogonal to pitch and stays on.

**`nmi_rate_adaptive` and `pitch_mult_*` — default off / 1.0.** Playback pitch is `R / sample_rate`, and both of these force R back up toward `sample_rate`: the adaptive loop (`_nmi_rate_step`) shrinks the CIA #2 latch from a measured-R estimate, and the static per-mode `pitch_mult_*` multipliers do the same open-loop.

They are off because the **net pitch error** they would correct is already ≈0. Hardware measurement (2026-07-02, `scripts/diags/nmi_pitch_ab.py` — full-pipeline capture, pitch via log-spectrum cross-correlation against the source, robust to avfoundation's chunk-drops) puts it at **≈0** with the bitmap+digi fps cap, `VideoScene` frame dedup, and the REU-staged double-buffer in play. DAC-path mhires video, with no compensation at all, plays at +0.07 % on a near-static clip and −0.01 % on a high-motion one.

Read that as a *pitch* result and nothing more. It is tempo-blind, and it is not a claim that a host write costs the 6510 nothing. What a write actually costs was measured directly in 2026-08-05 by `scripts/diags/halt_shape_probe.py`, which counts stolen cycles with R itself: R advances once per serviced NMI, so the deficit in dR/dt under paced background writes — against the same measurement with the bus quiet — is the halt, counted in lost samples by the machine that lost them. Sweeping payload at a fixed write rate splits it into `lost_ticks_per_write = a + b·payload`, r² = 1.000 on both backends:

| | TeensyROM+ | Ultimate 64 |
|---|---|---|
| fixed cost `a` | ≈0 (−45 cycles, i.e. noise) | ≈0 (−34 cycles) |
| per-byte `b` | **1.055 cycles/byte** | **1.270 cycles/byte** |
| at the worker's 128 B × 91/s | 0.80 % of the sample stream | 1.15 % |

Three things follow. A host `DMAWRITE` really costs **1.06–1.27 cycles/byte**, so the REC DMA model's 1 cycle/byte is low and every halt figure inferred from it is an underestimate. Neither backend has a measurable fixed per-write cost, so subdividing a payload is close to free in cycles — the shipped 128-byte halt quantum costs what its bytes cost and nothing extra for the extra writes, which is what makes the command-rate budget, not the halt, its binding constraint. And the cost is **stationary** (`--hold` at the worker's operating point: 1.079..1.098 ticks/write over 150 s, drift +0.002/min), so it is not a candidate for anything that grows over a run.

Do not treat the two measurements as interchangeable. Under full production load — video writes on the same link, char mode, no `tempo_scale` compensation — R has been observed at 11655 against a 12032.1 nominal, a **3.1 % tick deficit**, alongside the ≈0 % pitch result above. Nothing here has established which of the servo's re-pacing, the fps caps or the pitch estimator's own limits reconciles those two numbers. The pitch A/B is what governs these knobs, because pitch is what they move; `halt_shape_probe.py` is what to run when the question is the halt.

Against that baseline, enabling either one only injects error:

* Static `pitch_mult_mhires = 1.015` overcorrects to **+1.36 % high**.
* The adaptive loop is worse. Its dR/dt estimator reads ≈12 % high — a torn DMA read-back of the `$C025/$C026` read pointer over REST — so it drives the latch the wrong way. One clip measured **−8.5 % slow**, and the error is content-dependent and non-deterministic.

So the DAC path runs at the nominal latch, dead-on, with the servo still centering the ring. Both knobs are kept for platforms that may still lose ticks (PAL at 50 fps, the lower-latency TeensyROM+ backend) — but the adaptive estimator's bias would need fixing first.

Unaffected: the U64's default video path uses the off-bus Ultimate Audio sampler, which never writes `$D418` and takes its pitch from `sampler_clock_hz`.

### `position_seconds()`

The audio-master clock: `(pushed - queued) / effective_rate`. The C64-side ring buffer adds ≈1 s of constant latency beyond this, which is harmless for relative sync.

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

**`UltimateAudioSampler`** is the scene-facing object, mirroring the slice of `AudioStreamer` that scenes actually call: `sample_rate`, `position_seconds`, `push_samples`, `get_recent_samples`, `stop`, `start_for_external_source` (an alias for `start()` so a `push_samples`-feeding caller can bring up either backend uniformly), an `analysis_sink` hook (fed the pre-DSP floats in `push_samples`, so `audio_source = "file"`'s reactive analyzer installs identically on either backend), plus no-op `set_pre_emphasis` / `mark_eof`, and `is_sampler=True`.

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

**Why no host-side check catches this.** Comparing the FPGA's observed rate against the *same* assumed clock agrees by construction — that self-check reports "< 0.3 %" whatever the true clock is. The lead telemetry cannot see the true rate either, since there is no read-back, and the sampler's end-of-sample/wrap IRQ is not DMA-readable. There is no host-only runtime signal to self-calibrate against; this is hardware-proven, see `scripts/diags/sampler_irq_clock_probe.py`.

**Why it ships as a constant, not a per-unit calibration.** The offset is a firmware/FPGA-derivation property — identical across U64 units on the same firmware, not chip-to-chip variation. So it is measured once and shipped as `SAMPLER_REF_CLOCK_DEFAULT = 6_160_000`, threaded into `[audio].sampler_clock_hz`, rather than stored per-unit the way `$D418` DAC calibration is.

`divider_for_rate`, `actual_rate_for_divider`, `program_channel`, and `UltimateAudioSampler` all take a `ref_clock`, so the programmed divider and the resample target shift together. Heard speed is `real_ref / assumed_ref` — the divider cancels, making the setting monotonic.

#### The measurement

`scripts/diags/sampler_av_align_calib.py` is definitive. At each interval it emits two tones into one captured stream, plus a border flash for visual A/V confirmation:

* a **SID tone**, clocked by the accurate C64 system crystal — a true wall-clock marker;
* a **sampler tone**, riding the FPGA clock.

Fitting each band's onset-time-vs-index slope and taking their **ratio** cancels the capture-side time compression. That compression is real and large: the avfoundation/Cam Link path drops samples under heavy host DMA load (here the sampler's REU-streaming writes; elsewhere bitmap re-uploads), uniformly compressing the recorded timeline.

Critically, its magnitude is **DMA-load-dependent**, not a fixed capture-clock property. SID reference markers fired at exact 5.000 s wall-clock landed at a captured factor of ≈0.90 under a light click-train load, but ≈0.77–0.87 under the sampler's streaming DMA. Any absolute-timing method is therefore unusable — which is why the pitch-based `sampler_clock_calib.py` falls back to pitch plus ear-tuning — while the per-run differential measures the factor and cancels it whatever its value.

Results on a U64-II:

* Nominal-driven run: ratio 0.9852 → 1.48 % slow → 6.157 MHz, r²≈0.9999 over 36 markers.
* Confirmation runs driven at the candidate converged to ≈6.16 MHz.
* A run at 6,160,000 showed residual drift of only **−1.3 ms per 5 s** — 17× better than nominal. Verdict `ALIGNED`.

Re-measure and bump `SAMPLER_REF_CLOCK_DEFAULT` after any firmware release that changes sampler timing; the diag prints the new value. Hardware or firmware that clocks the sampler correctly can set `[audio].sampler_clock_hz` back to 6.25 MHz.

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
* **`Expander`** — downward expander with **hysteresis**, which is what a hard noise gate cannot offer: a signal hovering at a single threshold toggles the gate rapidly, and the chatter is audible. The gate opens at `threshold_db` but only closes once the level falls `hysteresis_db` below it, and gain changes are attack/release-smoothed (fast open, slow close).
* **`Compressor`** — soft-knee feed-forward, attack/release-smoothed peak detector, static dB curve. The headline win: it's what lets quiet detail survive quantization. `makeup_db=None` (the default) auto-computes makeup as `-threshold_db * (1 - 1/ratio)` so a signal *at* the threshold exits near unity.
* **`Limiter`** — instant-attack peak detector + release-smoothed recovery + a final hard clip against intra-sample overshoot. Transparent below the ceiling.
* **`AGC`** — slow broadband gain for the mic path only (line/video audio is already peak-normalized upstream). **Known limitation, measured** (2026-06-12, Kaggle speech-noise set, `scripts/diags/dsp_noise.py`): being level-based, AGC cannot distinguish a −30 dB noise floor from −30 dB quiet speech. `noise_floor_db` is the only "this is just noise" signal and it is *absolute*, so setting it below the real floor means sustained noise gets boosted toward target during long pauses. A VAD (or a tuned expander ahead of it) is the real fix; for noisy mics prefer the chatter-free expander, or raise `noise_floor_db` and accept that genuinely quiet speech won't be lifted.

**Streaming contract (the invariant to preserve when editing).** Every processor is stateful and fed arbitrary-sized blocks from realtime callbacks, so processing a signal split across blocks **must** match processing it in one shot — the recursive smoothers carry envelope/gain state across `process()` calls. `tests/test_dsp.py` asserts this continuity per processor. Note `AGC` deliberately smooths per-*sample* rather than per-block for exactly this reason: a per-block gain trajectory would depend on the callback block size and break the property.

**Performance.** `_ar_envelope` (the attack/release follower) and the expander/AGC loops are genuinely recursive — per-sample state with an attack≠release branch — so they use Python loops rather than a vectorized form (no scipy in the dep set). At DAC sample rates with realtime mic blocks (hundreds of samples) this is negligible; the offline video pre-encode runs it once over the whole track (≈1 s for a 2.5-min clip), acceptable for one-time scene setup.

## `audio_features.py` — audio-input music features (reactive visuals from live input)

The **second producer** of `modulation.MusicModulation`, alongside [`music_features.SidFeatureStream`](sid.md#waveformpy--sidemupy--sid_host_emupy--sid-oscilloscope-scene). The SID stream reads envelope/gate/frequency out of a host-side 6502 running the same tune the chip plays; this one analyzes **actual audio samples**, so a generative scene reacts to music c64cast has no symbolic knowledge of — an instrument or mixer feed through an audio interface, a phone into an iRig, a mic in the room.

Everything downstream of `MusicModulation` was already source-agnostic (`generators.py`, the effect chain, `wled_sync.py`), so this module *is* the whole feature: an analyzer, a ring the audio path pushes into, and a poll thread between them. `MicAudioSource.features()` returns that analyzer's snapshot.

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
* **`voice_freqs` / `voice_gates`** — zeros/False. They are SID-specific with no audio-input analog, so the two generators that read them (moire, kaleidoscope) fall back to their base geometry and react through level/onset/beat_phase/bands like everything else.

### `modulation.TempoEstimator` — one tempo implementation, two producers

Lifted verbatim (logic and constants) out of `SidFeatureStream`, which was the only producer until this module needed exactly the same math: EMA the inter-onset interval, fold near-simultaneous onsets into one beat, re-anchor across long rests, clamp to a plausible BPM band, and integrate `bpm/60` into `beat_phase` so a jittery estimate never causes a phase discontinuity. It lives in `modulation.py` because that module is stdlib-only by design — the one place both the py65-backed SID stream and the numpy-backed audio analyzer can import without dragging in each other's deps. `SidFeatureStream` delegates to it, and `tests/test_music_features.py` guards the equivalence.

`MusicModulation` also carries **`bands: tuple[float, ...] = ()`** plus `bass`/`mid`/`treble` properties that fold whatever band count the analyzer was configured for into thirds. It defaults to empty and stays empty on the SID path, which is what keeps the SID look identical whether or not bands exist: `generators._reactive_value`'s bass term and `_reactive_hue_offset`'s treble term both evaluate to exactly 0.0 there.

**Why bass→brightness and treble→hue** (and not saturation): the 16-color quantizer handles a desaturated hue badly — it lands in the grays — so the spectral split rides the two axes that survive quantization. A kick punches the value, a hi-hat pattern shimmers the hue, and they read as different events.

### The stream + tap

`AnalysisTap` is a small lock-protected mono float ring with the same wrap arithmetic as `AudioStreamer._push_to_tap`/`get_recent_samples` — lifted rather than shared, because the writer here is a realtime callback on a streamer that may not exist yet (the tap outlives any single `start_mic`). `push()` is nothing but a couple of slice assignments under a short-lived lock.

`AudioFeatureStream` is the `PollThread` between them, modeled directly on `SidFeatureStream`: `start()` / `stop()` / `features()`, a `_lock` around the snapshot, `features()` returning `None` before the first tick, and `_process_tick` split out so tests drive it over a hand-filled tap with no thread. The FFT runs outside the lock; only the snapshot swap takes it.

### Config + wiring

`[audio_features]` (`config.AudioFeaturesCfg`): `bands` (8), `onset_sensitivity` (1.0), `poll_hz` (60.0), `fft_size` (1024), `listen_sample_rate` (44100). `onset_sensitivity` divides the flux threshold and is the one knob worth turning in practice — dense, heavily-compressed material reads as continuous transients at high values; sparse material needs a push. `listen_sample_rate` is the `audio_source = "listen"` capture rate (ignored by `mic`, which analyzes at the DAC rate).

`MicAudioSource` gained `reactive` (default True), `listen_only` (default False) + `features_cfg`. `setup()` installs the tap **before** `start_mic`/`start_listen` so the first callbacks already reach the analyzer; `teardown()` clears `analysis_sink` **before** `audio.stop()` so no callback can push into a tap whose thread is going away. A startup failure degrades to non-reactive with the audio intact — the same contract as `SidFileAudioSource.setup`.

The analyzer taps the capture callback, so `reactive = true` with `audio_source = "mic"`/`"listen"` needs `[audio].enabled` (the shared streamer owns the capture); `_validate_generative` **warns** rather than failing (`reactive` defaults True, so someone who only wanted silent generative visuals shouldn't have to opt out explicitly). Listen additionally warns on `reactive = false`, since a listen source exists only to drive the visuals — with reactivity off it opens nothing.

Demo config: `c64cast/examples/audio-reactive-input.toml`.

## `audio_source.py` — AudioFileSource (audio-file reactive source)

The third `MusicModulation` producer's *plumbing*, and what makes `c64cast tune.mp3` a first-class reactive source (`audio_source = "file"`). Where `MicAudioSource` analyzes a live capture, `AudioFileSource` decodes an **audio file** (mp3/wav/flac/… via PyAV) and plays it while the same analyzer reacts to it — the "full-track sampled streaming" the `AudioSource` protocol always anticipated.

**Mechanism.** A background decode thread demuxes + resamples the file to the audio object's mono int16 rate and feeds its `push_samples` — exactly as `AVFileSource` feeds a video's audio. `push_samples` both encodes the samples for the C64 *and* forwards them (pre-DSP) to `analysis_sink`, so the identical `AudioFeatureAnalyzer` the mic path uses drives the visuals off the decoded track. Playback is real-time-paced by `push_samples`' queue-full backpressure, so the decode thread tracks consumption without a separate clock. The analyzer opens at the audio object's sample rate (what the C64 actually plays, like the mic path — *not* the 44.1 kHz listen rate).

**Backend: sampler by default, DAC on demand (the audio-quality fix, 2026-07-24).** The audio object is whichever backend `config.build_scene` resolves — the same `resolve_audio_backend` a video scene uses. On a sampler-capable U64 with `[audio].backend` = auto/sampler, that is a **per-scene off-bus `UltimateAudioSampler`** (16-bit PCM straight from REU); otherwise the shared 4-bit `$D418` `AudioStreamer`. This exists because the DAC path is **audibly unusable for music on the U64, independent of display mode**: HW-measured 2026-07-24, a decoded track through the DAC is a *louder-than-the-signal* broadband static on both `mhires` and `mcm` (spectral flatness ≈0.15, HF hash ≈0.09), where the sampler is clean (flatness ≈0.009, HF ≈0.011). Two causes stack: (1) the default `dac_curve = auto → mahoney_ultisid` writes the full `$D418` byte through a 256-entry table calibrated for the U64's *UltiSID core* — mismatched to whatever chip actually answers `$D418` on a given unit, it scrambles sample levels into loud hash (linear 4-bit measured ≈5× cleaner but quiet and lo-fi); (2) 4-bit @ 12 kHz is fundamentally lo-fi for loud full-bandwidth content (HF aliases past the 6 kHz Nyquist). The sampler sidesteps both — no `$D418`/NMI/DSP/4-bit — and is immune to the CPU-freeze that host-DMA RAM writes inflict on the NMI DAC. Both backends satisfy the same scene-facing contract (`sample_rate`/`push_samples`/`position_seconds`/`stop`/`analysis_sink`), so `AudioFileSource` drives either polymorphically; `_is_sampler` (from the `is_sampler` duck-type) only selects the setup ordering. `[audio].backend = "dac"` still forces the DAC (also the only path on TeensyROM, and for mic/webcam everywhere).

**Sampler bring-up ordering.** The sampler's `start()` blocks up to ~2 s collecting a prebuffer from `push_samples`, so `setup()` starts the decode thread **first** for the sampler (it enqueues before the ring is gated, so the prebuffer fills promptly), then calls `start_for_external_source()` (a thin alias for `start()`). The DAC's `start_for_external_source` just arms its worker, so the DAC path keeps the original start-then-decode order. The sampler grew an `analysis_sink` hook mirroring `AudioStreamer`'s (fed the pre-DSP floats in `push_samples`) so the reactive analyzer installs identically on either backend.

**Why a focused source, not `AVFileSource`.** `AVFileSource` hard-requires `container.streams.video[0]` (an audio file has none), and it carries the whole video/atempo/transport apparatus. `AudioFileSource` is a small decode loop — open, resample, push — plus the reactive-analyzer install lifted from `MicAudioSource`. It reuses `video._av_open`/`_ensure_pyav` for the PyAV bring-up.

**Scene sizing.** `AudioFileSource` reads the container `duration_s` at construction (parity with `SidFileAudioSource`'s init-time validate); `config.build_scene` sizes the scene to the track when the cfg leaves `duration_s` unset, so `c64cast tune.mp3` plays the whole song then advances (or loops in single-scene mode). An explicit `duration_s`/`-t` still wins. A dir/glob spec random-picks one file per play, like the SID path.

**Wiring + gating.** In `build_scene`'s generative branch, `audio_source = "file"` builds the source where `"mic"` does, gated on `scene_audio is not None` (so it inherits the ensemble live-audio suppression and falls back to `NullAudioSource` when `[audio]` is off / `audio = false`), then resolves the backend and — when it lands on the sampler — swaps `scene_audio` for a fresh `UltimateAudioSampler` before constructing the source. That resolved object is *also* passed as the `SourceScene`'s base `.audio` (the `set_pre_emphasis` hook + overlay sample tap), so `self.audio` and the source's audio object stay the same instance. `_validate_generative` **requires** `file` (no default dir), resolves the spec at load time, and **warns** (doesn't fail) when audio is off. A startup/decode failure degrades to non-reactive with the visual intact, the same contract as `MicAudioSource`/`SidFileAudioSource`.

*Provisioning.* `doctor._wants_sampler` counts a `generative` + `audio_source = "file"` scene (not just `video`), so a sampler-routed file scene triggers the live FPGA-map enable + Sampler-mixer unmute + REU 16 MB provisioning — without it the sampler ring would play silently.

*Frame rate (the crash lesson).* A sampler video scene uncaps its bitmap frame-push to the system rate because `VideoScene` **dedups** — a 24 fps clip polled at 60 pushes only 24 genuinely-new frames/s. A **generative** source has no such redundancy: it renders a fresh frame every tick, so uncapping to 60 pushes 60 real `mhires` frames/s of REU bank-swap traffic, which *starves the sampler's own REU writes* (the ring's write-ahead lead collapsed to ~322 B → audible static) **and** overloads the bus (C64-side visual crash — HW 2026-07-24). So a sampler-routed file scene keeps the **muted-bitmap 30/25 cap** (`_frame_push_default_fps` with `has_digitized_audio=False`, *no* `off_bus_audio` uncap): the audio is off-bus, but the video frame push must stay bounded. At 30 fps the lead held ≈46 KB and the audio was clean. The DAC file path keeps its 20 fps bitmap cap.

Quick playback: `quickcast` maps an audio extension (`AUDIO_EXTS`, shared with `config.py`) to a `generative` + `audio_source = "file"` scene (a `plasma` visual on an `mcm` char display). On a sampler-capable U64 that track plays through the off-bus 16-bit sampler; the `mcm` char display keeps the (small, 1 KB) frame pushes cheap regardless.
