"""Live-performance transport + live-tune plumbing.

Phase 1 of the MIDI live-tune feature (see docs/architecture.md → "Live
performance") shipped the pieces that don't need a transport engine:

- :func:`atomic_write_text` — the crash-safe "temp file in the same dir +
  ``os.replace``" write, factored out of :class:`wled_device.PresetStore` so the
  loop-preset store (Phase 3) and the config save-back below share one
  implementation instead of duplicating it.
- :class:`LiveTuneTracker` — records every live parameter change a performer
  makes (a knob sweep, a choice cycle) so the exit save-back flow can write the
  final values back into the run's TOML — the ``[color]`` section for the knobs
  the whole show shares, the scene's own ``[[scenes]]`` block for the ones a
  scene owns — or print a pasteable snippet for a quick-playback run that has no
  file.

Phase 2 adds the actual transport session — DJ-style control of a playing
:class:`~c64cast.scenes.scenes.VideoScene` (pause in place, seek/scrub, RW/FF with
acceleration, an A/B loop) driven from the same ``[midi_control]`` surface
Phase 1 built:

- :class:`TransportEvent` / :class:`TransportSession` — a thread-safe queue
  the MIDI reader thread enqueues into (:mod:`midi_control`'s reader thread,
  never the playlist thread) and :meth:`TransportSession.tick` drains once per
  frame from :meth:`~c64cast.app.playlist.Playlist.run_one_frame`, dispatching
  against whatever scene is current via a duck-typed ``transport_*`` surface
  (see :class:`~c64cast.scenes.scenes.VideoScene`). Held rw/ff notes accelerate over
  time; this keeps all scene/DMA-adjacent mutation on the playlist thread,
  matching the module's existing rule for :class:`LiveTuneTracker`.

Phase 3 adds the record workflow + loop preset slots: a Record/Stop button
pair driving the same ``_loop_a``/``_loop_b``/``_loop_state`` state machine
``transport_loop_toggle`` already used, a red border while a loop is armed,
and Stop-held+pad / Record-held+pad chords (save / clear) into a per-video
:class:`LoopPresetStore`. Phase 5 adds :class:`ControllerProfileStore` — the
``--midi-setup`` learn wizard's output, one JSON file per controller under
:func:`paths.controllers_dir`, cloned from the same tolerant-load / atomic-write
shape. Kept import-light (stdlib
plus the leaf :mod:`c64cast.app.paths` module, which itself imports nothing from
the package; ``Config``/``Playlist``/``Scene`` referenced under TYPE_CHECKING)
so it can be pulled in from playlist.py (and now scenes.py) without a cycle.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import queue
import re
import tempfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from c64cast.app import paths

if TYPE_CHECKING:
    from c64cast.app.config import Config
    from c64cast.app.playlist import Playlist

log = logging.getLogger(__name__)


def timecode(seconds: float) -> str:
    """Format seconds as M:SS — the transport OSD posts and the video
    frame-number debug overlay share this."""
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Write `data` to `path` atomically: a temp file in the same directory,
    fsync'd, then ``os.replace``d onto the target (rename is atomic within a
    filesystem), so a crash mid-write can never leave a half-written file. The
    parent directory is created if missing.

    :func:`atomic_write_text` is the UTF-8 flavor of this; the character-ROM
    installer (:mod:`c64cast.hw.char_rom`) is the binary caller."""
    p = os.fspath(path)
    parent = os.path.dirname(p) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Write `text` to `path` atomically as UTF-8 (see
    :func:`atomic_write_bytes`). Shared by PresetStore and the live-tune
    save-back; the loop-preset store (Phase 3) reuses it too."""
    atomic_write_bytes(path, text.encode("utf-8"))


class JsonSlotStore:
    """Shared shape of the numbered-slot JSON stores: WLED presets
    (:class:`~c64cast.wled.wled_device.PresetStore`), performance looks
    (:class:`~c64cast.control.performance.LookStore`), and per-video loop
    presets (:class:`LoopPresetStore`).

    The contract every subclass keeps: loads are *tolerant* — a missing,
    corrupt, or wrong-shaped file reads as an empty map, and only well-formed
    numeric-key → dict entries survive — and writes are *atomic* via
    :func:`atomic_write_text`, with the parent directory created on demand.
    Slots are stored as string keys of their int value. Subclasses set the
    slot range as class attrs and override the hooks when the payload nests
    the slot map inside an envelope (see :class:`LoopPresetStore`) or needs
    per-entry validation. The path is injectable so tests point it at a
    tempdir. (`transport.ControllerProfileStore` is deliberately *not* one of
    these — its payload is a single mapping-list, not numbered slots.)"""

    #: Valid slot range for :meth:`save`. Slot 0 is the reserved empty slot
    #: everywhere and is never stored.
    SLOT_MIN = 1
    SLOT_MAX = 250

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        slots = self._unwrap(data)
        if not isinstance(slots, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for k, v in slots.items():
            if not (isinstance(v, dict) and str(k).isdigit()):
                continue
            entry = self._coerce_entry(int(k), v)
            if entry is not None:
                out[str(int(k))] = entry
        return out

    def save(self, slot: int, entry: Mapping[str, Any]) -> None:
        if not self.SLOT_MIN <= slot <= self.SLOT_MAX:
            return
        data = self.load()
        data[str(slot)] = dict(entry)
        self._write(data)

    def delete(self, slot: int) -> None:
        data = self.load()
        if data.pop(str(slot), None) is not None:
            self._write(data)

    def _write(self, slots: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path, json.dumps(self._envelope(slots), indent=2, sort_keys=True))

    def _unwrap(self, data: dict[str, Any]) -> object:
        """The slot map inside a loaded payload (default: the payload itself)."""
        return data

    def _envelope(self, slots: Mapping[str, Any]) -> Mapping[str, Any]:
        """The JSON payload persisted around a slot map (default: the map)."""
        return slots

    def _coerce_entry(self, slot: int, entry: dict[str, Any]) -> dict[str, Any] | None:
        """Validate/normalize one loaded entry; ``None`` drops it."""
        return entry if slot != 0 else None


# Live-tune targets whose `mode.<field>` name maps back to a field of the same
# (or a renamed) name on the global [color] section. Live tuning drives the
# running DisplayMode; the save-back writes the tuned value into the Config so
# the next run starts there. `dither_method` on the mode is `[color].dither` in
# the config (the config knob also accepts "auto", which the build step resolves
# to a concrete method — writing the concrete method back is intentional: it
# pins what the performer actually dialed in).
#
# `mode.cell_pick` is `[color].hires_cell_pick` — a second renaming, and one
# that went missing for a while: the knob was declared live-tunable, turned fine
# from every surface, and recorded a change no save-back could ever write.
# tests/test_live_tune.py's drift test now holds this map to the mode registries
# so the next such knob can't ship half-connected.
_MODE_FIELD_TO_COLOR: dict[str, str] = {
    "dither_strength": "dither_strength",
    "motion_smoothing": "motion_smoothing",
    "auto_fit_strength": "auto_fit_strength",
    "dither_method": "dither",
    "cell_strategy": "cell_strategy",
    "cell_pick": "hires_cell_pick",
    "color_match": "color_match",
}

# Live-tune targets whose config home is one `[[scenes]]` block rather than the
# shared [color] section: {mode field: scene field}. These are recorded with the
# index of the scene that was playing when the knob moved, and a save-back writes
# that block and no other — the same knob turned during two scenes is two
# entries, because it is two settings.
_MODE_FIELD_TO_SCENE: dict[str, str] = {
    "palette_mode": "palette_mode",
    "border": "border",
    "background": "background",
}

# The live-tunable mode params that deliberately have no config field at all,
# and why. The drift test above allows exactly these; anything else missing from
# both maps is an oversight, not a decision. Empty today — `palette_mode` was the
# only member until it grew the per-scene home above.
MODE_FIELDS_WITH_NO_CONFIG_HOME: frozenset[str] = frozenset()

# ColorCfg field names a `_MODE_FIELD_TO_COLOR` target may map to — used to
# tell, at save-back time, whether a per-scene row belongs in that scene's
# [scenes.color] override dict rather than as a plain scene attribute (the
# `_MODE_FIELD_TO_SCENE` fields' home).
COLOR_FIELD_NAMES: frozenset[str] = frozenset(_MODE_FIELD_TO_COLOR.values())


def _key(target: str, scene: int | None) -> str:
    """What identifies a tracked change to a surface that wants to drop it. The
    target alone for a global, ``target@<scene>`` for a per-scene one — so the
    same knob on two scenes is two rows a console can discard independently."""
    return target if scene is None else f"{target}@{scene}"


def write_live_tune_row(cfg: Config, scene: int | None, field: str, new: Any) -> str | None:
    """Write one save-back row into `cfg` in place, returning the
    ``<where>.<field> = <value>`` line describing what was written, or None
    if `scene` names an index `cfg` no longer has a block for.

    Shared by :meth:`LiveTuneTracker.apply` and the web console's
    ``_restamp`` (see web_api.py) so the two save-back paths can't drift
    apart on where a row lands: `scene is None` always means the shared
    [color] section; a per-scene row lands on the scene's own attribute
    UNLESS `field` is one of the color-shaping fields (`COLOR_FIELD_NAMES`),
    which instead go into that scene's ``[scenes.color]`` override dict — the
    only way the write ends up where the scene will actually read it back."""
    if scene is None:
        setattr(cfg.color, field, new)
        return f"[color].{field} = {_fmt(new)}"
    if not 0 <= scene < len(cfg.scenes):
        return None
    sc = cfg.scenes[scene]
    if field in COLOR_FIELD_NAMES:
        sc.color[field] = new
        return f"[[scenes]][{scene}].color.{field} = {_fmt(new)}"
    setattr(sc, field, new)
    return f"[[scenes]][{scene}].{field} = {_fmt(new)}"


class _Change(NamedTuple):
    """One tracked movement: what moved, which scene's copy of it (None for a
    [color] field, and for a per-scene one turned on a scene the config did not
    name — a launched clip, an interleaved video), and the ends of the move."""

    target: str
    scene: int | None
    old: Any
    new: Any


class LiveTuneTracker:
    """Records live parameter changes for the exit save-back flow.

    A change is keyed by its live target string (``mode.dither_strength``,
    ``mode.color_match`` …), plus the scene it was made on when the target's
    config home is a ``[[scenes]]`` block rather than ``[color]``. Re-tuning the
    same target keeps the ORIGINAL value as `old` and overwrites `new`, so what's
    recorded is the net change from the config the run started with — a performer
    sweeping a knob back and forth ends up with a single (old → final) entry, not
    a churn of intermediates. A per-scene knob turned on two different scenes is
    two entries for the same reason: they are two settings, not one moved twice.

    A `_MODE_FIELD_TO_COLOR` target (e.g. ``mode.dither_strength``) normally
    saves to the shared ``[color]`` section, but a scene that carries its own
    ``[scenes.color]`` override for that field reads *that*, not the global —
    so tuning the knob while such a scene plays has to save into its block
    instead, or the save-back would write somewhere the scene ignores. That
    check needs the run's `Config`, passed in at construction; without one
    (the common case in tests, and any caller with no file to save back to)
    every `_MODE_FIELD_TO_COLOR` target keeps its old, always-global home.

    Thread-safe: the MIDI reader thread and the WLED server thread both record;
    the exit flow (main thread) reads. `has_changes` / `describe` / `pending` /
    `apply` are the read side, and a web console's HTTP worker is a third
    reader — `pending` is the structured face of `describe`, for a save-back
    surface that has to render the changes rather than print them."""

    def __init__(self, cfg: Config | None = None) -> None:
        self._lock = threading.Lock()
        # key (see _key) -> _Change; insertion order preserved.
        self._changes: dict[str, _Change] = {}
        self._cfg = cfg

    def _scene_overrides(self, scene: int | None, color_field: str) -> bool:
        """True if `scene` authors `color_field` in its own [scenes.color] —
        the only case a `_MODE_FIELD_TO_COLOR` target routes per-scene rather
        than to the shared [color] section."""
        if self._cfg is None or scene is None:
            return False
        if not 0 <= scene < len(self._cfg.scenes):
            return False
        return color_field in self._cfg.scenes[scene].color

    def _config_home(self, target: str, scene: int | None) -> tuple[str | None, bool]:
        """Where `target`'s value is kept in a config file: the field's name
        there, and whether that field belongs to `scene` rather than to the
        shared section.

        (None, False) for a target no config carries — every non-``mode.`` one,
        since effect and generator knobs are runtime state and writing them
        would put knob positions in a show file. A `_MODE_FIELD_TO_SCENE`
        target is always per-scene, as it always was; a `_MODE_FIELD_TO_COLOR`
        one is per-scene only when `_scene_overrides` says `scene` claims it."""
        holder, _, name = target.partition(".")
        if holder != "mode":
            return None, False
        scene_field = _MODE_FIELD_TO_SCENE.get(name)
        if scene_field is not None:
            return scene_field, True
        color_field = _MODE_FIELD_TO_COLOR.get(name)
        if color_field is None:
            return None, False
        return color_field, self._scene_overrides(scene, color_field)

    def record(self, target: str, old: Any, new: Any, *, scene: int | None = None) -> None:
        """Note that `target` moved from `old` to `new` while `scene` was
        playing. No-op when the value didn't actually change (a knob landing back
        where it started clears the entry).

        `scene` is the index of the ``[[scenes]]`` block the current scene was
        built from (None when it came from no block). Callers pass whatever is
        playing and let this decide whether it matters: it is part of the key
        only for the per-scene targets, so a ``[color]`` knob swept across a
        scene change stays one entry."""
        _, per_scene = self._config_home(target, scene)
        at = scene if per_scene else None
        key = _key(target, at)
        with self._lock:
            existing = self._changes.get(key)
            base = existing.old if existing is not None else old
            if _values_equal(base, new):
                # Back to where it started (or a no-op write) — drop the entry.
                self._changes.pop(key, None)
            else:
                self._changes[key] = _Change(target, at, base, new)

    def has_changes(self) -> bool:
        with self._lock:
            return bool(self._changes)

    def describe(self) -> list[str]:
        """Human-readable ``target: old -> new`` lines, for the exit prompt.
        A per-scene target names the scene it belongs to, counted from 1 to match
        the playlist's own ``scene N/M`` logging."""
        with self._lock:
            changes = list(self._changes.values())
        return [
            f"{c.target}{'' if c.scene is None else f' (scene {c.scene + 1})'}: "
            f"{_fmt(c.old)} -> {_fmt(c.new)}"
            for c in changes
        ]

    def pending(self) -> list[dict[str, Any]]:
        """Every tracked change as a row: the target, where it started, where it
        is now, and where a save-back would write it.

        ``field`` is the config field's own name and ``scene`` says which file
        section carries it — None for ``[color]``, otherwise the index of the
        ``[[scenes]]`` block. ``field`` is None for a change nothing can write:
        every non-``mode.`` target (effect and generator knobs are runtime state),
        and a per-scene target turned on a scene the config did not name, which
        has no block to write to. ``key`` is what :meth:`forget` takes.

        A surface that offers to save has to say which kind a row is, so the one
        snapshot answers every question about it; taking it once also means a
        knob turned between two reads cannot make the list disagree with itself."""
        with self._lock:
            items = list(self._changes.items())
        rows: list[dict[str, Any]] = []
        for key, c in items:
            field, per_scene = self._config_home(c.target, c.scene)
            if per_scene and c.scene is None:
                field = None
            rows.append(
                {
                    "key": key,
                    "target": c.target,
                    "old": c.old,
                    "new": c.new,
                    "field": field,
                    "scene": c.scene,
                }
            )
        return rows

    def forget(self, keys: Iterable[str]) -> int:
        """Drop `keys`, returning how many were actually held.

        What a surface that has *written* them somewhere calls, so a console
        stops offering to save a change that is now in the file — and what a
        "discard" is, handed the ``key`` of every :meth:`pending` row."""
        with self._lock:
            return sum(int(self._changes.pop(k, None) is not None) for k in keys)

    def _persistable(self) -> list[dict[str, Any]]:
        """The :meth:`pending` rows a config file can actually take."""
        return [r for r in self.pending() if r["field"] is not None]

    def apply(self, cfg: Config) -> list[str]:
        """Write the tracked changes into `cfg` (in place), returning a
        ``<where>.<field> = <value>`` line for each one applied.

        Changes nothing can carry are skipped, and so is a scene index `cfg` has
        no block for — a config reloaded with fewer scenes since the knob moved
        would otherwise write the value into whichever scene inherited the
        index. See :func:`write_live_tune_row` for where each row lands."""
        applied: list[str] = []
        for row in self._persistable():
            line = write_live_tune_row(cfg, row["scene"], row["field"], row["new"])
            if line is not None:
                applied.append(line)
        return applied

    def toml_snippet(self) -> str:
        """A pasteable ``[color]`` TOML block for the tracked changes — used for
        quick-playback runs that have no config file to write back to. Empty
        string when nothing persistable changed.

        Per-scene changes ride along as comments rather than as TOML: they belong
        *inside* a ``[[scenes]]`` block, and a pasted ``[[scenes]]`` header would
        append a scene instead of editing one."""
        rows = self._persistable()
        if not rows:
            return ""
        # De-dupe (last write wins) while keeping a stable order.
        merged: dict[str, Any] = {}
        notes: list[str] = []
        for row in rows:
            if row["scene"] is None:
                merged[row["field"]] = row["new"]
            elif row["field"] in COLOR_FIELD_NAMES:
                notes.append(
                    f"# in scene {row['scene'] + 1}'s [scenes.color] block: "
                    f"{row['field']} = {_toml_value(row['new'])}"
                )
            else:
                notes.append(
                    f"# in scene {row['scene'] + 1}'s [[scenes]] block: "
                    f"{row['field']} = {_toml_value(row['new'])}"
                )
        lines: list[str] = []
        if merged:
            lines.append("[color]")
            lines.extend(f"{f} = {_toml_value(v)}" for f, v in merged.items())
        return "\n".join(lines + notes)


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

