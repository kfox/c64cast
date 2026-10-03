"""Events for ``POST /v1/machine:input``, the keyboard and joystick injection
firmware 3.15 added on the Ultimate 64: their vocabulary, the firmware's
limits, and the split of a long sequence into request bodies the firmware
accepts.

Pure: nothing here talks to the machine. ``Ultimate64API.send_input`` posts
what `encode_batches` returns, so every limit the firmware enforces is checked
here first, where a bad event is a ``ValueError`` naming it rather than a 400
that drops the whole batch. The vocabulary and limits are the firmware's
(``software/api/input_api.h`` and ``route_input.cc`` at v3.15a).

Key names are keyboard-matrix positions, not characters: a double quote is
``left_shift`` with ``2``. `text_to_events` does that mapping for typing.

See docs/architecture/hardware-io.md#machine_inputpy--keyboard-and-joystick-injection.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any, Final

MAX_EVENTS: Final = 64
MAX_KEYBOARD_INPUTS: Final = 8
# The firmware refuses a body of 4096 bytes or more.
MAX_BODY_BYTES: Final = 4095

TRANSITIONS: Final = ("press", "release", "tap")
JOYSTICK_PORTS: Final = (1, 2)
JOYSTICK_INPUTS: Final = ("up", "down", "left", "right", "fire", "fire2", "fire3")

# `restore` is the NMI line, not a matrix key, so the firmware acts on it only
# in a tap.
RESTORE: Final = "restore"
KEYBOARD_KEYS: Final = frozenset(
    {
        *"abcdefghijklmnopqrstuvwxyz0123456789",
        "inst_del",
        "return",
        "cursor_left_right",
        "cursor_up_down",
        "f1",
        "f3",
        "f5",
        "f7",
        "left_shift",
        "right_shift",
        "commodore",
        "ctrl",
        "run_stop",
        "clr_home",
        "space",
        "plus",
        "minus",
        "period",
        "colon",
        "at",
        "comma",
        "pound",
        "star",
        "semicolon",
        "equals",
        "arrow_up",
        "arrow_left",
        "slash",
        RESTORE,
    }
)

Event = dict[str, Any]
RELEASE_ALL: Final[Event] = {"kind": "release_all"}


def is_joystick_port(port: object) -> bool:
    """Whether `port` is a port the firmware takes: the integer 1 or 2. A bool
    compares equal to 1 but goes out as JSON ``true``, which the firmware
    refuses."""
    return isinstance(port, int) and not isinstance(port, bool) and port in JOYSTICK_PORTS


def keyboard_event(transition: str, inputs: Sequence[str]) -> Event:
    return {"kind": "keyboard", "transition": transition, "inputs": list(inputs)}


def joystick_event(port: int, transition: str, inputs: Sequence[str]) -> Event:
    return {"kind": "joystick", "port": port, "transition": transition, "inputs": list(inputs)}


def validate_event(event: Event) -> None:
    """Raise ValueError when the firmware would refuse `event`."""
    kind = event.get("kind")
    if kind == "release_all":
        if set(event) != {"kind"}:
            raise ValueError(f"release_all takes no other key: {event!r}")
        return
    if kind not in ("keyboard", "joystick"):
        raise ValueError(f"event kind must be keyboard, joystick or release_all: {event!r}")
    allowed = {"kind", "transition", "inputs"} | ({"port"} if kind == "joystick" else set())
    if set(event) != allowed:
        raise ValueError(f"{kind} event needs exactly {sorted(allowed)}: {event!r}")
    transition = event["transition"]
    if transition not in TRANSITIONS:
        raise ValueError(f"transition must be one of {TRANSITIONS}: {event!r}")
    inputs = event["inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise ValueError(f"inputs must be a non-empty list: {event!r}")
    if len(set(inputs)) != len(inputs):
        raise ValueError(f"inputs repeat a name: {event!r}")
    if kind == "keyboard":
        if len(inputs) > MAX_KEYBOARD_INPUTS:
            raise ValueError(f"at most {MAX_KEYBOARD_INPUTS} keys per event: {event!r}")
        unknown = [name for name in inputs if name not in KEYBOARD_KEYS]
        if unknown:
            raise ValueError(f"unknown key names {unknown}: {event!r}")
        if RESTORE in inputs and transition != "tap":
            raise ValueError(f"restore can only be tapped: {event!r}")
        return
    if not is_joystick_port(event["port"]):
        raise ValueError(f"joystick port must be 1 or 2: {event!r}")
    unknown = [name for name in inputs if name not in JOYSTICK_INPUTS]
    if unknown:
        raise ValueError(f"unknown joystick inputs {unknown}: {event!r}")


def _body(events: Sequence[Event]) -> bytes:
    return json.dumps({"events": list(events)}, separators=(",", ":")).encode()


def encode_batches(events: Iterable[Event]) -> list[bytes]:
    """Validate every event, then pack them in order into as few request
    bodies as the firmware's limits allow: at most `MAX_EVENTS` events and
    `MAX_BODY_BYTES` bytes each. Raises ValueError before anything is packed
    if any event is invalid, so a caller never sends half a sequence."""
    pending = list(events)
    for event in pending:
        validate_event(event)
    bodies: list[bytes] = []
    batch: list[Event] = []
    for event in pending:
        candidate = [*batch, event]
        if batch and (len(candidate) > MAX_EVENTS or len(_body(candidate)) > MAX_BODY_BYTES):
            bodies.append(_body(batch))
            candidate = [event]
        batch = candidate
    if batch:
        bodies.append(_body(batch))
    return bodies


_UNSHIFTED: Final = {
    " ": "space",
    "\n": "return",
    "\r": "return",
    "+": "plus",
    "-": "minus",
    ".": "period",
    ":": "colon",
    "@": "at",
    ",": "comma",
    "£": "pound",
    "*": "star",
    ";": "semicolon",
    "=": "equals",
    "↑": "arrow_up",
    "^": "arrow_up",
    "←": "arrow_left",
    "/": "slash",
}
_SHIFTED: Final = {
    "!": "1",
    '"': "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "&": "6",
    "'": "7",
    "(": "8",
    ")": "9",
    "[": "colon",
    "]": "semicolon",
    "<": "comma",
    ">": "period",
    "?": "slash",
}


def text_to_events(text: str) -> list[Event]:
    """One tap per character of `text`, as typed on a C64 in its power-on
    uppercase/graphics mode: a letter in either case is its unshifted key.
    Raises ValueError for a character with no key."""
    events = []
    for ch in text:
        lower = ch.lower()
        if lower in KEYBOARD_KEYS and len(lower) == 1:
            keys = [lower]
        elif ch in _UNSHIFTED:
            keys = [_UNSHIFTED[ch]]
        elif ch in _SHIFTED:
            keys = ["left_shift", _SHIFTED[ch]]
        else:
            raise ValueError(f"no C64 key types {ch!r}")
        events.append(keyboard_event("tap", keys))
    return events
