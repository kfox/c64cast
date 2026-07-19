#!/usr/bin/env python
"""Drive c64cast's ``[midi_control]`` surface from a virtual MIDI port.

Opens a virtual MIDI *output* port (visible system-wide as an input to other
apps, the CoreMIDI/ALSA convention) and sends notes / CCs / program-changes to
it. Point a running c64cast at the same port name and its MIDI control listener
picks the messages up — no physical controller needed. This is the reusable
form of the throwaway ``midi_smoke.py`` scripts used to HW-verify the MidiScene
and MIDI live-tune (transport / audio-resync) features.

The port MUST exist before c64cast boots, or ``midi_control`` finds no matching
input and disables itself. So start this tool (or at least open the port) first.

Usage
-----
  # 1) start the virtual port + run a scripted sequence:
  python scripts/diags/midi_drive.py --port c64castmidi --script - <<'EOF'
  wait 3          # let the scene establish
  note 62         # e.g. transport.loop_toggle -> mark A
  wait 3
  note 62         # mark B + start looping
  wait 30
  EOF

  # 2) or interactively (type commands, one per line; Ctrl-D to quit):
  python scripts/diags/midi_drive.py --port c64castmidi -i

  # 3) or one-shot:
  python scripts/diags/midi_drive.py --port c64castmidi --send "cc 20 64"

  # then, in another shell, run c64cast with a matching cc_map + port. Example
  # [midi_control] (see --describe section:midi_control for the full surface):
  #   [midi_control]
  #   enabled = true
  #   port = "c64castmidi"
  #   [[midi_control.cc_map]]
  #   type = "note"; number = 60; action = "transport.play_pause"
  #   [[midi_control.cc_map]]
  #   type = "note"; number = 62; action = "transport.loop_toggle"
  #   [[midi_control.cc_map]]
  #   type = "cc"; number = 20; action = "transport.jog"; mode = "abs"

Script / command language (one per line; '#' comments ok)
  note  N [V]     note_on  N (velocity V, default 100)
  noteoff N       note_off N
  cc    N V       control_change N = V
  pc    N         program_change N
  raw   ...       hex bytes of a raw message (e.g. "raw F0 7F 7F 06 02 F7" = MMC play)
  wait  S         sleep S seconds (float)
"""

from __future__ import annotations

import argparse
import sys
import time

import mido


def _msg(tokens: list[str]) -> mido.Message | None:
    """Parse one command line into a mido Message (or None for wait/comment)."""
    if not tokens or tokens[0].startswith("#"):
        return None
    op = tokens[0].lower()
    if op == "note":
        n = int(tokens[1])
        v = int(tokens[2]) if len(tokens) > 2 else 100
        return mido.Message("note_on", note=n, velocity=v)
    if op == "noteoff":
        return mido.Message("note_off", note=int(tokens[1]))
    if op == "cc":
        return mido.Message("control_change", control=int(tokens[1]), value=int(tokens[2]))
    if op == "pc":
        return mido.Message("program_change", program=int(tokens[1]))
    if op == "raw":
        data = [int(t, 16) for t in tokens[1:]]
        # A full F0..F7 frame is a sysex; otherwise pass bytes through.
        if data and data[0] == 0xF0 and data[-1] == 0xF7:
            return mido.Message("sysex", data=data[1:-1])
        return mido.Message.from_bytes(bytes(data))
    raise ValueError(f"unknown command: {tokens[0]}")


def run_line(port: mido.ports.BaseOutput, line: str) -> None:
    tokens = line.split()
    if not tokens or tokens[0].startswith("#"):
        return
    if tokens[0].lower() == "wait":
        time.sleep(float(tokens[1]))
        return
    m = _msg(tokens)
    if m is not None:
        port.send(m)
        print(f"  [{time.strftime('%H:%M:%S')}] -> {m}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--port", default="c64castmidi", help="virtual MIDI port name (default: c64castmidi)"
    )
    ap.add_argument("--script", metavar="FILE", help="run a command script ('-' = stdin)")
    ap.add_argument(
        "--send", metavar="CMD", help="send a single command then exit (e.g. 'cc 20 64')"
    )
    ap.add_argument(
        "-i", "--interactive", action="store_true", help="read commands from stdin interactively"
    )
    ap.add_argument(
        "--hold", type=float, default=0.0, help="keep the port open N extra seconds at the end"
    )
    args = ap.parse_args()

    port = mido.open_output(args.port, virtual=True)
    print(f"virtual MIDI port '{args.port}' open (point c64cast's [midi_control].port at it)")
    try:
        if args.send:
            run_line(port, args.send)
        elif args.script:
            if args.script == "-":
                lines = sys.stdin.readlines()
            else:
                with open(args.script) as f:
                    lines = f.readlines()
            for line in lines:
                run_line(port, line.rstrip("\n"))
        elif args.interactive:
            print("commands: note/noteoff/cc/pc/raw/wait (Ctrl-D to quit)")
            for line in sys.stdin:
                run_line(port, line.rstrip("\n"))
        else:
            print("nothing to send (use --script, --send, or -i); holding port open")
            args.hold = max(args.hold, 3600.0)
        if args.hold:
            print(f"holding port open {args.hold:g}s")
            time.sleep(args.hold)
    finally:
        port.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
