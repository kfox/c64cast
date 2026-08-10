# Hardware I/O & transports

How c64cast talks to the machine: the backend contract every consumer is written against, the DMA/REST client for the Ultimate, the TeensyROM serial/TCP link, the BASIC stub the C64 runs while c64cast drives it, the constant register the whole tree names addresses from, and the live REU/sampler provisioning.

Part of the [architecture reference](../architecture.md). For end-user configuration see [the Programmer’s Reference Guide](../reference/README.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`backend.py` — the C64Backend duck type, hardware profiles, and the shared write path](#backendpy--the-c64backend-duck-type-hardware-profiles-and-the-shared-write-path)
* [`api.py` — Ultimate64API + `socket_dma.py` — SocketDMAClient](#apipy--ultimate64api--socket_dmapy--socketdmaclient)
* [`teensyrom_api.py` — the TeensyROM+ backend](#teensyrom_apipy--the-teensyrom-backend)
* [`teensyrom_dma.py` — TeensyROM link errors + the launcher upload race](#teensyrom_dmapy--teensyrom-link-errors--the-launcher-upload-race)
* [Startup: BASIC clear-and-loop program](#startup-basic-clear-and-loop-program)
* [`char_rom.py` — reading the character ROM off the machine](#char_rompy--reading-the-character-rom-off-the-machine)
* [`c64.py` — the hardware constant register](#c64py--the-hardware-constant-register)
* [`hw_provision.py` — live REU + sampler auto-provisioning](#hw_provisionpy--live-reu--sampler-auto-provisioning)

---

## `backend.py` — the C64Backend duck type, hardware profiles, and the shared write path

c64cast started life talking to exactly one device, and every consumer — scenes, modes, overlays, the playlist, the audio streamer — was duck-typed on the `Ultimate64API` method surface, injected from a single construction site. [backend.py](../../c64cast/hw/backend.py) is that implicit contract made explicit so a second hardware family (the TeensyROM+) could drop in at the same seam. The split inside the ABC is the design:

* The **write path** (`write_memory*`, `write_regs`, `write_region`, `flush`, plus the cache/listener/stats bookkeeping) is **mandatory** — it carries 100% of rendering and audio programming, so a backend that can't write can't usefully exist.
* Everything that needs a *response* from the machine (`read_memory`), a firmware *runner* (`reset`, `run_*`), the REU, or the Ultimate's config REST surface is **capability-gated**: the ABC ships defaults that raise `BackendCapabilityError`, and callers check the matching `profile.supports_*` flag first. The exception marks a *missed gate* — a programming error — not a runtime condition to catch.

**`HardwareProfile`** is why callers never branch on device family: a scene asks `supports_read` / `supports_run_prg` / `supports_reu` instead of testing `isinstance` or a family string. The flags describe the *connected device*, not the backend class — `TEENSYROM_PROFILE` declares `supports_read` at the protocol level, and `TeensyROMBackend.__init__` probes the actual firmware at connect and downgrades it via `dataclasses.replace` (the profile is frozen, so a downgrade is a new value, never shared mutation). Behavioral facts that would otherwise be scattered family checks ride the profile too: `writes_are_acked` (TR — makes `flush` ~free), `reu_bus_clean` (the U64's REUWRITE is an ARM-side memcpy, no bus halt), `kernal_irq_intact`. The Ultimate 64 and Ultimate II+ are protocol-equivalent for c64cast's purposes and share one profile; a per-variant selector (e.g. differing `max_fps`) can be added without touching the factory contract.

`max_write_rate_hz` is 200.0 on **both** profiles, and on the TR that number is a measured floor, not a wall: the link sustained 188 writes/s of 64 bytes with zero missed slots, and a later run held 557 writes/s with zero underruns (`scripts/diags/audio_fm_probe.py`, 2026-08-05). It is deliberately not raised — spending the headroom measures *worse*, raising the DAC noise floor 2–3 dB for NMI ticks nobody can hear. See [the video-path note in audio.md](audio.md#the-video-path-is-deliberately-not-split-and-lost-ticks-are-not-the-hiss).

**`BufferedWriteBackend`** is one shared implementation of the host-side write path, lifted verbatim from the original Ultimate64API so both families get identical cache/diff semantics; a concrete backend implements a single transport primitive, `_emit(addr, payload)`. Sharing it is the point — the delta strategy is subtle enough to drift if it existed twice. `write_region`'s strategy: no cache or a length change → full push; a contiguous dirty span under `full_threshold` (0.6) of the buffer → one span write; otherwise diff in `DELTA_CHUNK_BYTES` (256) slabs and push only the dirty ones, falling back to a full push when chunking wouldn't save enough. The chunked branch exists for sparse frames — a waveform trace's dirty *range* spans the whole region while only a fraction of cells changed, and a span-only strategy degrades exactly those frames to full pushes.

`_emit` implementations never raise on transport failure: a transient blip must not crash the playlist, so both backends route failures through the shared escalating log ladder (debug on the 1st consecutive failure, warning at the 10th and 50th, error at the 200th; reset on any success). Write listeners — the software framebuffer behind preview and recording — are synchronous and exception-isolated. The semantic helpers (`silence_sid`, `blank_display`, `disable_case_switch`, `restore_kernal_irq_vector`) are pure writes on the standard memory map, implemented once here so any write-capable backend gets them for free.

**`describe_device()`** is the connect-time identity line — `"Ultimate II+ 5D327C (firmware 3.14d, FPGA 122)"`, or `"TeensyROM+ 12345678 (full firmware, serial /dev/cu.usbmodem12345678@2000000)"`. The ABC default returns `""`, so a backend that can't tell simply logs nothing; the Ultimate reads `GET /v1/info` and the TR reads the USB serial number of a serial-attached board (a TCP-attached one has no per-unit identifier, so the link description carries the rest). It is best-effort by construction — a device that won't answer costs a log line, not a run.

The connection target alone is not enough to identify what a run talked to. An IP or serial path names an *endpoint*: two devices can trade addresses between runs, and `192.168.2.64` is merely the Ultimate's factory default, so several units on one bench answer to it at different times. The `unique_id` is stable across DHCP re-leases, which is why `dac_calibration_store` already keys per-unit calibrations on it. The sharper reason on the Ultimate family is `product` — the *only* field over this API that distinguishes a U64 from a U2+, and the two expose different config categories. A U2+ run that fails a `SID Addressing` PUT reports a bare `404` against a URL, which reads as a firmware or c64cast bug rather than as "this isn't a U64"; the identity line is what makes that legible from a log alone.

`pause_idle()` encodes a contract that is easy to miss: whatever idle it reaches must keep the kernal keyboard scan alive, or `$028D` freezes and the C=-held-to-resume gesture can never be detected — the stream is stranded paused. The default (`reset()`) satisfies it on the Ultimate, whose reset lands at the BASIC READY prompt with the editor IRQ scanning; the TR must override it (see [teensyrom_api.py](#teensyrom_apipy--the-teensyrom-backend)).

**`make_backend(cfg)`** keeps construction policy out of the CLI: it defaults to the Ultimate family so a config with no `[hardware]` section behaves byte-for-byte as before, and folds the resolved NTSC/PAL rate into `profile.default_fps` (video system is orthogonal to hardware variant, so the playlist reads one number). One subtlety on the TR serial path: when auto-detection resolves the device, the factory writes the resolved port *back into the config* — `dac_calibration_store.resolve_calibration_key` only looks up the board's USB serial number when `serial_port` is set, so leaving it empty would silently key two different TR+ boards on one host to a single shared `"tr-serial-auto"` calibration file.

## `api.py` — Ultimate64API + `socket_dma.py` — SocketDMAClient

Split-transport client:

* **Writes** go through [socket_dma.py](../../c64cast/hw/socket_dma.py) — a persistent TCP socket to the U64's Ultimate DMA Service (port 64) sending opcode `0xFF06 DMAWRITE`. Per-connection FIFO ordering at the server, ≈5 ms per write, ≈200 writes/sec sustained. The constructor calls `connect()` immediately so failure (service disabled, auth rejected, etc.) surfaces as `SocketDMAError` at startup, before the playlist runs. `api.flush()` is a trailing IDENTIFY round-trip — when it returns, the server has drained every prior write.
* **Reads, reset, runners, probe** stay on REST via `requests`. These are low-rate and one-shot; the HTTP throughput wall (≈50-70/sec) doesn't apply.

Two coalescing/caching layers on top:

1. **`write_regs(base_addr, *values)`** — packs N contiguous register writes into one DMA write (e.g. `D020-D023` border + 3 backgrounds in one packet).
2. **`write_region(address, data, region_id=…)`** — caches the last-pushed bytes per region; only sends the changed sub-range. Above `full_threshold` (0.6) it falls back to a full upload. Display modes call `api.invalidate_cache()` in `setup()` because a mode switch can repurpose the same address.

Latency tracking lives on the DMA client (`socket_dma.latency_summary()` / `format_latency()`); `api.format_write_latency()` is the playlist-facing shim (`teensyrom_api.py` exposes the same method name over `teensyrom_dma`'s own latency tracker, so the playlist calls it backend-agnostically). The heartbeat line and the `--profile` summary both surface this.

## `teensyrom_api.py` — the TeensyROM+ backend

The TR-family implementation of `C64Backend`, on top of the token protocol in [teensyrom_dma.py](../../c64cast/hw/teensyrom_dma.py). Nearly everything unusual in it traces to two firmware facts that shipped together in TR+ v0.7.2.5: WriteC64Mem became **cycle-clean** (its /DMA assert gates on a safe VIC cycle), and **ReadC64Mem** (`0x64FD`) appeared. `profile.supports_read` is therefore the proxy for "new enough firmware" throughout the module — it gates not just reads but the choice of idle, SID playback, and the char-ROM dump.

**Connect-time capability probe.** The profile declares read support at the protocol level, but a given device may run older firmware. `__init__` reads 2 bytes at `$FFFC` (the KERNAL reset vector — always mapped, value-stable) and downgrades `supports_read` on failure — version-robust without parsing the ping banner. The failure path drains stale bytes, because an unknown token on old firmware can leave trailing bytes that would desync the next real command.

**Two idles.** On cycle-clean firmware, bring-up launches the same IRQ-enabled BASIC clear-loop the Ultimate runs, keeping the kernal keyboard scan — and so `$028D` — live for the keyboard poller. On older firmware it falls back to the spin stub: `SEI`, 252 × `NOP`, `JMP` back to the top. Pre-cycle-clean DMA perturbs the running 6510, and streaming over a live interpreter corrupts it within seconds (`?UNDEF'D STATEMENT`, `?SYNTAX ERROR`); a perturbed cycle in a NOP sled just lands somewhere in the sled and slides back to the `JMP` — there is no interpreter state to corrupt. The cost is that IRQs stay masked, `$028D` freezes, and physical-keyboard control is gone. The clear-loop path's own repairs — LaunchFile leaving the program un-run, the CURLIN READY probe, draining the loader's post-ack chatter — are the [Startup section's](#startup-basic-clear-and-loop-program) story; the CURLIN constants also record that the usual "high byte `$FF` means direct mode" summary did **not** match on hardware (READY read `$0000`), which had silently disabled the repair until the probe accepted both.

**`pause_idle` — both constraints are hardware, not style.** (1) *Don't reset*: a TR reset boots to the TeensyROM menu, which does not run the kernal keyboard scan, so `$028D` freezes and resume can never be detected. (2) *Don't blank*: DEN=0 removes badlines, and the cycle-clean DMA gates its /DMA assert on a badline — with none, every subsequent read *and* write hangs, which strands resume and wedges the TR until a power-cycle. Since the clear-loop keeps running underneath every scene anyway (the DMA only overwrites screen/VIC RAM, never the BASIC program), pause is simply a DMA screen-clear with DEN left on — the closest possible idle to the working live-scene state.

**`read_memory` returns `None`, never raises.** The keyboard and menu pollers call it every ~100 ms and rely on `None` meaning "couldn't tell"; a raise would turn every transient link blip into a dead playlist. This mirrors the Ultimate's REST read contract.

**Kicks are `$0314` vector swaps, not LaunchFile.** LaunchFile resets the C64, and its async boot + fast-LOAD raced the scope bring-up and the keyboard poll; a launch-based SID start would have needed a pile of boot-race workarounds (boot settle, a bus-silent launch lock, a trampoline + pre-uploaded SYS stub, a verify-during-boot read). Instead, with the IRQ-enabled clear-loop chaining through `$0314`, both the SID player start and the char-ROM dump DMA their payload and swap the vector so the next kernal IRQ runs the stub once — no reset, no boot, and the display the caller painted survives, which is what lets `defer_audio` put the oscilloscope on screen before the first note. Both kicks refuse via `_require_irq_idle` on old firmware: the spin stub masks IRQs, the swap would never fire, and without the gate the failure mode is a silent hang or silent playback rather than an error. The orchestration above the kick is shared with the Ultimate — see [backend-agnostic orchestration in sid.md](sid.md#backend-agnostic-orchestration) and [char_rom.py](#char_rompy--reading-the-character-rom-off-the-machine). The post-swap `$0314` read-back (`_verify_player_irq`) is best-effort — a mismatch logs "audio may be dead" rather than raising.

**Bring-up retries; uploads replace.** After ResetC64Token the TR reboots its menu and re-inits SD, and PostFile is refused (FailToken) until the menu handler is ready — so bring-up retries up to 6 × 1.0 s instead of trusting any fixed post-reset delay. `_upload` deletes before posting because PostFile refuses to overwrite (`"File already exists."`); helper PRGs live under a dedicated `c64cast/` folder on the TR's SD/USB so retries and re-runs never touch the user's own file roots. Bring-up is best-effort (failures log), but `launch_program` re-raises — its caller (LauncherScene) has to know the launch never happened.

## `teensyrom_dma.py` — TeensyROM link errors + the launcher upload race

### Errors carry the firmware's reason

`_expect_ack` captures the trailing text the TR emits after a NAK and puts it in the raised error, instead of surfacing a bare `FailToken (0x9B7F)`:

* A `"Busy!"` reply — program running, or menu handler inactive — raises `TRBusyError`, a subclass of `TRError`, so callers can distinguish it.
* Any other reply has its literal text appended (`"Not enough room"`, `"File already exists."`, …).

### Known issue: the launcher upload race

Under investigation, **not yet fixed.** The TR launcher (`launch_program` = PostFile + LaunchFile) can produce an intermittently-corrupt upload.

The mechanism: the keyboard poller's `ReadC64Mem` — and likely the launcher's own input poll — shares the TR link with the launcher's reset+PostFile. A poll read landing in the post-reset chatter desyncs the stream, so the next PostFile drops a byte. The `.prg` then loads one byte short and BASIC reports `?SYNTAX ERROR`.

It is a race, so it reproduces intermittently; single-threaded runs and the Ultimate backend are both reliable. Three candidate fixes are open — a desync-safe `read_segment`, suspending the poller across reset+upload, or draining before upload — and all need a soak harness to verify. See [caveats.md](../caveats.md).

## Startup: BASIC clear-and-loop program

After `api.reset()`, `api.run_basic_clear_loop()` POSTs a 25-byte tokenized BASIC PRG (`10 PRINT CHR$(147) : 20 GOTO 20`) to `/v1/runners:run_prg`. `PRINT CHR$(147)` wipes the BASIC READY banner and homes the cursor; the infinite `GOTO 20` keeps BASIC out of the editor's direct-input mode so the kernal cursor-blink IRQ stays naturally suppressed. Audio bring-up still just uploads the NMI routine and starts the CIA #2 timer; the NMI fires regardless of what the BASIC loop is doing.

### The loop is the *only* way to stop the cursor blinking

Nothing c64cast writes touches BLNSW (`$00CC`), and nothing should. The obvious move — DMA `$80` there to tell the kernal editor's blink code to skip its toggle — was tried repeatedly and does not work: the editor's input-wait loop copies NDX (`$00C6`) into BLNSW on every pass, so with BASIC at READY the byte is gone microseconds after the DMA lands. Measured on hardware, a write to `$00CC` never reads back while every other zero-page address holds fine. Nothing the host can write holds that address down.

There was a `suppress_cursor_blink()` helper that made that write anyway, on the theory that it was still worth doing for the moments BASIC is parked elsewhere (after a SID player's `JMP *` spin survives teardown). It was removed: it is a no-op wherever the blink is actually visible, and its presence invited exactly the "just poke `$CC` harder" reflex that never works. Getting BASIC into the `GOTO 20` loop is the whole mechanism — there is no second one to fall back on.

### TeensyROM: LaunchFile leaves the loop un-run

The TR has no `run_prg`; `_bring_up_irq_clear_loop` PostFiles the same PRG and LaunchFiles it. Measured on hardware, the body lands at `$0801` but the first line's link pointer reads `00 00` and VARTAB sits at `$0803` — the signature of a BASIC cold-start init arriving *after* the copy. BASIC therefore sees an empty program, RUN drops straight back to READY, and the machine spends the whole session in the editor's input-wait loop. That is where the TR's blinking cursor came from, and why no amount of writing `$CC` could stop it. It also let the editor eat the keystrokes the keyboard poller reads out of KEYD.

`_ensure_clear_loop_running` repairs it with DMA only: re-write the program body to `$0801`, fix VARTAB to just past it (or RUN's `CLR` puts variables on top of the program), then type `RUN` + RETURN into the kernal keyboard buffer — which works *because* BASIC is stuck at READY, since that wait loop is exactly what consumes KEYD.

`_basic_is_at_ready` gates that repair, and is a **read**: CURLIN's high byte (`$003A`) is `$FF` in direct mode and the executing line number otherwise. The gate matters because typing into a running loop would leave keystrokes in KEYD for the poller to read as menu input (RETURN is a nav code). It used to infer the same state from the `$CC` write above — write `$80`, read it back, conclude from whether the editor had clobbered it — reading the state only as a side effect of a write that shouldn't have been happening at all.

### The probe has to wait for the loader to stop talking

LaunchFile acks and *then* streams its own console text back over the same link — `Remote Launch:` / `P:` / `F:` / `Loading IO handler` / `Resetting C64`, a C64 reset and BASIC cold start included. A fixed `time.sleep` after the launch guessed at how long that takes, and guessed short: measured on hardware, the first post-launch command's reply misaligned with the text and came back as the ASCII of `…mote Launch:`. The probe then failed, `_basic_is_at_ready` returned `None`, `_ensure_clear_loop_running` took its can't-read-the-state early return, and the repair was **silently skipped** — leaving BASIC in the editor with the blinking cursor the repair exists to prevent.

This is not a property of writes. A read collides identically; the earlier `$CC` write was simply the command that happened to be first in line. `_settle_after_launch` replaces the sleep with `drain_text`, which re-arms its deadline on every chunk and so waits for the stream to actually go quiet, consuming the text instead of leaving it to desync the next command.

## `char_rom.py` — reading the character ROM off the machine

Every C64-native glyph c64cast draws comes from the character ROM: the bitmap-mode text overlays (`scrolling_text` → `TextSurface` → `bitmap_text.load_glyphs`), `big_text`'s 8×-scaled scroller, the on-C64 menu, the oscilloscope's text rows, and the preview/recording renderers. With no ROM installed, `framebuffer._builtin_charset()` synthesises an ASCII font with `cv2.putText` — legible, but not the C64 font, and PETSCII graphics codes come out blank. A user reported "the scrolling text looks bad"; that was the whole explanation, and it was a first-run defect rather than a rendering bug.

[char_rom.py](../../c64cast/hw/char_rom.py) is the single resolver every glyph consumer goes through — an explicitly configured path, else `paths.roms_dir()/chargen.bin`, else the legacy cwd-relative `assets/roms/characters.901225-01.bin` (so an existing checkout keeps working), else the cv2 fallback. Only the automatic answer is cached process-wide; an explicit path is read fresh, because the cache is deliberately not keyed by path and would otherwise hand every caller whichever file was asked for first.

**Why it takes a 6502 stub.** The ROM is not RAM. A host `read_memory($D000)` returns the I/O page — VIC/SID/CIA registers — because what `$01` maps is decided *on the C64*, at read time. The only way to see the charset is to run code on the machine: clear CHAREN (`$01 = $33`, i.e. `$37` with bit 2 cleared), copy `$D000-$DFFF` down into plain RAM, restore the bank, and let the host read the copy back. `api.build_char_rom_dump_stub` is that code, in the hand-assembled-with-a-listing style of the SID player MC.

**Placement — both addresses are load-bearing:**

* The landing zone must be RAM readable **under default banking**, since `read_memory` sees whatever `$01` is at read time. That rules out the `$A000`/`$D000` underlay RAM. `$C000-$CFFF` is the proven-safe high RAM the SID player already lives in, and it survives `run_prg`'s soft reset (RAMTAS's memory-size scan restores every byte it probes).
* The stub cannot share those 4 KB — the copy would overwrite it mid-flight — and cannot live at `$0200-$03FF`, because RAMTAS zeroes the cassette buffer on every reset and the Ultimate kick *is* a reset. `$8100` is BASIC program RAM: far above the one-line `SYS` program `run_prg` loads at `$0801` (which creates no variables, so BASIC never grows into it), and clear of the `$8004` cartridge-signature window the KERNAL checks at reset.

**The two kicks**, split exactly like `_launch_sid_player` — the same shape of shared orchestration (here on `_StubRunnerBackend`, which owns the dump; the SID player's equivalent lives in `_SidPlayerMixin`), one per-backend hook (`_kick_char_rom_dump`):

* **Ultimate** — POST `10 SYS 33024` to `run_prg`. The stub gets the `CLI`/`RTS` tail (it was called from BASIC). Because the kick soft-resets, `Ultimate64API.dump_char_rom` re-establishes the BASIC clear loop in a `finally`, so the caller gets the machine back in the idle state it handed over.
* **TeensyROM** — swap `$0314/$0315` to the stub so the next kernal IRQ runs it once, the same primitive the SID player start uses: no reset, no boot, no fast-LOAD window. The stub gets the IRQ tail — restore the vector, `JMP $EA31` — and no `CLI`, because `RTI` restores the I flag from the stacked status. Gated on `supports_read`: the pre-cycle-clean spin-stub idle masks IRQs forever, so the swap would never fire.

**The completion flag** is what makes the read deterministic. The last byte of the blob is uploaded as `$00` and set to `$FF` by the stub as its final act; the host polls that one byte before reading the landing zone. Without it the read races the ~45 ms on-C64 copy, and there is no other signal — the Ultimate's `run_prg` POST returns once BASIC has *started* the program, not once `SYS` has returned.

**Verification is structural, not an equality test** against a known image, because a Swedish machine's charset (or a JiffyDOS font, or a replacement) is exactly the charset that user wants. `char_rom.verify` proves we got a charset and not I/O registers or blank RAM: at least one full 2 KB set; within each set, screen codes `$80-$FF` complement `$00-$7F` (the check that catches a botched `$01` bank — I/O and RAM cannot satisfy it); screen code `$20` blank and `$01` not (which is what rules out an all-zero or all-`$FF` buffer, since those pass the complement test trivially). The complement check carries a small tolerance: the stock 901225-01 is *almost* exact but not quite — the reversed `@` at screen code `$80`, row 5, reads `$99` where the complement would be `$9D`. A verifier demanding exactness would reject the very ROM it exists to accept. The SHA-256 comparison against the stock digests is reported as a note and never affects the verdict.

A failed verification is a hard failure of `--dump-char-rom` and a logged warning of the auto path — an unverified file is never written, because a bad one is worse than none (the cv2 fallback at least renders legibly) and it would suppress the auto-dump forever.

**When it runs.** `char_rom.ensure_installed` is called from `cli.build_stack` right after the reset + BASIC clear loop, where the machine is idle and nothing has painted. It fires only when nothing resolves already, the backend has both `supports_read` and `supports_run_prg`, and `[hardware].dump_char_rom` is on. It costs one `run_prg` round trip on the very first run against a given machine, invisible behind the existing startup, and never again. It is best-effort and never fatal; on success it calls `invalidate_cache()` so the run that triggered the dump already benefits.

No ROM bytes enter the repo, the sdist, the wheel, or a release asset. The bytes move from the user's hardware to the user's disk and stop.

## `c64.py` — the hardware constant register

Every bare hex address in the tree resolves through [c64.py](../../c64cast/hw/c64.py), so the code is greppable (`VIC.D018_MEMORY`, not `"d018"`) and porting to another Commodore variant stays tractable. Most of it is a plain name table; the groups below carry *policy*, which is what earns the module a section:

* **`CIA2.PORT_A_BANK_*` are whole-byte values, not bit masks.** The upper bits of `$DD00` drive the serial bus / RS-232 outputs; c64cast writes the whole byte and deliberately clobbers them, with the `0x97` base keeping the serial lines idle-high to match the kernal's post-init state. Which VIC bank a scene may use is encoded here too: banks 0 and 2 are the only ones with kernal char-ROM mapped at their `$1000` offset, so the char-mode double-buffer swaps between those; bank 1 is normally off-limits because the audio ring lives at `$4000-$5FFF`. The waveform scene is the one exception — bitmap-only (no char-ROM dependency) and it stops the ring at setup (the SID plays on the real chip), so it can claim bank 1 for tunes whose payload occupies banks 0 and 2.

* **The NMI budget constants are hardware measurements, not estimates.** `NMI_HANDLER_WORST_CYCLES = 68` supersedes an earlier 81-cycle *estimate* — a ring-prefill tone sweep on real hardware found the overrun onset, and `NMI_SAFE_MIN_PERIOD_CYCLES = 75` keeps margin for PAL and unit variation. `halt_quantum_bytes` sizes host writes from the measured ~1 cycle/byte DMA halt (1.02 µs/byte U64, 0.97 TR). The measurements and their consequences are told in [audio.md](audio.md#sample-rate-and-the-overrun-ceiling) (and [the ring-write split](audio.md#the-ring-write-is-split-and-spread)); the constants live *here* so `nmi_rate_safety` stays pure and is the single source of truth that config validation, `--doctor`, and the tests all import.

* **`RegionID` is a collision registry.** The write path's delta cache keys by region ID, not address — that is what gives a mode switch that reuses `$0400` a clean diff baseline after `invalidate_cache()`. IDs are claimed centrally, with reserved strides (per-voice `+0..9`, menu rows `+0..24`, and separate bank-2 IDs so the host-DMA double-buffer diffs each bank against its *own* prior content, not the other bank's), precisely so a collision is visible at definition time instead of surfacing as silent cache corruption mid-run.

* **`U64_API` is also a list of refusals.** `/v1/machine:writemem` is deliberately absent — writes go over socket DMA, not REST ([caveats.md](../caveats.md) has the transport measurements). `/v1/runners:sidplay` is deliberately absent because the firmware UI it draws hides VIC output (see `api.run_sid_player`).

* **`KEYBUF` names decoded PETSCII as it lands in KEYD** (`$0277`) — what the U64's keyboard-inject opcode writes and what the on-C64 menu poller drains — distinct from the raw matrix scan codes at `$00CB`. The kernal folds SHIFT into the cursor codes (SHIFT+CRSR-down decodes to CRSR-up, `$91`), which is why the menu reads direction straight off the code with no separate modifier read.

## `hw_provision.py` — live REU + sampler auto-provisioning

[hw_provision.py](../../c64cast/hw/hw_provision.py) enables the U64 REU and the Ultimate Audio sampler over the REST config API when the run needs them, and restores the originals at teardown. It lived in doctor.py until the name stopped fitting — every call from the normal run path went through a `from . import doctor as _doctor` alias, which was the code saying the name is wrong: diagnostics *observe*, this module *mutates* the machine.

The load-bearing property is **live + volatile**: a single-item `PUT /v1/configs/<cat>/<item>` applies through the firmware's `effectuate` and is never saved to flash (verified in the firmware source — `route_configs.cc` + `config.h` `at_close_config` call effectuate only). So even a missed restore reverts on the next power-cycle, which is what makes best-effort acceptable everywhere in the module: a failed restore just logs, and `provision_*` returns whatever it changed *so far* even when a later PUT fails, so teardown still restores partial changes.

`wants_reu` / `wants_sampler` are the single statement of which config shapes need each feature. Doctor's REU/sampler probes import them, so the `--doctor` report and the provisioner can never disagree about what a run requires; both return the *reasons* (which flags flipped the want) so log lines and diagnostics can point at the exact setting. One subtlety is deliberate: `use_reu_staged` defaults to the *string* `"auto"`, which is truthy — `wants_reu` tests `is True` so that only an explicit `true` is a hard requirement. Auto self-heals to the host-DMA double-buffer (also tear-free), so it must never be grounds for mutating the user's machine config. The provisioner is gated entirely inside itself (`auto_reu`, `profile.supports_*`, not `--skip-probe` — config is never written that couldn't first be read back — and the hard want), so `cli.build_stack` calls it unconditionally. The full run-lifecycle narrative (call ordering around the availability resolvers, teardown, doctor severity) is [audio.md's auto_reu section](audio.md#ultimate64auto_reu--automatic-reu-provisioning).

REU sizing is always 16 MB: c64cast's highest REU offset is the video staging region near 14 MB (`modes_irq.REU_VIDEO_BITMAP_COLOR_BASE = $E13000`), so a smaller REU would silently wrap, and 16 MB is FPGA-backed — the maximum costs nothing.

`fetch_config_section` is the firmware response-shape normalizer — firmware 3.x nests the section under the category name; older/variant firmware returns the dict directly or as a single-item list, recognized via `field_hint`. It is single-sourced here because it previously lived, identically, in two doctor probes; a firmware shape change is a one-place fix.

The sampler half mirrors the REU half, with three quirks of its own: the sampler's *presence* is detected by whether the firmware exposes its config keys at all (there is no feature-flag endpoint); the mixer's audible volume label is `" 0 dB"` **with a leading space**, verbatim from the firmware's `volumes[]` table — the GET returns it and the PUT expects it; and the restore dict spans two config categories (I/O map vs. mixer), hence the composite `"category\x1ffield"` keys. `wants_sampler` names the scene shapes that route audio through the sampler (video scenes, and generative scenes with `audio_source = "file"`, whose 4-bit DAC rendering is staticky) — missing a sampler-routed shape there doesn't error, it leaves the sampler silently un-provisioned, which is why the predicate is single-sourced with the doctor probe. `sampler_is_available` runs *after* `provision_sampler` so a box this run just enabled reads as available.
