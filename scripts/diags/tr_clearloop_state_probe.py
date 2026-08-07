#!/usr/bin/env python3
"""Read what the C64 is actually doing at each step of the TeensyROM+ BASIC
clear-loop bring-up, so "is the loop running?" stops being inferred.

The clear loop (`10 PRINT CHR$(147) : 20 GOTO 20`) is the only thing that stops
the kernal cursor blinking — poking BLNSW ($CC) never holds, because the
editor's input-wait loop rewrites it every pass. So a blinking cursor means the
loop is not running, and this tool says why.

Reported at every step, all via ReadC64Mem (no writes, so the probe can't be
what perturbs the state):

  * CURLIN ($39/$3A) — the line BASIC is executing. Measured on this hardware it
    reads $0000 at the READY prompt and $0014 (line 20) running the clear loop;
    the widely-quoted "$FF in the high byte means direct mode" did not hold.
  * TXTTAB ($0801..) — the program body, with its first line-link pointer. TR
    LaunchFile is known to leave that pointer zeroed, which makes BASIC see an
    empty program.
  * VARTAB ($2D/$2E) — $0803 means BASIC believes the program is empty.
  * BLNSW ($CC) + the kernal cursor bytes BLNON ($CF) / GDBLN ($CE) / BLNCT
    ($CD) — what the blink itself is doing.
  * A slice of screen RAM, so a READY banner or a stray cursor cell is visible
    from here.

Steps walked (--step to stop early):

  1. connect  — state as found, before anything is touched
  2. reset    — after TR reset(); does it land at the TR menu or a BASIC screen?
  3. launch   — after PostFile + LaunchFile of the clear-loop PRG
  4. repair   — after _ensure_clear_loop_running's hand-DMA + typed RUN

    scripts/diags/tr_clearloop_state_probe.py
    scripts/diags/tr_clearloop_state_probe.py --serial /dev/cu.usbmodem<XXXX>
    scripts/diags/tr_clearloop_state_probe.py --step reset --repeat 3

Always resets the C64 on the way out (the standing silence-and-reset rule).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from c64cast.api import BASIC_CLEAR_LOOP_PRG  # noqa: E402
from c64cast.backend import make_backend  # noqa: E402
from c64cast.c64 import SCREEN  # noqa: E402
from c64cast.config import Config  # noqa: E402
from c64cast.connect import apply_to_config, parse_connection_uri  # noqa: E402
from c64cast.teensyrom_api import _RUN_RETURN  # noqa: E402

_STEPS = ("connect", "reset", "launch", "repair")

# Zero page the kernal editor + BASIC keep their state in.
_CURLIN = 0x0039  # low/high; $0000 at READY here, else the executing line
_VARTAB = 0x002D
_BLNCT = 0x00CD  # countdown to next blink toggle
_GDBLN = 0x00CE  # char under the cursor
_BLNON = 0x00CF  # 0 = cursor currently drawn as the char, else inverted
_BLNSW = 0x00CC  # 0 = blink enabled
_TXTTAB = 0x0801
_SCREEN = 0x0400


def _le16(b: bytes) -> int:
    """The first two bytes as the little-endian word every 6502 pointer is."""
    return b[0] | (b[1] << 8)


def _rd(be, addr: int, n: int) -> bytes | None:
    try:
        return be.read_memory(addr, n)
    except Exception as e:  # noqa: BLE001 — diag: report, never abort a walk
        print(f"    read ${addr:04X}+{n} failed: {e}")
        return None


def _hex(b: bytes | None) -> str:
    return "??" if b is None else " ".join(f"{x:02X}" for x in b)


def _petscii_screen(b: bytes | None) -> str:
    """Screen codes -> readable ASCII, for spotting READY./cursor cells."""
    if b is None:
        return "??"
    out = []
    for c in b:
        ch = c & 0x7F
        if ch == 0x20:
            out.append(" ")
        elif 0x01 <= ch <= 0x1A:
            out.append(chr(ord("A") + ch - 1))
        elif 0x30 <= ch <= 0x39:
            out.append(chr(ch))
        elif ch == 0x2E:
            out.append(".")
        else:
            out.append("·")
    return "".join(out)


def _force_repair(be) -> None:
    """The repair `_ensure_clear_loop_running` performs, minus its at-READY
    gate: the same body + VARTAB DMA and the same RUN keystrokes, built from
    the same named constants, so a change to the real sequence is measured
    here rather than silently diverged from."""
    body = BASIC_CLEAR_LOOP_PRG[2:]  # drop the 2-byte load address
    end = _TXTTAB + len(body)
    be.write_memory_file(f"{_TXTTAB:04X}", body)
    be.write_memory(f"{_VARTAB:04X}", f"{end & 0xFF:02X}{(end >> 8) & 0xFF:02X}")
    be.write_memory_file(f"{SCREEN.KB_BUFFER:04X}", _RUN_RETURN)
    be.write_memory(f"{SCREEN.KB_BUFFER_LEN:04X}", f"{len(_RUN_RETURN):02X}")
    be.flush()


def report(be, label: str) -> None:
    print(f"  [{label}]")
    curlin = _rd(be, _CURLIN, 2)
    vartab = _rd(be, _VARTAB, 2)
    body = _rd(be, _TXTTAB, 16)
    blink = _rd(be, _BLNSW, 4)  # $CC $CD $CE $CF
    screen = _rd(be, _SCREEN, 40 * 6)

    if curlin is not None:
        line = _le16(curlin)
        at_ready = curlin[1] == 0xFF or line == 0
        mode = "DIRECT (READY prompt)" if at_ready else f"running line {line}"
        print(f"    CURLIN  $39/$3A = {_hex(curlin)}  -> {mode}")
    if vartab is not None:
        v = _le16(vartab)
        note = "  <- $0803: BASIC sees an EMPTY program" if v == 0x0803 else ""
        print(f"    VARTAB  $2D/$2E = {_hex(vartab)}  -> ${v:04X}{note}")
    if body is not None:
        link = _le16(body)
        note = "  <- zeroed: first line link is broken" if link == 0 else ""
        print(f"    TXTTAB  $0801   = {_hex(body)}")
        print(f"      line-link       = ${link:04X}{note}")
    if blink is not None:
        print(
            f"    BLNSW ${blink[0]:02X}  BLNCT ${blink[1]:02X}  "
            f"GDBLN ${blink[2]:02X}  BLNON ${blink[3]:02X}"
            + ("  <- blink ENABLED" if blink[0] == 0 else "  <- blink suppressed")
        )
    if screen is not None:
        for r in range(len(screen) // 40):
            print(f"    screen row {r}    = |{_petscii_screen(screen[r * 40 : (r + 1) * 40])}|")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="tr://", help="connection URI (default tr://)")
    ap.add_argument("--step", choices=_STEPS, default="repair", help="stop after this step")
    ap.add_argument(
        "--force-repair",
        action="store_true",
        help="do the repair unconditionally, bypassing the _basic_is_at_ready gate, "
        "so a known-good running loop can be measured",
    )
    ap.add_argument("--repeat", type=int, default=1, help="re-read the final state N times")
    ap.add_argument("--settle", type=float, default=1.0, help="seconds between re-reads")
    args = ap.parse_args()

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)
    stop = _STEPS.index(args.step)

    try:
        print(f"== connect ({args.url}) ==")
        report(be, "as found")

        if stop >= 1:
            print("\n== reset ==")
            # Screen content alone can't tell "reset and redrew the banner" from
            # "never reset" — both look identical. A marker can: the kernal's
            # init clears the screen, so surviving the reset proves it didn't
            # happen. Row 10 is well clear of the banner.
            marker = bytes([0x0D, 0x05, 0x01, 0x04, 0x0D, 0x05]) * 2  # "MEADME" x2
            be.write_memory_file(f"{_SCREEN + 400:04X}", marker)
            be.flush()
            print(f"    marker written to ${_SCREEN + 400:04X}: {_hex(marker)}")
            reply = be.tr.transport.drain_text(0.3)
            be.reset()
            reply = be.tr.transport.drain_text(0.5)
            print(f"    reset reply: {reply.strip()!r}")
            time.sleep(2.0)
            got = _rd(be, _SCREEN + 400, len(marker))
            print(f"    marker now: {_hex(got)}")
            if got == marker:
                print("      <- MARKER SURVIVED: the C64 did NOT actually reset")
            else:
                print("      <- marker cleared: a real reset happened")
            report(be, "after reset")

        if stop >= 2:
            print("\n== launch clear-loop ==")
            path = "/c64cast/clearloop.prg"
            ok = be._upload_and_launch_retry(BASIC_CLEAR_LOOP_PRG, path, "clear-loop")
            print(f"    upload+launch ok={ok}")
            be._settle_after_launch()
            report(be, "after launch")

        if stop >= 3:
            print("\n== repair ==")
            print(f"    _basic_is_at_ready() -> {be._basic_is_at_ready()}")
            if args.force_repair:
                _force_repair(be)
                print("    forced: body + VARTAB re-DMA'd, RUN typed into KEYD")
                time.sleep(1.0)
            else:
                be._ensure_clear_loop_running(BASIC_CLEAR_LOOP_PRG)
            report(be, "after repair")

        for i in range(1, args.repeat):
            time.sleep(args.settle)
            print(f"\n== re-read {i} (+{args.settle * i:.1f}s) ==")
            report(be, "steady state")
    finally:
        print("\n== reset on exit ==")
        try:
            be.reset()
        except Exception as e:  # noqa: BLE001
            print(f"    reset failed: {e}")
        be.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