# Record/Stop are single-button hold-tracked modifiers for the loop_slot pad
# chords (Stop-held+pad = save, Record-held+pad = clear) — distinct from
# _HOLD_ACTIONS above, which drives the continuous rw/ff seek ramp. A held
# flag auto-expires after this many seconds even with no release, because an
# MMC-sourced Record/Stop press (see midi_control._dispatch) never generates
# a release event at all — without an expiry, one MMC press would wedge
# every later pad press as a chord for the rest of the session.
_CHORD_HOLD_WINDOW_S = 5.0


@dataclass(frozen=True)
class TransportEvent:
    """One transport action, queued by whichever surface fired it (the MIDI
    reader thread, or a web console's HTTP worker — see
    :meth:`c64cast.control.perf_console.PerfBridge.transport`) and drained on
    the playlist thread by :meth:`TransportSession.tick`.

    ``action`` is the short form (``"play_pause"``, ``"stop"``, ``"record"``,
    ``"loop_toggle"``, ``"rw"``, ``"ff"``, ``"jog"``, ``"seek"``,
    ``"loop_slot"`` — the cc_map action string with any ``"transport."``
    prefix stripped; plain ``loop_slot`` has no prefix to strip). ``pressed``
    distinguishes a note-on from a note-off for the hold-aware rw/ff actions
    (ignored by the others). ``value`` is the raw MIDI value/velocity
    (0-127) — used by ``jog``. ``mode`` is jog's ``"abs"``/``"rel"``
    (default ``"rel"``), from the cc_map entry. ``slot`` is the pad number
    for ``loop_slot`` (unused otherwise). ``target`` is an absolute
    content-seconds position for ``seek`` — a scrub bar's drag target, in a
    different domain than ``jog``'s 0..127 controller reading. ``save``/
    ``clear`` override ``loop_slot``'s MIDI Stop/Record-held chord detection
    with an explicit choice — what a console with no "hold Stop" gesture
    sends instead; ``None`` (the MIDI default) falls back to the chord."""

    action: str
    pressed: bool = True
    value: int = 0
    mode: str = "rel"
    slot: int = 0
    target: float | None = None
    save: bool | None = None
    clear: bool | None = None


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

    Construct one per :class:`~c64cast.app.playlist.Playlist` (mirrors
    ``Playlist.live_tracker``). :meth:`enqueue` is called from the MIDI
    reader thread; :meth:`tick` is called from the playlist thread only —
    all scene mutation happens there, never on the MIDI thread (the same
    rule ``midi_control``'s other actions already follow via
    ``threading.Event``/direct ``LIVE_PARAMS`` writes).

    Dispatch is duck-typed against ``pl.current`` — a scene that doesn't
    declare the ``transport_*`` surface (see
    :class:`~c64cast.scenes.scenes.VideoScene`) is a silent no-op, exactly like a
    ``LIVE_PARAMS``/``LIVE_CHOICES`` target that doesn't exist on the
    current holder."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[TransportEvent] = queue.SimpleQueue()
        # action name -> wall-time the hold started. Mutated only in tick()
        # (playlist thread) — enqueue() only ever pushes onto _queue.
        self._held: dict[str, float] = {}
        self._last_tick: float | None = None
        # Record/Stop hold state for the loop_slot pad chords — wall-time the
        # button was pressed, or None when released/expired. See
        # _CHORD_HOLD_WINDOW_S.
        self._record_held_since: float | None = None
        self._stop_held_since: float | None = None

    def enqueue(self, event: TransportEvent) -> None:
        self._queue.put(event)

    def tick(self, pl: Playlist, now: float) -> None:
        """Drain queued events, dispatch each against ``pl.current``, then
        advance any held rw/ff ramp. Called once per frame from
        ``Playlist.run_one_frame``, right before ``scene.process_frame``."""
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
        # Record/Stop chord bookkeeping — same "survives no current scene"
        # rule as rw/ff above — but these ALSO have a one-shot press action
        # (arm / the 3-way stop state machine), so they fall through to the
        # dispatch below instead of returning early.
        if event.action == "record":
            self._record_held_since = now if event.pressed else None
        elif event.action == "stop":
            self._stop_held_since = now if event.pressed else None
        if pl.transitioning or pl.current is None:
            return
        scene = pl.current
        if event.action == "play_pause":
            if event.pressed:
                toggle = getattr(scene, "transport_toggle_pause", None)
                if toggle is not None:
                    toggle()
        elif event.action in ("freeze", "unfreeze"):
            # Checked here rather than by the caller that enqueued this: two
            # duplicate requests (two open consoles, a network retry) both
            # land on this one drain loop, so the second sees the first's
            # effect and no-ops instead of toggling back.
            is_paused = getattr(scene, "transport_is_paused", None)
            if is_paused is not None and is_paused() != (event.action == "freeze"):
                toggle = getattr(scene, "transport_toggle_pause", None)
                if toggle is not None:
                    toggle()
        elif event.action == "stop":
            if event.pressed:
                stop = getattr(scene, "transport_stop", None)
                if stop is not None and stop():
                    pl.stop_event.set()
        elif event.action == "loop_toggle":
            if event.pressed:
                loop_toggle = getattr(scene, "transport_loop_toggle", None)
                if loop_toggle is not None:
                    loop_toggle()
        elif event.action == "record":
            if event.pressed:
                record = getattr(scene, "transport_record", None)
                if record is not None:
                    record()
        elif event.action == "loop_slot":
            if event.pressed:
                loop_slot = getattr(scene, "transport_loop_slot", None)
                if loop_slot is not None:
                    if event.save is not None or event.clear is not None:
                        # An explicit choice (a console with no "hold Stop"
                        # gesture) overrides the MIDI chord entirely.
                        clear = bool(event.clear)
                        save = (not clear) and bool(event.save)
                    else:
                        clear = self._chord_active(self._record_held_since, now)
                        save = (not clear) and self._chord_active(self._stop_held_since, now)
                    loop_slot(event.slot, save=save, clear=clear)
        elif event.action == "jog":
            self._apply_jog(scene, event)
        elif event.action == "seek":
            seek = getattr(scene, "transport_seek", None)
            if event.pressed and event.target is not None and seek is not None:
                seek(event.target)

    @staticmethod
    def _chord_active(held_since: float | None, now: float) -> bool:
        return held_since is not None and (now - held_since) < _CHORD_HOLD_WINDOW_S

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


