"""TeensyROM+ backend — the TR-family implementation of `C64Backend`.

Implements the same duck-typed surface the rest of c64cast depends on, on
top of the TR token protocol ([teensyrom_dma.py](teensyrom_dma.py)):

  * **Writes** (`write_memory*`, `write_regs`, `write_region`) ride the
    shared `BufferedWriteBackend` delta-cache path; `_emit` splits each push
    into WriteC64Mem segments and waits for the per-segment ack.
  * **reset** maps to ResetC64Token; **run_prg** is synthesised from
    PostFile (upload to SD/USB) + LaunchFile; **probe** uses Ping.
  * **read_memory** rides ReadC64Mem (`0x64FD`), added in the cycle-clean TR+
    firmware (v0.7.2.5). The protocol-level capability is declared on the
    profile, but `__init__` *probes* for it at connect (a tiny ROM read) and
    downgrades `supports_read` if the connected build lacks the token — so an
    older firmware still degrades gracefully instead of NAK/timeout-ing every
    keyboard poll. Read support unlocks the `$028D` keyboard poller (physical
    pause/skip/cycle/menu control), same as the Ultimate.
  * **run_sid_player / cue_song_reinit / reu_write** are left on the ABC's
    raising defaults — SID-player launch is a later phase and there is no
    REUWRITE opcode. Callers gate on `profile.supports_*`.

The semantic helpers (`silence_sid`, `restore_kernal_irq_vector`,
`suppress_cursor_blink`, `disable_case_switch`) are inherited from
`BufferedWriteBackend` — they're pure writes on the standard C64 map.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from dataclasses import replace

from .backend import BufferedWriteBackend, HardwareProfile
from .teensyrom_dma import (
    DRIVE_SD,
    DRIVE_USB,
    TRClient,
    TRError,
    TRTransport,
)

log = logging.getLogger(__name__)

# Where uploaded helper PRGs live on the TR's storage. PostFile auto-creates
# the directory; a dedicated folder keeps our files out of the user's roots.
_UPLOAD_DIR = "c64cast"
_SPIN_NAME = "spin.prg"
_CLEARLOOP_NAME = "clearloop.prg"

# Idle bring-up has two strategies, picked by firmware (see
# run_basic_clear_loop):
#
# (1) IRQ-ENABLED CLEAR-LOOP (default on cycle-clean firmware, fw >= v0.7.2.5).
#     Launch the same `10 PRINT CHR$(147):20 GOTO 20` BASIC PRG the Ultimate
#     runs (api.BASIC_CLEAR_LOOP_PRG): CHR$(147) clears the screen, the GOTO
#     loop keeps the kernal IRQ scanning the keyboard (so $028D stays live for
#     the keyboard poller) while staying out of the editor's direct-input mode
#     (cursor blink suppressed for free). Safe now that the TR's WriteC64Mem
#     DMA is cycle-clean — a running interpreter survives sustained hammering
#     (HW-verified). Read support is the proxy for "new enough firmware": both
#     ReadC64Mem and the cycle-clean DMA fix shipped together, so the idle
#     follows `profile.supports_read`.
#
# (2) SPIN-STUB FALLBACK (older firmware, before the DMA was cycle-clean).
#     C64-side "park the CPU" machine code, DMA'd to $C000 and entered via a
#     launched BASIC `SYS 49152` stub. On pre-cycle-clean firmware the TR's
#     WriteC64Mem DMA perturbs the running 6510, so streaming over a running
#     BASIC program corrupts it within seconds ("?UNDEF'D STATEMENT", "?SYNTAX
#     ERROR"). This stub instead parks the CPU with IRQs masked, executing only
#     a NOP sled that loops on itself: SEI, 252×NOP, then JMP back to the top.
#     A perturbed cycle just lands somewhere in the sled and slides back to the
#     JMP — there's no interpreter state to corrupt. The trade-off is that the
#     kernal IRQ (cursor blink / keyboard scan) never runs, so $028D is frozen
#     and there's no physical-keyboard control on old firmware. The VIC keeps
#     refreshing the display from screen RAM, which we update by DMA.
#   $C000 SEI            ; 78        mask IRQs
#   $C001 NOP × 252      ; EA…       glitch-tolerant sled
#   $C0FD JMP $C000      ; 4C 00 C0  loop forever
_SPIN_STUB_ADDR = 0xC000
_SPIN_STUB = bytes([0x78]) + bytes([0xEA]) * 252 + bytes([0x4C, 0x00, 0xC0])

# After ResetC64Token the TR reboots its menu + re-inits SD, which takes a
# few seconds; PostFile is refused (FailToken) until the menu handler is
# ready. Retry the bring-up with backoff so it's robust to that timing
# regardless of the caller's fixed post-reset delay.
_BRINGUP_ATTEMPTS = 6
_BRINGUP_RETRY_S = 1.0

# After LaunchFile the TR loader prints "RUNNING..." on the C64 screen; let it
# land before we DMA-clear, so the clear isn't immediately overwritten.
_LAUNCH_SETTLE_S = 0.6

# Standard C64 screen RAM + dimensions, used to blank the boot banner / loader
# message the spin stub leaves on screen (the spin MC, unlike the U64's BASIC
# clear-loop, doesn't PRINT CHR$(147)).
_SCREEN_RAM = 0x0400
_SCREEN_CELLS = 1000
_SC_SPACE = 0x20


class TeensyROMBackend(BufferedWriteBackend):
    def __init__(self, transport: TRTransport, *, profile: HardwareProfile, storage: str = "sd"):
        super().__init__()
        self.profile = profile
        self._drive = DRIVE_SD if storage.lower() == "sd" else DRIVE_USB
        self.tr = TRClient(transport)
        # connect() raises TRError on a bad port / unreachable listener; let
        # it propagate so the CLI can render a user-actionable message.
        self.tr.connect()
        # The profile declares read support at the protocol level (ReadC64Mem
        # exists), but a given device may run pre-v0.7.2.5 firmware that lacks
        # the token. Probe once and downgrade rather than NAK/timeout-ing every
        # keyboard poll — version-robust without parsing the ping banner.
        if self.profile.supports_read and not self._probe_read():
            self.profile = replace(self.profile, supports_read=False)
            log.info(
                "TR firmware lacks ReadC64Mem — physical-keyboard control "
                "disabled (use the control plane); upgrade to fw >= v0.7.2.5"
            )

    def _probe_read(self) -> bool:
        """Confirm the connected firmware answers ReadC64Mem. Reads 2 bytes of
        KERNAL ROM ($FFFC reset vector — always mapped, value-stable) and only
        checks the round-trip succeeds; older builds NAK/timeout the unknown
        token. Best-effort: any failure -> no read support, drained to resync."""
        try:
            data = self.tr.read_segment(0xFFFC, 2)
            return len(data) == 2
        except (OSError, TRError) as e:
            log.debug("TR read-capability probe failed (%s); assuming no read", e)
            # An unknown token on old firmware may leave trailing bytes; clear
            # them so the next real command starts on a clean offset.
            with contextlib.suppress(OSError, TRError):
                self.tr._drain_stale(0.2)
            return False

    # ---- write path -------------------------------------------------------
    _EMIT_WRITE_LABEL = "TR write"
    _EMIT_DEVICE_LABEL = "TR"

    def _emit(self, addr: int, payload: bytes) -> None:
        """Split `payload` into WriteC64Mem segments and push each, waiting on
        its ack. Like the Ultimate's _emit, transient transport failures are
        absorbed (logged on the shared escalating ladder) rather than raised —
        a blip shouldn't crash the playlist."""
        try:
            off, n = 0, len(payload)
            while off < n:
                chunk = payload[off : off + self.tr.MAX_SEGMENT_BYTES]
                self.tr.write_segment(addr + off, chunk)
                self._stats["writes"] += 1
                off += len(chunk)
            self._note_emit_success()
        except (OSError, TRError) as e:
            self._note_emit_failure(addr, e)

    def flush(self) -> None:
        # Writes are acked, so this is a no-op barrier; kept for parity.
        self.tr.flush()

    def close(self) -> None:
        self.tr.close()

    def format_write_latency(self) -> str | None:
        return self.tr.format_latency()

    # ---- capability-gated surface (the supported subset) ------------------
    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        """Read `length` bytes from C64 `address` via ReadC64Mem, chunked at
        MAX_SEGMENT_BYTES. Returns the bytes, or **None** on any transport /
        protocol failure.

        Returning None (never raising) is a hard contract: keyboard.py and the
        menu poller call this every ~100 ms and rely on None meaning "couldn't
        tell" so a transient blip doesn't crash the playlist (parallel to the
        Ultimate's REST read). `timeout` is accepted for the C64Backend
        signature; the transport's own io_timeout governs the actual wait."""
        try:
            out = bytearray()
            off = 0
            while off < length:
                n = min(length - off, self.tr.MAX_SEGMENT_BYTES)
                out += self.tr.read_segment(address + off, n)
                off += n
            return bytes(out)
        except (OSError, TRError) as e:
            log.debug("TR read_memory $%04X failed: %s", address, e)
            return None

    def probe(self, timeout: float = 2.0) -> str | None:
        """Liveness probe via Ping; returns the TR's status line or None."""
        try:
            line = self.tr.ping()
            return line or f"TeensyROM ({self.tr.firmware} firmware)"
        except TRError as e:
            log.debug("TR ping failed: %s", e)
            return None

    def reset(self) -> None:
        """Reset the C64 (boots to the TR menu). Best-effort: a failure logs
        but doesn't raise (mirrors the Ultimate's reset)."""
        self.invalidate_cache()
        try:
            self.tr.reset()
        except (OSError, TRError) as e:
            log.warning("TR reset failed: %s", e)

    def run_basic_clear_loop(self, timeout: float = 5.0) -> None:
        """Bring the C64 to a clean, streamable idle state.

        Two strategies, picked by firmware (see the _SPIN_STUB strategy comment
        above):

          * **cycle-clean firmware** (proxied by `profile.supports_read`, since
            ReadC64Mem + the cycle-clean DMA fix shipped together) — launch the
            same IRQ-enabled BASIC clear-loop the Ultimate runs, so the kernal
            keyboard scan keeps `$028D` live for the keyboard poller.
          * **older firmware** (no read support) — fall back to the IRQ-masked
            spin stub, which survives a non-cycle-clean DMA but freezes `$028D`
            (no physical-keyboard control).

        Best-effort: failures log, don't raise. Requires the TR menu active
        (true right after reset()).
        """
        if self.profile.supports_read:
            self._bring_up_irq_clear_loop()
        else:
            self._bring_up_spin_stub()

    def pause_idle(self) -> None:
        """TR paused-idle state — clear the screen but keep the VIC display ON.

        Two hard constraints, both HW-confirmed, shape this:

        1. **Don't reset.** A TR reset() lands at the TeensyROM menu, whose own
           input handling doesn't run the kernal keyboard scan — $028D would
           freeze and the C=-held-to-resume gesture could never be detected.

        2. **Keep DEN on (don't blank_display).** The TR's cycle-clean DMA
           gates its /DMA assert on a safe VIC cycle (a badline). Turning the
           display OFF (DEN=0) removes badlines, so the DMA can never assert —
           every subsequent read (and write) hangs, which both strands resume
           AND wedges the TR until a power-cycle.

        Neither a reset nor a blank is needed: the IRQ-enabled BASIC clear-loop
        set up at bring-up is still running underneath every scene (it's what
        keeps $028D live so pause/skip/cycle work *during* a scene — the DMA
        only overwrites screen/VIC RAM, never the BASIC program). So we just
        DMA-clear screen RAM to spaces for a clean 'paused' screen, leaving DEN
        on — badlines keep flowing and the resume-hold reads keep working. This
        is the closest possible idle to the (working) live-scene state. We also
        suppress the kernal editor's cursor blink (BASIC sits at READY under the
        cleared screen) so the paused screen is a clean blank, not a lone
        blinking cursor."""
        self.write_memory_file(f"{_SCREEN_RAM:04X}", bytes([_SC_SPACE]) * _SCREEN_CELLS)
        self.suppress_cursor_blink()

    def _bring_up_irq_clear_loop(self) -> None:
        """IRQ-enabled idle: PostFile + LaunchFile the BASIC clear-loop PRG
        (`10 PRINT CHR$(147):20 GOTO 20`, shared with the Ultimate). The kernal
        IRQ keeps the keyboard scan alive — so `$028D` updates for the keyboard
        poller — and the DMA is cycle-clean, so this is safe over a live
        interpreter.

        HW note: TR LaunchFile prints "RUNNING..." and doesn't reliably leave
        the tiny PRG in its `GOTO` loop, so the loader text + a BASIC READY
        banner + a blinking cursor are left on screen (the program's CHR$(147)
        never runs). $028D is still live (kernal editor IRQ), so keyboard
        control works regardless — but, like the spin-stub path, we DMA-clear
        the screen + suppress the cursor blink once the loader settles so the
        first scene/interstitial doesn't paint over leftover text. DEN stays
        on, so the cycle-clean DMA keeps working."""
        from .api import BASIC_CLEAR_LOOP_PRG

        self.invalidate_cache()
        path = f"{_UPLOAD_DIR}/{_CLEARLOOP_NAME}"
        if self._upload_and_launch_retry(BASIC_CLEAR_LOOP_PRG, path, "clear-loop"):
            time.sleep(_LAUNCH_SETTLE_S)  # let the loader's "RUNNING..." land
            self.write_memory_file(f"{_SCREEN_RAM:04X}", bytes([_SC_SPACE]) * _SCREEN_CELLS)
            self.suppress_cursor_blink()
            self.invalidate_cache()

    def _bring_up_spin_stub(self) -> None:
        """Legacy IRQ-masked idle for pre-cycle-clean firmware: DMA the spin MC
        to $C000, then launch a `SYS 49152` stub so the 6510 jumps into it.
        LaunchFile loads the BASIC stub at $0801 and won't touch $C000, so the
        MC is still there when SYS jumps to it."""
        from .api import _build_basic_sys_stub

        self.invalidate_cache()
        self.write_memory_file(f"{_SPIN_STUB_ADDR:04X}", _SPIN_STUB)
        self.flush()
        stub_prg = _build_basic_sys_stub(_SPIN_STUB_ADDR)
        path = f"{_UPLOAD_DIR}/{_SPIN_NAME}"
        if self._upload_and_launch_retry(stub_prg, path, "spin-stub"):
            # The CPU is now parked; clear the boot banner / "RUNNING..." the
            # spin stub left on screen (it doesn't PRINT CHR$(147)). Settle
            # first so the loader's print lands before we blank it.
            time.sleep(_LAUNCH_SETTLE_S)
            self.write_memory_file(f"{_SCREEN_RAM:04X}", bytes([_SC_SPACE]) * _SCREEN_CELLS)
            self.invalidate_cache()

    def _upload_and_launch_retry(self, prg: bytes, dest: str, label: str) -> bool:
        """Upload `prg` to `dest` and LaunchFile it, retrying through the brief
        post-reset window where the menu/SD isn't ready yet. `_upload` drains
        stale chatter + deletes any prior copy (PostFile won't overwrite) so
        retries resync cleanly. Returns True on success, False after exhausting
        attempts (logged, not raised — bring-up is best-effort)."""
        last_err: Exception | None = None
        for attempt in range(1, _BRINGUP_ATTEMPTS + 1):
            try:
                self._upload(prg, dest)
                self.tr.launch_file(dest, self._drive)
                if attempt > 1:
                    log.info("TR %s bring-up OK on attempt %d", label, attempt)
                return True
            except (OSError, TRError) as e:
                last_err = e
                log.debug(
                    "TR %s bring-up attempt %d/%d failed: %s",
                    label,
                    attempt,
                    _BRINGUP_ATTEMPTS,
                    e,
                )
                time.sleep(_BRINGUP_RETRY_S)
        log.warning(
            "TR %s bring-up failed after %d attempts: %s", label, _BRINGUP_ATTEMPTS, last_err
        )
        return False

    def _upload(self, data: bytes, dest: str) -> None:
        """Upload `data` to `dest`, replacing any existing file. PostFile
        refuses to overwrite ("File already exists."), so delete first; a
        missing file makes the delete fail, which is expected and ignored."""
        try:
            self.tr.delete_file(dest, self._drive)
        except (OSError, TRError) as e:
            log.debug("TR pre-upload delete of %s ignored: %s", dest, e)
        self.tr.post_file(data, dest, self._drive)

    def launch_program(self, path: str, timeout: float = 10.0) -> None:
        """Upload a local .prg/.crt to TR storage and launch it. Unlike the
        clear-loop, failures re-raise — the caller (LauncherScene) needs to
        know the launch never happened. Requires the TR menu active."""
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".prg", ".crt"):
            raise ValueError(
                f"launch_program: unsupported extension {ext!r} for {path!r} "
                f"(expected .prg or .crt)"
            )
        with open(path, "rb") as fh:
            data = fh.read()
        self.invalidate_cache()
        dest = f"{_UPLOAD_DIR}/{os.path.basename(path)}"
        self._upload(data, dest)
        self.tr.launch_file(dest, self._drive)
