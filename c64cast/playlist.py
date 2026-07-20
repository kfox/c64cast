"""Playlist state machine. Walks scenes, inserts an interstitial scene
between them, paces the main render loop to the target frame rate, prints
periodic health heartbeats, and tolerates per-scene crashes by advancing
the playlist.

The interstitial is built via an injected ``interstitial_factory(name) ->
Scene`` so callers can swap in custom designs (e.g. the colorful
InterstitialScene) or stub it out for tests."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .backend import C64Backend
from .profiler import FrameProfiler, NullProfiler
from .scenes import Scene
from .transport import LiveTuneTracker, TransportSession

if TYPE_CHECKING:
    from .config import SceneCfg
    from .ensemble import Ensemble
    from .modes import DisplayMode
    from .modulation import MusicModulation

InterstitialFactory = Callable[[str], Scene]
FollowerSceneFactory = Callable[["SceneCfg"], Scene]

# A deadline snap-forward dropping at least this many seconds of frames counts as
# a "large" playback disturbance (seek catch-up, stream rebuffer) worth telling the
# audio streamer about, so its adaptive NMI-rate loop re-arms its warm-up gate
# instead of chasing the abnormal bus load. Routine 1-3 frame drops stay below it.
_AUDIO_DISTURBANCE_DROP_S = 0.5


class Playlist:
    def __init__(
        self,
        scenes: list[Scene],
        api: C64Backend,
        target_fps: float,
        heartbeat_interval: float = 10.0,
        stop_event: threading.Event | None = None,
        interstitial_factory: InterstitialFactory | None = None,
        key_poller: Any = None,
        vision_controller: Any = None,
        profiler: FrameProfiler | NullProfiler | None = None,
        name: str = "system",
        loop: bool = True,
        fade_duration_s: float = 0.4,
        audio: Any = None,
        audio_calibration: dict[str, float] | None = None,
        menu_cfg: Any = None,
        config: Any = None,
        config_path: str | None = None,
        performance: Any = None,
    ) -> None:
        if not scenes:
            raise ValueError("Playlist needs at least one scene")
        # Per-instance logger so ensemble runs can tell which system a
        # given line came from. Child of the existing c64cast.playlist
        # logger, so assertLogs("c64cast.playlist", ...) in tests still
        # matches (per logging hierarchy: parent captures children).
        self.name = name
        self.log = logging.getLogger(f"c64cast.playlist.{name}")
        self.scenes = scenes
        # Single-scene mode: skip the interstitial cycle entirely, loop the
        # one scene via teardown+setup on is_done, and drop CTRL skip events
        # (there's nowhere to skip *to*). Auto-detected from the scene list.
        self.single_scene = len(scenes) == 1
        # Loop the playlist after the last scene finishes. False = exit
        # the streamer cleanly after one pass through `scenes` (or one
        # play of the single scene). Drives `_advance`'s end-of-list
        # branches: when False, the final teardown sets stop_event
        # instead of looping back / re-setting-up. See [playlist].loop in
        # config.py for the user-facing knob.
        self.loop = loop
        # Scene fade transitions. fade_duration_s <= 0 disables (hard cuts).
        # Fade-in overlaps the opening live frames (the display mode's
        # fade_alpha ramps 0→1 as frames render); fade-out freezes the last
        # composed frame and dims it to black before teardown on a NORMAL end.
        # A CTRL skip cancels both (see the skip branch in _run_one_frame and
        # the _ended_via_skip guard in _fade_out).
        self.fade_duration_s = fade_duration_s
        self._fade_in_remaining = 0
        self._fade_in_total = 0
        self._ended_via_skip = False
        self.api = api
        self.audio = audio  # Optional AudioStreamer for pitch retune
        # Optional {display_mode_name: playback-rate multiplier} for servo pitch.
        self.audio_calibration = audio_calibration
        self.default_target_fps = target_fps
        self.frame_time = 1.0 / target_fps
        self.heartbeat_interval = heartbeat_interval
        self.stop_event = stop_event or threading.Event()
        self.interstitial_factory = interstitial_factory or self._default_interstitial_factory()
        self.key_poller = key_poller
        # Optional vision controller — a second, camera-driven control surface
        # that sets the same pause/resume/skip/cycle events as the keyboard
        # poller (started/stopped alongside it). None unless [vision].enabled.
        self.vision_controller = vision_controller
        # Optional per-frame profiler. NullProfiler keeps the hot path
        # branch-free; cli.py also calls set_profiler() so sub-stages from
        # scenes._render_with_overlays land in the same instance.
        self.profiler: FrameProfiler | NullProfiler = profiler or NullProfiler()
        # Pause/resume events: poller sets pause_event on C= press; we set
        # resume_event after the held-key check, also via the poller.
        # Skip event: poller sets it on CTRL press (while running), or
        # the FastAPI control plane sets it on POST /skip. The run loop
        # forces is_done = True on the current scene when it fires.
        # Cycle event: poller sets it on SHIFT press (while running). The
        # run loop calls display_mode.cycle_style() on the current scene,
        # which lets the mode rotate through its visual styles. No-op for
        # modes that don't override cycle_style().
        self.pause_event = threading.Event()
        self.resume_event = threading.Event()
        self.skip_event = threading.Event()
        self.cycle_event = threading.Event()
        # Jump-to-index request (e.g. from midi_control.py). Reuses
        # skip_event to force the current scene done at the next clean
        # frame boundary; _advance() then consumes _jump_target instead of
        # advancing to index+1. Locked because it's written from whatever
        # thread requests the jump and read from the run-loop thread.
        # Last-write-wins: a burst of requests before the run loop drains
        # collapses to the final target, which is correct for a performer
        # mashing pads.
        self._jump_target: int | None = None
        self._jump_skip_interstitial = True
        self._jump_lock = threading.Lock()
        # On-C64 menu plumbing. menu_event is toggled by the poller on SPACE
        # (open when running / close when open); menu_active is held set by the
        # run loop while the MenuOverlay is injected, which flips the poller into
        # nav mode (pause/skip/cycle suspended, key edges pushed onto nav_queue).
        # menu_cfg/config/config_path drive the save-back flow. All optional:
        # menu is gated on [menu].enabled + a read-capable backend in cli.py.
        self.menu_event = threading.Event()
        self.menu_active = threading.Event()
        # Set while the current scene can host the menu; gates the poller's
        # access to the kernal keyboard buffer (it writes $00C6=0 to consume
        # keys, which must not disturb a kernal-input launcher's own watch).
        self.menu_eligible = threading.Event()
        self.nav_queue: deque[int] = deque(maxlen=8)
        self.menu_cfg = menu_cfg
        self.config = config
        self.config_path = config_path
        # WLED audio-sync broadcaster (bridge Mode 3): a process-wide UDP sender
        # that pulls the active scene's music features and multicasts them as
        # WLED Audio Sync packets so LAN LED matrices react to the SID. Built
        # only when [wled].broadcast is on; started/stopped around the run loop.
        self._wled: Any = None
        if config is not None:
            from .config import resolve_wled_broadcast

            broadcast_on, broadcast_host, broadcast_port = resolve_wled_broadcast(config)
            if broadcast_on:
                from .wled_sync import WledAudioSyncBroadcaster

                self._wled = WledAudioSyncBroadcaster(
                    self._active_features,
                    host=broadcast_host,
                    port=broadcast_port,
                    rate_hz=config.wled.rate_hz,
                )
        self._menu_overlay: Any = None
        # While the menu is open the background is frozen (not re-rendered every
        # frame) so the post-render panel can't flicker against a per-frame
        # scene redraw. This flag requests a one-shot re-render on open / nav /
        # value-change so the live preview still updates. See _service_menu +
        # the freeze gate in run().
        self._menu_repaint = False

        self.index = 0
        self.current: Scene | None = None
        # Persistent user brightness dim (WLED bridge Mode 1 `bri` slider),
        # 0 < user_dim <= 1.0. Owned here — not on the display mode — so it
        # survives scene auto-advance: `_safe_setup` re-stamps it onto each
        # fresh scene's display mode (the mode instance is per-scene). The
        # bridge sets both this and the current mode's `user_dim` for an
        # instant effect that also outlasts the current scene.
        self.user_dim: float = 1.0
        # Live-tune change log for the exit save-back flow (--overwrite / prompt).
        # The MIDI/WLED live-tune controls record each applied param change here;
        # cli.main reads it after teardown. Always present (cheap), so callers
        # needn't guard. See transport.LiveTuneTracker.
        self.live_tracker = LiveTuneTracker()
        # DJ-style transport control (seek/pause/loop) driven by
        # [midi_control]'s transport.* actions — see transport.TransportSession.
        # Always present (cheap; the queue just stays empty for a run with no
        # transport CC mappings), so callers needn't guard.
        self.transport = TransportSession()
        # Process-wide musical beat grid (Live-performance Phase 1). Built from
        # [performance] (or a 120-BPM 4/4 internal default), fed by the MIDI
        # control listener's reader thread (external clock) and tap-tempo pads.
        # Always present like `transport` so consumers (launch quantize,
        # tempo-locked effects — later phases) can read `pl.tempo` unguarded.
        # In-memory only — the reader thread never touches DMA to update it.
        from .tempo import build_tempo_clock

        self.tempo = build_tempo_clock(performance)
        self.transitioning = False
        self._last_heartbeat = 0.0
        self._last_stats = {"writes": 0, "skipped": 0, "errors": 0, "bytes": 0}
        # Reload event + pending replacement state. The CLI sets these on
        # SIGHUP; the run loop notices, finishes the current scene's frame,
        # then swaps in the new playlist at the next advance boundary.
        self.reload_event = threading.Event()
        self._pending_scenes: list[Scene] | None = None
        self._pending_interstitial: InterstitialFactory | None = None
        self._reload_lock = threading.Lock()
        # Ensemble + broadcast-interrupt plumbing. None in single-system
        # mode; wired up by cli.py after Ensemble construction in
        # multi-system mode. When `_broadcast_interrupt` fires the run
        # loop tears down the current scene, runs a follower scene
        # driven by `ensemble.active_orchestrator`, and resumes the
        # saved index. `build_follower_scene` builds the per-system
        # follower Scene from a SceneCfg (the playlist itself doesn't
        # know about audio/source/cfg; the factory closes over them).
        self.ensemble: Ensemble | None = None
        self._broadcast_interrupt: threading.Event | None = None
        self._broadcast_resume: threading.Event | None = None
        self.build_follower_scene: FollowerSceneFactory | None = None

    def request_reload(
        self, new_scenes: list[Scene], new_interstitial: InterstitialFactory | None = None
    ) -> None:
        """Queue a playlist swap. The run loop applies it at the next
        natural advance boundary (after the current scene finishes, or
        immediately if the current scene is an interstitial). Pass None
        for `new_interstitial` to keep the existing factory."""
        with self._reload_lock:
            self._pending_scenes = list(new_scenes)
            self._pending_interstitial = new_interstitial
            self.reload_event.set()

    def post_osd(self, text: str, duration_s: float = 2.5) -> None:
        """Show a brief on-screen message on the current scene (live-tune
        feedback). Routed to the scene's OsdState, which the render loop reads;
        a no-op when no scene is live or the scene's OSD is disabled. Called from
        the MIDI reader / WLED server threads — OsdState.post is thread-safe."""
        scene = self.current
        if scene is not None:
            scene.osd.post(text, duration_s)

    def cycle_osd(self, *, double_tap: bool) -> None:
        """The osd.position MIDI action (Phase 5). A normal tap toggles the
        current scene's OSD corner top/bottom (or re-enables it if it was
        hidden); a double_tap hides it. No-op when no scene is live. Called from
        the MIDI reader thread — OsdState attrs are simple thread-safe writes,
        same rationale as post_osd."""
        scene = self.current
        if scene is None:
            return
        osd = scene.osd
        if double_tap:
            osd.enabled = False
        elif not osd.enabled:
            osd.enabled = True
            osd.post(f"OSD {osd.position}")
        else:
            osd.position = "top" if osd.position == "bottom" else "bottom"
            osd.post(f"OSD {osd.position}")

    def request_jump(self, index: int, *, skip_interstitial: bool = True) -> None:
        """Cut to scenes[index] at the next clean frame boundary (reuses
        skip_event to force the current scene done — see _advance's
        end-of-scene branch, which consumes _jump_target in place of the
        usual index+1). No-op in single-scene mode (nowhere to jump to).

        skip_interstitial=True (the default, and what a live-performance
        control surface should always pass) bypasses the "UP NEXT" card
        entirely for a hard cut, still gated on the ensemble audio claim
        for the target scene like single-scene looping is. False routes
        through the normal transitioning/interstitial path instead, for
        callers that still want the card.

        Known limitation: a jump requested while self.current is None
        (the brief startup / finished-without-loop window) is dropped —
        re-send once the playlist has a current scene."""
        if self.single_scene:
            self.log.debug("jump to %d ignored — single-scene mode", index)
            return
        if not 0 <= index < len(self.scenes):
            raise ValueError(f"jump index {index} out of range (0..{len(self.scenes) - 1})")
        with self._jump_lock:
            self._jump_target = index
            self._jump_skip_interstitial = skip_interstitial
        self.skip_event.set()

    def _apply_reload(self) -> None:
        """Swap in the queued scenes + interstitial factory. The current
        scene is torn down so its overlays release threads/network state
        cleanly; the new scenes start from index 0."""
        with self._reload_lock:
            new_scenes = self._pending_scenes
            new_interstitial = self._pending_interstitial
            self._pending_scenes = None
            self._pending_interstitial = None
            self.reload_event.clear()
        if not new_scenes:
            return
        self.log.info("playlist: reloading (%d → %d scenes)", len(self.scenes), len(new_scenes))
        if self.current is not None:
            self._safe_teardown(self.current)
            self.current = None
        self.scenes = new_scenes
        self.single_scene = len(new_scenes) == 1
        if new_interstitial is not None:
            self.interstitial_factory = new_interstitial
        self.index = 0
        self.transitioning = False

    def _default_interstitial_factory(self) -> InterstitialFactory:
        # Late import so tests that supply their own factory don't have
        # to pull in backgrounds + InterstitialCfg.
        from .interstitial import InterstitialScene

        api = self.api
        return lambda name: InterstitialScene(api, name)

    def _frame_time_for(self, scene: Scene) -> float:
        """Resolve the scene's per-frame budget.

        Precedence:
          1. scene.target_fps (explicit, set in WaveformScene's __init__ etc.)
          2. display_mode.default_target_fps (None for bitmap → falls through)
          3. self.default_target_fps (the Playlist's system-level fps)

        Returns seconds per frame."""
        scene_fps: float | None = getattr(scene, "target_fps", None)
        if scene_fps is not None and scene_fps > 0:
            return 1.0 / float(scene_fps)
        dm = getattr(scene, "display_mode", None)
        mode_fps: float | None = getattr(dm, "default_target_fps", None) if dm else None
        if mode_fps is not None and mode_fps > 0:
            return 1.0 / float(mode_fps)
        return 1.0 / self.default_target_fps

    def _fade_frames(self, scene: Scene) -> int:
        """How many frames a fade spans for `scene` at its current frame rate.
        0 when fades are disabled (fade_duration_s <= 0)."""
        if self.fade_duration_s <= 0:
            return 0
        return max(1, round(self.fade_duration_s / self._frame_time_for(scene)))

    def _fade_mode(self, scene: Scene) -> DisplayMode | None:
        """The compose-based display mode the fade can drive, or None. Non-compose
        scenes (waveform/midi oscilloscope, native launcher) and scenes without a
        display mode are left untouched."""
        dm: DisplayMode | None = getattr(scene, "display_mode", None)
        if dm is not None and getattr(dm, "supports_compose", False):
            return dm
        return None

    def _begin_fade_in(self, scene: Scene) -> None:
        """Arm a fade-in for `scene`: start its display mode fully black and let
        _run_one_frame ramp fade_alpha 0→1 over the opening live frames. No-op
        (and clears any stale fade) when fades are off or unsupported."""
        self._ended_via_skip = False
        self._fade_in_remaining = 0
        dm = self._fade_mode(scene)
        if dm is None:
            return
        n = self._fade_frames(scene)
        if n <= 0:
            dm.fade_alpha = 1.0
            return
        dm.fade_alpha = 0.0
        self._fade_in_remaining = n
        self._fade_in_total = n

    def _advance_fade_in(self, scene: Scene) -> None:
        """Step the fade-in ramp one frame, before the scene composes. Called at
        the top of each rendered frame so the dimming overlaps live playback."""
        if self._fade_in_remaining <= 0:
            return
        dm = getattr(scene, "display_mode", None)
        if dm is None:
            self._fade_in_remaining = 0
            return
        done = self._fade_in_total - self._fade_in_remaining + 1
        dm.fade_alpha = min(1.0, done / self._fade_in_total)
        self._fade_in_remaining -= 1

    def _cancel_fade_in(self, scene: Scene) -> None:
        """Snap to full brightness and stop the fade-in ramp (CTRL skip)."""
        self._fade_in_remaining = 0
        dm = getattr(scene, "display_mode", None)
        if dm is not None:
            dm.fade_alpha = 1.0

    def _fade_out(self, scene: Scene) -> None:
        """Freeze the scene's last composed frame and dim it to black over the
        fade window, then leave the mode at full brightness for the next scene.
        Aborts immediately on a CTRL skip (consuming the event so it doesn't
        also skip the next scene) or a stop request. No-op when fades are off,
        the scene ended via skip, the mode can't compose, or nothing was
        rendered yet."""
        if self._ended_via_skip:
            return
        dm = self._fade_mode(scene)
        if dm is None or dm._last_buffers is None:
            return
        n = self._fade_frames(scene)
        if n <= 0:
            return
        frame_time = self._frame_time_for(scene)
        for i in range(1, n + 1):
            if self.stop_event.is_set():
                break
            if self.skip_event.is_set():
                self.skip_event.clear()  # satisfied by ending the fade early
                break
            try:
                dm.repush_faded(self.api, 1.0 - i / n)
            except Exception:
                self.log.exception("fade-out push failed on %r — ending fade", scene.name)
                break
            self.stop_event.wait(timeout=frame_time)
        dm.fade_alpha = 1.0

    def _wait_for_audio_claim(self, scene: Scene) -> bool:
        """If the playlist is part of an ensemble and `scene` actually
        contends for audio (`competes_for_audio_lock()`), block until we
        hold the ensemble's audio slot — or return False if stop_event
        fires first. Stamps the scene with `_audio_lock_held = True` on
        success so the matching `_safe_teardown` releases. Always
        returns True for non-ensemble runs or scenes that don't
        compete for audio (including a muted video).

        Used by single-scene mode (which can't skip itself, so the
        only sensible option is to wait). Multi-scene playlists use
        `_resolve_next_index` instead — that one skips past gated
        scenes to a runnable one before falling back to wait."""
        if self.ensemble is None or not scene.competes_for_audio_lock():
            return True
        poll_interval = 0.1
        first_wait = True
        while not self.stop_event.is_set():
            if self.ensemble.try_claim_audio(self.name):
                scene.__dict__["_audio_lock_held"] = True
                return True
            if first_wait:
                self.log.info(
                    "audio-bearing scene %r waiting — slot held by %s",
                    scene.name,
                    self.ensemble.audio_holder,
                )
                first_wait = False
            self.stop_event.wait(timeout=poll_interval)
        return False

    def _resolve_next_index(self) -> int | None:
        """Walk forward from self.index in ensemble mode to find the
        next scene we can actually run. Scenes that actually contend for
        audio (`competes_for_audio_lock()`) whose lock is held by another
        system are skipped; a muted video passes through like any
        non-audio scene. If every scene is gated,
        blocks (stop_event-aware) until the lock frees and a candidate
        becomes claimable. Returns the resolved index, or None only if
        stop_event fires while waiting.

        Side effect: on a successful audio-bearing claim, marks the
        chosen scene so its eventual `_safe_teardown` releases the slot.

        In single-system mode (ensemble is None) returns self.index
        directly — no gating possible."""
        if self.ensemble is None:
            return self.index
        n = len(self.scenes)
        poll_interval = 0.1
        first_full_wait = True
        while not self.stop_event.is_set():
            first_pass_log = first_full_wait
            for offset in range(n):
                idx = (self.index + offset) % n
                scene = self.scenes[idx]
                if not scene.competes_for_audio_lock():
                    return idx
                if self.ensemble.try_claim_audio(self.name):
                    scene.__dict__["_audio_lock_held"] = True
                    return idx
                if first_pass_log:
                    self.log.info(
                        "skipping audio-bearing %r — slot held by %s",
                        scene.name,
                        self.ensemble.audio_holder,
                    )
            if first_full_wait:
                self.log.info("all scenes audio-gated; waiting for ensemble audio slot to free")
                first_full_wait = False
            self.stop_event.wait(timeout=poll_interval)
        return None

    def _advance(self) -> None:
        if self.single_scene:
            if self.current is None:
                scene = self.scenes[0]
                if not self._wait_for_audio_claim(scene):
                    return
                self.current = scene
                self.log.info(
                    "scene %r (single-scene mode, %s)",
                    self.current.name,
                    "looping" if self.loop else "once-through",
                )
                self._safe_setup(self.current)
            elif self.current.is_done:
                if not self.loop:
                    self.log.info("scene %r finished and loop=False — stopping", self.current.name)
                    self._fade_out(self.current)
                    self._safe_teardown(self.current)
                    self.current = None
                    self.stop_event.set()
                    return
                # Loop the same scene back-to-back: teardown + setup. Works
                # for every scene type — webcam re-reads source, video
                # re-opens the file, waveform restarts the SID.
                scene = self.current
                self._fade_out(scene)
                self._safe_teardown(scene)
                if not self._wait_for_audio_claim(scene):
                    self.current = None
                    return
                self._safe_setup(scene)
                scene.is_done = False
            return
        if self.current is None:
            resolved = self._resolve_next_index()
            if resolved is None:
                return  # stop_event fired during the gate wait
            self.index = resolved
            self._enter_interstitial()
        elif self.transitioning and self.current.is_done:
            self._fade_out(self.current)
            self._safe_teardown(self.current)
            self.current = self.scenes[self.index]
            self.log.info("scene %d/%d → %r", self.index + 1, len(self.scenes), self.current.name)
            self._safe_setup(self.current)
            self.transitioning = False
        elif not self.transitioning and self.current.is_done:
            self._fade_out(self.current)
            self._safe_teardown(self.current)
            with self._jump_lock:
                jump_target = self._jump_target
                jump_skip_interstitial = self._jump_skip_interstitial
                self._jump_target = None
            if jump_target is not None:
                self.index = jump_target
                if jump_skip_interstitial:
                    scene = self.scenes[self.index]
                    if not self._wait_for_audio_claim(scene):
                        self.current = None
                        return
                    self.log.info(
                        "scene %d/%d → %r (jump)", self.index + 1, len(self.scenes), scene.name
                    )
                    self.current = scene
                    self._safe_setup(self.current)
                    self.transitioning = False
                    return
                resolved = self._resolve_next_index()
                if resolved is None:
                    self.current = None
                    return
                self.index = resolved
                self._enter_interstitial()
                return
            next_index = self.index + 1
            if next_index >= len(self.scenes):
                if not self.loop:
                    self.log.info("playlist finished and loop=False — stopping")
                    self.current = None
                    self.stop_event.set()
                    return
                next_index = 0
            self.index = next_index
            resolved = self._resolve_next_index()
            if resolved is None:
                self.current = None
                return
            self.index = resolved
            self._enter_interstitial()

    def _enter_interstitial(self) -> None:
        """Set up the interstitial "UP NEXT" card for the scene at
        `self.index` (which must already point at the resolved upcoming
        scene) and flip `transitioning` on. Shared by both interstitial-
        entry paths in `_advance` (first scene + scene-to-scene)."""
        nxt = self.scenes[self.index]
        # Let randomized scenes pick their file now so the "UP NEXT" card
        # shows the real upcoming content (not a directory spec / stale
        # prior pick). Must run before we read nxt.name.
        self._safe_prepare_next(nxt)
        self.log.info("interstitial → %r (scene %d/%d)", nxt.name, self.index + 1, len(self.scenes))
        self.current = self.interstitial_factory(nxt.name)
        self._safe_setup(self.current)
        self.transitioning = True

    def _maybe_install_conductor(self, scene: Scene) -> None:
        """If this scene's SceneCfg has `orchestrate = true` AND we're
        running in ensemble mode, resolve the right Orchestrator
        subclass, instantiate it, and stamp the scene so overlays can
        find it. The overlay (e.g. big_text) is what actually calls
        orch.begin() to fire the follower interrupts — we just put the
        orchestrator in place + set the ensemble's active slot."""
        if self.ensemble is None:
            return
        # Skip if the scene is already wired with an orchestrator —
        # _handle_broadcast_interrupt stamps follower scenes before
        # calling us, and we must not clobber the follower role with a
        # fresh conductor (especially when the follower's fallback cfg
        # IS the conductor's orchestrate=true cfg, which carries that
        # flag with it).
        if scene.__dict__.get("_orchestrator") is not None:
            return
        cfg = scene.__dict__.get("_cfg")
        if cfg is None or not getattr(cfg, "orchestrate", False):
            return
        try:
            from .orchestrator import resolve_orchestrator

            orch_cls = resolve_orchestrator(cfg)
        except Exception:
            self.log.exception(
                "orchestrate=true on scene %r: could not "
                "resolve orchestrator subclass; running "
                "scene as local-only",
                scene.name,
            )
            return
        orch = orch_cls(self.ensemble, self.name)
        self.ensemble.active_orchestrator = orch
        scene._orchestrator = orch
        scene._is_conductor = True
        scene._system_index = self.ensemble.system_names().index(self.name)

    def _safe_prepare_next(self, scene: Scene) -> None:
        """Invoke a scene's prepare_next() hook defensively. A failure here
        must not strand the transition — the scene's own setup() re-picks
        (and flips is_done on a hard failure), so we just log and fall
        through to the interstitial."""
        try:
            scene.prepare_next()
        except Exception:
            self.log.exception(
                "prepare_next failed on %r — interstitial will show a stale name", scene.name
            )

    def _safe_setup(self, scene: Scene) -> None:
        self._maybe_install_conductor(scene)
        scene.setup()
        # Re-stamp the persistent user brightness onto this fresh scene's display
        # mode (mode instances are per-scene, so a dim set on a previous scene's
        # mode wouldn't otherwise carry). No-op at the 1.0 default.
        if self.user_dim < 1.0:
            dm = getattr(scene, "display_mode", None)
            if dm is not None:
                dm.user_dim = self.user_dim
        # Arm the fade-in: the display mode starts black and ramps up over the
        # opening live frames (driven by _advance_fade_in in _run_one_frame).
        self._begin_fade_in(scene)
        # Adjust NMI latch for the new display mode (if audio is active and has
        # a calibration table). This restores pitch under the host-DMA servo by
        # boosting the NMI consumer rate back toward 8000 Hz after being throttled
        # by video DMA bus-halts.
        if (
            self.audio is not None
            and self.audio_calibration is not None
            and hasattr(scene, "display_mode")
            and scene.display_mode is not None
        ):
            mode_name = getattr(scene.display_mode, "name", None)
            if mode_name:
                self.audio.set_nmi_latch_for_mode(mode_name, self.audio_calibration)
        for ov in getattr(scene, "overlays", ()):
            try:
                ov.setup(self.api, scene)
            except Exception:
                # Overlay failure mustn't strand the scene — log and drop
                # the offending overlay from the run list for this scene.
                self.log.exception("overlay %r setup failed on %r — disabling", ov.name, scene.name)
                ov._disabled = True  # checked in process_frame loop
        self._log_scene_recording_metadata(scene)

    def _log_scene_recording_metadata(self, scene: Scene) -> None:
        """Log a SCENE_CONFIG_JSON snapshot of this scene's coalesced
        settings, once per activation — the source for
        scripts/scene_config_to_description.py. No-op without a Config
        (self.config is only unset in tests that build a Playlist directly)."""
        if self.config is None:
            return
        from .recording_metadata import log_scene_recording_metadata

        log_scene_recording_metadata(scene, self.config, self.name)

    def _safe_teardown(self, scene: Scene) -> None:
        for ov in getattr(scene, "overlays", ()):
            if getattr(ov, "_disabled", False):
                continue
            try:
                ov.teardown(self.api, scene)
            except Exception:
                self.log.exception("overlay %r teardown failed", ov.name)
        try:
            scene.teardown()
        except Exception:
            self.log.exception("teardown of %r failed", scene.name)
        # Clear the ensemble's active-orchestrator slot if this teardown
        # was for a conductor scene. Big_text.teardown already calls
        # orch.end() defensively; here we release the slot so the next
        # orchestrate=true scene can install a fresh orchestrator. Also
        # clear the per-scene conductor stamps: the same Scene instance
        # is reused across loop iterations and a stale _orchestrator
        # would make _maybe_install_conductor short-circuit on the next
        # setup, leaving ensemble.active_orchestrator unset — followers
        # would then drop the broadcast interrupt as "no active orch".
        if self.ensemble is not None and scene.__dict__.get("_is_conductor", False):
            self.ensemble.active_orchestrator = None
            scene.__dict__["_orchestrator"] = None
            scene.__dict__["_is_conductor"] = False
        # Release the ensemble audio lock if this scene held it. Runs
        # even when teardown raised — a crashing VideoScene must
        # not strand the slot. The flag is reset on the scene so a
        # subsequent re-setup (single-scene loop) re-resolves the claim
        # rather than thinking it still holds the previous one.
        if self.ensemble is not None and scene.__dict__.get("_audio_lock_held", False):
            self.ensemble.release_audio(self.name)
            scene.__dict__["_audio_lock_held"] = False

    def _maybe_heartbeat(self, now: float) -> None:
        if self.heartbeat_interval <= 0:
            return
        if now - self._last_heartbeat < self.heartbeat_interval:
            return
        # First call just establishes the baseline; don't emit a 0-second window.
        if self._last_heartbeat == 0.0:
            self._last_heartbeat = now
            self._last_stats = self.api.stats
            return
        s = self.api.stats
        dt = max(now - self._last_heartbeat, 1e-6)
        d_w = s["writes"] - self._last_stats["writes"]
        d_e = s["errors"] - self._last_stats["errors"]
        d_sk = s["skipped"] - self._last_stats["skipped"]
        d_by = s["bytes"] - self._last_stats["bytes"]
        name = self.current.name if self.current else "(none)"
        msg = (
            f"[{name}] writes={d_w / dt:.0f}/s errors={d_e / dt:.2f}/s "
            f"skipped={d_sk / dt:.0f}/s "
            f"bytes={d_by / dt / 1024.0:.0f}KiB/s"
        )
        # Promote to WARNING when errors are actually flowing — without this
        # the user wouldn't see issues unless they ran with -v.
        if d_e / dt > 1.0:
            self.log.warning(msg)
        else:
            self.log.info(msg)
        self._last_stats = s
        self._last_heartbeat = now

    def _idle_pace(self, scene: Scene, next_deadline: float) -> float:
        """Pace one frame WITHOUT rendering — holds the current (frozen) frame
        while the menu is open and idle. Single-buffer VIC RAM retains the last
        scene+panel, so skipping the re-render keeps the panel rock-steady;
        events (nav keys, close, pause/skip) are still serviced each loop. The
        deadline advances by one frame_time so cadence resumes cleanly when the
        menu closes or an interaction forces a re-render."""
        frame_time = self._frame_time_for(scene)
        now = time.time()
        if now < next_deadline:
            self.stop_event.wait(timeout=next_deadline - now)
        return max(next_deadline + frame_time, time.time())

    def _run_one_frame(self, scene: Scene, next_deadline: float) -> float:
        """Render one frame of `scene`. Returns the new next_deadline.

        Extracted from the inner loop of run() so the broadcast-interrupt
        path can drive a follower scene through the same render +
        overlay + heartbeat + frame-drop machinery without duplicating
        any of it. Skip/cycle events are honored against this scene
        (consistent with `self.current` being the active scene)."""
        frame_time = self._frame_time_for(scene)
        with self.profiler.frame(scene.name):
            t0 = time.time()
            # Sleep until the deadline if we're early. Slow DMA pushes
            # mean t0 can be later than expected; natural pacing absorbs
            # that, and the catch-up below absorbs the rest.
            if t0 < next_deadline:
                with self.profiler.stage("wait"):
                    self.stop_event.wait(timeout=next_deadline - t0)
                t0 = time.time()

            stats_before = self.api.stats

            # Step the fade-in ramp before the scene composes, so the opening
            # frames render progressively brighter (overlapping live playback).
            self._advance_fade_in(scene)
            # Apply any queued MIDI transport events (seek/pause/loop/rw/ff/jog)
            # against this scene before it renders, so a seek issued this tick
            # is reflected in the frame we're about to compose.
            self.transport.tick(self, t0)

            with self.profiler.stage("cpu_render"):
                try:
                    still_active = scene.process_frame(t0)
                except Exception:
                    self.log.exception("scene %r raised; advancing", scene.name)
                    still_active = False
                # Run process_frame for overlays that still write directly
                # to the U64. Overlays with PAINTS_INTO_BUFFERS were
                # already composed into the scene's screen+color buffers
                # during scene.process_frame — calling process_frame
                # again would race the scene write.
                for ov in getattr(scene, "overlays", ()):
                    if getattr(ov, "_disabled", False):
                        continue
                    if getattr(ov, "PAINTS_INTO_BUFFERS", False):
                        continue
                    try:
                        ov.process_frame(self.api, scene, t0)
                    except Exception:
                        self.log.exception(
                            "overlay %r raised on %r — disabling", ov.name, scene.name
                        )
                        ov._disabled = True

            stats_after = self.api.stats
            self.profiler.record_counts(
                writes=stats_after["writes"] - stats_before["writes"],
                bytes_=stats_after["bytes"] - stats_before["bytes"],
            )

            scene.is_done = not still_active
            # Defer auto-advance while any overlay reports busy (e.g.
            # BigText with an unfinished scroll-off). CTRL skip below
            # still wins — it forces is_done = True regardless.
            if scene.is_done and any(
                not getattr(ov, "_disabled", False) and ov.is_busy()
                for ov in getattr(scene, "overlays", ())
            ):
                scene.is_done = False
            # Skip request (CTRL key from poller, or POST /skip from
            # the control plane): force is_done so the next iteration
            # advances. Race-free because we apply *after* the
            # is_done = not still_active assignment.
            if self.skip_event.is_set():
                if self.single_scene:
                    self.log.debug("skip ignored — single-scene mode")
                else:
                    self.log.info("skip requested — advancing past %r", scene.name)
                    scene.is_done = True
                    # A skip means "get to the next scene now" — abort any
                    # in-progress fade-in and suppress the fade-out.
                    self._cancel_fade_in(scene)
                    self._ended_via_skip = True
                self.skip_event.clear()

            # Cycle request (SHIFT key, or future POST /cycle):
            # rotate the current scene's display style. Ignored
            # during an interstitial transition — cycling the
            # interstitial mid-flight would be confusing and the
            # interstitial doesn't implement cycle_style anyway.
            if self.cycle_event.is_set():
                if not self.transitioning:
                    self._handle_cycle()
                self.cycle_event.clear()

            self._maybe_heartbeat(t0)
            if self.profiler.emit_if_due(t0, self.log):
                # Same cadence as the profiler — surfaces U64
                # per-DMA-write latency so we can tell whether
                # cpu_render is really CPU work or producer blocked
                # on the network.
                latency_line = self.api.format_write_latency()
                if latency_line is not None:
                    self.log.info(latency_line)

        next_deadline += frame_time
        # If we fell more than 2 frames behind, snap the deadline
        # forward (drop frames) so we don't burst to catch up.
        now = time.time()
        if now > next_deadline + 2 * frame_time:
            dropped = int((now - next_deadline) / frame_time)
            if dropped > 0:
                next_deadline += dropped * frame_time
                self.log.debug(
                    "[%s] dropped %d frame(s); behind by %.0fms",
                    scene.name,
                    dropped,
                    (now - next_deadline + frame_time) * 1000,
                )
                # A large snap (seek catch-up / stream rebuffer) abnormally loads
                # the bus; tell the audio loop to hold its NMI rate steady through
                # it instead of chasing the transient and gliding the pitch.
                if self.audio is not None and dropped * frame_time >= _AUDIO_DISTURBANCE_DROP_S:
                    self.audio.note_playback_disturbance()
        return next_deadline

    def _handle_broadcast_interrupt(self) -> None:
        """Save current scene state, swap in a follower scene driven by
        the ensemble's active orchestrator, run frames until the
        orchestrator releases us, then restore the saved scene index.

        Called from the run loop when `_broadcast_interrupt` is set
        (only happens in ensemble mode where the orchestrator wired the
        events). The actual orchestrator subclass + its protocol live
        in c64cast/orchestrator.py + subclasses."""
        assert self._broadcast_interrupt is not None
        assert self._broadcast_resume is not None
        self._broadcast_interrupt.clear()
        if self.ensemble is None or self.ensemble.active_orchestrator is None:
            # Stale event (orchestrator ended between set and our
            # observation). Drop the interrupt and let the run loop
            # continue normally.
            return
        if self.build_follower_scene is None:
            self.log.error(
                "broadcast interrupt arrived but no follower scene factory wired; ignoring"
            )
            return
        orch = self.ensemble.active_orchestrator

        # Force-resume if paused. The pause_event was set by the keyboard
        # poller; we clear it + set resume_event so any concurrent
        # _handle_pause loop exits cleanly. Per the design, paused
        # systems get woken by a broadcast and are left un-paused after
        # (matches user expectation: emergency broadcast overrides pause).
        if self.pause_event.is_set():
            self.log.info("broadcast: force-resuming paused playlist")
            self.pause_event.clear()
            self.resume_event.set()

        # Save scene index; tear down the current scene cleanly so its
        # overlays release threads/network state. The follower scene
        # runs in its place until the orchestrator releases us.
        saved_idx = self.index
        if self.current is not None:
            self._safe_teardown(self.current)
            self.current = None

        follower_cfg = orch.follower_scene_cfg_for(self.name)
        try:
            follower_scene = self.build_follower_scene(follower_cfg)
        except Exception:
            self.log.exception("broadcast: follower scene build failed; skipping interrupt")
            return
        # Stamp orchestrator + role + this system's index in the
        # ensemble (left-to-right) onto the scene so overlays that
        # participate in the broadcast (e.g. big_text) can find them in
        # their setup(). Followers are not conductors; the index is
        # used by span-mode orchestrators to compute each follower's
        # slice of the global content.
        follower_scene._orchestrator = orch
        follower_scene._is_conductor = False
        follower_scene._system_index = self.ensemble.system_names().index(self.name)
        self._safe_setup(follower_scene)
        self.current = follower_scene

        self.log.info("broadcast: follower scene %r running until resume", follower_scene.name)

        # Spin frames until the orchestrator releases us or stop fires.
        next_deadline = time.time()
        while not self._broadcast_resume.is_set() and not self.stop_event.is_set():
            next_deadline = self._run_one_frame(follower_scene, next_deadline)
        self._broadcast_resume.clear()

        self.log.info(
            "broadcast: resume — tearing down follower, restoring scene index %d", saved_idx
        )
        self._safe_teardown(follower_scene)
        self.current = None
        # Defensive: _advance() reads self.index on the next iteration
        # and re-sets-up the scene at that index from scratch. We didn't
        # touch self.index during the broadcast, but pin it anyway in
        # case some future code path mutates it mid-flight.
        self.index = saved_idx

    def _active_features(self) -> MusicModulation | None:
        """The currently-playing scene's live music features (None when there's
        no scene, or the scene has no music source). The WLED broadcaster polls
        this from its own thread — Scene.features() reads are self-synchronized
        (SID scenes take their own lock), so no extra locking is needed here."""
        scene = self.current
        return scene.features() if scene is not None else None

    def run(self) -> None:
        self._last_heartbeat = 0.0
        self.log.info(
            "playlist: starting (%d scene(s), default %.1f fps, heartbeat %.0fs)",
            len(self.scenes),
            self.default_target_fps,
            self.heartbeat_interval,
        )
        menu_enabled = self.menu_cfg is not None and getattr(self.menu_cfg, "enabled", False)
        for controller in (self.key_poller, self.vision_controller):
            if controller is not None:
                controller.start(
                    self.pause_event,
                    self.resume_event,
                    skip_event=self.skip_event,
                    cycle_event=self.cycle_event,
                    # Only wire the menu (and the extra buffer read) when enabled.
                    menu_event=self.menu_event if menu_enabled else None,
                    menu_active=self.menu_active if menu_enabled else None,
                    menu_eligible=self.menu_eligible if menu_enabled else None,
                    nav_queue=self.nav_queue if menu_enabled else None,
                )
        # Deadline-based pacing: after each frame we advance the deadline by
        # one frame_time. If real wall clock has fallen far behind the
        # deadline, jump it forward (effectively dropping the missed frames)
        # so animations stay tied to wall-clock time instead of compounding lag.
        if self._wled is not None:
            self._wled.start()
        next_deadline = time.time()
        try:
            while not self.stop_event.is_set():
                if self.pause_event.is_set():
                    self._handle_pause()
                    next_deadline = time.time()
                    if self.stop_event.is_set():
                        break
                if self.reload_event.is_set():
                    self._apply_reload()
                    next_deadline = time.time()
                if self._broadcast_interrupt is not None and self._broadcast_interrupt.is_set():
                    self._handle_broadcast_interrupt()
                    next_deadline = time.time()
                    if self.stop_event.is_set():
                        break

                try:
                    self._advance()
                except Exception:
                    self.log.exception("playlist advance failed; aborting")
                    break
                # loop=False end-of-playlist: _advance has torn down the
                # last scene, set self.current = None, and set stop_event.
                # Skip the render and let the while-loop condition exit.
                if self.current is None:
                    break

                self._service_menu()
                # Freeze the background while the menu is open and idle: holding
                # the last frame stops the post-render panel from flickering
                # against a scene that redraws the whole frame every tick. A
                # menu interaction (open / nav / value change) sets
                # _menu_repaint, so the live preview still re-renders on demand.
                if self.menu_active.is_set() and not self._menu_repaint:
                    next_deadline = self._idle_pace(self.current, next_deadline)
                    continue
                self._menu_repaint = False
                next_deadline = self._run_one_frame(self.current, next_deadline)
        except KeyboardInterrupt:
            self.log.info("interrupted")
        finally:
            if self._wled is not None:
                self._wled.stop()
            for controller in (self.key_poller, self.vision_controller):
                if controller is not None:
                    controller.stop()
            if self.current is not None:
                self._safe_teardown(self.current)

    def _service_menu(self) -> None:
        """Open/close the on-C64 menu on SPACE (menu_event) and forward nav
        keys to an open menu. Called each loop iteration before the frame
        renders, so a value change previews on the same frame."""
        scene = self.current
        if scene is None:
            self.menu_eligible.clear()
            return
        if self.menu_cfg is None or not getattr(self.menu_cfg, "enabled", False):
            return
        from .overlays.menu import can_show_menu

        # Publish eligibility to the poller every frame: only an eligible scene
        # lets it drain/clear the keyboard buffer (so SPACE-to-open is inert,
        # and $00C6 untouched, on launcher/waveform/midi scenes).
        if can_show_menu(scene):
            self.menu_eligible.set()
        else:
            self.menu_eligible.clear()
        # Defensive: if the scene changed out from under an open menu (reload,
        # broadcast), drop the menu state cleanly.
        if self._menu_overlay is not None and self._menu_overlay not in getattr(
            scene, "overlays", ()
        ):
            self._menu_overlay = None
            self.menu_active.clear()
        if self.menu_event.is_set():
            self.menu_event.clear()
            if self._menu_overlay is None:
                self._open_menu()
            elif self._menu_overlay.on_toggle():
                self._close_menu()
            self._menu_repaint = True  # open / close / confirm changed the view
        if self._menu_overlay is not None:
            while self.nav_queue:
                try:
                    code = self.nav_queue.popleft()
                except IndexError:
                    break
                self._menu_overlay.on_key(code)
                self._menu_repaint = True  # nav / value change → preview update
            if self._menu_overlay.closed:
                self._close_menu()
                self._menu_repaint = True

    def _menu_can_save(self) -> bool:
        """Save-back is available only when we know the source TOML path and
        have the in-memory Config (single-system or a per-system ensemble
        config; the serializer rejects an ensemble master)."""
        return self.config is not None and bool(self.config_path)

    def _open_menu(self) -> None:
        from .overlays.menu import MenuOverlay, can_show_menu

        scene = self.current
        if scene is None or not can_show_menu(scene):
            self.log.info("menu: not available for this scene")
            return
        overlay = MenuOverlay(
            scene,
            self.api,
            can_save=self._menu_can_save(),
            prompt_to_save=bool(getattr(self.menu_cfg, "prompt_to_save", True)),
            save_fn=self._save_config,
            logger=self.log,
        )
        scene.overlays = list(getattr(scene, "overlays", [])) + [overlay]
        self._menu_overlay = overlay
        self.menu_active.set()
        self.nav_queue.clear()  # drop any keys queued before the menu opened
        self.api.invalidate_cache()  # full repaint so the panel composites cleanly
        self.log.info("menu: opened (%d options)", len(overlay.items))

    def _close_menu(self) -> None:
        scene = self.current
        if scene is not None and self._menu_overlay is not None:
            with contextlib.suppress(ValueError, AttributeError):
                scene.overlays.remove(self._menu_overlay)
        self._menu_overlay = None
        self.menu_active.clear()
        # Reclaim the panel cells: the scene's delta cache is unaware the menu
        # overwrote them, so force a full repaint on the next frame.
        self.api.invalidate_cache()
        self.log.info("menu: closed")

    def _save_config(self) -> bool:
        """Write the (menu-mutated) Config back to its source path, keeping a
        .bak of the original. Returns True on success."""
        import os
        import shutil

        from . import config_serialize

        if self.config is None or not self.config_path:
            return False
        try:
            if os.path.exists(self.config_path):
                shutil.copy2(self.config_path, self.config_path + ".bak")
            config_serialize.dump(self.config, self.config_path)
            self.log.info("menu: saved config → %s (backup .bak)", self.config_path)
            return True
        except Exception:
            self.log.exception("menu: failed to save config")
            return False

    def _handle_cycle(self) -> None:
        """Broadcast a style cycle to the current scene, its display mode,
        and every overlay attached to it.

        Three opt-in surfaces respond to SHIFT:
          * scene.cycle_style(api) — for scenes without a display_mode that
            still want their own SHIFT behavior (e.g. WaveformScene cycles
            the SID subtune).
          * scene.display_mode.cycle_style(api) — the usual path used by
            PETSCII style packs and the MCM/MHires palette modes.
          * each overlay.cycle_style(api, scene).

        Default cycle_style implementations return None, so opt-in
        modes/overlays are the only ones that actually rotate; the rest
        just ignore the request. Failures are logged but don't tear down
        the scene — a broken style cycle is way better than killing the
        playlist mid-stream."""
        if self.current is None:
            return
        labels: list[str] = []
        scene_cycle = getattr(self.current, "cycle_style", None)
        if callable(scene_cycle):
            try:
                new_style = scene_cycle(self.api)
            except Exception:
                self.log.exception(
                    "cycle_style failed on scene %r — leaving as-is", self.current.name
                )
                new_style = None
            if new_style is not None:
                labels.append(f"scene={new_style}")
        dm = getattr(self.current, "display_mode", None)
        if dm is not None:
            try:
                new_style = dm.cycle_style(self.api)
            except Exception:
                self.log.exception(
                    "cycle_style failed on %r display mode — leaving style as-is", self.current.name
                )
                new_style = None
            if new_style is not None:
                labels.append(f"display={new_style}")
        for ov in getattr(self.current, "overlays", ()):
            if getattr(ov, "_disabled", False):
                continue
            try:
                ov_style = ov.cycle_style(self.api, self.current)
            except Exception:
                self.log.exception(
                    "cycle_style failed on overlay %r — leaving style as-is",
                    getattr(ov, "name", ov),
                )
                continue
            if ov_style is not None:
                labels.append(f"{ov.name}={ov_style}")
        if labels:
            self.log.info("cycle: %r → %s", self.current.name, ", ".join(labels))
        else:
            self.log.debug("cycle ignored: %r has no cyclable styles", self.current.name)

    def _handle_pause(self) -> None:
        """Tear down the current scene, idle the machine, and wait until either
        the resume signal fires (C= held N seconds) or stop fires.

        We do NOT advance self.index — the same scene picks back up after
        the next `_advance()` call when we leave this method."""
        self.log.info("paused — hold Commodore key to resume")
        if self.current is not None:
            self._safe_teardown(self.current)
            self.current = None
        # Clear any stale resume signal BEFORE idling. The poller can set
        # resume_event the moment it sees a 3 s C= hold — which, if pause_idle
        # is slow (it brings $028D live mid-call on some backends), can land
        # *during* pause_idle. Clearing afterwards would then wipe a legitimate
        # resume and strand the pause; clearing first lets that detection stick.
        self.resume_event.clear()
        try:
            # pause_idle() leaves the machine in its paused state with the
            # kernal keyboard scan still alive so $028D keeps updating for the
            # resume-hold detection. Backend-specific: the Ultimate resets to
            # the BASIC READY banner; the TeensyROM clears the screen but keeps
            # the display ON (a bare reset lands at the TR menu, freezing $028D;
            # blanking the display would remove the VIC badlines the TR's
            # cycle-clean DMA needs, hanging the resume reads).
            self.api.pause_idle()
        except Exception:
            self.log.exception("pause_idle failed")

        # Spin on stop_event.wait so SIGTERM can shortcut the pause.
        while not self.stop_event.is_set() and not self.resume_event.is_set():
            self.stop_event.wait(timeout=0.1)
        if self.stop_event.is_set():
            return

        self.log.info("resuming — reset + run clear loop")
        try:
            self.api.reset()
            time.sleep(1)
            self.api.run_basic_clear_loop()
            self.api.disable_case_switch()
        except Exception:
            self.log.exception("reset/clear during resume failed")
        # The same scene gets re-set-up by _advance() on the next loop pass.
        self.pause_event.clear()
        self.resume_event.clear()
