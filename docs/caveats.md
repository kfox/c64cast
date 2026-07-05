# Caveats

Surprises, footguns, and design choices that look weird until you know
why. Read this before you spend an evening debugging "it's almost
working, but…". For end-user options see [usage.md](usage.md); for the
architecture overview see [CLAUDE.md](../CLAUDE.md).

## Audio is intentionally lo-fi (the 4-bit `$D418` DAC)

The SID DAC streaming path writes 4-bit samples (0-15) to the SID volume
nibble at $D418 at ~10.5 kHz. That's an objectively bad audio format — but
it's the format a real C64 plays back. You can raise `[audio]
sample_rate` in config, but the C64-side NMI is sized for that rate and
nothing in the pipeline resamples; a different rate just plays at the
wrong pitch.

There is no master volume, no SID filter, no anti-aliasing. The noise
gate (`noise_gate`) and pre-DAC gain (`mic_sensitivity`) are the only
shaping knobs. Hum and hiss are part of the aesthetic.

## High-fidelity video audio: the Ultimate Audio FPGA sampler (U64)

On the Ultimate 64 the lo-fi DAC above is **not** the default for *video*
playback. The U64 firmware exposes an "Ultimate Audio" FPGA PCM sampler at
`$DF20-$DFFF` that plays 8/16-bit PCM (up to 48 kHz) **directly out of REU
SDRAM** — the FPGA fetches and converts the samples itself, with **zero**
SID / `$D418` / NMI / CPU / turbo involvement. So it sidesteps every bus-halt
and badline problem the 4-bit DAC fights (it kept the DAC capped at ~16 kHz and
20 fps), and it sounds like an actual sound card instead of a digi-player.

`[audio].backend` selects it: `"auto"` (default) uses the sampler on a capable
U64 and falls back to the 4-bit DAC otherwise; `"dac"` forces the lo-fi DAC
(the only path on the TeensyROM, which has no FPGA sampler); `"sampler"` forces
it and warns + falls back to the DAC if it isn't available. `sampler_sample_rate`
(default 44100) and `sampler_bits` (8 or 16, default 16) tune quality. Mic and
webcam audio always use the 4-bit DAC.

Implementation (`c64cast/sampler.py`): a **streaming REU ring**. Channel 0 is
programmed as an A↔B loop over a region of REU; a host writer thread REUWRITEs
decoded PCM ahead of a *wall-clock-computed* read head and wraps. The FPGA
sample clock is crystal-exact, so the read position is computed (never read
back) and the whole thing is open-loop and drift-free — no servo, no governor,
no NMI. The sample rate is the FPGA's exact `6.25 MHz / divider`, a constant
<0.5 % offset from the nominal request (inaudible, and drift-free because A/V
both ride the same clock). The ring lives in REU SDRAM, so a sampler run also
provisions the REU (16 MB) — which makes overlay-free bitmap video resolve to
the tear-free REU bank-swap path; the sampler installs no `$0314` IRQ, so the
two coexist with no contention.

Prerequisites on the U64 (auto-provisioned live + restored at teardown when
missing, or set them yourself in F2): **C64 and Cartridge Settings → Map Ultimate
Audio $DF20-DFFF = Enabled**, and **Audio Mixer → Vol Sampler L / Vol Sampler R**
audible (0 dB, not OFF). `c64cast --doctor` reports the sampler's state.

## Forced-DAC bitmap video plays ~12% slow (tempo compensation)

Force `[audio].backend = "dac"` on a **bitmap** display mode (hires / hires_edges
/ mhires) — or run the always-DAC TeensyROM+ — and video+audio play **~12% slow
at correct pitch**. The default U64 video path (the off-bus sampler above) and
the char modes (petscii/mcm) are unaffected.

Cause: video is slaved to the audio **drain clock** (`AudioStreamer.
position_seconds` → `VideoScene._clock_s`). In bitmap mode the audio worker
shares the single socket-DMA link with heavy REU bank-swap bitmap writes; the
host-DMA servo reads the ring pointer biased under that load and throttles the
worker ~12% (`clock/wall` ≈ 0.88 mhires vs ≈1.0 petscii, servo-tuning-
independent). The `$D418` **output** rate stays ≈ `sample_rate` (a pure 1000 Hz
tone reads ~993 Hz → pitch correct), so it's a **pitch-preserving time stretch**:
the ring under-fills and the NMI re-reads/duplicates samples at the right
per-sample rate. No host-side servo tuning fixes both speed *and* smoothness
(servo on = smooth but slow; open-loop = correct tempo but skips; REU-pump =
wobbly — all confirmed by ear).

Fix: because the stretch is pitch-preserving, **pre-compress the content in the
time domain by the inverse factor** so it nets to real time. `[audio].
dac_bitmap_tempo_hires` / `dac_bitmap_tempo_mhires` (defaults **0.89 hires /
0.88 mhires**, the measured U64-II NTSC speed fractions `s`) drive it: for the gated bitmap+DAC path,
`AVFileSource` time-compresses the audio pitch-preserving by `1/s` via an
`atempo` filter graph and multiplies each video PTS by `s`. The existing
drain-clock A/V sync (which reads ~`s`) then lands both content streams at real
time, in sync, pitch intact. `clock/wall` telemetry still reads ~`s` **by design**
(it gauges the drain rate; the compensation makes *content* real-time, not the
drain clock). Set the field to `1.0` to disable. Other platforms (U64+PAL, U2P,
TR+ PAL/NTSC) have different `s` — measure per platform with
`scripts/diags/mhires_tempo_clock_ab.py`. This is orthogonal to the
`[audio].pitch_mult_*` NMI-rate knobs (which correct *pitch*, not tempo). See the
`video.py` tempo-compensation note in [architecture.md](architecture.md).

## SID playback uses a C64-side player PRG, not `runners:sidplay`

`WaveformScene` deliberately avoids the U64 firmware's
`POST /v1/runners:sidplay` endpoint: firmware 3.14d hijacks the HDMI
scaler with its own "ULTIMATE C-64 SID PLAYER" UI while that runner is
active, which covers c64cast's oscilloscope visualization. There's no
documented way to suppress the UI.

Instead, `api.run_sid_player()` DMAs the SID payload to its declared
load address + a small hand-encoded 6502 player (plus a SHIFT-driven
re-INIT stub), then POSTs a matching `10 SYS <player_base>` BASIC stub
via `runners:run_prg`. The player and stub are **relocated per-tune** by
`_choose_player_layout` — the default location is `$C300` (so the BASIC
stub is `SYS 49920`), but a tune whose payload would overlap gets the
bundle relocated to free RAM the tune doesn't touch (the waveform scene
passes a footprint and picks the largest hole the tune never writes; the
generic path places the bundle just past the payload), with the SYS
argument rebuilt to match. The real 6510 sets the CPU port (`$01`) bank
config around each call (see below), calls
INIT once, installs an IRQ that calls PLAY then chains to kernal
`$EA31` (so keyboard scan at `$028D` + cursor-blink suppression
survive), and then spins forever in a tight `JMP *`. The player
intentionally never returns to BASIC: most SID INIT routines clobber
zero-page locations BASIC depends on, so an RTS would land back in
the interpreter with corrupted state and print `?SYNTAX ERROR` on
screen. The kernal IRQ keeps firing regardless, so PLAY runs at the
system rate and `$028D` keeps updating for the keyboard poller.
Audio still comes from the real SID chip.

### Per-call memory banking (`$01`)

The player banks the 6510 CPU port at `$0001` **per call**, matching the
U64's own player: it rests at `$37`, switches to the right bank around
`JSR init`, restores `$37`, then switches again around `JSR play` and
restores `$37` before chaining to the kernal IRQ tail. `_init_bank_for`
and `_play_bank_for` in [api.py](../c64cast/api.py) pick each value
independently (init-bank from the load-end page, play-bank from the
play-addr page):

* **`$37` (default + resting)** — BASIC ROM + KERNAL ROM + I/O all mapped.
  Used for ordinary tunes, including ones that deliberately *read* BASIC
  ROM as a data table (e.g. Galway's Comic Bakery), and is the environment
  tunes assume *between* calls.
* **`$36`** — BASIC ROM banked **out** (`LORAM=0`), KERNAL + I/O kept.
  Used for the call (init and/or play) whose entry point lives **under
  BASIC ROM** (`$A000-$BFFF`). Without it, `JSR init` would execute BASIC
  ROM bytes instead of the tune — e.g. Matt Gray's Hyperion 2 (load/init
  `$AE2A`) lands on the ROM's SYNTAX-error stub at `$AF08` and prints
  `?SYNTAX ERROR IN 10` (the `SYS` line), with no music. KERNAL stays
  mapped so the `$EA31` IRQ chain still works.

This replaces two earlier dead ends: an *unconditional* `$36` (broke
Comic Bakery and Last Ninja 2), and a *per-tune-but-permanent* `$36`
(set once, never restored — crashed tunes like Election ~24 s in because
they assume the `$37` resting environment between PLAY calls, and the
`$36`/`$37` choice for "data under ROM, entry points in RAM" tunes proved
undecidable offline). Banking per call sidesteps both. The re-INIT stub
(SHIFT subtune cycling) carries the same per-call banking.

Known limitations:

* **PSID only.** RSIDs expect their own raster IRQ and don't cooperate
  with kernal chaining — they're refused with a clear error.
* **`load_addr` must be ≥ `$0820`.** The BASIC SYS stub occupies
  `$0801-$0811`; SIDs that load any lower are refused (the threshold
  is rounded up to `$0820` for safety margin).
* **`play_addr` must be non-zero.** Tunes whose INIT installs their own
  IRQ instead of exposing a PLAY entrypoint are refused.
* **No code/data under KERNAL ROM (`$E000-$FFFF`).** The player keeps
  KERNAL mapped to chain its `$EA31` IRQ tail, so it can't bank KERNAL
  out to expose RAM there — such tunes are refused. (Tunes under *BASIC*
  ROM at `$A000-$BFFF` **are** supported; see "Per-tune memory banking"
  above.)
* **PAL/NTSC speed flag is ignored.** The kernal's default CIA #1 Timer
  A latch is left alone, so PAL tunes on an NTSC kernal play ~20% fast
  (and vice versa). A future enhancement could reprogram the timer
  based on the SID's `speed_flags`.

`WaveformScene`'s oscilloscope can't read SID register state back from
the U64 — the FPGA SID is faithful to real hardware, so `$D400-$D418`
is write-only and reads return open-bus zeros. The Socket DMA protocol
has no general-memory-read opcode either. So
[sid_host_emu.py](../c64cast/sid_host_emu.py) runs the same SID file
in parallel on a host-side [py65](https://github.com/mnaberez/py65)
6502 emulator, trapping writes to `$D400-$D418` into a 25-byte shadow
that the render thread consumes. Audio still comes from the real SID
on the U64; the host emulator's audio (if any — most SID PLAYs only
write `$D4xx`) is discarded. The two run at the system video rate (60
NTSC / 50 PAL) with no drift correction — one tick of skew is
invisible in an oscilloscope view. The PSID validation above is
shared, so if `run_sid_player` refuses a tune, `SidHostEmu` refuses
the same tune with the same error.

The player MC defaults to `$C300` because [audio.py](../c64cast/audio.py)
owns `$C000-$C2FF` (NMI DAC at `$C020`, REU pump at `$C100`, REU mic
tracker at `$C200`); the relocation picker refuses any layout that would
overlap that region. `WaveformScene.setup()` calls `audio.stop()` before
SID setup so the NMI handler is silent during playback, but the bytes
remain installed for any later scene that re-arms audio.

### Multi-SID (2SID/3SID) tunes: split scope, best-effort U64 audio

A multi-SID PSID writes to extra SID chips at fixed `$Dxxx` addresses
declared in its v3/v4 header (`secondSIDAddress` `$7A`, `thirdSIDAddress`
`$7B`; each byte `b` → base `$D000 | b<<4`). `WaveformScene` auto-detects the
count (`sid_host_emu.detect_sid_addresses`, with an `_<N>SID.sid` filename
fallback when the header understates it) and shows a **split scope** — one
side-by-side window per chip in each voice row. The single host `SidHostEmu`
shadows every chip's register bank (the one 6502 already writes them all), so
the display is always correct regardless of hardware.

**Audio is best-effort on the U64 and unavailable elsewhere.** The tune's
writes only make sound where the U64 has a SID mapped to that exact address,
so `_apply_sid_hw_config` maps the U64's UltiSID cores (and sockets) to the
tune's own addresses before the player's INIT runs
([asid_sidmap.plan_sid_map_for_addresses](../c64cast/asid_sidmap.py)). The
firmware exposes ≤2 sockets (`$D400`/`$D420`) + 2 UltiSID cores sharing one
range split (`1/2` → `$40`-aligned, `1/4` → `$80`-aligned; stride `$20`), so
consecutive layouts (`$D400/$D420/$D440`) and two-page layouts
(`$D400`+`$D500`) realize exactly; a scattered set needing three core windows
(`$D400`+`$DE00`+`$DF00`) can't, and falls back to the canonical
`plan_sid_map` layout (some chips silent — the scope stays correct). The prior
config is snapshotted and restored on teardown ([sid_hw_config.py](../c64cast/sid_hw_config.py)).
Backends without a SID config API (TeensyROM) skip this: every chip's scope
still renders; only `$D400` is audible. Single-SID tunes never touch the
config (one window, byte-identical to before). Verified on U64-II hardware:
`Enchanted_Forest_3SID.sid` → 3 windows, all 9 voices audible as each chip
enters.

### ASID buffered ring player (cycle-accurate multispeed)

`AsidScene`'s default *coalesced* path folds incoming ASID register frames into
per-chip shadows and flushes one `$D400-$D418` block write per chip at ≤60 Hz
(host socket DMA). That's fine for single-speed tunes but **drops intermediate
frames** on multispeed content (`0x31` up to 16×, or a small `frame_delta_us`
pushes frames far faster than 60 Hz): arpeggios, fast vibrato, and
gate-off→gate-on hard restarts get mangled, and every flush is a bus-halting,
wall-clock-jittered burst.

The *buffered* path (`asid_buffered_player`, default `auto`) fixes this on the
U64 by moving frame consumption onto the C64. The host serializes each frame
into a fixed-size slot and REUWRITEs it (bus-clean) into a REU ring ahead of a
**computed** read head; a 6502 player fired by CIA #1 Timer A at the ASID
cadence pops one slot per tick and applies the writes honoring the `0x30`
write-order + inter-write waits — no frames dropped, decoupled from host jitter.
It is the open-loop producer-ahead-of-read-head pattern the FPGA sampler uses
(the C64 crystal is exact, so no servo and **no C64→host reads during
playback** — it obeys the "don't rapid-poll the U64 during capture" rule) with
an IRQ ring consumer modeled on the REU audio pump. `AsidScene` runs no `$D418`
DAC, so the whole `$C000` page and the REU are free for it. See
[asid_player.py](../c64cast/asid_player.py).

**U64 only.** It needs a bus-clean `reu_write` (`profile.supports_reu`).
`auto` engages it where an REU exists and stays coalesced otherwise; `on` forces
it (warns + falls back on a no-REU backend); `off` always coalesces. TeensyROM /
any no-REU backend keeps the coalesced path unchanged (and its display is never
blanked). A buffered run folds the ASID ring into the REU auto-provisioner
(`doctor._wants_reu`), so the REU is enabled + sized like the sampler's.

**v1 limitations (documented, not over-engineered):**

* **Frame-fit ceiling.** The handler's per-frame cost (per-op overhead + `0x30`
  waits, summed across all chips) must fit the frame period. Realistic content
  fits — it's how the tune runs natively — but a pathological 8-SID × 16× frame
  can overrun and queue ticks, an inherent limit like the NMI DAC cycle budget.
* **Coarse cycle delay.** The on-C64 busy-wait approximates each `0x30`
  `wait_cycles` within a few cycles (~`DELAY_CYCLES_PER_UNIT` per unit) — far
  better than dropped/instant, a refinement target if it ever matters audibly.
* **Frame grouping** assumes chip 0 (`0x4E`) is written every frame in
  multi-SID (the emit boundary); single-SID is unaffected. Costs one frame of
  constant latency.
* **Real-time cadence, open-loop lead.** The ASID host feeds frames in real time
  at exactly the consume cadence, so the ring's write-ahead lead can never *grow*
  past the startup prebuffer (unlike the FPGA sampler, whose demuxer races ahead
  of real time). The prebuffer is therefore seeded to the full lead for maximum
  jitter cushion; a genuine producer stall pads a **hold** (the SID holds its
  last state — graceful, no echo). Two consequences learned on hardware: the
  player **arms lazily** — the read-head clock + `$0314` swap wait for the
  prebuffer, or the read head runs away before the host starts streaming and
  every real frame lands in an already-consumed slot; and a `0x31` speed message
  is honored **before** arming, or the player would arm at the wrong (initial
  video-rate) cadence and decimate the tune to that rate. HW-verified on the
  U64-II: at 2× multispeed the buffered audio modulates at exactly 2× the
  coalesced path's rate (spectral-flux measured).

### TeensyROM: pure-DMA `$0314` vector-swap launch (no `run_prg`)

The host-side orchestration above (parse / layout / build / divider
auto-tune / subtune re-INIT) is backend-agnostic and shared via
`_SidPlayerBackend` in [api.py](../c64cast/api.py); only the **kick** —
how control reaches the player — differs per backend, behind the abstract
`_launch_sid_player`. The Ultimate POSTs the `SYS` stub to `run_prg`
(a synchronous soft reset that preserves RAM, then RUNs). The TeensyROM
has no synchronous run-PRG: `LaunchFile` resets the C64 and boots
**asynchronously**, and its timing-sensitive fast-LOAD is corrupted by the
badline-gated DMA reads of a concurrent `$028D` keyboard poll (and the
scope's bitmap bring-up raced the still-completing boot). Working around
the boot meant a fixed boot settle, a bus-silent launch lock, a `$C000`
trampoline + pre-uploaded `SYS` stub, and a verify-during-boot read — a
pile of fragile boot-race hacks.

So the TR doesn't boot at all. After cycle-clean bring-up the C64 runs the
IRQ-enabled BASIC clear-loop with the **stock kernal IRQ chaining through
`$0314`**, so the player is started exactly like a subtune cue
(`cue_song_reinit`): DMA the payload + player MC + re-INIT stub, then
atomically DMA-swap `$0314/$0315` to the re-INIT stub. The next kernal IRQ
runs the stub once — `JSR init` (banking `$01` per-call), restore `$D418`,
install `$0314` → the player's PLAY handler, `JMP $EA31` — and every
subsequent IRQ runs PLAY. The clear-loop the IRQ returns to keeps looping
harmlessly underneath; the player MC's own `SEI…JMP *` entry is never used
on this path, only its PLAY-handler tail. **No reset, no boot, no fast-LOAD
window to corrupt** — the whole class of boot-race workarounds is deleted,
and the display the caller set up survives the launch.

That last property is what makes the oscilloscope correct: a SID scene's
job is to show the waveforms *during* playback, not just play a tune. So
`run_sid_player(defer_audio=True)` loads the player **silent** (no `$0314`
swap yet); `WaveformScene` paints the hires scope (`_setup_hires`); then
`begin_sid_audio()` fires the `$0314` swap that actually starts INIT/PLAY —
the scope is on screen before the first note. (On the Ultimate, `run_prg`
re-inits VIC to text mode, so the bitmap is asserted *after* the player as
it always was, and `begin_sid_audio()` is a no-op there — the gap is one
frame.) The scene anchors its host-emu scope clock to `sid_audio_start_time()`,
which each backend records at the instant audio actually started.

The vector-swap launch requires the IRQ-enabled idle, so it's gated on
`profile.supports_read` (cycle-clean fw v0.7.2.5+ — ReadC64Mem and the
cycle-clean DMA shipped together). On older firmware the spin-stub idle
masks IRQs, so the swap would never fire — `run_sid_player` raises
`BackendCapabilityError` rather than play silently. (The TR also has no
REUWRITE opcode, so [cli.py](../c64cast/cli.py) coerces any `use_reu_pump`
/ explicit `use_reu_staged = true` opt-in off on a no-REU backend, routing
audio through the host-DMA NMI DAC and video through host-DMA; `--doctor`
reports the same.)

## Char ROM substitution

`[preview] charset_path` points at the C64 character ROM
(`characters.901225-01.bin`, 4 KB). The preview window and the
recording path both use it to render screen-code bytes back to actual
8×8 pixel cells.

If the file is missing, `framebuffer.py` falls back to a built-in
**8×8 ASCII font** — text is still readable but PETSCII line-art,
shaded blocks, and inverse video look wrong (they render as the
corresponding ASCII control codes' glyphs, which is to say "garbage").
For an accurate preview, drop a real CHARGEN dump in
`assets/roms/characters.901225-01.bin`; see
[assets/roms/README.md](../assets/roms/README.md) for sources.

## Ultimate 64 firmware version

This project is developed against U64 firmware 3.x (3.14d/3.14e on the
test hardware). Two transports are in play:

* **Socket DMA (TCP port 64)** carries every memory write — opcode
  `0xFF06 DMAWRITE`. Must be enabled in U64 settings (F2 → Network
  Settings → Ultimate DMA Service → Enabled) before `c64cast` will
  start; the CLI prints an actionable error otherwise.
* **REST** carries the operations that have no DMA equivalent:
  * `GET /v1/machine:readmem` — keyboard poller, waveform scene,
    U64-ping overlay
  * `PUT /v1/machine:reset` — between scenes
  * `POST /v1/runners:run_prg` — BASIC clear-and-loop at startup/resume,
    plus the `SYS 49920` stub that kicks the SID player
  * `GET /` — startup reachability probe

Older firmware may rename or omit endpoints; newer firmware sometimes
tightens parameter validation. If a previously-working setup starts
500ing, run `--skip-probe` to bypass the reachability check and inspect
the request bodies (`-vv` enables debug logging).

`AudioStreamer` **shares** the render path's `Ultimate64API` instance
rather than opening its own. The U64 DMA service is single-connection
only: a second concurrent TCP accept on port 64 succeeds, but its
IDENTIFY round-trip never gets a reply, and the first connection
continues to block subsequent ones for a few seconds after it closes.
Sharing the API instance is safe because `SocketDMAClient` serializes
every command on the wire via an internal lock, and the combined write
rate (audio ~8/sec + render ~30-60/sec) sits well under the ~200/sec
DMA ceiling.

## Socket DMA replaced HTTP for writes

The U64's REST server caps sustained writes at ~50-70/sec because of a
combination of `Connection: close` (every PUT pays a fresh TCP
handshake) and server-side request serialization — see the historical
section below for the full measurements. The fix landed in 2026-05: the
project now sends every memory write over the **Ultimate DMA Service**
on TCP port 64 (a persistent socket protocol; opcode `0xFF06`).

Measured impact on U64 Elite II + firmware 3.x + wired LAN:

| Transport       | Per-write latency (avg / p50 / p95) | Sustained writes/sec |
|-----------------|-------------------------------------|----------------------|
| HTTP (was)      | 14.0 ms / 14.8 ms / 19.9 ms         | ~71/s                |
| Socket DMA (is) | 5.3 ms / 5.0 ms / 6.8 ms            | ~200/s               |

The persistent socket eliminates the per-write TCP handshake. The DMA
service is single-connection only ([socket_dma.cc](https://github.com/GideonZ/1541ultimate/blob/master/software/network/socket_dma.cc)
accepts one connection at a time), so video and audio paths share a
single `Ultimate64API` instance and let `SocketDMAClient`'s per-command
mutex serialize them on the wire. `Ultimate64API.flush()` is
implemented as a trailing IDENTIFY round-trip — when it returns, every
prior DMA command on the socket has been drained by the server.

REST is still used for the operations that have no DMA equivalent (see
the firmware section above): `readmem`, `reset`, `run_prg`, `sidplay`,
the startup probe. These are low-rate and one-shot, so the HTTP wall
doesn't apply to them.

The CLI no longer exposes `--async-writes` / `--queue-depth` — they
were HTTP-pipeline knobs and have no meaning under DMA. The TCP send
buffer is the queue.

## U64 HTTP throughput wall: ~50-70 writes/sec

**Resolved 2026-05** by the Socket DMA migration above; this section is
kept as background on *why* we moved off REST for writes.

The U64's REST server has two firmware-level properties that together
cap how fast we can push state to it. Both were measured against U64
Elite II on firmware 3.x over wired LAN in 2026-05; re-measure if a
new firmware claims throughput improvements.

* **Every response includes `Connection: close`.** The server refuses
  HTTP keep-alive, even when explicitly asked via a request header.
  This means `requests.Session` cannot pool connections — every PUT /
  POST pays a fresh TCP handshake. The measured floor is **~14 ms per
  request sequential** (p50 14.8 ms, max 20 ms over 50 samples), of
  which most is TCP setup, not actual write processing.
* **The server serializes concurrent requests internally.** Running
  N=8 parallel writers over the same `requests.Session` produces
  effective throughput of 65 writes/s — slightly *worse* than the
  sequential rate of 71/s, because the requests just queue at the
  server with extra TCP-setup overhead. Per-request latency under
  concurrency scales linearly with worker count (8 workers → ~115 ms
  per request), which is the signature of a single-threaded server
  draining a FIFO.

The practical ceiling is therefore **~50-70 writes/sec sustained**
regardless of how many client threads we throw at it. Under real
workload (audio NMI firing, VIC raster IRQs, GIL pressure) the
sequential floor rises from 14 ms to ~20 ms per request, putting the
sustainable rate at the lower end of that range.

**Implications for design**:

* **Parallelizing the async write worker won't help.** This was
  considered as a follow-up to the profiling work and ruled out — the
  experiment is in the `Connection: close` measurement above. The
  single-worker FIFO in [api.py](../c64cast/api.py) is the right
  architecture for this server.
* **Reducing write *count* is the only real lever.** `write_regs`
  coalesces N contiguous register writes into one PUT; use it
  liberally. `write_region` skips writes when the buffer is unchanged
  vs. the per-region cache; this is why static char-mode scenes are
  cheap (most frames write nothing). Any new code path that issues
  N small writes per frame should be a yellow flag.
* **A scene targeting >30 fps with ≥2 writes/frame will saturate the
  pipe.** big_text at 50 fps with 1 write/frame is the canonical
  example — it produces exactly 50 writes/s, which is the wall, so
  every frame's enqueue blocks on the previous frame's HTTP. The
  classic mitigation is to cap such a scene at `target_fps = 30` (or
  redesign so per-frame state lives in U64 RAM and is consumed by a
  raster IRQ, so Python only writes when something macro-level
  changes).
* **To re-measure HTTP** (e.g. to check whether a new firmware has
  added keep-alive), temporarily revert the writes back to
  `requests.put` and run `--profile`. Today the profile summary line is
  `u64 dma latency: n=N avg=… p50=… p95=… max=… ms` and reports the
  DMA path, not REST.

## `WaveformScene` duration

The U64's sidplay endpoint doesn't tell us when a tune ends, and the
SID file itself doesn't carry song-length data. Two ways to know how
long to play a track:

1. Set `duration_s = N` in the scene config (overrides any DB lookup).
2. Configure `[playlist] songlengths_file = "assets/sids/C64Music/DOCUMENTS/Songlengths.md5"`
   and leave `duration_s` at the default. The HVSC SongLengths DB is
   keyed by an MD5 of the SID **data payload** (not the header) and
   covers most HVSC tunes — the file ships inside a full HVSC unpack at
   `C64Music/DOCUMENTS/Songlengths.md5`.

If neither applies, the waveform scene defaults to 180 s — usually wrong
for a specific tune. Pick one of the two options above for SID jukebox
setups.

## `WaveformScene` defaults to half the video rate (DMA ceiling)

`WaveformScene` renders 3 voice bitmap strips per frame, and because the
trace moves every frame the per-region delta cache skips almost nothing, so
each frame is ~3 near-full strip uploads. At the full video rate (60 NTSC /
50 PAL) that is **~170 writes/s** — right at the ~200/s DMA ceiling.

HW-verified 2026-06-09 on a real Ultimate-64: at ~170 writes/s into a
**bank-2-relocated** display (bitmap at `$A000-$BFFF`, the relocation target
used when the SID payload overlaps bank 0's bitmap — see the bank 0↔2
relocation note) the U64 **power-cycles itself mid-tune** (reproduced with
`Times_of_Lore`, a 2× multispeed Galway tune: clean for ~50-90 s, then the
DMA socket drops, the screen blacks out, and the machine physically powers
off). A bank-0 tune at the same ~170/s ran clean, and the *same* bank-2 tune
at half rate (~90 writes/s) played its full 7:40 length cleanly — so the
trigger is the **combination** of high write rate and writes landing in the
bank-2 region, and the host can only avoid it by lowering the write rate. A
C64 powering itself off from legal memory writes is a U64 firmware/FPGA
fault, not something the host causes through valid DMA.

Mitigation (shipped): `WaveformScene.target_fps` defaults to **half** the
system video rate (**30 NTSC / 25 PAL**) instead of the full rate. An
oscilloscope reads fine at half-rate, a half-integer divisor keeps the
render an exact submultiple of the video standard (so the wallclock
phase-lock stays clean), and it halves DMA to ~90 writes/s with comfortable
headroom. The host-emulator poll rate (`_video_hz`) is independent and stays
at the full video rate so the scope still tracks every PLAY tick. An
explicit `target_fps` in the CLI/TOML still overrides the default — but
raising a bank-2 tune back toward 60 fps risks the power-off above.

## Bitmap video/webcam scenes default lower when digitized audio streams

The same DMA-ceiling reasoning applies to the frame-pushing scenes that can
drive the 4-bit `$D418` digitized-audio DAC — `video`, live `webcam`, and a
`generative` scene with `audio_source = "mic"`. A bitmap display (hires /
hires_edges / mhires) re-uploads a full ~9-10 KB frame every frame, and each
DMA write halts the C64 bus for the duration of the transfer. When the
digitized-audio DAC is *also* streaming (the audio worker writing the ring +
the NMI consuming it), the two write streams compete for the bus and the
picture tears at the full system rate.

Defaults (all overridable with an explicit `target_fps`):

* **Bitmap + digitized audio → 20 fps** (both NTSC and PAL). The aggressive
  cap that keeps the combined DMA load under the bus-halt ceiling.
* **Bitmap, no digitized audio → half rate (30 NTSC / 25 PAL).** A muted
  bitmap video / no-mic bitmap webcam still pushes full frames, so it gets the
  same half-rate treatment as `WaveformScene`.
* **Bitmap video on the Ultimate Audio sampler → full system rate
  (60 NTSC / 50 PAL).** The FPGA PCM sampler plays straight from REU with zero
  bus involvement *and* forces the bus-clean REU-staged (bank-swap) video path,
  so neither bus-halt cap above applies. The full-rate value is a *ceiling*:
  because `VideoScene` only re-pushes a frame when the source yields a new one
  (dedup), the effective push rate equals the source video's fps — a 24 fps clip
  pushes 24/s, a 60 fps clip 60/s — i.e. source-rate playback, capped at the VIC
  refresh. HW-verified on .64 (no added shimmer on real ≤30 fps content; audio
  clean at a genuine 60/s push).
* **Char modes (petscii / blank) → unchanged.** A 1 KB screen that the
  per-region delta cache mostly skips is cheap, so these keep the playlist
  system default (60 NTSC / 50 PAL).

These caps (`config._frame_push_default_fps`) are worth revisiting once the
firmware no longer halts the CPU on DMA writes — see the U64 zero-halt DMA
path notes.

## `LauncherScene` runs a real program and only watches for input

The `launcher` scene resets the U64, uploads a `.prg` (firmware
`/v1/runners:run_prg`) or `.crt` cartridge (`/v1/runners:run_crt`) chosen
by extension, and then hands the whole machine to it. From that point the
program owns the VIC, SID, and CIAs — c64cast stops painting; the scene
only polls for player input and times out. `teardown()` resets the machine
so the next scene starts clean (mandatory for `.crt`, which `run_crt` leaves
active). Consequences worth knowing:

* **`duration_s` is an idle timeout, not a runtime.** It resets on player
  input, so an actively-played game stays up and an untouched demo runs the
  full window. `min_duration_s` is a floor; the optional `max_duration_s` is
  a hard ceiling that advances regardless of input (unset = no cap, so a
  game played non-stop never advances on its own).

* **Input detection deliberately excludes the modifier keys** c64cast
  already scans (Commodore / SHIFT / CTRL at `$028D`) — those drive
  pause/skip/style and must not count as "player active." `input_source`
  picks what *does* count: `cia` ($DC00/$DC01 joystick bits, default),
  `kernal` ($00C5 last-key + $00C6 buffer length), `auto` (both), or `none`.

* **Idle detection is best-effort.** A program that runs its own
  keyboard-matrix scan drives `$DC00` as an output, so `cia` reads can race
  that scan — producing false "activity" (the scene won't advance until
  `max_duration_s`) or missing a keypress. `kernal` mode is clean but only
  works while the program leaves the kernal IRQ intact (BASIC games,
  kernal-friendly demos); it's blind once a program installs its own IRQ —
  which most action games and demos do. There is no universally-reliable
  "any input" signal under an arbitrary program; joystick games on `cia`
  are the most dependable case.

  > **Unverified:** whether the U64's DMA `readmem` of `$DC00` returns the
  > live I/O register or RAM-under-I/O. The `cia` path depends on it. If a
  > hardware test shows it doesn't read the live register, switch the
  > default to `kernal`/`auto`.

* **Single program only.** No disk (`.d64`/`.d81`), tape (`.t64`/`.tap`),
  or multi-disk games — there's no mounting or disk-swap. Cartridge-type
  support depends on the firmware; PAL/NTSC is not switched; the local
  preview/recording windows show nothing (no host-side pixel writes).

* **`bypass_audio_lock` lets several players hear their own games.** In
  ensemble mode at most one system normally holds the exclusive audio slot,
  so an audio-bearing scene on another system is skipped while it's taken
  (see "Ensemble audio coordination" in CLAUDE.md). That's wrong for
  interactive launchers — two people at two machines both want to play and
  hear their own game. Setting `bypass_audio_lock = true` on a launcher
  makes it neither claim nor wait on the slot, so multiple launchers run
  concurrently, each driving its own SID. No effect on a single-system run.

* **TR launch/upload errors surface the firmware's reason.** When the TR NAKs a
  command, `teensyrom_dma._expect_ack` now captures the trailing text the
  firmware emits and puts it in the raised error — `TRBusyError` on a `"Busy!"`
  reply (a program is running / the menu handler isn't active), and the literal
  text (`"Not enough room"`, `"File already exists."`, …) appended otherwise —
  instead of a bare token code. This is what makes the failure below diagnosable.

  > **Known issue — intermittent TR launcher upload corruption (under
  > investigation, pre-existing).** On the TeensyROM the keyboard poller's
  > `ReadC64Mem $028D` (and likely the launcher's own input poll) shares one
  > serial/TCP link with the launcher's `reset()` + PostFile. A poll read that
  > lands in the post-reset menu chatter (or reads a running program's state)
  > can desync the stream and leave stray bytes that make the *next* PostFile
  > drop a byte — the uploaded `.prg` then loads one byte short (BASIC autostart
  > stub lists garbage, `?SYNTAX ERROR`). It's a **race**: intermittent, but when
  > it fires the symptom is consistent. The launcher works reliably
  > single-threaded (no concurrent poll) and on the Ultimate (no shared-link
  > poll), which is why it was never caught — the TR launcher had not been
  > HW-exercised under the live playlist. Candidate fixes (not yet shipped):
  > make `read_segment` fully resync on any desync so no reader can poison a
  > later command; suspend the poller across the launcher's reset+upload; a
  > robust pre-upload drain. Needs a soak harness (hundreds of launch cycles) to
  > verify, since it can't be reproduced on demand.

## `backgrounds.py` constants are screen codes, not PETSCII

PETSCII and the VIC screen-code encoding diverge above 0x40 — e.g. the
`@` character is PETSCII 0x40 but screen code 0x00. Anything that
writes directly to $0400 (overlays, backgrounds) deals in screen codes,
not PETSCII. The helper `overlays.ascii_to_screen()` does the
conversion for ASCII text, which is the common case.

If you copy a constant out of a PETSCII reference table and notice it's
painting the "wrong" character, that's the gap. Convert it.

## `C64_PALETTE_BGR` is OpenCV BGR order

[palette.py](../c64cast/palette.py) stores the C64 palette as BGR
(blue, green, red) tuples because OpenCV's frame format is BGR. If you
ever extract a color from this table to display somewhere that expects
RGB (matplotlib, PIL, a web page), swap channels first or you'll get
yellow where you wanted blue.

## Color shaping (`[color]`) is pre-quantization only in 3 of 4 modes

The global `[color]` stage — a per-channel gain (`channel_boost`) plus
hue-band corrections (`hue_corrections`) — runs before nearest-color
quantization in MCM, MultiHires, and PETSCII, biasing the palette match
toward C64-friendly hues. Hi-res mode skips it because its monochrome-
per-cell pipeline is already binary.

`channel_boost` defaults to `[1.3, 1.2, 1.0]` (BGR): blue/green lift, red
left neutral. The historical default cut red to `0.9`, but an A/B on real
TRON frames showed that only raised perceptual (Lab) error and starved warm
colors (yellow/red/purple) with no benefit to the blues it was meant to
favor — so red is now neutral. Override per-config via `[color].channel_boost`.

This stage is orthogonal to `palette_mode`, which only chooses the VIC-II
per-cell slot-allocation strategy. If you tune `[color]` and only some modes
change, that's why (hires ignores it).

### `[color].auto_fit` — per-source adaptive fit (on by default)

`channel_boost` and `hue_corrections` are the *same* nudge for every video.
`auto_fit` (default **true**) is their per-source adaptive sibling: for
**video and slideshow scenes only**, c64cast pre-scans the source (a
quick downscaled decode → one luma histogram + mean saturation) and derives a
contrast (levels) stretch plus a gentle saturation lift that expands the content
to *fill* the C64 tonal + chroma range. The quantizer's target is always the
fixed 16 colors and content that huddles in a corner of the gamut (dark, flat,
or low-chroma — the common case for vintage videos) otherwise leaves most
of the 16 colors unused and reads as muddy and monochromatic; the fit pushes it
out so more of the palette gets used.

It is **faithful** — a luma stretch applied to all channels preserves hue (it
expands what's there, it does not recolor — that's the deliberately-stylized
follow-up, not this), and the saturation lift is floored at 1.0 (never
desaturates). It is **do-no-harm**: black/white points come from the 1st/99th
luma percentiles (outlier-robust), a minimum-span floor caps the contrast gain
(~8×) so a near-flat frame doesn't blow noise up to full contrast, and a
well-exposed source resolves to an identity fit (no-op). `auto_fit_strength`
(0..1) lerps the whole transform toward identity — `auto_fit_strength = 0.0` is
equivalent to off, handy for an A/B.

**Where it runs:** the scene computes one `ColorFit` per video (video) or
per image (slideshow) and installs it on the display mode via `set_color_fit`;
the mode applies it as the first step of `compose`/`render`, *after* the cheap
downscale, so per-frame cost is two LUT passes. Webcam scenes never call
`set_color_fit` (they can't pre-scan, and a per-frame fit would flicker), so the
path is a no-op there — `_color_fit` stays `None`. See
`palette.ColorFitAccumulator` / `apply_color_fit` and `video.prescan_color_fit`.

## `Scene.video_buffer.maxlen` is "Optional[int]" to Pylance

`collections.deque(maxlen=8)` has type `deque[T]`, but `deque.maxlen`
can be None at the type level (for the unbounded form). The webcam
scene reads `len(self.video_buffer) >= self.video_buffer.maxlen` which
Pylance flags as comparing an int to Optional[int]. The runtime is
fine; the live scenes silence the warning with
`# type: ignore[operator]`. Don't "fix" it by adding an `if maxlen is
not None` guard — it's a Pylance limitation, not a real issue.

## BASIC clear-and-loop is how the cursor stays hidden

`api.run_basic_clear_loop()` POSTs `10 PRINT CHR$(147) : 20 GOTO 20`
to the `/v1/runners:run_prg` endpoint at startup and on resume. The
`PRINT CHR$(147)` clears the screen + homes the cursor; the infinite
`GOTO 20` keeps BASIC out of the editor's direct-input mode, which is
what keeps the kernal cursor-blink IRQ suppressed (the editor is what
toggles `$CC`; while BASIC is busy looping, the blink never re-arms).
Earlier code tried to write `$CC` and screen RAM directly from Python,
but the kernal cursor IRQ at $EA87 races the write and re-paints stale
state. **Don't go back to poking `$CC` directly** — let BASIC own it.

## Licensing of SIDs, videos, and ROMs

c64cast ships **none** of the following — you provide them:

* **SID files** — HVSC is the canonical archive. The HVSC license
  permits free non-commercial use; commercial use requires per-tune
  permission from each composer. Don't blindly include HVSC tunes in
  a Twitch VOD if you ever plan to monetize.
* **Videos** (the `video` scene type) — your problem.
  If you're playing a Coca-Cola ad at VCFSW for nostalgia, fair use is
  probably defensible; on a recorded stream the publisher may disagree.
* **CHARGEN ROM** (`characters.901225-01.bin`) — Commodore copyright.
  Distributed widely with C64 emulators under various legal gray-area
  arrangements; the project's stance is "you have a real C64, dump it
  from yours." The built-in ASCII fallback exists so the codebase can
  run without it at all.

## "Why doesn't `--list-devices` show my webcam?"

OpenCV on macOS sometimes fails to enumerate AVFoundation cameras
without `Privacy & Security → Camera` permission granted for the
terminal running the script. Grant the permission once, then
`--list-devices` will show numbered entries.

On Linux, the device index corresponds to `/dev/video<N>` — a USB
camera that re-enumerates may shift indices between reboots.

## Optional-deps groups can silently degrade

If you `pip install -e .` (no extras), the package imports fine but
most features are disabled. Failure modes:

* `[audio] enabled = true` without `[mic]` extra → `AudioStreamer`
  raises `ImportError` at scene setup.
* `type = "video"` without `[video]` extra → loader logs
  "Found N video files but PyAV is not installed; skipping videos"
  and the playlist runs without videos.
* `[preview]` enabled without `[preview]` extra → preview window
  silently disabled with a warning.
* `[control]` enabled without `[control]` extra → control plane
  silently disabled with a warning.
* `type = "midi"` without `[midi]` extra → scene setup raises
  `RuntimeError("MidiScene requires mido + python-rtmidi")`.
* `type = "obs_status"` overlay without `[obs]` extra → loader rejects
  the overlay with a clear `RuntimeError` at config load.

If something feels missing, double-check `pip install -e .[all]`.

## Single-scene mode is automatic, not opt-in

When the loaded playlist defines exactly **one** scene, the Playlist
auto-enters single-scene mode: no interstitial, no CTRL-skip (it's
silently dropped), and the one scene loops forever via teardown+setup.

Two surprises:

1. `[playlist] interleave_videos = true` with a single-scene config and a
   populated videos directory **does not insert videos** — the loader logs an
   info line and short-circuits because inserting a video would promote
   the playlist to 2 scenes and silently defeat the mode.
2. CTRL key presses (and HTTP `POST /skip`) are no-ops while running. C=
   pause/resume still works.

If you want videos or CTRL-skip back, define at least 2 scenes in your
config.
