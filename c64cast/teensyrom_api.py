"""TeensyROM+ backend — the TR-family implementation of `C64Backend`.

Implements the same duck-typed surface the rest of c64cast depends on, on
top of the TR token protocol ([teensyrom_dma.py](teensyrom_dma.py)):

  * **Writes** (`write_memory*`, `write_regs`, `write_region`) ride the
    shared `BufferedWriteBackend` delta-cache path; `_emit` splits each push
    into WriteC64Mem segments and waits for the per-segment ack.
  * **reset** maps to ResetC64Token; **run_prg** is synthesised from
    PostFile (upload to SD/USB) + LaunchFile; **probe** uses Ping.
  * **read_memory / run_sid_player / cue_song_reinit / reu_write** are left
    on the ABC's raising defaults — the protocol has no read-C64-memory token
    (the one true gap), SID-player launch is a later phase, and there is no
    REUWRITE opcode. Callers gate on `profile.supports_*`.

The semantic helpers (`silence_sid`, `restore_kernal_irq_vector`,
`suppress_cursor_blink`, `disable_case_switch`) are inherited from
`BufferedWriteBackend` — they're pure writes on the standard C64 map.
"""
from __future__ import annotations

import logging
import os
import time

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

# C64-side "park the CPU" machine code, DMA'd to $C000 and entered via a
# launched BASIC `SYS 49152` stub. The TR's WriteC64Mem DMA is NOT cycle-clean
# — it perturbs the running 6510, so streaming over a running BASIC program
# (the U64's clear-loop approach) corrupts it within seconds ("?UNDEF'D
# STATEMENT", "?SYNTAX ERROR"). This stub instead parks the CPU with IRQs
# masked, executing only a NOP sled that loops on itself: SEI, 252×NOP, then
# JMP back to the top. A perturbed cycle just lands somewhere in the sled and
# slides back to the JMP — there's no interpreter state to corrupt and the
# kernal IRQ (cursor blink / keyboard scan) never runs. The VIC keeps
# refreshing the display from screen RAM, which we update by DMA.
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
    def __init__(self, transport: TRTransport, *,
                 profile: HardwareProfile,
                 storage: str = "sd"):
        super().__init__()
        self.profile = profile
        self._drive = DRIVE_SD if storage.lower() == "sd" else DRIVE_USB
        self.tr = TRClient(transport)
        # connect() raises TRError on a bad port / unreachable listener; let
        # it propagate so the CLI can render a user-actionable message.
        self.tr.connect()

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
                chunk = payload[off:off + self.tr.MAX_SEGMENT_BYTES]
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

        Unlike the Ultimate (which RUNs a BASIC clear-loop), the TR parks the
        6510 in a glitch-tolerant machine-code spin loop — see [_SPIN_STUB].
        The TR's WriteC64Mem DMA perturbs a running CPU, so leaving BASIC's
        interpreter executing corrupts it within seconds; the NOP-sled spin
        has no interpreter state to corrupt and masks IRQs (no cursor blink).

        Sequence: DMA the spin MC to $C000, then launch a `SYS 49152` BASIC
        stub so the 6510 jumps into it. Best-effort: failures log, don't raise.
        Requires the TR menu active (true right after reset()).
        """
        from .api import _build_basic_sys_stub
        self.invalidate_cache()
        # DMA the spin MC first; LaunchFile loads the BASIC stub at $0801 and
        # won't touch $C000, so the MC is still there when SYS jumps to it.
        self.write_memory_file(f"{_SPIN_STUB_ADDR:04X}", _SPIN_STUB)
        self.flush()
        stub_prg = _build_basic_sys_stub(_SPIN_STUB_ADDR)
        path = f"{_UPLOAD_DIR}/{_SPIN_NAME}"
        # Retry handles the brief window after reset where the menu/SD isn't
        # ready yet; _upload drains stale chatter + deletes any prior copy
        # (PostFile won't overwrite) so retries resync cleanly.
        last_err: Exception | None = None
        for attempt in range(1, _BRINGUP_ATTEMPTS + 1):
            try:
                self._upload(stub_prg, path)
                self.tr.launch_file(path, self._drive)
                if attempt > 1:
                    log.info("TR spin-stub bring-up OK on attempt %d", attempt)
                # The CPU is now parked; clear the boot banner / "RUNNING..."
                # the spin stub left on screen (it doesn't PRINT CHR$(147)).
                # Settle first so the loader's print lands before we blank it.
                time.sleep(_LAUNCH_SETTLE_S)
                self.write_memory_file(f"{_SCREEN_RAM:04X}",
                                       bytes([_SC_SPACE]) * _SCREEN_CELLS)
                self.invalidate_cache()
                return
            except (OSError, TRError) as e:
                last_err = e
                log.debug("TR bring-up attempt %d/%d failed: %s",
                          attempt, _BRINGUP_ATTEMPTS, e)
                time.sleep(_BRINGUP_RETRY_S)
        log.warning("TR spin-stub bring-up failed after %d attempts: %s",
                    _BRINGUP_ATTEMPTS, last_err)

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
                f"(expected .prg or .crt)")
        with open(path, "rb") as fh:
            data = fh.read()
        self.invalidate_cache()
        dest = f"{_UPLOAD_DIR}/{os.path.basename(path)}"
        self._upload(data, dest)
        self.tr.launch_file(dest, self._drive)
