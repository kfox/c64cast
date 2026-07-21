#!/usr/bin/env python
"""Print the MIDI messages a controller is sending — the read/monitor
counterpart to ``midi_drive.py`` (which *sends*).

Point it at a MIDI input port and it streams every note / CC / program-change /
pitchbend it receives, one clean line each, plus the **exact value to paste**
into a c64cast config: a pad's ``pad = N`` / ``type = "note"``, a knob or
fader's ``type = "cc", number = N``, etc. On exit (Ctrl+C) it prints a summary
table of every distinct control it saw, with the observed value range — so you
can tell a pad (fixed note) from a knob (0..127 sweep) from a fader at a glance.

It reuses c64cast's own ``classify_message``, so it interprets a message
*identically* to the live ``[midi_control]`` listener — a value it prints can't
disagree with how c64cast will later read the same message.

Device selection mirrors the rest of the tooling:
  * ``--port SUBSTR``  — case-insensitive substring match (like the config's
    ``midi_port``); first match wins.
  * no ``--port``      — one port is auto-selected; several prompts a numbered
    menu (when run interactively).
  * ``--list``         — just list input ports and exit.

Real-time clock spam (``0xF8`` clock, active-sensing) is hidden by default so a
controller that also sends MIDI clock doesn't drown the notes; ``--clock`` shows
it (and confirms clock is flowing, e.g. for ``tempo_source = "midi"``).

Needs the ``midi`` extra:  uv sync --all-extras   (mido + python-rtmidi)

Usage
-----
  # list input ports and exit
  python scripts/diags/midi_monitor.py --list

  # auto-pick (or prompt), then stream messages
  python scripts/diags/midi_monitor.py

  # target a specific device by name substring
  python scripts/diags/midi_monitor.py --port Beatstep

  # also show MIDI clock / active-sensing traffic
  python scripts/diags/midi_monitor.py --port MPC --clock

Press the pads, twist the knobs, move the faders — then Ctrl+C for the summary.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])
from c64cast.midi_control import MIDI_AVAILABLE, classify_message, mido  # noqa: E402

# Real-time / housekeeping message types hidden unless --clock is passed.
_NOISE = {"clock", "active_sensing", "start", "stop", "continue", "songpos", "reset"}

# How each classified kind maps back into config syntax, for the paste hints.
_KIND_LABEL = {
    "note": "pad / key",
    "cc": "knob / fader",
    "pc": "program change",
    "mmc": "transport (MMC)",
}


def _input_names() -> list[str]:
    return list(mido.get_input_names())


def _list_ports(names: list[str]) -> None:
    if not names:
        print(
            "No MIDI input ports found. Connect a controller (or open a "
            "virtual port) and try again."
        )
        return
    print("MIDI input ports:")
    for i, name in enumerate(names):
        print(f"  [{i}] {name}")


def _select_port(names: list[str], want: str | None) -> str | None:
    """Resolve the port to open: substring match, sole-port auto-pick, or an
    interactive numbered prompt. Returns None if nothing usable."""
    if not names:
        print(
            "No MIDI input ports found. Connect a controller (or open a "
            "virtual port) and try again."
        )
        return None
    if want:
        matches = [n for n in names if want.lower() in n.lower()]
        if not matches:
            print(f"No input port matches {want!r}. Available:")
            _list_ports(names)
            return None
        if len(matches) > 1:
            print(f"{want!r} matches {len(matches)} ports; using the first: {matches[0]!r}")
        return matches[0]
    if len(names) == 1:
        print(f"Using the only MIDI input port: {names[0]!r}")
        return names[0]
    # Several ports, no --port: prompt if we have a terminal, else bail with help.
    _list_ports(names)
    if not sys.stdin.isatty():
        print(
            "\nSeveral ports available and no TTY to prompt — re-run with "
            "--port SUBSTR (e.g. --port Beatstep)."
        )
        return None
    while True:
        raw = input(f"Select a port [0-{len(names) - 1}] (Enter for 0, q to quit): ").strip()
        if raw.lower() == "q":
            return None
        if raw == "":
            return names[0]
        if raw.isdigit() and 0 <= int(raw) < len(names):
            return names[int(raw)]
        print("  not a valid choice.")


def _describe(msg: object) -> tuple[str, str]:
    """Return (stream_line, hint) for one message. `hint` is '' when there's
    nothing mappable to paste (e.g. pitchbend)."""
    ch = getattr(msg, "channel", None)
    ch_str = f"ch{ch + 1:<2}" if ch is not None else "  — "  # 1-based, human-facing
    kind = classify_message(msg)
    mtype = getattr(msg, "type", "?")
    if kind is not None:
        k, number, value, pressed = kind
        if k == "note":
            state = "on " if pressed else "off"
            line = f"{ch_str}  note {number:<3} {state} vel {value:<3}"
            hint = f'pad = {number}   (or  type = "note", number = {number})   [{_KIND_LABEL[k]}]'
            return line, hint
        if k == "cc":
            line = f"{ch_str}  cc   {number:<3}     val {value:<3}"
            hint = f'type = "cc", number = {number}   val now {value}   [{_KIND_LABEL[k]}]'
            return line, hint
        if k == "pc":
            line = f"{ch_str}  pc   {number:<3}"
            hint = f'type = "pc", number = {number}   [{_KIND_LABEL[k]}]'
            return line, hint
        if k == "mmc":
            line = f"{ch_str}  mmc  cmd {number}"
            hint = f'type = "mmc", number = {number}   [{_KIND_LABEL[k]}]'
            return line, hint
    # Not mappable in a cc_map — still worth showing as info.
    if mtype == "pitchwheel":
        return f"{ch_str}  pitchbend {getattr(msg, 'pitch', 0)}", ""
    if mtype == "aftertouch":
        return f"{ch_str}  aftertouch {getattr(msg, 'value', 0)}", ""
    if mtype == "polytouch":
        return f"{ch_str}  polytouch note {getattr(msg, 'note', 0)} {getattr(msg, 'value', 0)}", ""
    return f"{ch_str}  {mtype}", ""


def _summary(seen: dict[tuple[str, int, int | None], dict[str, int]]) -> None:
    if not seen:
        print("\nNo controls were received.")
        return
    print("\n" + "=" * 70)
    print("Controls seen — paste these into a c64cast config:")
    print("=" * 70)
    # Sort by kind (note, cc, pc, mmc), then number, then channel.
    order = {"note": 0, "cc": 1, "pc": 2, "mmc": 3}
    for (k, number, ch), stat in sorted(
        seen.items(), key=lambda kv: (order.get(kv[0][0], 9), kv[0][1], kv[0][2] or 0)
    ):
        ch_str = f"ch{ch + 1}" if ch is not None else "—"
        rng = f"{stat['min']}" if stat["min"] == stat["max"] else f"{stat['min']}..{stat['max']}"
        label = _KIND_LABEL.get(k, k)
        hits = stat["hits"]
        if k == "note":
            paste = f'pad = {number}   /   type = "note", number = {number}'
            extra = f"velocity {rng}"
        elif k == "cc":
            paste = f'type = "cc", number = {number}'
            # A full 0..127 sweep smells like a knob/fader; a narrow range, a button.
            extra = f"value {rng}" + (
                "  (full sweep — knob/fader)" if stat["max"] - stat["min"] > 100 else ""
            )
        elif k == "pc":
            paste = f'type = "pc", number = {number}'
            extra = ""
        else:
            paste = f'type = "{k}", number = {number}'
            extra = ""
        print(f"  {label:<14} {ch_str:<4} ×{hits:<4} {paste}")
        if extra:
            print(f"  {'':<14} {'':<4}  {'':<4} {extra}")
    print("=" * 70)
    print(
        'Notes fire clips (`pad = N`) and map as `type = "note"`. Knobs/faders '
        'map as `type = "cc"`.\nChannel is 1-based here; c64cast targets '
        "ensemble systems by channel (single-system ignores it)."
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Monitor incoming MIDI messages and print pad/CC values."
    )
    ap.add_argument("-p", "--port", help="input port name substring (first match wins)")
    ap.add_argument("-l", "--list", action="store_true", help="list MIDI input ports and exit")
    ap.add_argument(
        "--clock",
        action="store_true",
        help="also show MIDI clock / active-sensing traffic (hidden by default)",
    )
    ap.add_argument(
        "--raw", action="store_true", help="also print the raw mido repr for each message"
    )
    args = ap.parse_args(argv)

    if not MIDI_AVAILABLE:
        print(
            "This tool needs the 'midi' extra (mido + python-rtmidi):\n  uv sync --all-extras",
            file=sys.stderr,
        )
        return 2

    names = _input_names()
    if args.list:
        _list_ports(names)
        return 0

    port_name = _select_port(names, args.port)
    if port_name is None:
        return 2

    print(
        f"\nListening on {port_name!r}. "
        + (
            "Showing clock/realtime too. "
            if args.clock
            else "Clock/realtime hidden (use --clock to show). "
        )
        + "Press Ctrl+C for the summary.\n"
    )

    seen: dict[tuple[str, int, int | None], dict[str, int]] = {}
    try:
        with mido.open_input(port_name) as port:  # type: ignore[union-attr]
            for msg in port:
                if not args.clock and getattr(msg, "type", "") in _NOISE:
                    continue
                line, hint = _describe(msg)
                if args.raw:
                    line = f"{line:<38} {msg!r}"
                print(f"{line}   {hint}" if hint else line)
                # Accumulate for the summary (note on/off collapse to one key).
                classified = classify_message(msg)
                if classified is not None:
                    k, number, value, _ = classified
                    ch = getattr(msg, "channel", None)
                    stat = seen.setdefault((k, number, ch), {"hits": 0, "min": value, "max": value})
                    stat["hits"] += 1
                    stat["min"] = min(stat["min"], value)
                    stat["max"] = max(stat["max"], value)
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"\nCould not open {port_name!r}: {e}", file=sys.stderr)
        return 1
    _summary(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
