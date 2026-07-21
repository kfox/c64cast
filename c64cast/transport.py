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

Phase 3 adds the record workflow + loop preset slots: a Record/Stop button
pair driving the same ``_loop_a``/``_loop_b``/``_loop_state`` state machine
``transport_loop_toggle`` already used, a red border while a loop is armed,
and Stop-held+pad / Record-held+pad chords (save / clear) into a per-video
:class:`LoopPresetStore`. Phase 5 adds :class:`ControllerProfileStore` — the
``--midi-setup`` learn wizard's output, one JSON file per controller under
:func:`paths.controllers_dir`, cloned from the same tolerant-load / atomic-write
shape. Kept import-light (stdlib
plus the leaf :mod:`c64cast.paths` module, which itself imports nothing from
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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import paths

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
    """One MIDI-triggered transport action, queued by the MIDI reader thread
    and drained on the playlist thread by :meth:`TransportSession.tick`.

    ``action`` is the short form (``"play_pause"``, ``"stop"``, ``"record"``,
    ``"loop_toggle"``, ``"rw"``, ``"ff"``, ``"jog"``, ``"loop_slot"`` — the
    cc_map action string with any ``"transport."`` prefix stripped; plain
    ``loop_slot`` has no prefix to strip). ``pressed`` distinguishes a
    note-on from a note-off for the hold-aware rw/ff/record/stop actions
    (ignored by the others). ``value`` is the raw MIDI value/velocity
    (0-127) — used by ``jog``. ``mode`` is jog's ``"abs"``/``"rel"``
    (default ``"rel"``), from the cc_map entry. ``slot`` is the pad number
    for ``loop_slot`` (unused otherwise)."""

    action: str
    pressed: bool = True
    value: int = 0
    mode: str = "rel"
    slot: int = 0


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
                    clear = self._chord_active(self._record_held_since, now)
                    save = (not clear) and self._chord_active(self._stop_held_since, now)
                    loop_slot(event.slot, save=save, clear=clear)
        elif event.action == "jog":
            self._apply_jog(scene, event)

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
# checkout, a pip install, or a PyPI wheel (and honors $C64CAST_DATA_DIR).
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


class LoopPresetStore:
    """Persists named A/B loop points for one video file (one JSON file per
    video). Loads are tolerant (a missing or corrupt file reads as an empty
    map); writes are atomic via :func:`atomic_write_text`, mirroring
    :class:`~c64cast.wled_device.PresetStore`'s shape (cloned, not shared —
    the id scheme differs: a hash-string key with no fixed range, one file
    per video rather than one file per device holding many numbered
    presets). The path is injectable so tests point it at a tempdir."""

    SCHEMA = 1

    def __init__(self, path: Path, *, video_ref: str, size: int | None) -> None:
        self._path = Path(path)
        self._video_ref = video_ref
        self._size = size

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, dict[str, float | None]]:
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
        loops = data.get("loops")
        if not isinstance(loops, dict):
            return {}
        out: dict[str, dict[str, float | None]] = {}
        for k, v in loops.items():
            if not (isinstance(v, dict) and str(k).isdigit()):
                continue
            a = v.get("a")
            b = v.get("b")
            if not isinstance(a, (int, float)):
                continue
            if b is not None and not isinstance(b, (int, float)):
                continue
            out[str(int(k))] = {"a": float(a), "b": float(b) if b is not None else None}
        return out

    def save(self, slot: int, a: float, b: float | None) -> None:
        data = self.load()
        data[str(slot)] = {"a": a, "b": b}
        self._write(data)

    def delete(self, slot: int) -> None:
        data = self.load()
        if data.pop(str(slot), None) is not None:
            self._write(data)

    def _write(self, loops: dict[str, dict[str, float | None]]) -> None:
        payload = {
            "schema": self.SCHEMA,
            "video": self._video_ref,
            "size": self._size,
            "loops": loops,
        }
        atomic_write_text(self._path, json.dumps(payload, indent=2, sort_keys=True))


def make_loop_preset_store(filepath: str) -> LoopPresetStore:
    _, size = _video_identity(filepath)
    return LoopPresetStore(loop_preset_path(filepath), video_ref=filepath, size=size)


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
        :meth:`c64cast.midi_control.FeedbackMap.from_dict` then falls back to the
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
