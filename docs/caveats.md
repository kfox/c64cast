# Caveats

Surprises, footguns, and design choices that look weird until you know
why. Read this before you spend an evening debugging "it's almost
working, but…". For end-user options see [usage.md](usage.md); for the
architecture overview see [CLAUDE.md](../CLAUDE.md).

## Audio is intentionally lo-fi

The SID DAC streaming path writes 4-bit samples (0-15) to the SID volume
nibble at $D418 at 8 kHz. That's an objectively bad audio format — but
it's the format a real C64 plays back. You can raise `[audio]
sample_rate` in config, but the C64-side NMI is sized for 8 kHz and
nothing in the pipeline resamples; a different rate just plays at the
wrong pitch.

There is no master volume, no SID filter, no anti-aliasing. The noise
gate (`noise_gate`) and pre-DAC gain (`mic_sensitivity`) are the only
shaping knobs. Hum and hiss are part of the aesthetic.

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
**commercial and slideshow scenes only**, c64cast pre-scans the source (a
quick downscaled decode → one luma histogram + mean saturation) and derives a
contrast (levels) stretch plus a gentle saturation lift that expands the content
to *fill* the C64 tonal + chroma range. The quantizer's target is always the
fixed 16 colors and content that huddles in a corner of the gamut (dark, flat,
or low-chroma — the common case for vintage commercials) otherwise leaves most
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

**Where it runs:** the scene computes one `ColorFit` per video (commercial) or
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
* **Commercial videos** (the `commercial` scene type) — your problem.
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
* `type = "commercial"` without `[commercials]` extra → loader logs
  "Found N ad files but PyAV is not installed; skipping commercials"
  and the playlist runs without ads.
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

1. `[playlist] interleave_ads = true` with a single-scene config and a
   populated ads directory **does not insert ads** — the loader logs an
   info line and short-circuits because inserting an ad would promote
   the playlist to 2 scenes and silently defeat the mode.
2. CTRL key presses (and HTTP `POST /skip`) are no-ops while running. C=
   pause/resume still works.

If you want ads or CTRL-skip back, define at least 2 scenes in your
config.
