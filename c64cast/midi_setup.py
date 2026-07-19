"""``--midi-setup`` — the MIDI-learn wizard that writes a controller profile.

Everything ``[midi_control]`` can do live today requires hand-authoring a
``cc_map`` TOML: you have to know a controller's CC/note numbers *and* the
internal target vocabulary (``effect.decay``, ``mode.dither_strength``,
``transport.jog``, …). This wizard removes both: it watches the controller,
you press/twist each control when prompted, and it writes a reusable
:class:`~c64cast.transport.ControllerProfileStore` profile so a plain
``c64cast --config …`` run (with ``[midi_control].controller_profile = "auto"``,
the default) picks the mappings up with zero TOML edits.

Mirrors :mod:`c64cast.wizard`'s split: **pure helpers** (``detect_encoder``,
``dominant_control``, ``build_*`` — all testable with scripted fake-mido
messages) plus a **thin questionary shell** (:func:`run_setup`). Runs *instead
of* playback, like ``--init``. Needs the ``midi`` + ``wizard`` extras.

The learn loop reads a controller identically to the live listener by reusing
:func:`c64cast.midi_control.classify_message` — a learned mapping can't disagree
with how the listener will later interpret the same message. The target picker
is driven by :func:`c64cast.introspect.live_targets`, the single source of truth
over the ``LIVE_PARAMS``/``LIVE_CHOICES`` registries.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from . import introspect
from .midi_control import MIDI_AVAILABLE, classify_message, mido
from .transport import make_controller_profile_store

# The transport / OSD buttons the wizard offers to learn, in prompt order.
# (label, action). An action that a button emits as MMC SysEx is auto-recognized
# (classify_message returns kind "mmc") — no separate MMC step needed.
_TRANSPORT_BUTTONS: tuple[tuple[str, str], ...] = (
    ("Play / Pause", "transport.play_pause"),
    ("Stop", "transport.stop"),
    ("Record (arms an A/B loop)", "transport.record"),
    ("Rewind (hold to scrub)", "transport.rw"),
    ("Fast-forward (hold to scrub)", "transport.ff"),
    ("Loop toggle (mark A / B)", "transport.loop_toggle"),
    ("OSD toggle (tap corner, double-tap hide)", "osd.position"),
)

# A learned knob detected as a relative encoder can drive the jog/scrub instead
# of sweeping a param — offered as an extra target for encoders.
_JOG_TARGET_LABEL = "transport.jog (DJ scrub — relative encoder)"


# ---------------------------------------------------------------------------
# Pure helpers (no I/O — scripted-message testable)
# ---------------------------------------------------------------------------


def detect_encoder(values: list[int]) -> bool:
    """Heuristic: does this CC's value stream look like a *relative* (endless)
    encoder rather than an absolute knob/fader?

    Relative encoders emit small two's-complement deltas — values clustering
    near 0 (1,2,3 = clockwise) and near 127 (127,126,125 = counter-clockwise) —
    and never traverse the mid-range, whereas a swept absolute knob passes
    through the middle. Requires a few samples so a single stray value can't
    trip it; the review step lets the user override either way."""
    if len(values) < 3:
        return False
    mids = [v for v in values if 8 <= v <= 119]
    extremes = [v for v in values if v <= 7 or v >= 120]
    return not mids and len(extremes) >= 3


def dominant_control(events: list[tuple[str, int, int, bool]]) -> tuple[str, int] | None:
    """Pick the ``(kind, number)`` a learn burst most likely intended: the most
    frequent one among *pressed* events (a button/pad may repeat; a knob emits
    many CCs). Returns None when the burst held no pressed, mappable event."""
    counts: Counter[tuple[str, int]] = Counter(
        (kind, number) for kind, number, _value, pressed in events if pressed
    )
    if not counts:
        return None
    # most_common ties break by insertion order (first-seen wins) — deterministic.
    return counts.most_common(1)[0][0]


def values_for(events: list[tuple[str, int, int, bool]], kind: str, number: int) -> list[int]:
    """The value stream for one ``(kind, number)`` in a learn burst (for
    :func:`detect_encoder`)."""
    return [v for k, n, v, _p in events if k == kind and n == number]


def build_transport_entry(action: str, kind: str, number: int) -> dict[str, Any]:
    """A cc_map entry for a learned transport/OSD button. An MMC button records
    ``type: "mmc"`` with the command byte; otherwise the raw kind/number."""
    return {"type": kind, "number": number, "action": action}


def build_param_entry(number: int, target: str, *, kind: str = "cc") -> dict[str, Any]:
    return {"type": kind, "number": number, "action": "param", "target": target}


def build_jog_entry(number: int) -> dict[str, Any]:
    return {"type": "cc", "number": number, "action": "transport.jog", "mode": "rel"}


def build_jump_entry(kind: str, number: int, scene: int) -> dict[str, Any]:
    return {"type": kind, "number": number, "action": "jump", "scene": scene}


def dedupe_mappings(mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Last-wins dedupe by ``(type, number)`` — mirrors the runtime cc_map
    layering (:func:`c64cast.midi_control._parse_cc_map`), so what the wizard
    previews is what the listener will resolve."""
    out: dict[tuple[Any, Any], dict[str, Any]] = {}
    for m in mappings:
        out[(m.get("type"), m.get("number"))] = m
    return list(out.values())


