"""Live-performance transport + live-tune plumbing.

Phase 1 of the MIDI live-tune feature (see docs/architecture.md → "Live
performance") shipped the pieces that don't need a transport engine:

- :func:`atomic_write_text` — the crash-safe "temp file in the same dir +
  ``os.replace``" write, factored out of :class:`wled_device.PresetStore` so the
  loop-preset store (Phase 3) and the config save-back below share one
  implementation instead of duplicating it.
- :class:`LiveTuneTracker` — records every live parameter change a performer
  makes (a knob sweep, a choice cycle) so the exit save-back flow can write the
  final values back into the ``[color]`` section of the run's TOML, or print a
  pasteable snippet for a quick-playback run that has no file.

Phase 2 adds the actual transport session — DJ-style control of a playing
:class:`~c64cast.scenes.VideoScene` (pause in place, seek/scrub, RW/FF with
acceleration, an A/B loop) driven from the same ``[midi_control]`` surface
Phase 1 built:

- :class:`TransportEvent` / :class:`TransportSession` — a thread-safe queue
  the MIDI reader thread enqueues into (:mod:`midi_control`'s reader thread,
  never the playlist thread) and :meth:`TransportSession.tick` drains once per
  frame from :meth:`~c64cast.playlist.Playlist._run_one_frame`, dispatching
  against whatever scene is current via a duck-typed ``transport_*`` surface
  (see :class:`~c64cast.scenes.VideoScene`). Held rw/ff notes accelerate over
  time; this keeps all scene/DMA-adjacent mutation on the playlist thread,
  matching the module's existing rule for :class:`LiveTuneTracker`.

Later phases (3-5) add the record workflow + loop preset slots, real audio
resync across a seek, and the ``--midi-setup`` learn wizard. Kept import-light
(stdlib only; ``Config``/``Playlist``/``Scene`` referenced under
TYPE_CHECKING) so it can be pulled in from playlist.py without a cycle.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import tempfile
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .playlist import Playlist

log = logging.getLogger(__name__)


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Write `text` to `path` atomically: a temp file in the same directory,
    fsync'd, then ``os.replace``d onto the target (rename is atomic within a
    filesystem), so a crash mid-write can never leave a half-written file. The
    parent directory is created if missing. Shared by PresetStore and the
    live-tune save-back; the loop-preset store (Phase 3) reuses it too."""
    p = os.fspath(path)
    parent = os.path.dirname(p) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# Live-tune targets whose `mode.<field>` name maps back to a field of the same
# (or a renamed) name on the global [color] section. Live tuning drives the
# running DisplayMode; the save-back writes the tuned value into the Config so
# the next run starts there. `dither_method` on the mode is `[color].dither` in
# the config (the config knob also accepts "auto", which the build step resolves
# to a concrete method — writing the concrete method back is intentional: it
# pins what the performer actually dialed in).
#
# `mode.palette_mode` is deliberately absent: palette_mode lives per-scene
# ([[scenes]].palette_mode), not in the shared [color] section, so persisting it
# would need the scene index and is left to a later phase — the live change still
# takes effect at runtime, it just isn't saved.
_MODE_FIELD_TO_COLOR: dict[str, str] = {
    "dither_strength": "dither_strength",
    "motion_smoothing": "motion_smoothing",
    "auto_fit_strength": "auto_fit_strength",
    "dither_method": "dither",
    "cell_strategy": "cell_strategy",
    "color_match": "color_match",
}


