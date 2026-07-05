#!/usr/bin/env python
"""Stream a synthetic multi-SID / multispeed ASID sequence to a virtual MIDI port.

Drives AsidScene's multi-SID + buffered-player paths for hardware verification:
opens a virtual MIDI *output* port (visible system-wide) and streams packed ASID
register frames for N SID chips, each playing a distinct triad so every chip's
three voices are audible and visible on the split oscilloscope.

Two patterns (`--pattern`):
  * ``static``     — steady triads (the original behavior); good for multi-SID
                     routing + scope layout checks.
  * ``multispeed`` — per-frame modulation (a fast arpeggio, vibrato, and a
                     periodic gate-off→gate-on hard restart) at ``50 × --multiplier``
                     Hz, with a leading 0x31 speed message (multiplier + buffering
                     bit) and, with ``--recipe``, a 0x30 write-order recipe. This
                     is the A/B stimulus for `asid_buffered_player`: clean under
                     the buffered ring player, mangled/decimated under ``off``.

Usage:
  # Multi-SID routing (static):
  python scripts/diags/asid_multisid_stream.py --chips 3 --seconds 20 \
      --port c64cast-asid-test
  # High-multispeed A/B (buffered vs off):
  python scripts/diags/asid_multisid_stream.py --pattern multispeed \
      --multiplier 8 --recipe --seconds 20 --port c64cast-asid-test
  # then, in another shell (set asid_port = "c64cast-asid-test" in the config):
  scripts/c64cast.sh -u u64://192.168.2.64 --config config/examples/scene-asid.toml

ASID frame format (spec /Users/kfox/src/asid-protocol): F0 2D <cmd> <mask4>
<msb4> <data...> F7 — cmd 0x4E = SID1, 0x50+k = SID(k+2). Register IDs per the
spec table (see c64cast/asid.py::_ASID_REG_TO_OFFSET).
"""

from __future__ import annotations

import argparse
import math
import time

import mido

MANUF = 0x2D
CMD_TIMING = 0x30
CMD_SPEED = 0x31
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
V_CTRL = (22, 23, 24)  # control-register first write (voices 1/2/3)
V_CTRL2 = (25, 26, 27)  # control-register second write → hard restart
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


def _multispeed_regs(chip: int, frame: int) -> dict[int, int]:
    """A per-frame-modulated chip frame that only sounds right when EVERY frame
    reaches the SID — the buffered-player stimulus:

      * voice 1: a fast arpeggio (rotate the triad's three notes each frame),
      * voice 2: vibrato (sine-modulate the pitch each frame),
      * voice 3: a hard restart every 8 frames (gate-off then gate-on + waveform
        in one frame, i.e. the ASID double control write).

    Under the coalesced path these collapse to whatever the last flush caught
    (arps stutter, vibrato steps coarsely, restarts vanish); under the buffered
    ring player they replay exactly."""
    triad = _CHIP_TRIADS[chip % len(_CHIP_TRIADS)]
    regs: dict[int, int] = {ID_VOLUME: 0x0F}

    # Voice 1 — arpeggio (SAW: bright, so each note step is audible): step
    # through the triad every frame.
    arp = triad[frame % 3]
    regs[V_FREQ_LO[0]] = arp & 0xFF
    regs[V_FREQ_HI[0]] = (arp >> 8) & 0xFF
    regs[V_PW_LO[0]], regs[V_PW_HI[0]] = 0x00, 0x08
    regs[V_AD[0]], regs[V_SR[0]] = 0x00, 0xF0  # instant attack (arps need it)
    regs[V_CTRL[0]] = WAVE_SAWTOOTH | GATE

    # Voice 2 — vibrato (TRI: smooth) ±~3% around the note, ~one cycle / 16 frames.
    base = triad[1]
    vib = int(base * 0.03 * math.sin(frame * math.pi / 8))
    f2 = max(1, min(0xFFFF, base + vib))
    regs[V_FREQ_LO[1]] = f2 & 0xFF
    regs[V_FREQ_HI[1]] = (f2 >> 8) & 0xFF
    regs[V_PW_LO[1]], regs[V_PW_HI[1]] = 0x00, 0x08
    regs[V_AD[1]], regs[V_SR[1]] = 0x09, 0xF0
    regs[V_CTRL[1]] = WAVE_TRIANGLE | GATE

    # Voice 3 — hard restart every 8 frames (PULSE + a percussive AD envelope so
    # each re-attack is a distinct audible pluck): gate off (control-first) then
    # gate on + waveform in the same frame.
    f3 = triad[2]
    regs[V_FREQ_LO[2]] = f3 & 0xFF
    regs[V_FREQ_HI[2]] = (f3 >> 8) & 0xFF
    regs[V_PW_LO[2]], regs[V_PW_HI[2]] = 0x00, 0x08
    regs[V_AD[2]], regs[V_SR[2]] = 0x0A, 0x00  # fast attack, quick decay to 0 → plucky
    if frame % 8 == 0:
        regs[V_CTRL[2]] = 0x08  # TEST/gate-off first write (id 24)
        regs[V_CTRL2[2]] = WAVE_PULSE | GATE  # second write (id 27) → hard restart
    else:
        regs[V_CTRL[2]] = WAVE_PULSE | GATE
    return regs


