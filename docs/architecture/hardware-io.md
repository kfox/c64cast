# Hardware I/O & transports

How c64cast talks to the machine: the DMA/REST client for the Ultimate, the TeensyROM serial/TCP link, and the BASIC stub the C64 runs while c64cast drives it.

Part of the [architecture reference](../architecture.md). For end-user configuration see [usage.md](../usage.md), for known limitations [caveats.md](../caveats.md), and for adding a new Scene/Overlay/DisplayMode/Background [extending.md](../extending.md).

**Contents**

* [`api.py` — Ultimate64API + `socket_dma.py` — SocketDMAClient](#apipy--ultimate64api--socket_dmapy--socketdmaclient)
* [`teensyrom_dma.py` — TeensyROM link errors + the launcher upload race](#teensyrom_dmapy--teensyrom-link-errors--the-launcher-upload-race)
* [Startup: BASIC clear-and-loop program](#startup-basic-clear-and-loop-program)

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
