#!/usr/bin/env python3
"""What a held bus costs TeensyROM+ writes while slicing is on.

    scripts/diags/tr_spans_refusal.py --url tr://
    scripts/diags/tr_spans_refusal.py --url tr:// --held-s 20 --free-s 20

The firmware refuses WriteC64Spans at its token while another DMA user holds
the bus: REU emulation running, or the C64 paused. This tool pauses the C64
with the firmware's own pause command (C64PauseOnToken `0x6431`), writes
`--size` bytes to $4000 every `--period` seconds as an NMI consumer would see
them sliced, then unpauses and keeps writing. One line per write: the phase,
the time, the write's round trip, how many WriteC64Spans it asked for, how many
were refused and the time spent in them, and the bitmap + DAC tempo the backend
would report at that moment.

Read it for three things: a write that asked and was refused pays the
refusal's text drain; a write inside the back-off asks nothing and costs a
plain WriteC64Mem; and the asks while held are spaced by the doubling back-off,
the first one after the unpause landing sliced again.

Writes land at $4000, plain RAM. Unpauses and resets the machine on exit.
"""

from __future__ import annotations

import argparse
import logging
import os
import time

import _diaglib as d

from c64cast.app.config import Config
from c64cast.app.connect import apply_to_config, parse_connection_uri
from c64cast.hw.backend import make_backend
from c64cast.hw.teensyrom_api import TeensyROMBackend
from c64cast.hw.teensyrom_dma import TOK_ACK, TRSpansRefused

# Common_Defs.h; the firmware acks each.
_PAUSE_ON = 0x6431
_PAUSE_OFF = 0x6430
_TARGET = 0x4000


def _send_pause(be: TeensyROMBackend, token: int) -> bool:
    """Send a pause token; True when the firmware acked it."""
    tr = be.tr
    with tr._command():
        # Stale chatter read as this ack would leave the real ack for the next
        # write to read as its own, and every write after it one reply behind.
        tr._drain_stale()
        tr.transport.send_all(tr._u16(token))
        tok = tr._read_token()
        # A refused token is followed by its text, which the next write would
        # read as its ack.
        detail = "" if tok == TOK_ACK else tr.transport.drain_text(0.3).strip()
    state = "ack" if tok == TOK_ACK else f"unexpected 0x{tok:04X} {detail!r}"
    print(f"[pause] token 0x{token:04X}: {state}")
    return tok == TOK_ACK


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="tr://")
    ap.add_argument(
        "--free-s",
        type=float,
        default=2.0,
        help="seconds before the pause, and after the first ask once it is lifted",
    )
    ap.add_argument("--held-s", type=float, default=10.0, help="seconds paused")
    ap.add_argument("--period", type=float, default=0.1, help="seconds between writes")
    ap.add_argument("--size", type=int, default=2048, help="bytes per write")
    d.add_tr_slicing_args(ap)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(relativeCreated)7.0f ms %(message)s")

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    cfg.teensyrom.dma_slicing = "auto"
    d.apply_tr_slicing(cfg, args)
    be = make_backend(cfg)
    if not isinstance(be, TeensyROMBackend):
        print("[abort] not a TeensyROM URL")
        be.close()
        return 1
    print(f"[setup] {args.url}  writes: {d.describe_tr_writes(be)}")

    asked = {"n": 0, "refused": 0, "ms": 0.0}
    write_spans = be.tr.write_spans

    def counted(*a, **kw):
        asked["n"] += 1
        t0 = time.perf_counter()
        try:
            return write_spans(*a, **kw)
        except TRSpansRefused:
            asked["refused"] += 1
            raise
        finally:
            asked["ms"] += (time.perf_counter() - t0) * 1000

    be.tr.write_spans = counted  # type: ignore[method-assign]
    t_start = time.monotonic()

    def burst(phase: str, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            n0, r0, m0 = asked["n"], asked["refused"], asked["ms"]
            t0 = time.perf_counter()
            # Fresh bytes each time, so the delta cache cannot skip the write.
            be.write_memory_file(f"{_TARGET:04X}", os.urandom(args.size))
            ms = (time.perf_counter() - t0) * 1000
            print(
                f"{phase:6s} t={time.monotonic() - t_start:6.2f}s write={ms:7.1f} ms "
                f"asked={asked['n'] - n0} refused={asked['refused'] - r0} "
                f"in_spans={asked['ms'] - m0:6.1f} ms tempo={be.dac_bitmap_tempo(True):.2f}"
            )
            time.sleep(args.period)

    try:
        be.note_nmi_consumer(True)
        burst("free", args.free_s)
        if not _send_pause(be, _PAUSE_ON):
            # The bus was never held, so a "held" phase would time free writes.
            print("[abort] the firmware did not ack the pause")
            return 1
        burst("held", args.held_s)
        if not _send_pause(be, _PAUSE_OFF):
            # The bus may still be held, so a "freed" phase would time held writes.
            print("[abort] the firmware did not ack the unpause")
            return 1
        # The first ask after the unpause waits out the back-off then in force,
        # up to SPANS_REFUSED_BACKOFF_MAX_S, which can outlast --free-s.
        burst("freed", max(0.0, be._spans_retry_at - time.monotonic()) + args.free_s)
        print(f"[stats] {be.stats}")
    finally:
        try:
            _send_pause(be, _PAUSE_OFF)
        except Exception as e:  # noqa: BLE001 — the reset below must still run
            print(f"[teardown] unpause failed: {type(e).__name__}: {e}")
        be.note_nmi_consumer(False)
        be.reset()
        be.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