def describe_mapping(m: dict[str, Any]) -> str:
    """One-line human summary of a cc_map entry for the review table."""
    head = f"{m.get('type')} {m.get('number')} → {m.get('action')}"
    for extra in ("target", "scene", "mode", "slot"):
        if extra in m and m[extra] is not None:
            head += f" {extra}={m[extra]}"
    return head


# ---------------------------------------------------------------------------
# I/O: reading learn bursts from a live port
# ---------------------------------------------------------------------------


def _drain(port: Any) -> None:
    """Discard any messages already queued (so a learn step starts clean)."""
    for _ in port.iter_pending():
        pass


def _read_burst(
    port: Any, *, settle_s: float = 0.6, timeout_s: float = 15.0
) -> list[tuple[str, int, int, bool]]:
    """Block until the first mappable message arrives (or `timeout_s` elapses),
    then keep collecting for `settle_s` after the last message so a knob sweep /
    a pad's note-on+off both land in one burst. Returns the classified events."""
    events: list[tuple[str, int, int, bool]] = []
    deadline = time.monotonic() + timeout_s
    last_msg = 0.0
    while True:
        got = False
        for msg in port.iter_pending():
            c = classify_message(msg)
            if c is not None:
                events.append(c)
                got = True
        now = time.monotonic()
        if got:
            last_msg = now
        if events and (now - last_msg) >= settle_s:
            break
        if not events and now >= deadline:
            break
        time.sleep(0.005)
    return events


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------


def _ensure_questionary():  # type: ignore[no-untyped-def]
    try:
        import questionary

        return questionary
    except ImportError:
        return None


def _pick_port(q: Any) -> str | None:
    names = mido.get_input_names()
    if not names:
        print(
            "No MIDI input ports found. Connect your controller (or open a "
            "virtual port) and try again."
        )
        return None
    if len(names) == 1:
        print(f"Using the only MIDI input port: {names[0]!r}")
        return str(names[0])
    return q.select("Which MIDI input port is your controller?", choices=list(names)).ask()


def _learn_transport(q: Any, port: Any) -> list[dict[str, Any]]:
    print(
        "\nTransport / OSD buttons. Press the control when prompted, or Enter to "
        "skip it. A dedicated transport button that sends MMC is recognized "
        "automatically.\n"
        "(Tip: map Record/Stop to real MIDI notes — not MMC — if you want the "
        "loop-preset pad chords, which need a held button.)\n"
    )
    out: list[dict[str, Any]] = []
    for label, action in _TRANSPORT_BUTTONS:
        if not q.confirm(f"Learn '{label}'?", default=True).ask():
            continue
        print(f"  → press '{label}' now…")
        _drain(port)
        events = _read_burst(port)
        ctrl = dominant_control(events)
        if ctrl is None:
            print("    (nothing received — skipped)")
            continue
        kind, number = ctrl
        out.append(build_transport_entry(action, kind, number))
        tag = "MMC" if kind == "mmc" else f"{kind} {number}"
        print(f"    learned {tag} → {action}")
    return out


def _learn_knobs(q: Any, port: Any) -> list[tuple[int, bool]]:
    """Learn CC knobs/faders one at a time. Returns (cc_number, is_encoder)
    pairs, deduped by CC number (last sweep wins the encoder verdict)."""
    print("\nKnobs / faders. Twist one fully when prompted; repeat for each.\n")
    learned: dict[int, bool] = {}
    while q.confirm("Learn a knob/fader?", default=not learned).ask():
        print("  → sweep the knob now…")
        _drain(port)
        events = _read_burst(port)
        ctrl = dominant_control(events)
        if ctrl is None or ctrl[0] != "cc":
            print("    (no CC received — skipped)")
            continue
        _kind, number = ctrl
        is_enc = detect_encoder(values_for(events, "cc", number))
        learned[number] = is_enc
        kind_label = "relative encoder" if is_enc else "absolute knob"
        print(f"    learned CC {number} ({kind_label})")
    return list(learned.items())


