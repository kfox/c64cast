"""Process-wide MIDI control surface for live performance: scene jumps,
style cycling, transport, and live effect/generator parameter sweeps from
a MIDI controller — turns a playlist run into something a performer can
drive in real time.

Unlike :mod:`midi_scene` (a *Scene* that plays the SID directly and is only
live while that scene is on screen), this is a standalone service that runs
for the whole process, mirroring :mod:`control_plane`'s "one server for the
whole ensemble" shape. It opens its OWN ``mido.open_input()`` — mido ports
are exclusive opens, so this is always a second port, never shared with a
running :class:`~c64cast.midi_scene.MidiScene` even on the same physical
controller (route the controller to two virtual MIDI ports, or use OS-level
MIDI Thru, if you want one physical device to feed both).

Every action bottoms out in one of two cheap, already-existing mechanisms:

- Discrete actions (pause/resume/skip/cycle_style/jump) set a
  :class:`~c64cast.playlist.Playlist` ``threading.Event`` — the same
  mechanism :mod:`control_plane` and :mod:`keyboard` already use. Picked up
  at the next clean frame boundary (one frame period, worst case).
- Continuous parameter sweeps (a CC mapped to an effect/generator's
  ``LIVE_PARAMS`` entry) are a direct, unlocked ``setattr()`` onto the
  running scene's ``effect``/``source`` — no Event, no frame-boundary wait,
  picked up on the render loop's very next read of that attribute. This is
  the cheapest path in the system.

Because neither path touches the DMA socket from this module's reader
thread (unlike :class:`~c64cast.midi_scene.MidiScene`, which writes SID
registers directly), there is nothing to coalesce: every message is
dispatched immediately. The 1ms poll interval mirrors ``MidiScene._reader``
for the same reason it was chosen there — it keeps latency tight — but
without that class's ``_CONTROL_FLUSH_INTERVAL_S`` throttle, since there's
no DMA burst risk here to guard against.

Ensemble targeting is by MIDI channel: channel *N* (1-based) addresses the
Nth system in ensemble order, and a reserved broadcast channel (default 16)
addresses every system at once. A performer retargets by switching their
controller's transmit channel — zero app-side round trip, unlike a menu.
Single-system mode ignores channel entirely.

Out of scope (see CLAUDE.md's [midi_control] note / docs/architecture.md):
anything that would need a scene rebuild (display-mode switches, scene-type
changes) — those cost real network/DMA setup time and are categorically
wrong for a live-hit control. Only Playlist-level Events and
``LIVE_PARAMS``-declared single-numeric-attribute writes are exposed.

Requires the `midi` extra (``pip install c64cast[midi]``).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import MidiControlCfg
    from .playlist import Playlist

log = logging.getLogger(__name__)

# Typed as Any so Pyright doesn't flag every mido.XXX as accessing attributes
# of None — the MIDI_AVAILABLE flag is the runtime guard. Mirrors
# midi_scene.py's import-guard pattern exactly.
try:
    import mido as _mido

    mido: Any = _mido
    MIDI_AVAILABLE = True
except ImportError:
    mido = None
    MIDI_AVAILABLE = False

_CC_TYPES = ("cc", "note", "pc")
_ACTIONS = ("pause", "resume", "toggle_pause", "skip", "cycle_style", "jump", "param")

# 1ms poll — mirrors MidiScene._reader's interval ("keeps note latency
# tight"). No coalescing here (see module docstring): every message this
# module receives is a plain in-process Event/attribute write, not a DMA
# write, so there's no burst risk to throttle against.
_POLL_INTERVAL_S = 0.001


@dataclass(frozen=True)
class _CCMapping:
    kind: str  # "cc" | "note" | "pc"
    number: int  # 0-127
    action: str  # one of _ACTIONS
    scene: int | None = None  # for "jump"
    target: str | None = None  # for "param": "effect.<name>" | "source.<name>"


def _parse_cc_map(raw: list[dict[str, Any]]) -> dict[tuple[str, int], _CCMapping]:
    """Parse cc_map dicts into a (kind, number)-keyed lookup table. Raises
    ValueError on a malformed entry — mirrors config.validate_midi_control_cfg's
    checks, kept independent so this module is testable without config.py's
    ConfigError (the same "config stays import-light" rationale in reverse:
    midi_control.py doesn't need to import config's private choice tuples).
    A later entry with the same (kind, number) overwrites an earlier one, same
    as any TOML list — matches user-override-wins for the shipped defaults."""
    out: dict[tuple[str, int], _CCMapping] = {}
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"cc_map[{i}] must be a table, got {entry!r}")
        kind = entry.get("type")
        if kind not in _CC_TYPES:
            raise ValueError(f"cc_map[{i}].type must be one of {_CC_TYPES}, got {kind!r}")
        number = entry.get("number")
        if not isinstance(number, int) or not 0 <= number <= 127:
            raise ValueError(f"cc_map[{i}].number must be 0..127, got {number!r}")
        action = entry.get("action")
        if action not in _ACTIONS:
            raise ValueError(f"cc_map[{i}].action must be one of {_ACTIONS}, got {action!r}")
        scene = entry.get("scene")
        if action == "jump" and not isinstance(scene, int):
            raise ValueError(f"cc_map[{i}] action 'jump' needs an int 'scene'")
        target = entry.get("target")
        if action == "param" and (not isinstance(target, str) or "." not in target):
            raise ValueError(
                f"cc_map[{i}] action 'param' needs a string 'target' "
                "('effect.<name>' or 'source.<name>')"
            )
        out[(kind, number)] = _CCMapping(
            kind=kind, number=number, action=action, scene=scene, target=target
        )
    return out


class MidiControlListener:
    """Opens its own MIDI input port and dispatches mapped messages to one
    or more :class:`~c64cast.playlist.Playlist` instances. Construct via
    :func:`build_midi_control_listener`; call :meth:`start` / :meth:`stop`
    to run the reader thread for the life of the process."""

    def __init__(
        self,
        playlists: Mapping[str, Playlist],
        cc_map: list[dict[str, Any]],
        *,
        port: str | None = None,
        broadcast_channel: int = 16,
        jump_transition: str = "cut",
    ) -> None:
        if not playlists:
            raise ValueError("midi_control needs at least one playlist")
        self._playlists = dict(playlists)
        self._all = list(self._playlists.values())
        self.port_name = port
        self.broadcast_channel = broadcast_channel
        self.jump_transition = jump_transition
        self._mapping = _parse_cc_map(cc_map)
        self._midi_port: Any = None
        self._stop = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._warned_channels: set[int] = set()

    # ---- MIDI plumbing --------------------------------------------------
    def _open_port(self) -> None:
        assert mido is not None
        if self.port_name in (None, "", "default"):
            names = mido.get_input_names()
            if not names:
                raise RuntimeError("midi_control: no MIDI input ports available")
            self._midi_port = mido.open_input(names[0])
            log.info("midi_control: opened MIDI port %r", names[0])
            return
        names = mido.get_input_names()
        match = next((n for n in names if self.port_name.lower() in n.lower()), None)
        if match is None:
            raise RuntimeError(
                f"midi_control: no MIDI input port matches {self.port_name!r}; available: {names}"
            )
        self._midi_port = mido.open_input(match)
        log.info("midi_control: opened MIDI port %r", match)

    def start(self) -> None:
        if mido is None:
            raise RuntimeError("midi_control requires mido: pip install c64cast[midi]")
        self._open_port()
        self._stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader, daemon=True, name="midi-control-reader"
        )
        self._reader_thread.start()
        log.info(
            "midi_control: listening (%d mapping(s), %d target(s))",
            len(self._mapping),
            len(self._all),
        )

    def stop(self) -> None:
        self._stop.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        if self._midi_port is not None:
            try:
                self._midi_port.close()
            except Exception:
                log.debug("midi_control: port close failed", exc_info=True)
            self._midi_port = None

    def _reader(self) -> None:
        port = self._midi_port
        if port is None:
            return
        try:
            while not self._stop.is_set():
                for msg in port.iter_pending():
                    try:
                        self._dispatch(msg)
                    except Exception:
                        log.exception("midi_control: dispatch failed for %r", msg)
                time.sleep(_POLL_INTERVAL_S)
        except Exception:
            log.exception("midi_control reader crashed")

    # ---- dispatch ---------------------------------------------------------
    def _targets(self, msg: Any) -> list[Playlist]:
        """channel == broadcast_channel-1 -> every playlist; other channel N
        (0-based) -> the Nth playlist in ensemble order if in range, else no
        target (logged once at debug, not per-message — a performer's
        controller idly sending on unrelated channels shouldn't spam logs).
        Single-playlist mode ignores channel entirely."""
        if len(self._all) <= 1:
            return self._all
        channel = getattr(msg, "channel", 0)
        if channel == self.broadcast_channel - 1:
            return self._all
        if 0 <= channel < len(self._all):
            return [self._all[channel]]
        if channel not in self._warned_channels:
            self._warned_channels.add(channel)
            log.debug(
                "midi_control: channel %d has no target system (channels 1..%d "
                "address a system; %d is the broadcast channel)",
                channel + 1,
                len(self._all),
                self.broadcast_channel,
            )
        return []

    def _dispatch(self, msg: Any) -> None:
        if msg.type == "note_on" and msg.velocity > 0:
            kind, number, value = "note", msg.note, msg.velocity
        elif msg.type == "control_change":
            kind, number, value = "cc", msg.control, msg.value
        elif msg.type == "program_change":
            kind, number, value = "pc", msg.program, msg.program
        else:
            return  # note_off, note_on velocity==0, pitchwheel, etc. — not mapped
        mapping = self._mapping.get((kind, number))
        if mapping is None:
            return
        for pl in self._targets(msg):
            try:
                self._apply(pl, mapping, value)
            except Exception:
                log.exception(
                    "midi_control: action %r failed on system %r", mapping.action, pl.name
                )

    def _apply(self, pl: Playlist, mapping: _CCMapping, value: int) -> None:
        action = mapping.action
        if action == "pause":
            pl.pause_event.set()
        elif action == "resume":
            pl.resume_event.set()
        elif action == "toggle_pause":
            (pl.resume_event if pl.pause_event.is_set() else pl.pause_event).set()
        elif action == "skip":
            pl.skip_event.set()
        elif action == "cycle_style":
            pl.cycle_event.set()
        elif action == "jump":
            if mapping.scene is not None and 0 <= mapping.scene < len(pl.scenes):
                pl.request_jump(mapping.scene, skip_interstitial=self.jump_transition == "cut")
        elif action == "param":
            self._apply_param(pl, mapping.target, value)

    def _apply_param(self, pl: Playlist, target: str | None, value_0_127: int) -> None:
        if target is None or pl.current is None:
            return
        holder_attr, _, name = target.partition(".")
        # `scene.<name>` targets the scene itself (scope scenes mix in the
        # renderer, so the param lives on the scene, not a source/effect holder).
        # Kept verbatim-mirrored with wled_device._set_live_param.
        holder = pl.current if holder_attr == "scene" else getattr(pl.current, holder_attr, None)
        if holder is None:
            return
        live_params = getattr(type(holder), "LIVE_PARAMS", {})
        if name not in live_params:
            return  # target has no such LIVE_PARAM — a documented, silent no-op
        lo, hi = live_params[name]
        setattr(holder, name, lo + (value_0_127 / 127.0) * (hi - lo))


def build_midi_control_listener(
    playlists: Mapping[str, Playlist], cfg: MidiControlCfg
) -> MidiControlListener:
    """The one entry point cli.py calls (mirrors
    control_plane.start_control_server's shape, minus the auto-start — call
    .start() on the result). Raises RuntimeError when the `midi` extra isn't
    installed, same pattern MidiScene.__init__ already uses."""
    if not MIDI_AVAILABLE:
        raise RuntimeError("midi_control requires mido: pip install c64cast[midi]")
    return MidiControlListener(
        playlists,
        cfg.cc_map,
        port=cfg.port,
        broadcast_channel=cfg.broadcast_channel,
        jump_transition=cfg.jump_transition,
    )