# ---- Loop preset store (Phase 3) -------------------------------------------
#
# One JSON file per video under `paths.loop_presets_dir()`
# (<data root>/presets/loops), resolved at use time so it works from a repo
# checkout or an installed wheel (and honors $C64CAST_DATA_DIR).
# Keyed by a path-move-tolerant identity: local files hash on basename+size
# (survives a move, not a content edit — the same tradeoff
# wled_device.PresetStore already accepts for its own presets); URL-backed
# scenes hash on the URL itself. Slots are pad numbers (small positive ints);
# b=None means "loop to end of file".


def _video_identity(filepath: str) -> tuple[str, int | None]:
    """(hash_basis, size). `size` is None for a URL or an unreadable path."""
    if "://" in filepath:
        return filepath, None
    try:
        size: int | None = os.path.getsize(filepath)
    except OSError:
        size = None
    return f"{os.path.basename(filepath)}:{size}", size


def loop_preset_key(filepath: str) -> str:
    basis, _ = _video_identity(filepath)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _slugify(filepath: str) -> str:
    base = filepath if "://" in filepath else os.path.splitext(os.path.basename(filepath))[0]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return slug[:40] or "video"


def loop_preset_path(filepath: str) -> Path:
    return paths.loop_presets_dir() / f"{_slugify(filepath)}.{loop_preset_key(filepath)}.json"