def _speed_msg(multiplier: int, *, buffering: bool, ntsc: bool = True) -> mido.Message:
    """0x31 speed: bit0 = NTSC, bits1-4 = multiplier-1, bit6 = buffering."""
    data0 = (0x01 if ntsc else 0x00) | (((multiplier - 1) & 0x0F) << 1)
    if buffering:
        data0 |= 0x40
    # 4-byte payload with frame_delta = 0 (multiplier governs the rate).
    return mido.Message("sysex", data=[MANUF, CMD_SPEED, data0, 0, 0, 0])


def _recipe_msg() -> mido.Message:
    """0x30 identity write-order recipe (ids 0..27, no extra waits) — exercises
    the decoder + buffered player's recipe path without reordering."""
    pairs: list[int] = []
    for rid in range(28):
        pairs += [rid & 0x3F, 0]  # data0 = reg id, data1 = wait 0
    return mido.Message("sysex", data=[MANUF, CMD_TIMING, *pairs])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chips", type=int, default=3, help="number of SID chips (1-8)")
    ap.add_argument("--seconds", type=float, default=20.0, help="stream duration")
    ap.add_argument("--port", default="c64cast-asid-test", help="virtual port name")
    ap.add_argument(
        "--pattern",
        choices=("static", "multispeed"),
        default="static",
        help="static triads, or per-frame arp/vibrato/hard-restart modulation",
    )
    ap.add_argument(
        "--multiplier",
        type=int,
        default=1,
        help="0x31 speed multiplier (1-16); frame rate = base x multiplier Hz",
    )
    ap.add_argument(
        "--system",
        choices=("ntsc", "pal"),
        default="ntsc",
        help="declared system: sets the 0x31 bit AND the base frame rate "
        "(60 Hz ntsc / 50 Hz pal) so the emit rate matches the client's cadence",
    )
    ap.add_argument(
        "--recipe",
        action="store_true",
        help="emit a 0x30 identity write-order recipe before streaming",
    )
    ap.add_argument(
        "--wait",
        type=float,
        default=6.0,
        help="seconds to wait for c64cast to connect before streaming",
    )
    args = ap.parse_args()
    multiplier = max(1, min(args.multiplier, 16))
    ntsc = args.system == "ntsc"
    base_hz = 60.0 if ntsc else 50.0  # match the client's video-rate cadence
    rate = base_hz * multiplier

    print(f"Opening virtual MIDI output {args.port!r} ...")
    with mido.open_output(args.port, virtual=True) as out:
        print(
            f"Port open. Waiting {args.wait}s for c64cast to connect to it "
            f"(set asid_port = {args.port!r}) ..."
        )
        time.sleep(args.wait)
        out.send(mido.Message("sysex", data=[MANUF, CMD_START]))
        if args.pattern == "multispeed":
            # Announce the cadence + ask for buffering; optionally the recipe.
            out.send(_speed_msg(multiplier, buffering=True, ntsc=ntsc))
            if args.recipe:
                out.send(_recipe_msg())
            print(f"  multispeed: {rate:.0f} Hz ({multiplier}x), recipe={args.recipe}")
        # Stream each chip's frame at `rate`; chip 0 uses 0x4E, others 0x50+.
        # Pace against an ABSOLUTE monotonic schedule (next_t += period) rather
        # than sleep(period) so per-loop send overhead doesn't accumulate into a
        # systematic under-production (which would starve a buffered client's
        # ring → hold-pads). This lets the sender actually hit `rate`.
        period = 1.0 / rate
        start = time.monotonic()
        next_t = start
        end = start + args.seconds
        frame = 0
        while time.monotonic() < end:
            for chip in range(args.chips):
                cmd = CMD_REG if chip == 0 else CMD_MULTI_LO + (chip - 1)
                regs = (
                    _multispeed_regs(chip, frame)
                    if args.pattern == "multispeed"
                    else _chip_regs(chip)
                )
                out.send(_reg_frame(cmd, regs))
            frame += 1
            if frame % int(rate) == 0:
                print(f"  streamed {frame} frames for {args.chips} chip(s)")
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        out.send(mido.Message("sysex", data=[MANUF, CMD_STOP]))
        print(f"Done streaming ({frame} frames in {time.monotonic() - start:.1f}s).")


if __name__ == "__main__":
    main()