def _bind_knobs(q: Any, knobs: list[tuple[int, bool]]) -> list[dict[str, Any]]:
    if not knobs:
        return []
    targets = introspect.live_targets()
    # Grouped picker labels → target string.
    label_to_target: dict[str, str] = {}
    choices_by_group: dict[str, list[str]] = {}
    for t in targets:
        rng = (
            f" [{t.lo:g}..{t.hi:g}]"
            if t.kind == "scalar" and t.lo is not None and t.hi is not None
            else f" {{{'/'.join(t.choices)}}}"
            if t.choices
            else ""
        )
        label = f"{t.target}{rng}"
        label_to_target[label] = t.target
        choices_by_group.setdefault(t.group, []).append(label)

    print("\nBind each learned knob to a live target.\n")
    out: list[dict[str, Any]] = []
    for number, is_enc in knobs:
        # Build the flat choice list: an encoder gets the jog option first.
        choices: list[str] = ["(skip)"]
        if is_enc:
            choices.append(_JOG_TARGET_LABEL)
        for group in ("Color pipeline", "Effect", "Generator", "Scope"):
            choices.extend(choices_by_group.get(group, []))
        pick = q.select(
            f"CC {number} ({'encoder' if is_enc else 'knob'}) →",
            choices=choices,
        ).ask()
        if pick in (None, "(skip)"):
            continue
        if pick == _JOG_TARGET_LABEL:
            out.append(build_jog_entry(number))
        else:
            out.append(build_param_entry(number, label_to_target[pick]))
    return out


def _learn_scene_jumps(q: Any, port: Any) -> list[dict[str, Any]]:
    if not q.confirm("Learn scene-jump pads?", default=False).ask():
        return []
    print(
        "\nScene-jump pads. Press a pad, then enter the scene index it should jump to (0-based).\n"
    )
    out: list[dict[str, Any]] = []
    while q.confirm("Learn a scene-jump pad?", default=True).ask():
        print("  → press the pad now…")
        _drain(port)
        events = _read_burst(port)
        ctrl = dominant_control(events)
        if ctrl is None:
            print("    (nothing received — skipped)")
            continue
        kind, number = ctrl
        scene_raw = q.text("    jump to scene index (0-based)", default="0").ask()
        if scene_raw is None:
            continue
        try:
            scene = int(scene_raw)
        except ValueError:
            print("    (not a number — skipped)")
            continue
        out.append(build_jump_entry(kind, number, scene))
        print(f"    learned {kind} {number} → jump scene {scene}")
    return out


def _review(q: Any, mappings: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    while True:
        if not mappings:
            print("\nNo mappings learned.")
            return mappings
        print("\nLearned mappings:")
        for i, m in enumerate(mappings):
            print(f"  {i}: {describe_mapping(m)}")
        action = q.select("Review", choices=["Save these", "Remove one", "Discard all"]).ask()
        if action is None or action == "Discard all":
            return None
        if action == "Save these":
            return mappings
        idx = q.text("Remove which index?", default="0").ask()
        try:
            mappings.pop(int(idx))
        except (ValueError, IndexError):
            print("  (bad index)")


def run_setup() -> int:
    """Drive the MIDI-learn wizard. Returns an exit code: 0 on a saved profile,
    2 on cancel / nothing learned / a missing extra."""
    if not MIDI_AVAILABLE:
        print(
            "--midi-setup needs the 'midi' extra:\n"
            "  uv sync --extra midi   (or: pip install c64cast[midi])"
        )
        return 2
    q = _ensure_questionary()
    if q is None:
        print(
            "--midi-setup needs the 'wizard' extra:\n"
            "  uv sync --extra wizard   (or: pip install c64cast[wizard])"
        )
        return 2

    print(
        "c64cast MIDI controller setup.\n"
        "Learn your controller's transport buttons, knobs, and pads, then save a "
        "reusable profile. Press Enter to accept a [default]; Ctrl-C to cancel.\n"
    )

    port_name = _pick_port(q)
    if not port_name:
        return 2

    try:
        port = mido.open_input(port_name)
    except OSError as e:
        print(f"Could not open MIDI port {port_name!r}: {e}")
        return 2

    try:
        mappings: list[dict[str, Any]] = []
        mappings += _learn_transport(q, port)
        knobs = _learn_knobs(q, port)
        mappings += _bind_knobs(q, knobs)
        mappings += _learn_scene_jumps(q, port)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 2
    finally:
        port.close()

    mappings = dedupe_mappings(mappings)
    reviewed = _review(q, mappings)
    if not reviewed:
        print("Nothing saved.")
        return 2

    store = make_controller_profile_store(port_name)
    store.save(port_name, reviewed)
    print(f"\nSaved {len(reviewed)} mapping(s) to {store.path}")
    print(
        "Use it by enabling MIDI control in your config:\n"
        "  [midi_control]\n"
        "  enabled = true\n"
        '  controller_profile = "auto"   # the default — matches this port by name\n'
    )
    return 0