class LoopPresetStore(JsonSlotStore):
    """Persists named A/B loop points for one video file (one JSON file per
    video), on the shared :class:`JsonSlotStore` contract. The slot map lives
    under a ``{"schema", "video", "size", "loops": {...}}`` envelope (the
    hooks below), entries are normalized to ``{"a": float, "b": float|None}``
    on load, and :meth:`save` takes the loop points directly — loop slots are
    pad numbers with no fixed range, so it skips the base range check."""

    SCHEMA = 1

    def __init__(self, path: Path, *, video_ref: str, size: int | None) -> None:
        super().__init__(path)
        self._video_ref = video_ref
        self._size = size

    def save(self, slot: int, a: float, b: float | None) -> None:  # type: ignore[override]
        data = self.load()
        data[str(slot)] = {"a": a, "b": b}
        self._write(data)

    def _unwrap(self, data: dict[str, Any]) -> object:
        return data.get("loops")

    def _envelope(self, slots: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "schema": self.SCHEMA,
            "video": self._video_ref,
            "size": self._size,
            "loops": slots,
        }

    def _coerce_entry(self, slot: int, entry: dict[str, Any]) -> dict[str, Any] | None:
        a = entry.get("a")
        b = entry.get("b")
        if not isinstance(a, (int, float)):
            return None
        if b is not None and not isinstance(b, (int, float)):
            return None
        return {"a": float(a), "b": float(b) if b is not None else None}


