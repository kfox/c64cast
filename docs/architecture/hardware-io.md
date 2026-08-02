# Hardware I/O & transports

How c64cast talks to the machine: the DMA/REST client for the Ultimate, the TeensyROM serial/TCP link, and the BASIC stub the C64 runs while c64cast drives it.

Part of the [architecture reference](../architecture.md). For end-user configuration see [usage.md](../usage.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`api.py` — Ultimate64API + `socket_dma.py` — SocketDMAClient](#apipy--ultimate64api--socket_dmapy--socketdmaclient)
* [`teensyrom_dma.py` — TeensyROM link errors + the launcher upload race](#teensyrom_dmapy--teensyrom-link-errors--the-launcher-upload-race)
* [Startup: BASIC clear-and-loop program](#startup-basic-clear-and-loop-program)
* [`char_rom.py` — reading the character ROM off the machine](#char_rompy--reading-the-character-rom-off-the-machine)

---

## `api.py` — Ultimate64API + `socket_dma.py` — SocketDMAClient

Split-transport client:

* **Writes** go through [socket_dma.py](../../c64cast/socket_dma.py) — a persistent TCP socket to the U64's Ultimate DMA Service (port 64) sending opcode `0xFF06 DMAWRITE`. Per-connection FIFO ordering at the server, ≈5 ms per write, ≈200 writes/sec sustained. The constructor calls `connect()` immediately so failure (service disabled, auth rejected, etc.) surfaces as `SocketDMAError` at startup, before the playlist runs. `api.flush()` is a trailing IDENTIFY round-trip — when it returns, the server has drained every prior write.
* **Reads, reset, runners, probe** stay on REST via `requests`. These are low-rate and one-shot; the HTTP throughput wall (≈50-70/sec) doesn't apply.

Two coalescing/caching layers on top:

1. **`write_regs(base_addr, *values)`** — packs N contiguous register writes into one DMA write (e.g. `D020-D023` border + 3 backgrounds in one packet).
2. **`write_region(address, data, region_id=…)`** — caches the last-pushed bytes per region; only sends the changed sub-range. Above `full_threshold` (0.6) it falls back to a full upload. Display modes call `api.invalidate_cache()` in `setup()` because a mode switch can repurpose the same address.

Latency tracking lives on the DMA client (`socket_dma.latency_summary()` / `format_latency()`); `api.format_write_latency()` is the playlist-facing shim (`teensyrom_api.py` exposes the same method name over `teensyrom_dma`'s own latency tracker, so the playlist calls it backend-agnostically). The heartbeat line and the `--profile` summary both surface this.

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

After `api.reset()`, `api.run_basic_clear_loop()` POSTs a 25-byte tokenized BASIC PRG (`10 PRINT CHR$(147) : 20 GOTO 20`) to `/v1/runners:run_prg`. `PRINT CHR$(147)` wipes the BASIC READY banner and homes the cursor; the infinite `GOTO 20` keeps BASIC out of the editor's direct-input mode so the kernal cursor-blink IRQ stays naturally suppressed (the editor is what flips `$CC` between 0 and 1 — when BASIC is busy in a tight loop, the blink never re-arms). Audio bring-up still just uploads the NMI routine and starts the CIA #2 timer; the NMI fires regardless of what the BASIC loop is doing.

## `char_rom.py` — reading the character ROM off the machine

Every C64-native glyph c64cast draws comes from the character ROM: the bitmap-mode text overlays (`scrolling_text` → `TextSurface` → `bitmap_text.load_glyphs`), `big_text`'s 8×-scaled scroller, the on-C64 menu, the oscilloscope's text rows, and the preview/recording renderers. With no ROM installed, `framebuffer._builtin_charset()` synthesises an ASCII font with `cv2.putText` — legible, but not the C64 font, and PETSCII graphics codes come out blank. A user reported "the scrolling text looks bad"; that was the whole explanation, and it was a first-run defect rather than a rendering bug.

[char_rom.py](../../c64cast/char_rom.py) is the single resolver every glyph consumer goes through — an explicitly configured path, else `paths.roms_dir()/chargen.bin`, else the legacy cwd-relative `assets/roms/characters.901225-01.bin` (so an existing checkout keeps working), else the cv2 fallback. Only the automatic answer is cached process-wide; an explicit path is read fresh, because the cache is deliberately not keyed by path and would otherwise hand every caller whichever file was asked for first.

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
