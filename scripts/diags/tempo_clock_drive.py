#!/usr/bin/env python
"""Emit a MIDI beat clock over a virtual port to drive c64cast's ``[performance]``
tempo grid (Live DJ/VJ Phase 1 — see docs/architecture/control.md).

Opens a virtual MIDI *output* port (visible system-wide as an input to other
apps, the CoreMIDI/ALSA convention) and streams the real-time transport bytes the
:class:`c64cast.tempo.TempoClock` consumes: ``0xFA`` Start, ``0xF8`` clock at
24 PPQN paced to a target BPM, an optional ``0xF2`` Song-Position seek, and
``0xFC`` Stop at the end. Point a running c64cast at the same port name (with
``[performance] tempo_source = "midi"`` and ``[midi_control] enabled = true``,
or a dedicated ``[performance] clock_port``) and its beat grid locks to this
tempo — no DAW or hardware needed.

The port MUST exist before c64cast boots, or ``midi_control`` finds no matching
input. So start this tool first.

Usage
-----
  # stream 128 BPM for 8 bars into a virtual port c64cast can open:
  python scripts/diags/tempo_clock_drive.py --port c64castclock --bpm 128 --bars 8

  # jump to bar 3 via SPP before starting the clock:
  python scripts/diags/tempo_clock_drive.py --port c64castclock --bpm 120 --spp 32

  # offline self-check (no external app / second port): feed the same stream into
  # a local TempoClock and assert the tracked bpm + beat_phase match. This is the
  # established "assert bpm/beat_phase track over a virtual mido port" verify.
  python scripts/diags/tempo_clock_drive.py --bpm 140 --bars 4 --verify

Matching c64cast config
-----------------------
  [midi_control]
  enabled = true
  port = "c64castclock"       # or route control elsewhere and set clock_port below
  [performance]
  tempo_source = "midi"
  # clock_port = "c64castclock"   # if the clock is on its own port
  [[midi_control.cc_map]]
  type = "note"; number = 48; action = "tempo_tap"   # optional live tap pad
"""

from __future__ import annotations

import argparse
import sys
import time

# tempo.py is stdlib-only (no mido), so --verify works even without a MIDI backend.
sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])
from c64cast.tempo import TempoClock  # noqa: E402

PPQN = TempoClock.PPQN


def _run(args: argparse.Namespace) -> int:
    port = None
    if args.port:
        import mido

        port = mido.open_output(args.port, virtual=True)
        print(f"opened virtual MIDI output {args.port!r} — point c64cast at it, then Enter", end="")
        if not args.no_wait:
            input()
        else:
            print()

    clock = (
        TempoClock(bpm=args.bpm, beats_per_bar=args.beats_per_bar, source="midi")
        if args.verify
        else None
    )

    pulse_dt = 60.0 / (args.bpm * PPQN)
    total_pulses = int(round(args.bars * args.beats_per_bar * PPQN))

    def send(kind: str, **kw: int) -> None:
        now = time.monotonic()
        if port is not None:
            import mido

            port.send(mido.Message(kind, **kw))
        if clock is not None:
            # Feed a tiny duck-typed shim so we exercise TempoClock.feed_message
            # exactly as midi_control's reader does.
            clock.feed_message(_Msg(kind, kw.get("pos", 0)), now)

    # A DAW seeks with SPP then resumes with Continue (0xFB, keeps the seeked
    # position); a plain Start (0xFA) always rewinds to the top. So SPP pairs
    # with Continue, and a from-zero run uses Start.
    if args.spp:
        send("songpos", pos=args.spp)
        send("continue")
        print(f"SPP -> {args.spp} sixteenths ({args.spp / 4:.2f} beats), CONTINUE")
    else:
        send("start")
    print(
        f"streaming {total_pulses} clock pulses at {args.bpm} BPM "
        f"({args.beats_per_bar}/4, {args.bars} bars)"
    )

    t0 = time.monotonic()
    for i in range(total_pulses):
        target = t0 + (i + 1) * pulse_dt
        send("clock")
        if clock is not None and (i + 1) % (PPQN * args.beats_per_bar) == 0:
            bar = (i + 1) // (PPQN * args.beats_per_bar)
            print(
                f"  bar {bar}: tracked bpm={clock.bpm:6.2f}  beat_phase={clock.beat_phase:7.3f}  "
                f"bar_phase={clock.bar_phase:6.3f}"
            )
        sleep = target - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
    send("stop")
    print("STOP")

    if port is not None:
        port.close()

    if clock is not None:
        expected_beats = (args.spp / 4.0 if args.spp else 0.0) + args.bars * args.beats_per_bar
        got_beats = clock.beat_phase
        bpm_err = abs(clock.bpm - args.bpm)
        beat_err = abs(got_beats - expected_beats)
        print(f"\nverify: bpm {clock.bpm:.2f} (target {args.bpm}, err {bpm_err:.2f})")
        print(
            f"verify: beat_phase {got_beats:.3f} (expected {expected_beats:.3f}, "
            f"err {beat_err:.3f})"
        )
        # beat_phase is pulse-counted (exact); bpm is estimated from wall-clock
        # inter-pulse timing, so its tolerance absorbs host sleep jitter. The
        # exact/deterministic bpm assertions live in tests/test_tempo.py, which
        # feeds synthetic evenly-spaced timestamps.
        ok = bpm_err <= max(3.0, args.bpm * 0.05) and beat_err <= 0.5
        print("verify: PASS" if ok else "verify: FAIL")
        return 0 if ok else 1
    return 0


class _Msg:
    """Minimal duck-typed stand-in for a mido real-time message (type + pos)."""

    __slots__ = ("type", "pos")

    def __init__(self, mtype: str, pos: int = 0) -> None:
        self.type = mtype
        self.pos = pos


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--port",
        default=None,
        help="virtual MIDI output port name to create (omit for --verify-only offline run)",
    )
    p.add_argument("--bpm", type=float, default=120.0, help="tempo to stream (default 120)")
    p.add_argument(
        "--bars", type=float, default=4.0, help="how many bars of clock to send (default 4)"
    )
    p.add_argument(
        "--beats-per-bar", type=int, default=4, help="time-signature numerator (default 4)"
    )
    p.add_argument(
        "--spp",
        type=int,
        default=0,
        help="send a Song-Position seek (in sixteenths) before Start (default: none)",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="feed the same stream into a local TempoClock and assert bpm/beat_phase track",
    )
    p.add_argument(
        "--no-wait", action="store_true", help="with --port, don't pause for Enter before streaming"
    )
    args = p.parse_args(argv)
    if not args.port and not args.verify:
        p.error(
            "nothing to do: pass --port to stream to an app, and/or --verify for an offline check"
        )
    try:
        return _run(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
