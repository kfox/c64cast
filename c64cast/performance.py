"""Clip-launch grid — the "video sampler" core of the Live DJ/VJ arc (Phase 2;
see docs/architecture/control.md → "Live performance").

`PerformanceSession` turns a bank of ``[[performance.clips]]`` slots into
pad-fired scenes, quantized to the process-wide :class:`~c64cast.tempo.TempoClock`
beat grid (Phase 1). It is constructed one-per-:class:`~c64cast.playlist.Playlist`
(mirrors :class:`~c64cast.transport.TransportSession` / ``pl.tempo``), and — like
the transport session — **all scene mutation happens on the playlist thread**:
the MIDI reader thread only ever :meth:`enqueue`\\s a :class:`ClipEvent`; the
playlist thread drains it in :meth:`service`, which is called at the top of
``Playlist._advance`` once per frame.

Launch lifecycle (the thing that makes scene-*type* changes viable as live hits,
which the plain ``midi_control`` surface deliberately excludes):

1. **Arm** — a pad press arms the slot. The scene is *built* on a background
   thread (``pl.build_performance_scene`` → ``config.build_scene``), so the
   network/decoder setup cost is hidden under the bar count-in. A second press
   supersedes a pending arm.
2. **Quantized swap** — once the build is ready *and* the next ``quantize``
   boundary (``off`` = immediately, ``beat``/``bar`` = the grid) arrives, the
   armed scene is swapped in via ``Playlist._perf_swap_scene`` (the single-scene
   hot-swap generalized from ``_apply_reload``: ``_safe_teardown`` the current
   scene, ``_safe_setup`` the armed one). When the clock isn't running the
   quantize is treated as ``off`` so a pad always fires.
3. **Launch semantics** — ``trigger`` plays through / loops; ``gate`` plays while
   the pad is held and restores the prior program on release; ``toggle`` latches
   on/off. A finished clip either re-setups (``loop``) or restores the program it
   replaced (a one-level return target: the playlist scene it interrupted, or the
   clip that was running under it).

There is **no on-screen feedback** here (the C64 output is audience-facing):
count-in / armed / active state is surfaced to controller LEDs (Phase 4) and the
web console (Phase 5), never ``post_osd`` to the screen.

Kept import-light (stdlib only; ``Playlist``/``Scene`` under ``TYPE_CHECKING``,
``build_performance_scene`` injected by cli.py) so playlist.py can pull it in
without a cycle — the same rule transport.py follows.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .playlist import Playlist
    from .scenes import Scene

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClipEvent:
    """One pad press/release for a clip slot, queued by the MIDI reader thread
    (:mod:`midi_control`'s ``clip_launch`` action) and drained on the playlist
    thread by :meth:`PerformanceSession.service`. ``pressed`` is False for a
    note release — the signal ``gate`` (return on release) and ``toggle`` (latch
    off) read, exactly like the transport module's held-note actions."""

    slot: int
    pressed: bool = True


@dataclass(frozen=True)
class LookEvent:
    """A "look" snapshot/recall pad press (Live DJ/VJ Phase 6), queued by a
    control thread (:mod:`midi_control`'s ``look_save``/``look_recall`` actions,
    or the web console) and drained on the playlist thread by
    :meth:`PerformanceSession.service`. A *look* is the current active clip plus
    the on-screen scene's effect-chain state (per-layer bypass + params +
    choices); ``save`` captures it to slot ``slot``, else recalls it."""

    slot: int
    save: bool = False


def _snapshot_effects(scene: Any) -> list[dict[str, Any]]:
    """Capture the effect-chain state of ``scene`` — one dict per layer with its
    name, bypass (``enabled``), ``mod_source``, LIVE_PARAMS values, and any
    LIVE_CHOICES selection. Pure reads (GIL-atomic attribute/param reads), so
    it's safe on the playlist thread. Empty list when the scene has no chain."""
    effects = getattr(scene, "effects", None) or []
    out: list[dict[str, Any]] = []
    for eff in effects:
        cls = type(eff)
        params = {
            k: float(getattr(eff, k))
            for k in getattr(cls, "LIVE_PARAMS", {})
            if isinstance(getattr(eff, k, None), (int, float))
        }
        choices: dict[str, Any] = {}
        get_choice = getattr(eff, "get_live_choice", None)
        if get_choice is not None:
            for k in getattr(cls, "LIVE_CHOICES", {}):
                v = get_choice(k)
                if v is not None:
                    choices[k] = v
        out.append(
            {
                "name": getattr(eff, "name", "?"),
                "enabled": bool(getattr(eff, "enabled", True)),
                "mod_source": getattr(eff, "mod_source", "audio"),
                "params": params,
                "choices": choices,
            }
        )
    return out


def _apply_effects_state(scene: Any, states: list[dict[str, Any]]) -> None:
    """Re-apply a captured effect-chain state (from :func:`_snapshot_effects`) to
    ``scene``'s chain, matching by layer index (extra states past the chain
    length are ignored). Only touches per-layer live knobs — ``enabled``,
    ``mod_source``, declared LIVE_PARAMS (clamped to range) and LIVE_CHOICES — so
    it never rebuilds the chain; a look recalled onto a scene with a different
    chain simply skips the layers/params it doesn't have. All GIL-atomic writes
    the render loop reads next frame; runs on the playlist thread."""
    effects = getattr(scene, "effects", None) or []
    for idx, st in enumerate(states):
        if idx >= len(effects) or not isinstance(st, dict):
            continue
        eff = effects[idx]
        if "enabled" in st:
            eff.enabled = bool(st["enabled"])
        ms = st.get("mod_source")
        if isinstance(ms, str):
            eff.mod_source = ms
        live_params: dict[str, tuple[float, float]] = getattr(type(eff), "LIVE_PARAMS", {}) or {}
        for k, v in (st.get("params") or {}).items():
            if k in live_params and isinstance(v, (int, float)) and not isinstance(v, bool):
                lo, hi = live_params[k]
                setattr(eff, k, max(lo, min(hi, float(v))))
        set_choice = getattr(eff, "set_live_choice", None)
        if set_choice is not None:
            for k, v in (st.get("choices") or {}).items():
                try:
                    set_choice(None, k, v)
                except Exception:  # noqa: BLE001 — a bad stored choice must not abort recall
                    log.debug("performance: look choice %r=%r rejected", k, v, exc_info=True)


def _slugify_name(name: str) -> str:
    """Filesystem-safe slug for a system name (the look-preset filename)."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return slug or "c64cast"


class LookStore:
    """Persist performance "looks" to a JSON file, one map per system.

    A look is ``{"clip": <slot|null>, "effects": [<layer state>, ...]}`` keyed by
    a 1-based pad slot. Mirrors :class:`~c64cast.wled_device.PresetStore`'s
    tolerant-load / atomic-write contract exactly: a missing or corrupt file
    reads as an empty map, and writes go through ``transport.atomic_write_text``
    (temp file + ``os.replace``) so a crash mid-write can't leave a half-written
    map. The path is injectable so tests point it at a tempdir."""

    #: Look slots are 1-based pad ids (slot 0 is never stored).
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
        out: dict[str, dict[str, Any]] = {}
        for k, v in data.items():
            if isinstance(v, dict) and str(k).isdigit() and int(k) != 0:
                out[str(int(k))] = v
        return out

    def get(self, slot: int) -> dict[str, Any] | None:
        return self.load().get(str(int(slot)))

    def save(self, slot: int, look: Mapping[str, Any]) -> None:
        if not self.SLOT_MIN <= slot <= self.SLOT_MAX:
            return
        data = self.load()
        data[str(slot)] = dict(look)
        self._write(data)

    def delete(self, slot: int) -> None:
        if slot == 0:
            return
        data = self.load()
        if data.pop(str(slot), None) is not None:
            self._write(data)

    def _write(self, data: Mapping[str, Any]) -> None:
        from .transport import atomic_write_text

        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path, json.dumps(data, indent=2, sort_keys=True))


def default_look_store(name: str) -> LookStore:
    """The :class:`LookStore` for a system, under ``paths.presets_dir()`` (one
    ``looks-<slug>.json`` per system name, alongside the WLED presets). Resolved
    at call time so it honors ``$C64CAST_DATA_DIR`` like every other data file."""
    from . import paths

    return LookStore(paths.presets_dir() / f"looks-{_slugify_name(name)}.json")


# A "program" to return to when an overlaid clip ends: either a declared playlist
# scene (by index — its Scene object is reused, so restoring is a cheap re-setup)
# or a prior clip (by its config dict — rebuilt via the factory). Kept to a
# single level (a restored clip returns to the playlist base) so gate/toggle over
# a running loop returns sensibly without an unbounded rebuild chain.
@dataclass
class _Return:
    kind: str  # "playlist" | "clip"
    index: int = 0  # for "playlist"
    clip: dict[str, Any] | None = None  # for "clip"


@dataclass
class _Armed:
    """A slot armed and building on a background thread, awaiting its quantize
    boundary. ``scene`` is set by the build thread once construction finishes;
    ``built`` gates the read on the playlist thread (a simple ref hand-off, so an
    Event is all the synchronization needed)."""

    slot: int
    clip: dict[str, Any]
    launch: str
    quantize: str
    loop: bool
    arm_beat: float
    arm_bar: float
    built: threading.Event = field(default_factory=threading.Event)
    scene: Scene | None = None
    error: bool = False


@dataclass
class _Active:
    """The clip currently on screen (performance owns ``pl.current``)."""

    slot: int
    clip: dict[str, Any]
    scene: Scene
    launch: str
    loop: bool

    @property
    def momentary(self) -> bool:
        return self.launch == "gate"


class PerformanceSession:
    """Owns the clip-launch grid for one playlist. See the module docstring.

    Construct with the ``[[performance.clips]]`` list (raw dicts, already
    validated by :func:`config._validate_clips`). :meth:`enqueue` is called from
    the MIDI reader thread; :meth:`service` from the playlist thread only. Both
    are cheap in-memory work — the only network/DMA touch is inside the
    playlist-thread ``_safe_setup``/``_safe_teardown`` a swap triggers."""

    def __init__(
        self, clips: list[dict[str, Any]] | None = None, look_store: LookStore | None = None
    ) -> None:
        import queue

        self._queue: queue.SimpleQueue[ClipEvent] = queue.SimpleQueue()
        # Look snapshot/recall pad presses (Live DJ/VJ Phase 6) — a separate
        # queue so a save/recall doesn't interleave with the clip-event stream;
        # both drain on the playlist thread in `service`.
        self._look_queue: queue.SimpleQueue[LookEvent] = queue.SimpleQueue()
        self._look_store = look_store
        # Effect state a pending look recall will apply once its clip activates
        # (set by a recall that also launches a clip; consumed in `_activate`).
        self._pending_look_effects: list[dict[str, Any]] | None = None
        # slot -> clip dict, in declaration order.
        self._clips: dict[int, dict[str, Any]] = {}
        for clip in clips or []:
            slot = clip.get("slot")
            if isinstance(slot, int) and not isinstance(slot, bool):
                self._clips[slot] = dict(clip)
        self._armed: _Armed | None = None
        self._active: _Active | None = None
        self._return: _Return | None = None
        # Playlist index performance first took over from — the ultimate fallback
        # a restored-clip chain collapses back to.
        self._base_index: int = 0

    # ---- introspection (LED / web feedback, later phases) ------------------
    @property
    def has_clips(self) -> bool:
        return bool(self._clips)

    @property
    def active_slot(self) -> int | None:
        return self._active.slot if self._active is not None else None

    @property
    def armed_slot(self) -> int | None:
        return self._armed.slot if self._armed is not None else None

    @property
    def armed_detail(self) -> tuple[int, str, float, float] | None:
        """``(slot, quantize, arm_beat, arm_bar)`` for the pending arm, or None —
        the web console (Phase 5) reads it to render a count-in (beats remaining
        to the quantize boundary, computed against ``pl.tempo``). A GIL-atomic
        snapshot of the immutable fields of the ``_Armed`` record; ``None`` once
        the swap lands or the arm is cancelled."""
        armed = self._armed
        if armed is None:
            return None
        return (armed.slot, armed.quantize, armed.arm_beat, armed.arm_bar)

    def clips_info(self) -> list[dict[str, Any]]:
        """The configured grid as a list of ``{slot, name, type, pad, pad_type,
        launch, quantize, loop}`` dicts in declaration order — the data the web
        console (Phase 5) and any other feedback surface renders the pad grid
        from. Read-only view of the validated clip specs; ``name`` falls back to
        ``"clip <slot>"`` when the spec declares none."""
        out: list[dict[str, Any]] = []
        for slot, clip in self._clips.items():
            out.append(
                {
                    "slot": slot,
                    "name": str(clip.get("name") or f"clip {slot}"),
                    "type": clip.get("type"),
                    "pad": clip.get("pad"),
                    "pad_type": clip.get("pad_type", "note"),
                    "launch": clip.get("launch", "trigger"),
                    "quantize": clip.get("quantize", "bar"),
                    "loop": bool(clip.get("loop", True)),
                }
            )
        return out

    def clip_pad_mappings(self) -> list[tuple[str, int, int]]:
        """``(pad_type, pad_number, slot)`` for every clip that declares a
        ``pad`` — the auto-mappings :mod:`midi_control` folds into its cc_map so a
        clip fires from its own note/PC with no separate mapping entry."""
        out: list[tuple[str, int, int]] = []
        for slot, clip in self._clips.items():
            pad = clip.get("pad")
            if isinstance(pad, int) and not isinstance(pad, bool):
                out.append((clip.get("pad_type", "note"), pad, slot))
        return out

    @property
    def has_looks(self) -> bool:
        """Whether a look store is wired (looks can be saved/recalled)."""
        return self._look_store is not None

    def saved_look_slots(self) -> list[int]:
        """Sorted slot ids that currently hold a saved look (for LED/web
        feedback). Reads the store from disk; empty when no store is wired."""
        if self._look_store is None:
            return []
        return sorted(int(k) for k in self._look_store.load())

    # ---- MIDI reader thread ------------------------------------------------
    def enqueue(self, event: ClipEvent) -> None:
        self._queue.put(event)

    def enqueue_look(self, slot: int, *, save: bool) -> None:
        """Queue a look save (``save=True``) or recall for pad ``slot`` — drained
        on the playlist thread in :meth:`service`, so a control thread never
        reads/writes a scene or the store directly. A no-op stream when no look
        store is wired (the events drain to nothing)."""
        self._look_queue.put(LookEvent(slot=slot, save=save))

    def advance_clip(self) -> int | None:
        """Enqueue a launch of the clip slot *after* the currently active/armed
        one, cycling back to the first — the "next clip in a row" gesture the
        vision controller fires (Live DJ/VJ Phase 6). Returns the slot enqueued
        (or None when the grid is empty). Slots are taken in declaration order;
        with nothing playing yet it fires the first slot. Enqueue-only (a
        :class:`ClipEvent` drained on the playlist thread), so it's safe to call
        from any control thread, exactly like :meth:`enqueue`."""
        slots = list(self._clips.keys())
        if not slots:
            return None
        current = self.active_slot if self.active_slot is not None else self.armed_slot
        if current is None or current not in slots:
            nxt = slots[0]
        else:
            nxt = slots[(slots.index(current) + 1) % len(slots)]
        self._queue.put(ClipEvent(slot=nxt, pressed=True))
        return nxt

    # ---- playlist thread ---------------------------------------------------
    def service(self, pl: Playlist) -> bool:
        """Drain queued pad events, progress any armed build, swap on the grid
        boundary, and manage the active clip's loop/end. Returns True iff
        performance currently owns ``pl.current`` (an active clip) — the caller
        (``Playlist._advance``) then skips the normal playlist advance for this
        frame. Never raises: a bug here must not abort the playlist loop."""
        try:
            self._reconcile(pl)
            self._drain(pl)
            self._drain_looks(pl)
            self._progress_armed(pl)
            self._progress_active(pl)
        except Exception:
            log.exception("performance: service failed")
        return self._active is not None

    def _reconcile(self, pl: Playlist) -> None:
        """Relinquish ownership if the active clip's scene was torn down or
        replaced out from under us — pause (`_handle_pause`), reload
        (`_apply_reload`), and the broadcast interrupt all tear down
        `pl.current` without going through the engine. Dropping `_active` (the
        scene is already gone) hands control back to the normal `_advance`, which
        re-enters the playlist; a pending arm is abandoned too (its captured
        return index is now stale)."""
        if self._active is not None and pl.current is not self._active.scene:
            self._active = None
            self._return = None
            self._armed = None
            self._pending_look_effects = None

    def _drain(self, pl: Playlist) -> None:
        import queue

        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(pl, event)

    def _handle_event(self, pl: Playlist, event: ClipEvent) -> None:
        if event.pressed:
            clip = self._clips.get(event.slot)
            if clip is None:
                log.debug("performance: no clip in slot %d", event.slot)
                return
            launch = clip.get("launch", "trigger")
            # A toggle press onto the already-latched slot turns it off.
            if (
                launch == "toggle"
                and self._active is not None
                and self._active.slot == event.slot
                and self._active.launch == "toggle"
            ):
                self._end_active(pl)
                return
            self._arm(pl, event.slot, clip)
        else:
            # Release: end a held gate clip, or cancel a gate arm the performer
            # let go of before it launched. trigger/toggle ignore releases.
            if (
                self._active is not None
                and self._active.slot == event.slot
                and self._active.momentary
            ):
                self._end_active(pl)
            elif (
                self._armed is not None
                and self._armed.slot == event.slot
                and self._armed.launch == "gate"
            ):
                self._cancel_arm()

    def _drain_looks(self, pl: Playlist) -> None:
        import queue

        while True:
            try:
                event = self._look_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_look(pl, event)

    def _handle_look(self, pl: Playlist, event: LookEvent) -> None:
        if self._look_store is None:
            return
        if event.save:
            self._snapshot_look(pl, event.slot)
        else:
            self._recall_look(pl, event.slot)

    def _snapshot_look(self, pl: Playlist, slot: int) -> None:
        """Capture the active clip + the on-screen scene's effect chain to a look
        slot. Runs on the playlist thread, so the scene reads are consistent."""
        assert self._look_store is not None
        look = {
            "clip": self.active_slot,
            "effects": _snapshot_effects(pl.current),
        }
        self._look_store.save(slot, look)
        log.info(
            "performance: saved look %d (clip %s, %d fx layer(s))",
            slot,
            look["clip"],
            len(look["effects"]),
        )

    def _recall_look(self, pl: Playlist, slot: int) -> None:
        """Recall a saved look: (re)launch its captured clip if it's still a valid
        slot — applying the captured effect state once the clip activates — else
        apply the effect state to the current scene straight away."""
        assert self._look_store is not None
        look = self._look_store.get(slot)
        if not look:
            log.debug("performance: recall of empty look slot %d", slot)
            return
        effects_state = look.get("effects") or []
        clip_slot = look.get("clip")
        if (
            isinstance(clip_slot, int)
            and not isinstance(clip_slot, bool)
            and clip_slot in self._clips
        ):
            # Arm the captured clip; stash the effect state to apply after the
            # swap lands (set AFTER _arm, which clears any prior pending look).
            self._arm(pl, clip_slot, self._clips[clip_slot])
            self._pending_look_effects = list(effects_state)
            log.info("performance: recalled look %d → clip %d", slot, clip_slot)
        else:
            _apply_effects_state(pl.current, effects_state)
            log.info("performance: recalled look %d → effects on current scene", slot)

    def _arm(self, pl: Playlist, slot: int, clip: dict[str, Any]) -> None:
        """Arm a slot: capture the quantize anchor and kick off the background
        build. Supersedes any pending arm (last press wins, like a performer
        re-choosing before the boundary)."""
        if pl.build_performance_scene is None:
            log.warning("performance: clip slot %d armed but no build factory wired", slot)
            return
        self._cancel_arm()
        armed = _Armed(
            slot=slot,
            clip=clip,
            launch=clip.get("launch", "trigger"),
            quantize=clip.get("quantize", "bar"),
            loop=bool(clip.get("loop", True)),
            arm_beat=pl.tempo.beat_phase,
            arm_bar=pl.tempo.bar_phase,
        )
        self._armed = armed
        factory = pl.build_performance_scene

        def _build() -> None:
            try:
                scene = factory(clip)
            except Exception:
                log.exception("performance: clip slot %d build failed", slot)
                armed.error = True
            else:
                armed.scene = scene
            finally:
                armed.built.set()

        threading.Thread(target=_build, name=f"clip-build-{slot}", daemon=True).start()

    def _cancel_arm(self) -> None:
        """Drop the pending arm. A scene already built but never swapped in is
        best-effort torn down (it was constructed but never ``setup``, so this is
        just to release any file/decoder handle the constructor opened)."""
        armed = self._armed
        self._armed = None
        # Any look-recall effect state was tied to this arm; drop it so an
        # unrelated later launch can't inherit it.
        self._pending_look_effects = None
        if armed is None:
            return
        if armed.built.is_set() and armed.scene is not None:
            try:
                armed.scene.teardown()
            except Exception:
                log.debug("performance: discarded clip teardown failed", exc_info=True)

    def _progress_armed(self, pl: Playlist) -> None:
        armed = self._armed
        if armed is None or not armed.built.is_set():
            return
        if armed.error:
            self._armed = None
            self._pending_look_effects = None
            return
        if not self._boundary_reached(pl, armed):
            return
        self._armed = None
        self._activate(pl, armed)

    def _boundary_reached(self, pl: Playlist, armed: _Armed) -> bool:
        """Has the armed clip's quantize boundary arrived? ``off`` (or a stopped
        clock, so a pad still fires) launches at once; ``beat``/``bar`` wait for
        the accumulator to cross into the next whole beat/bar past the arm."""
        if armed.quantize == "off" or not pl.tempo.running:
            return True
        if armed.quantize == "beat":
            return math.floor(pl.tempo.beat_phase) > math.floor(armed.arm_beat)
        return math.floor(pl.tempo.bar_phase) > math.floor(armed.arm_bar)

    def _activate(self, pl: Playlist, armed: _Armed) -> None:
        """Swap the armed clip in as the current program, remembering what it
        interrupted so gate/toggle/one-shot can return there."""
        assert armed.scene is not None
        if self._active is None:
            self._base_index = pl.index
            self._return = _Return(kind="playlist", index=pl.index)
        else:
            self._return = _Return(kind="clip", clip=self._active.clip)
        if not pl._perf_swap_scene(armed.scene):
            # Swap aborted (stop fired / audio claim lost) — drop ownership.
            self._active = None
            self._return = None
            return
        self._active = _Active(
            slot=armed.slot,
            clip=armed.clip,
            scene=armed.scene,
            launch=armed.launch,
            loop=armed.loop,
        )
        # A look recall that launched this clip stashed its effect state; apply it
        # now the scene is set up (its chain exists), then clear.
        if self._pending_look_effects is not None:
            _apply_effects_state(armed.scene, self._pending_look_effects)
            self._pending_look_effects = None
        log.info(
            "performance: launched clip slot %d (%s, %s)",
            armed.slot,
            armed.launch,
            "loop" if armed.loop else "one-shot",
        )

    def _progress_active(self, pl: Playlist) -> None:
        active = self._active
        if active is None:
            return
        # Only react once the scene actually reports done. Loop = re-setup the
        # same Scene object (works for every type: video re-opens the file,
        # waveform restarts the SID); one-shot = restore the interrupted program.
        if not active.scene.is_done:
            return
        if active.loop:
            pl._perf_swap_scene(active.scene)
        else:
            self._end_active(pl)

    def _end_active(self, pl: Playlist) -> None:
        """Tear down the active clip and restore the program it interrupted."""
        active = self._active
        ret = self._return
        self._active = None
        self._return = None
        if active is None:
            return
        if ret is not None and ret.kind == "clip" and ret.clip is not None:
            self._restore_clip(pl, ret.clip)
            return
        index = ret.index if ret is not None else self._base_index
        self._restore_playlist(pl, index)

    def _restore_playlist(self, pl: Playlist, index: int) -> None:
        """Return to a declared playlist scene (its Scene object is reused, so
        this is a cheap re-setup, not a rebuild). Dropping ``_active`` first hands
        ownership back to the normal ``_advance`` state machine."""
        if not pl.scenes:
            return
        index = index % len(pl.scenes)
        pl.index = index
        pl.transitioning = False
        pl._perf_swap_scene(pl.scenes[index])

    def _restore_clip(self, pl: Playlist, clip: dict[str, Any]) -> None:
        """Return to a prior clip by rebuilding it (a brief gap — the deferred
        pre-warmed-decks enhancement is what removes it). Its own return target
        collapses to the playlist base, bounding the chain to one level."""
        if pl.build_performance_scene is None:
            self._restore_playlist(pl, self._base_index)
            return
        try:
            scene = pl.build_performance_scene(clip)
        except Exception:
            log.exception("performance: prior-clip rebuild failed; returning to playlist")
            self._restore_playlist(pl, self._base_index)
            return
        if not pl._perf_swap_scene(scene):
            return
        self._active = _Active(
            slot=clip.get("slot", 0),
            clip=clip,
            scene=scene,
            launch=clip.get("launch", "trigger"),
            loop=bool(clip.get("loop", True)),
        )
        self._return = _Return(kind="playlist", index=self._base_index)
