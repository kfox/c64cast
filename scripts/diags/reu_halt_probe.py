#!/usr/bin/env python3
"""Measure what a C64-driven REU transfer halts the 6510 for, per byte.

    scripts/diags/hw_lock.py .venv/bin/python scripts/diags/reu_halt_probe.py --url tr://
    scripts/diags/hw_lock.py .venv/bin/python scripts/diags/reu_halt_probe.py --url u64://HOST

A REC transfer the 6510 starts (a write to $DF01) halts the CPU until the
last byte lands, and the bank-swap IRQ handlers size their chunks so that halt
fits inside one NMI period (modes_irq.BANK_SWAP_CHUNK_SIZE). That sizing needs
the halt's cost per byte, which differs between REU implementations: the
Ultimate's FPGA REC moves a byte a cycle, and the TeensyROM's emulation does
its transfer from the Teensy's PHI2 interrupt.

The C64 times it itself. A routine on the kernal IRQ chain programs a FETCH of
`length` bytes into scratch RAM, starts CIA #2 Timer B from $FFFF, fires the
transfer, and stops the timer on the next instruction; the timer counts PHI2
cycles straight through the halt. Each length is timed several times at
whatever raster line the jiffy IRQ lands on, so the minimum is the transfer
off a badline and the maximum includes the badlines it crossed. The slope of
the minimum over length is the cost per byte; its intercept is the fixed cost
of starting one transfer.

The TeensyROM's REU runs only while REU emulation is on (Special IO set to an
REU in its menu); the clear-loop launch starts it. Resets the machine on exit.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

from c64cast.app.config import Config
from c64cast.app.connect import apply_to_config, parse_connection_uri
from c64cast.hw.asm6502 import assemble
from c64cast.hw.backend import make_backend
from c64cast.hw.c64 import CIA2, KERNAL, REU

ROUTINE = 0xC800
RESULTS = 0xC900  # lo bytes at +0, hi bytes at +$80
INDEX = 0xC8F0
SCRATCH = 0x3000  # BASIC program space; the clear loop leaves it empty
LENGTHS = (1, 8, 20, 40, 80, 160, 255)
TRIALS = 12
IRQ_VECTOR = 0x0314


def _routine(lengths: list[int]) -> bytes:
    table = ", ".join(f"${n:02X}" for n in lengths)
    source = f"""
            LDX ${INDEX:04X}
            CPX #${len(lengths):02X}
            BCS done
            LDA #${SCRATCH & 0xFF:02X}
            STA ${REU.C64_ADDR_LO:04X}
            LDA #${SCRATCH >> 8:02X}
            STA ${REU.C64_ADDR_LO + 1:04X}
            LDA #$00
            STA ${REU.REU_ADDR_LO:04X}
            STA ${REU.REU_ADDR_MI:04X}
            STA ${REU.REU_ADDR_HI:04X}
            STA ${REU.LENGTH_LO + 1:04X}
            STA ${REU.ADDR_CONTROL:04X}
            LDA lengths,X
            STA ${REU.LENGTH_LO:04X}
            LDA #$FF
            STA ${CIA2.BASE + 6:04X}
            STA ${CIA2.BASE + 7:04X}
            LDA #$11
            LDY #${REU.CMD_FETCH_EXEC:02X}
            STA ${CIA2.BASE + 0x0F:04X}
            STY ${REU.COMMAND:04X}
            LDA #$00
            STA ${CIA2.BASE + 0x0F:04X}
            LDA ${CIA2.BASE + 6:04X}
            STA ${RESULTS:04X},X
            LDA ${CIA2.BASE + 7:04X}
            STA ${RESULTS + 0x80:04X},X
            INC ${INDEX:04X}
        done:
            JMP ${KERNAL.IRQ_HANDLER:04X}
        lengths:
            .byte {table}
    """
    return assemble(source, ROUTINE)


def _fit(points: list[tuple[int, int]]) -> tuple[float, float]:
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    slope = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x, _ in points)
    return slope, my - slope * mx


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="tr://", help="connection URI (default tr:// auto serial)")
    args = ap.parse_args()

    cfg = Config()
    apply_to_config(cfg, parse_connection_uri(args.url))
    be = make_backend(cfg)
    lengths = [n for n in LENGTHS for _ in range(TRIALS)]
    try:
        be.reset()
        time.sleep(1.5)
        be.run_basic_clear_loop()
        time.sleep(0.3)
        be.write_memory(f"{INDEX:04X}", "00")
        be.write_memory_file(f"{ROUTINE:04X}", _routine(lengths))
        be.write_memory(f"{IRQ_VECTOR:04X}", f"{ROUTINE & 0xFF:02X}{ROUTINE >> 8:02X}")
        be.flush()
        time.sleep(len(lengths) / 60 + 1.0)
        be.write_memory(
            f"{IRQ_VECTOR:04X}",
            f"{KERNAL.IRQ_HANDLER & 0xFF:02X}{KERNAL.IRQ_HANDLER >> 8:02X}",
        )
        be.flush()
        done = be.read_memory(INDEX, 1)
        lo = be.read_memory(RESULTS, len(lengths))
        hi = be.read_memory(RESULTS + 0x80, len(lengths))
        if not done or done[0] != len(lengths) or lo is None or hi is None:
            print(f"[!] routine did not finish: index={done!r}")
            return 1
        cycles: dict[int, list[int]] = {}
        for n, a, b in zip(lengths, lo, hi, strict=True):
            cycles.setdefault(n, []).append(0xFFFF - (a | b << 8))
        print(f"{'bytes':>6} {'min':>6} {'median':>7} {'max':>6}  (cycles, timer start to stop)")
        for n, c in cycles.items():
            print(f"{n:>6} {min(c):>6} {statistics.median(c):>7.0f} {max(c):>6}")
        slope, intercept = _fit([(n, min(c)) for n, c in cycles.items()])
        print(
            f"\nhalt ~= {slope:.2f} cycles/byte + {intercept:.0f} cycles per transfer (off badlines)"
        )
    finally:
        be.silence_sid()
        be.reset()
        be.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
