#!/usr/bin/env python
"""Walk a grid controller's pads through the performance LED states so you can
confirm the colors on real hardware (Live DJ/VJ Phase 4 — see
docs/architecture/control.md → "Grid-controller LED feedback").

c64cast's live LED feedback lights a pad by sending it a ``note_on`` at the pad's
own note number, with the *velocity* selecting the color (the Launchpad/APC
convention). This tool sends exactly those messages — the same
:class:`c64cast.midi_control.FeedbackMap` velocities and
:func:`c64cast.midi_control.compute_pad_leds` mapping the running listener uses —
so what you see here is what a live show will light, with no c64cast run, no
tempo grid, and no clip grid needed.

It cycles a set of pads through: extinguish → all *loaded* (dim) → one *armed*
(blinking) → that one *active* (bright) → an *fx* pad on (lit) → extinguish. Use
it to eyeball a controller's palette and pick velocity overrides for its profile
`feedback` block (``--midi-setup`` writes that block).

Usage
-----
  # light notes 60..67 on a controller whose OUT port name contains "Launchpad":
  python scripts/diags/led_feedback_demo.py --port Launchpad --pads 60-67

  # try a controller-specific palette (same keys as a profile feedback block):
  python scripts/diags/led_feedback_demo.py --port APC --loaded 1 --active 21 \
      --armed 5 --fx-on 45 --channel 0

  # offline self-check (no port / hardware): assert compute_pad_leds lights the
  # right velocity in each state.
  python scripts/diags/led_feedback_demo.py --verify

The velocities are palette indices, not brightness — their meaning is
controller-specific (Launchpad-X programmer mode by default). If nothing lights,
the controller likely needs a "programmer"/"user" mode enabled first.
"""

from __future__ import annotations

import argparse
import sys
import time

# midi_control pulls in mido lazily; compute_pad_leds / FeedbackMap themselves are
# dependency-light, so --verify works without a MIDI backend.
sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])
from c64cast.midi_control import FeedbackMap, compute_pad_leds  # noqa: E402


def _parse_pads(spec: str) -> list[int]:
    """A `--pads` spec: comma-separated notes and/or `lo-hi` ranges."""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            out.extend(range(lo, hi + 1))
        else:
            out.append(int(part))
    return out


def _fmap(args: argparse.Namespace) -> FeedbackMap:
    d = FeedbackMap()
    return FeedbackMap(
        channel=args.channel if args.channel is not None else d.channel,
        off=d.off,
        loaded=args.loaded if args.loaded is not None else d.loaded,
        armed=args.armed if args.armed is not None else d.armed,
        active=args.active if args.active is not None else d.active,
        fx_on=args.fx_on if args.fx_on is not None else d.fx_on,
    )


def _verify(fmap: FeedbackMap) -> int:
    """Offline: assert each state lights the expected velocity."""
    clip_pads = [(60, 1), (61, 2)]
    fx_pads = [(70, 0)]
    checks = [
        (
            "loaded",
            compute_pad_leds(clip_pads, [], set(), set(), set(), fmap, blink_on=True)[60],
            fmap.loaded,
        ),
        (
            "active",
            compute_pad_leds(clip_pads, [], {1}, set(), set(), fmap, blink_on=True)[60],
            fmap.active,
        ),
        (
            "armed on-phase",
            compute_pad_leds(clip_pads, [], set(), {2}, set(), fmap, blink_on=True)[61],
            fmap.armed,
        ),
        (
            "armed off-phase",
            compute_pad_leds(clip_pads, [], set(), {2}, set(), fmap, blink_on=False)[61],
            fmap.off,
        ),
        (
            "fx on",
            compute_pad_leds([], fx_pads, set(), set(), {0}, fmap, blink_on=True)[70],
            fmap.fx_on,
        ),
        (
            "fx off",
            compute_pad_leds([], fx_pads, set(), set(), set(), fmap, blink_on=True)[70],
            fmap.off,
        ),
    ]
    ok = True
    for label, got, want in checks:
        status = "ok" if got == want else "FAIL"
        ok = ok and got == want
        print(f"  {label:16s}: velocity {got:3d} (want {want:3d}) {status}")
    print("verify: PASS" if ok else "verify: FAIL")
    return 0 if ok else 1


def _open_output(name: str):  # type: ignore[no-untyped-def]
    import mido

    names = mido.get_output_names()
    match = next((n for n in names if name.lower() in n.lower()), None)
    if match is None:
        print(f"no MIDI output matches {name!r}; available: {names}")
        return None
    print(f"opened MIDI output {match!r}")
    return mido.open_output(match)


def _run(args: argparse.Namespace) -> int:
    fmap = _fmap(args)
    if args.verify:
        return _verify(fmap)

    import mido

    port = _open_output(args.port)
    if port is None:
        return 2
    pads = _parse_pads(args.pads)
    if not pads:
        print("no pads to light (see --pads)")
        return 2
    # Model the pads as clip slots 1..N sharing their note; the last pad doubles
    # as the fx pad so every state is exercised on real hardware.
    clip_pads = [(note, i + 1) for i, note in enumerate(pads)]
    fx_pads = [(pads[-1], 0)]
    last_sent: dict[int, int] = {}

    def paint(desired: dict[int, int], label: str) -> None:
        print(f"  {label}")
        for note, vel in desired.items():
            if last_sent.get(note) != vel:
                port.send(mido.Message("note_on", channel=fmap.channel, note=note, velocity=vel))
                last_sent[note] = vel
        time.sleep(args.step_s)

    try:
        paint(dict.fromkeys(pads, fmap.off), "extinguish")
        paint(
            compute_pad_leds(clip_pads, [], set(), set(), set(), fmap, blink_on=True),
            "all loaded (dim)",
        )
        armed_slot = clip_pads[0][1]
        for i in range(args.blinks):
            paint(
                compute_pad_leds(
                    clip_pads, [], set(), {armed_slot}, set(), fmap, blink_on=(i % 2 == 0)
                ),
                f"pad {pads[0]} armed (blink {i + 1}/{args.blinks})",
            )
        paint(
            compute_pad_leds(clip_pads, [], {armed_slot}, set(), set(), fmap, blink_on=True),
            f"pad {pads[0]} active (bright)",
        )
        paint(
            compute_pad_leds(clip_pads, fx_pads, {armed_slot}, set(), {0}, fmap, blink_on=True),
            f"pad {pads[-1]} fx-on (lit)",
        )
        paint(dict.fromkeys(pads, fmap.off), "extinguish")
    finally:
        for n in pads:
            port.send(mido.Message("note_on", channel=fmap.channel, note=n, velocity=fmap.off))
        port.close()
    print("done")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--port", default="", help="MIDI OUTPUT port name (substring match)")
    p.add_argument("--pads", default="60-67", help="pad notes: e.g. '60-67' or '36,37,40'")
    p.add_argument("--channel", type=int, default=None, help="LED MIDI channel (0-based)")
    p.add_argument("--loaded", type=int, default=None, help="loaded (dim) velocity")
    p.add_argument("--armed", type=int, default=None, help="armed (blink) velocity")
    p.add_argument("--active", type=int, default=None, help="active (bright) velocity")
    p.add_argument("--fx-on", type=int, default=None, help="enabled effect-layer velocity")
    p.add_argument("--step-s", type=float, default=0.6, help="seconds per state")
    p.add_argument("--blinks", type=int, default=4, help="armed blink half-cycles")
    p.add_argument("--verify", action="store_true", help="offline self-check, no port")
    args = p.parse_args()
    if not args.verify and not args.port:
        p.error("--port is required (or use --verify for the offline check)")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
