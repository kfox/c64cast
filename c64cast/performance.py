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

import logging
import math
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

    def __init__(self, clips: list[dict[str, Any]] | None = None) -> None:
        import queue

        self._queue: queue.SimpleQueue[ClipEvent] = queue.SimpleQueue()
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

    # ---- MIDI reader thread ------------------------------------------------
    def enqueue(self, event: ClipEvent) -> None:
        self._queue.put(event)

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