def make_loop_preset_store(filepath: str) -> LoopPresetStore:
    warn_if_legacy_presets_orphaned()
    _, size = _video_identity(filepath)
    return LoopPresetStore(loop_preset_path(filepath), video_ref=filepath, size=size)


# One-time heads-up when presets are stranded at the old repo `presets/` dir.
# This replaces the removed `--doctor` migration nudge with a use-site log: it
# fires from the preset-store resolvers (WLED / looks / loops) the first time
# any of them runs, so a user who never touches presets never sees it. Presets
# moved to the canonical data dir (paths.presets_dir()), so files left behind
# in a source checkout are simply no longer read.
_warned_legacy_presets = False


def warn_if_legacy_presets_orphaned() -> None:
    """Log once (at most) if a source checkout still has preset files at the
    old repo ``presets/`` location — they are no longer read. No-op for an
    installed package, a clean checkout, or once the presets have been moved to
    the canonical data dir. See :func:`c64cast.app.paths.legacy_presets_dir`."""
    global _warned_legacy_presets
    if _warned_legacy_presets:
        return
    _warned_legacy_presets = True
    legacy = paths.legacy_presets_dir()
    if legacy is None:
        return
    canonical = paths.presets_dir()
    log.warning(
        "found preset files at the old repo location %s; they are no longer "
        "read. Move them to keep them: mkdir -p %s && mv %s/* %s/",
        legacy,
        canonical,
        legacy,
        canonical,
    )


