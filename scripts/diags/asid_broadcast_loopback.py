#!/usr/bin/env python
"""Offline verification of the ASID host/broadcast path — no C64 hardware.

Runs a real SID tune through the host-side 6502 register tracker
(:class:`c64cast.sid_host_emu.SidHostEmu`, the same source WaveformScene
broadcasts from), packs each PLAY tick with :class:`AsidBroadcaster` into ASID
SysEx, decodes it back with :func:`c64cast.asid.decode`, and applies the deltas
to a receiver-side 25-byte shadow — then asserts the shadow matches the host
emu's register image on every tick. This proves the encoder + delta encoding +
hard-restart reconstruction reproduce the register stream losslessly with real
tune data, and is the reproducible regression check for the broadcast path
(extend it when wiring MidiScene / AsidScene broadcast — see
project_asid_host_midi_asid_scenes).

    uv run python scripts/diags/asid_broadcast_loopback.py assets/sids/…/Gyruss.sid
    uv run python scripts/diags/asid_broadcast_loopback.py --ticks 1200 <tune.sid>

With --midi, additionally round-trips a handful of frames through a real rtmidi
*virtual* MIDI port (broadcaster output → listener input) to exercise mido
serialization end-to-end (skipped automatically if the backend has no virtual
ports). Exit code 0 = all checks pass.
"""

from __future__ import annotations

import argparse
import sys

from c64cast import asid
from c64cast.asid_broadcast import AsidBroadcaster
from c64cast.sid_host_emu import SidHostEmu


class _FakePort:
    """Records the SysEx payloads the broadcaster would send (no MIDI)."""

    def __init__(self) -> None:
        self.msgs: list[tuple[int, ...]] = []

    def send(self, m) -> None:
        self.msgs.append(tuple(m.data))

    def close(self) -> None:
        pass


def run_shadow_compare(path: str, ticks: int, song: int) -> int:
    with open(path, "rb") as f:
        data = f.read()
    emu = SidHostEmu(data, song=song)
    b = AsidBroadcaster("loopback")
    port = _FakePort()
    b._port = port  # inject — no real MIDI
    b.start(frame_rate_hz=50.0, chip_types=["6581"], text="LOOPBACK")

    recv = bytearray(25)
    mismatches = 0
    hard_restarts = 0
    for tick in range(ticks):
        emu.tick_play()
        port.msgs.clear()
        img = emu.regs(0)
        rt = emu.retriggers(0)
        if any(rt):
            hard_restarts += 1
        b.send_frame([img], retrigger=[rt])
        for m in port.msgs:
            u = asid.decode(m)
            assert u is not None and u.command == asid.CMD_REG
            for off, val in u.regs.items():
                recv[off] = val
        if bytes(recv) != bytes(img[:25]):
            mismatches += 1
            if mismatches <= 3:
                diff = [(i, img[i], recv[i]) for i in range(25) if img[i] != recv[i]]
                print(f"  tick {tick} mismatch: {diff}")
    print(
        f"shadow-compare: ticks={ticks} hard_restart_frames={hard_restarts} "
        f"mismatches={mismatches} -> {'PASS' if mismatches == 0 else 'FAIL'}"
    )
    return 0 if mismatches == 0 else 1


def run_midi_transport() -> int:
    import time

    import mido

    port_name = "c64cast-asid-test"
    try:
        inp = mido.open_input(port_name, virtual=True)
    except Exception as e:  # backend without virtual ports (e.g. some Windows)
        print(f"midi-transport: virtual ports unsupported ({e}) — skipped")
        return 0
    b = AsidBroadcaster(port_name, system="NTSC")
    b._open_port()
    b.start(frame_rate_hz=60.0, chip_types=["8580"], text="HELLO")
    img = bytearray(25)
    img[0], img[1], img[24] = 0x34, 0x12, 0x0F
    b.send_frame([img])
    b.send_frame([img])  # identical -> skipped
    img2 = bytearray(img)
    img2[1] = 0x22
    b.send_frame([img2])  # delta
    b.stop()
    time.sleep(0.2)
    cmds: list[int] = []
    reg_msgs: list[dict[int, int]] = []
    for msg in inp.iter_pending():
        if msg.type != "sysex":
            continue
        u = asid.decode(msg.data)
        if u is None:
            continue
        cmds.append(u.command)
        if u.command == asid.CMD_REG:
            reg_msgs.append(u.regs)
    inp.close()
    ok = (
        asid.CMD_START in cmds
        and asid.CMD_STOP in cmds
        and cmds.count(asid.CMD_REG) == 2
        and reg_msgs
        and reg_msgs[-1] == {0x01: 0x22}
    )
    print(f"midi-transport: commands={[hex(c) for c in cmds]} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sid", help="path to a .sid tune")
    ap.add_argument("--ticks", type=int, default=600, help="PLAY ticks to run (default 600 ≈ 12s)")
    ap.add_argument("--song", type=int, default=0, help="subtune (0 = default)")
    ap.add_argument("--midi", action="store_true", help="also run the rtmidi virtual-port loopback")
    args = ap.parse_args()
    rc = run_shadow_compare(args.sid, args.ticks, args.song)
    if args.midi:
        rc |= run_midi_transport()
    return rc


if __name__ == "__main__":
    sys.exit(main())