class LiveTuneTracker:
    """Records live parameter changes for the exit save-back flow.

    A change is keyed by its live target string (``mode.dither_strength``,
    ``mode.color_match`` …). Re-tuning the same target keeps the ORIGINAL value
    as `old` and overwrites `new`, so what's recorded is the net change from the
    config the run started with — a performer sweeping a knob back and forth ends
    up with a single (old → final) entry, not a churn of intermediates.

    Thread-safe: the MIDI reader thread and the WLED server thread both record;
    the exit flow (main thread) reads. `has_changes` / `describe` / `apply` are
    the read side."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # target -> (old_value, new_value); insertion order preserved.
        self._changes: dict[str, tuple[Any, Any]] = {}

    def record(self, target: str, old: Any, new: Any) -> None:
        """Note that `target` moved from `old` to `new`. No-op when the value
        didn't actually change (a knob landing back where it started clears the
        entry)."""
        with self._lock:
            existing = self._changes.get(target)
            base = existing[0] if existing is not None else old
            if _values_equal(base, new):
                # Back to where it started (or a no-op write) — drop the entry.
                self._changes.pop(target, None)
            else:
                self._changes[target] = (base, new)

    def has_changes(self) -> bool:
        with self._lock:
            return bool(self._changes)

    def describe(self) -> list[str]:
        """Human-readable ``target: old -> new`` lines, for the exit prompt."""
        with self._lock:
            return [f"{t}: {_fmt(o)} -> {_fmt(n)}" for t, (o, n) in self._changes.items()]

    def _persistable(self) -> list[tuple[str, Any]]:
        """(color_field, new_value) pairs for targets that map to [color]."""
        with self._lock:
            items = list(self._changes.items())
        out: list[tuple[str, Any]] = []
        for target, (_, new) in items:
            holder, _, name = target.partition(".")
            if holder != "mode":
                continue
            field = _MODE_FIELD_TO_COLOR.get(name)
            if field is not None:
                out.append((field, new))
        return out

    def apply(self, cfg: Config) -> list[str]:
        """Write the tracked changes into `cfg`'s [color] section (in place).
        Returns ``[color].<field> = <value>`` lines for the ones applied (targets
        that don't map to [color], e.g. palette_mode, are skipped)."""
        applied: list[str] = []
        for field, new in self._persistable():
            setattr(cfg.color, field, new)
            applied.append(f"[color].{field} = {_fmt(new)}")
        return applied

    def toml_snippet(self) -> str:
        """A pasteable ``[color]`` TOML block for the tracked changes — used for
        quick-playback runs that have no config file to write back to. Empty
        string when nothing persistable changed."""
        pairs = self._persistable()
        if not pairs:
            return ""
        # De-dupe (last write wins) while keeping a stable order.
        merged: dict[str, Any] = {}
        for field, new in pairs:
            merged[field] = new
        lines = ["[color]"]
        for field, new in merged.items():
            lines.append(f"{field} = {_toml_value(new)}")
        return "\n".join(lines)


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return bool(a == b)
    return bool(a == b)


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


# Held-action ramp (rw/ff): media-seconds covered per real second at hold
# duration `elapsed`. Starts near 1x and doubles every _RAMP_DOUBLE_S seconds,
# capped at _MAX_HOLD_SPEED — fine control on a quick tap, fast travel on a
# long hold. HW-tuned constants, see the transport design doc.
_MAX_HOLD_SPEED = 30.0
_RAMP_DOUBLE_S = 0.75
# Relative jog: media-seconds moved per encoder tick.
_JOG_SECONDS_PER_TICK = 1.0

_HOLD_ACTIONS = ("rw", "ff")


@dataclass(frozen=True)
class TransportEvent:
    """One MIDI-triggered transport action, queued by the MIDI reader thread
    and drained on the playlist thread by :meth:`TransportSession.tick`.

    ``action`` is the short form (``"play_pause"``, ``"stop"``,
    ``"loop_toggle"``, ``"rw"``, ``"ff"``, ``"jog"`` — the cc_map action
    string with its ``"transport."`` prefix stripped). ``pressed``
    distinguishes a note-on from a note-off for the hold-aware rw/ff actions
    (ignored by the others). ``value`` is the raw MIDI value/velocity
    (0-127) — used by ``jog``. ``mode`` is jog's ``"abs"``/``"rel"``
    (default ``"rel"``), from the cc_map entry."""

    action: str
    pressed: bool = True
    value: int = 0
    mode: str = "rel"


def _decode_relative_jog(value: int) -> int:
    """Decode a relative-encoder CC byte (the common Launch Control/APC
    two's-complement-style convention): 1..63 -> +N ticks, 65..127 ->
    -(128-N) ticks, 0/64 (no motion / center rest) -> 0 ticks."""
    if 1 <= value <= 63:
        return value
    if 65 <= value <= 127:
        return value - 128
    return 0


class TransportSession:
    """Applies queued :class:`TransportEvent`\\s to the playlist's current
    scene once per frame, and drives the RW/FF hold-acceleration ramp.

    Construct one per :class:`~c64cast.playlist.Playlist` (mirrors
    ``Playlist.live_tracker``). :meth:`enqueue` is called from the MIDI
    reader thread; :meth:`tick` is called from the playlist thread only —
    all scene mutation happens there, never on the MIDI thread (the same
    rule ``midi_control``'s other actions already follow via
    ``threading.Event``/direct ``LIVE_PARAMS`` writes).

    Dispatch is duck-typed against ``pl.current`` — a scene that doesn't
    declare the ``transport_*`` surface (see
    :class:`~c64cast.scenes.VideoScene`) is a silent no-op, exactly like a
    ``LIVE_PARAMS``/``LIVE_CHOICES`` target that doesn't exist on the
    current holder."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[TransportEvent] = queue.SimpleQueue()
        # action name -> wall-time the hold started. Mutated only in tick()
        # (playlist thread) — enqueue() only ever pushes onto _queue.
        self._held: dict[str, float] = {}
        self._last_tick: float | None = None

    def enqueue(self, event: TransportEvent) -> None:
        self._queue.put(event)

    def tick(self, pl: Playlist, now: float) -> None:
        """Drain queued events, dispatch each against ``pl.current``, then
        advance any held rw/ff ramp. Called once per frame from
        ``Playlist._run_one_frame``, right before ``scene.process_frame``."""
        dt = now - self._last_tick if self._last_tick is not None else 0.0
        self._last_tick = now
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            self._dispatch(pl, event, now)
        if pl.transitioning or pl.current is None or dt <= 0.0:
            return
        scene = pl.current
        seek = getattr(scene, "transport_seek", None)
        position = getattr(scene, "transport_position", None)
        if seek is None or position is None:
            return
        for action, start in list(self._held.items()):
            elapsed = now - start
            speed = min(_MAX_HOLD_SPEED, 2.0 ** (elapsed / _RAMP_DOUBLE_S))
            delta = speed * dt * (-1.0 if action == "rw" else 1.0)
            seek(position() + delta)

    def _dispatch(self, pl: Playlist, event: TransportEvent, now: float) -> None:
        if event.action in _HOLD_ACTIONS:
            # Hold bookkeeping happens regardless of whether a scene is
            # currently on screen — if one becomes current mid-hold, the
            # ramp in tick() picks it up from wherever the hold started.
            if event.pressed:
                self._held.setdefault(event.action, now)
            else:
                self._held.pop(event.action, None)
            return
        if pl.transitioning or pl.current is None:
            return
        scene = pl.current
        if event.action == "play_pause":
            toggle = getattr(scene, "transport_toggle_pause", None)
            if toggle is not None:
                toggle()
        elif event.action == "stop":
            # Full stop/record/quit state machine is Phase 3 — this phase
            # `transport.stop` is pause-only.
            pause = getattr(scene, "transport_pause", None)
            if pause is not None:
                pause()
        elif event.action == "loop_toggle":
            loop_toggle = getattr(scene, "transport_loop_toggle", None)
            if loop_toggle is not None:
                loop_toggle()
        elif event.action == "jog":
            self._apply_jog(scene, event)

    @staticmethod
    def _apply_jog(scene: Any, event: TransportEvent) -> None:
        seek = getattr(scene, "transport_seek", None)
        position = getattr(scene, "transport_position", None)
        duration = getattr(scene, "transport_duration", None)
        if seek is None or position is None:
            return
        if event.mode == "abs":
            total = duration() if duration is not None else None
            target = (event.value / 127.0) * (total or 0.0)
        else:
            ticks = _decode_relative_jog(event.value)
            if ticks == 0:
                return
            target = position() + ticks * _JOG_SECONDS_PER_TICK
        seek(target)
