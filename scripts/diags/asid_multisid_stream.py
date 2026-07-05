#!/usr/bin/env python
"""Stream a synthetic multi-SID ASID sequence to a virtual MIDI port.

Drives AsidScene's multi-SID path for hardware verification: opens a virtual
MIDI *output* port (visible system-wide) and streams packed ASID register frames
for N SID chips, each playing a distinct triad so every chip's three voices are
audible and visible on the split oscilloscope.

Usage:
  python scripts/diags/asid_multisid_stream.py --chips 3 --seconds 20 \
      --port c64cast-asid-test
  # then, in another shell:
  scripts/c64cast.sh -u u64://192.168.2.64 \
      --config config/examples/scene-asid.toml   # set asid_port = "c64cast-asid-test"

ASID frame format (spec /Users/kfox/src/asid-protocol): F0 2D <cmd> <mask4>
<msb4> <data...> F7 — cmd 0x4E = SID1, 0x50+k = SID(k+2). Register IDs per the
spec table (see c64cast/asid.py::_ASID_REG_TO_OFFSET).
"""

from __future__ import annotations

import argparse
import time

import mido

MANUF = 0x2D
CMD_REG = 0x4E
CMD_MULTI_LO = 0x50
CMD_START = 0x4C
CMD_STOP = 0x4D

# ASID register IDs (see spec table).
V_FREQ_LO = (0, 6, 12)
V_FREQ_HI = (1, 7, 13)
V_PW_LO = (2, 8, 14)
V_PW_HI = (3, 9, 15)
V_AD = (4, 10, 16)
V_SR = (5, 11, 17)
V_CTRL = (22, 23, 24)
ID_VOLUME = 21

WAVE_TRIANGLE = 0x10
WAVE_SAWTOOTH = 0x20
WAVE_PULSE = 0x40
GATE = 0x01

# A distinct triad + waveform per chip so chips are easy to tell apart.
_CHIP_WAVES = [
    WAVE_TRIANGLE,
    WAVE_SAWTOOTH,
    WAVE_PULSE,
    WAVE_TRIANGLE,
    WAVE_SAWTOOTH,
    WAVE_PULSE,
    WAVE_TRIANGLE,
    WAVE_SAWTOOTH,
]
# SID freq words for a rising set of triads (one triad per chip).
_CHIP_TRIADS = [
    (0x1D2C, 0x2452, 0x2B8F),  # ~A3 C#4 E4
    (0x3A59, 0x48A5, 0x571E),  # an octave up
    (0x2B8F, 0x36B8, 0x411A),  # a different chord
    (0x1687, 0x1B52, 0x2452),
    (0x48A5, 0x571E, 0x6D3A),
    (0x36B8, 0x411A, 0x4D04),
    (0x2452, 0x2B8F, 0x36B8),
    (0x571E, 0x6D3A, 0x7FFF),
]


def _reg_frame(cmd: int, regs: dict[int, int]) -> mido.Message:
    mask = [0, 0, 0, 0]
    msb = [0, 0, 0, 0]
    data: list[int] = []
    for rid in sorted(regs):
        bi, bit = divmod(rid, 7)
        mask[bi] |= 1 << bit
        if regs[rid] & 0x80:
            msb[bi] |= 1 << bit
        data.append(regs[rid] & 0x7F)
    return mido.Message("sysex", data=[MANUF, cmd, *mask, *msb, *data])


def _chip_regs(chip: int) -> dict[int, int]:
    triad = _CHIP_TRIADS[chip % len(_CHIP_TRIADS)]
    wave = _CHIP_WAVES[chip % len(_CHIP_WAVES)]
    regs: dict[int, int] = {ID_VOLUME: 0x0F}
    for v in range(3):
        freq = triad[v]
        regs[V_FREQ_LO[v]] = freq & 0xFF
        regs[V_FREQ_HI[v]] = (freq >> 8) & 0xFF
        regs[V_PW_LO[v]] = 0x00  # 50% duty (PW = $800) so pulse voices render
        regs[V_PW_HI[v]] = 0x08
        regs[V_AD[v]] = 0x09  # quick attack, short decay
        regs[V_SR[v]] = 0xF0  # full sustain
        regs[V_CTRL[v]] = wave | GATE
    return regs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chips", type=int, default=3, help="number of SID chips (1-8)")
    ap.add_argument("--seconds", type=float, default=20.0, help="stream duration")
    ap.add_argument("--port", default="c64cast-asid-test", help="virtual port name")
    ap.add_argument(
        "--wait",
        type=float,
        default=6.0,
        help="seconds to wait for c64cast to connect before streaming",
    )
    args = ap.parse_args()

    print(f"Opening virtual MIDI output {args.port!r} ...")
    with mido.open_output(args.port, virtual=True) as out:
        print(
            f"Port open. Waiting {args.wait}s for c64cast to connect to it "
            f"(set asid_port = {args.port!r}) ..."
        )
        time.sleep(args.wait)
        out.send(mido.Message("sysex", data=[MANUF, CMD_START]))
        # Stream each chip's frame at ~50 Hz; chip 0 uses 0x4E, others 0x50+.
        end = time.time() + args.seconds
        frame = 0
        while time.time() < end:
            for chip in range(args.chips):
                cmd = CMD_REG if chip == 0 else CMD_MULTI_LO + (chip - 1)
                out.send(_reg_frame(cmd, _chip_regs(chip)))
            frame += 1
            if frame % 50 == 0:
                print(f"  streamed {frame} frames for {args.chips} chip(s)")
            time.sleep(1.0 / 50.0)
        out.send(mido.Message("sysex", data=[MANUF, CMD_STOP]))
        print("Done streaming.")


if __name__ == "__main__":
    main()