def slugify_port(port_name: str) -> str:
    """A filesystem-safe slug of a mido port name (the controller-profile
    filename stem). Distinct from :func:`_slugify` (which is video-oriented:
    it strips a file extension and special-cases URLs) — a port name is neither
    a path nor a URL, so it just gets lower-cased alnum-run collapsing."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", port_name).strip("-").lower()
    return slug[:60] or "controller"


def controller_profile_path(port_name: str) -> Path:
    return paths.controllers_dir() / f"{slugify_port(port_name)}.json"


class ControllerProfileStore:
    """Persists a learned MIDI controller profile (the ``--midi-setup`` output):
    one JSON file per controller holding the full mido port name it was learned
    from plus a list of cc_map-style mapping dicts. Cloned from
    :class:`LoopPresetStore`'s tolerant-load / :func:`atomic_write_text` shape
    (not shared — the id scheme + payload differ). The path is injectable so the
    listener's profile resolver and the tests can point it at a tempdir.

    Schema: ``{"schema": 1, "port": "<full mido port name>",
    "mappings": [<cc_map dict>, ...]}``. A missing or corrupt file, or a
    malformed ``mappings`` list, loads as an empty profile — a bad profile
    can never crash a run, it just contributes no mappings."""

    SCHEMA = 1

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _load_raw(self) -> dict[str, Any]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def port(self) -> str:
        """The full mido port name the profile was learned from (``""`` when the
        file is missing/corrupt or omits it)."""
        port = self._load_raw().get("port")
        return port if isinstance(port, str) else ""

    def mappings(self) -> list[dict[str, Any]]:
        """The learned cc_map-style mappings (an empty list on any problem).
        Only well-formed dict entries survive — the caller (``_parse_cc_map`` /
        ``validate_midi_control_cfg``) still validates each entry's shape."""
        raw = self._load_raw().get("mappings")
        if not isinstance(raw, list):
            return []
        return [dict(m) for m in raw if isinstance(m, dict)]

    def feedback(self) -> dict[str, Any]:
        """The optional grid-controller LED-feedback block (Live DJ/VJ Phase 4):
        the per-controller velocity->color convention + an output `port`. An empty
        dict when the file is missing/corrupt or carries no `feedback` table —
        :meth:`c64cast.control.midi_control.FeedbackMap.from_dict` then falls back to the
        shipped defaults, so a bad block can never break feedback."""
        raw = self._load_raw().get("feedback")
        return dict(raw) if isinstance(raw, dict) else {}

    def save(
        self, port: str, mappings: list[dict[str, Any]], *, feedback: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {"schema": self.SCHEMA, "port": port, "mappings": mappings}
        if feedback:
            payload["feedback"] = feedback
        atomic_write_text(self._path, json.dumps(payload, indent=2, sort_keys=True))


def make_controller_profile_store(port_name: str) -> ControllerProfileStore:
    return ControllerProfileStore(controller_profile_path(port_name))
