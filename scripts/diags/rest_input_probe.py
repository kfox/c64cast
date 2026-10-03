#!/usr/bin/env python3
"""Check keyboard and joystick injection over ``POST /v1/machine:input`` on a
real machine, end to end through ``Ultimate64API``.

    scripts/diags/rest_input_probe.py
    scripts/diags/rest_input_probe.py --url u64://192.168.2.64 --text 'PRINT "HI"'

What it does, in order, and what each step proves:

1. ``refine_capabilities()`` and print ``supports_rest_input``. On firmware
   without the route (3.14, C64 Ultimate 1.1.0) it is False and the script
   stops there: that is the fallback to check on such a machine.
2. Reset to the READY prompt and type ``--text`` plus RETURN with
   ``text_to_events``, then read screen RAM and look for what BASIC printed.
3. Hold fire on joystick port 2, read CIA 1 port A ($DC00) and the API's own
   state, release fire and read both again: bit 4 low while held, high after.
4. ``release_all`` and a reset, whatever happened above.

Take an HDMI still with ``hdmi_capture.py`` after step 2 (``--pause``) to see
the typed line. Exits 1 when a check fails.
"""

from __future__ import annotations

import argparse
import sys
import time

import _diaglib as d

from c64cast.hw import machine_input as mi
from c64cast.hw.api import Ultimate64API
from c64cast.hw.c64 import CIA1

_SCREEN = 0x0400
_FIRE_BIT = 0x10


def _screen_text(api: Ultimate64API) -> list[str]:
    raw = api.read_memory(_SCREEN, 1000) or b""
    # Screen codes 1-26 are A-Z, 32-63 are the same as ASCII.
    chars = [chr(c + 64) if 1 <= c <= 26 else chr(c) if 32 <= c < 64 else " " for c in raw]
    return ["".join(chars[i : i + 40]).rstrip() for i in range(0, len(chars), 40)]


def _port_a(api: Ultimate64API) -> int | None:
    data = api.read_memory(CIA1.PORT_A, 1)
    return data[0] if data else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=d.U64_URL)
    ap.add_argument("--text", default='PRINT "HELLO"', help="typed at READY, then RETURN")
    ap.add_argument("--expect", default="HELLO", help="a screen line that proves it ran")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds to hold after typing")
    args = ap.parse_args()

    base = args.url.replace("u64://", "http://")
    api = Ultimate64API(base)
    ok = True
    try:
        api.refine_capabilities()
        print(f"supports_rest_input = {api.profile.supports_rest_input}")
        if not api.profile.supports_rest_input:
            return 0
        api.reset()
        time.sleep(3.0)

        state = api.send_input(mi.text_to_events(args.text + "\n"))
        print(f"after typing: {state}")
        time.sleep(1.0 + 0.1 * len(args.text))
        lines = _screen_text(api)
        typed = any(args.text in line for line in lines)
        printed = any(line.strip() == args.expect for line in lines)
        print(f"typed line on screen: {typed}; output {args.expect!r} on screen: {printed}")
        ok &= typed and printed
        if args.pause:
            time.sleep(args.pause)

        api.send_input([mi.joystick_event(2, "press", ["fire"])])
        held_port, held_state = _port_a(api), api.input_state()
        api.send_input([mi.joystick_event(2, "release", ["fire"])])
        released_port, released_state = _port_a(api), api.input_state()
        print(f"fire held:     $DC00={held_port!r:>5}  state={held_state}")
        print(f"fire released: $DC00={released_port!r:>5}  state={released_state}")
        ok &= held_port is not None and not held_port & _FIRE_BIT
        ok &= released_port is not None and bool(released_port & _FIRE_BIT)
    finally:
        if api.profile.supports_rest_input:
            api.send_input([mi.RELEASE_ALL])
        api.reset()
        api.close()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
