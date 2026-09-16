"""Playlist state machine. Walks scenes, inserts an interstitial scene
between them, paces the main render loop to the target frame rate, prints
periodic health heartbeats, and tolerates per-scene crashes by advancing
the playlist.

The interstitial is built via an injected ``interstitial_factory(name) ->
Scene`` so callers can swap in custom designs (e.g. the colorful
InterstitialScene) or stub it out for tests."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from c64cast.control.transport import LiveTuneTracker, TransportSession
from c64cast.hw.backend import C64Backend
from c64cast.scenes.scenes import Scene

from .playlist_support import EnsembleCoordinator, PlaylistMenu, SceneFades
from .profiler import FrameProfiler, NullProfiler

if TYPE_CHECKING:
    from c64cast.scenes.modulation import MusicModulation

    from .config import SceneCfg
    from .ensemble import Ensemble

InterstitialFactory = Callable[[str], Scene]
FollowerSceneFactory = Callable[["SceneCfg"], Scene]

# A deadline snap-forward dropping at least this many seconds of frames is a
# "large" disturbance (seek catch-up, stream rebuffer) the audio streamer is
# told about, so its adaptive NMI-rate loop re-arms its warm-up gate instead of
# chasing the abnormal bus load. Routine 1-3 frame drops stay below it.
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
        # Per-instance so an ensemble run attributes each line to a system; a
        # child of `c64cast.app.playlist`, so `assertLogs` on the parent still
        # captures it.
        self.name = name
        self.log = logging.getLogger(f"c64cast.app.playlist.{name}")
        self.scenes = scenes
        # Single-scene mode skips the interstitial cycle, loops the one scene by
        # teardown+setup on is_done, and drops CTRL skips — there is nowhere to
        # skip to.
        self.single_scene = len(scenes) == 1
        # False exits the streamer cleanly after one pass: `_advance`'s end-of-list
        # branch sets stop_event instead of looping back.
        self.loop = loop
        # fade_duration_s <= 0 disables (hard cuts). Fade-in overlaps the opening
        # live frames as the display mode's `fade_alpha` ramps 0→1; fade-out
        # freezes the last composed frame and dims it before teardown on a NORMAL
        # end. A CTRL skip cancels both.
        self.fades = SceneFades(self, duration_s=fade_duration_s)
        self.api = api
        self.audio = audio  # Optional AudioStreamer for pitch retune
        # {display_mode_name: playback-rate multiplier} for servo pitch.
        self.audio_calibration = audio_calibration
        self.default_target_fps = target_fps
        self.frame_time = 1.0 / target_fps
        self.heartbeat_interval = heartbeat_interval
        self.stop_event = stop_event or threading.Event()
        self.interstitial_factory = interstitial_factory or self._default_interstitial_factory()
        self.key_poller = key_poller
        # A second, camera-driven control surface setting the same
        # pause/resume/skip/cycle events as the keyboard poller, started and
        # stopped alongside it. None unless [vision].enabled.
        self.vision_controller = vision_controller
        # NullProfiler keeps the hot path branch-free. `cli.py` also calls
        # `set_profiler()`, so `scenes._render_with_overlays`' sub-stages land in
        # this same instance.
        self.profiler: FrameProfiler | NullProfiler = profiler or NullProfiler()
        # The poller sets `pause_event` on C=, `resume_event` after the held-key
        # check, `skip_event` on CTRL (as does the control plane's POST /skip) and
        # `cycle_event` on SHIFT. The run loop forces `is_done` on a skip and calls
        # `display_mode.cycle_style()` on a cycle.
        self.pause_event = threading.Event()
        self.resume_event = threading.Event()
        self.skip_event = threading.Event()
        self.cycle_event = threading.Event()
        # A jump reuses `skip_event` to force the current scene done at the next
        # clean frame boundary; `_advance()` then consumes `_jump_target` instead
        # of advancing. Locked because the requesting thread is not the run loop.
        # Last-write-wins, which is what a performer mashing pads wants.
        self._jump_target: int | None = None
        self._jump_skip_interstitial = True
        self._jump_lock = threading.Lock()
        # The poller toggles `menu_event` on SPACE; the run loop holds
        # `menu_active` set while the MenuOverlay is injected, which flips the
        # poller into nav mode (pause/skip/cycle suspended, key edges onto
        # `nav_queue`). Gated on [menu].enabled plus a read-capable backend.
        self.menu_event = threading.Event()
        self.menu_active = threading.Event()
        # Gates the poller's access to the kernal keyboard buffer: it writes
        # $00C6=0 to consume keys, which must not disturb a kernal-input
        # launcher's own watch.
        self.menu_eligible = threading.Event()
        self.nav_queue: deque[int] = deque(maxlen=8)
        self.menu_cfg = menu_cfg
        self.config = config
        self.config_path = config_path
        # A process-wide UDP sender that multicasts the active scene's music
        # features as WLED Audio Sync packets. Built only when [wled].broadcast is
        # on; started and stopped around the run loop.
        self._wled: Any = None
        # With [wled].broadcast_tempo_fallback on, a scene with no SID features
        # feeds the broadcaster off the beat grid instead. Read once here;
        # consulted in `_active_features`.
        self._wled_tempo_fallback: bool = bool(
            getattr(getattr(config, "wled", None), "broadcast_tempo_fallback", False)
        )
        if config is not None:
            from .scene_factory import resolve_wled_broadcast

            broadcast_on, broadcast_host, broadcast_port = resolve_wled_broadcast(config)
            if broadcast_on:
                from c64cast.wled.wled_sync import WledAudioSyncBroadcaster

                self._wled = WledAudioSyncBroadcaster(
                    self._active_features,
                    host=broadcast_host,
                    port=broadcast_port,
                    rate_hz=config.wled.rate_hz,
                )
        self.menu = PlaylistMenu(self)

        self.index = 0
        self.current: Scene | None = None
        # The WLED `bri` slider, 0 < user_dim <= 1.0. Owned here rather than on
        # the display mode, whose instance is per-scene, so it survives an
        # auto-advance: `safe_setup` re-stamps it onto each fresh scene.
        self.user_dim: float = 1.0
        # The C64 output is in front of an audience, so nothing may draw an OSD
        # line over it. Owned here for the same reason `user_dim` is — an
        # `OsdState` is per-scene — and re-stamped by `safe_setup`.
        #
        # This is the *runtime* control; `[midi_control].osd = "off"` is the static
        # one, applied per scene by `scene_factory.build_scene`. Config sets the
        # baseline, performance mode forces silence regardless, and turning
        # performance mode off restores the config's answer rather than "on".
        self.performance_mode: bool = False
        # The live-tune controls record each applied param change here and
        # `cli.main` reads it after teardown. `config` lets it route a
        # [color]-homed knob into a scene's own [scenes.color] block when that
        # scene overrides the field.
        self.live_tracker = LiveTuneTracker(config)
        # DJ-style transport control (seek/pause/loop) driven by
        # [midi_control]'s transport.* actions. Always present, so callers need no
        # guard; the queue just stays empty with no transport CC mappings.
        self.transport = TransportSession()
        # Process-wide musical beat grid, from [performance] or a 120-BPM 4/4
        # internal default, fed by the MIDI listener's reader thread and tap-tempo
        # pads. Always present, like `transport`. In-memory only — that reader
        # thread never touches DMA to update it.
        from c64cast.control.tempo import ClockModulationSource, build_tempo_clock

        self.tempo = build_tempo_clock(performance)
        # With tempo_source = "audio", each frame forwards the current scene's
        # analyzer BPM into `self.tempo`, so the detected beat drives the grid.
        self._tempo_audio_drive: bool = (
            performance is not None and getattr(performance, "tempo_source", "") == "audio"
        )
        # Lock-transition state, so the run loop can announce a locked or lost
        # beat without a per-frame record.
        self._tempo_audio_locked = False
        self._tempo_audio_log_t = 0.0
        # Wraps `self.tempo` as a MusicModulation source, so an effect layer with
        # `mod_source = "clock"` locks to the tempo grid the way an audio-reactive
        # layer locks to the SID feature stream. Stamped onto each scene in
        # `safe_setup`; read in `scenes._render_with_overlays`.
        self._clock_modulation = ClockModulationSource(self.tempo)
        # Fires scenes from pads quantized to `tempo`, empty when
        # [[performance.clips]] is. All scene mutation happens on the playlist
        # thread inside `service` (called from `_advance`), never on the MIDI
        # reader thread. `build_performance_scene` is the injected factory that
        # turns a clip dict into a Scene — None until wired, leaving the grid inert.
        from c64cast.control.performance import PerformanceSession, default_look_store

        self.performance = PerformanceSession(
            getattr(performance, "clips", None) if performance is not None else None,
            look_store=default_look_store(name),
        )
        self.build_performance_scene: Callable[[dict[str, Any]], Scene] | None = None
        self.transitioning = False
        self._last_heartbeat = 0.0
        self._last_stats = {"writes": 0, "skipped": 0, "errors": 0, "bytes": 0}
        # Set on SIGHUP; the run loop finishes the current frame, then swaps in
        # the new playlist at the next advance boundary.
        self.reload_event = threading.Event()
        self._pending_scenes: list[Scene] | None = None
        self._pending_interstitial: InterstitialFactory | None = None
        self._reload_lock = threading.Lock()
        # None in single-system mode. When `_broadcast_interrupt` fires, the run
        # loop tears down the current scene, runs a follower scene driven by
        # `ensemble.active_orchestrator`, and resumes the saved index.
        # `build_follower_scene` closes over the audio/source/cfg the playlist
        # itself cannot reach.
        self.ensemble: Ensemble | None = None
        self.ensemble_coord = EnsembleCoordinator(self)
        self.broadcast_interrupt: threading.Event | None = None
        self.broadcast_resume: threading.Event | None = None
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

    def bind_ensemble(
        self,
        ensemble: Ensemble,
        *,
        interrupt: threading.Event,
        resume: threading.Event,
        build_follower_scene: FollowerSceneFactory,
    ) -> None:
        """Wire this playlist into a multi-system ensemble: the shared
        Ensemble, its per-system broadcast interrupt/resume events, and the
        follower-scene factory (which closes over the stack's
        api/audio/source/cfg — the playlist can't build follower scenes
        itself). Called once by cli after Ensemble construction; see
        EnsembleCoordinator for what consumes each piece."""
        self.ensemble = ensemble
        self.broadcast_interrupt = interrupt
        self.broadcast_resume = resume
        self.build_follower_scene = build_follower_scene

    def post_osd(self, text: str, duration_s: float = 2.5) -> None:
        """Show a brief on-screen message on the current scene (live-tune
        feedback). Routed to the scene's OsdState, which the render loop reads;
        a no-op when no scene is live or the scene's OSD is disabled. Called from
        the MIDI reader / WLED server threads — OsdState.post is thread-safe."""
        scene = self.current
        if scene is not None:
            scene.osd.post(text, duration_s)

    def set_performance_mode(self, on: bool) -> bool:
        """Turn performance mode on or off, returning the new state.

        Performance mode means the C64 output is in front of an audience, so
        no OSD line may draw over it — every poster (live-tune, effect bypass,
        the transport engine's own `PAUSED`/`SEEK`/`LOOP`) goes silent through
        the one `OsdState.suppressed` gate. Off again restores whatever
        `[midi_control].osd` asked for, because `enabled` was never touched.

        **Turning it off posts nothing.** A `PERF OFF` flash would be the one
        thing this change set took off the audience screen everywhere else:
        confirmation that a control was pressed, rather than state the picture
        is in. The console already shows the mode — `performance_mode` rides
        every pushed state frame — and the next real poster is itself the
        proof the OSD is back.

        Applied to the current scene *and* stored on the playlist, mirroring
        how the WLED bridge sets `user_dim`: the write gives an instant
        effect, the field makes it outlast this scene (`safe_setup`
        re-stamps). This is the one place the mode is set — the `osd.position`
        pad's hide comes through here too (see :meth:`cycle_osd`) rather than
        keeping a second, scene-local hide of its own.

        Safe from any control thread — a bool write the render loop reads next
        frame, same rationale as post_osd."""
        self.performance_mode = on
        scene = self.current
        if scene is not None:
            scene.osd.suppressed = on
        self.log.info("performance mode: %s", "on (audience screen clean)" if on else "off")
        return on

    def cycle_osd(self, *, double_tap: bool) -> None:
        """The osd.position MIDI action (Phase 5). A normal tap toggles the
        current scene's OSD corner top/bottom, or brings the OSD back if it is
        hidden however it got that way; a double_tap hides it. No-op when no
        scene is live. Called from the MIDI reader thread — OsdState attrs are
        simple thread-safe writes, same rationale as post_osd.

        **The hide is performance mode**, not a second mechanism. It used to
        write `enabled`, which reached only the live scene and was lost on the
        next auto-advance — so a pad the performer hit to clear the audience
        screen quietly un-hid itself one scene later. Two controls hiding the
        same thing to two different depths is also the kind of split that
        drifts: a fix to one would keep missing the other.

        Re-pointing it at `suppressed` alone would have cost the pad something
        it has always been able to do — bring up an OSD that
        `[midi_control].osd = "off"` had disabled. The re-show branch asks
        `osd.visible` rather than either gate, and opens whichever one is
        shut, so nothing is lost: a config-disabled OSD still comes up, and a
        run-level hide is lifted at the same time (which is also how the
        console's PERF button gets turned off from a pad).

        The post here is the *pad's* feedback — it names the corner, which is
        only meaningful to whoever just pressed it — and is why
        :meth:`set_performance_mode` posting nothing is not contradicted."""
        scene = self.current
        if scene is None:
            return
        osd = scene.osd
        if double_tap:
            self.set_performance_mode(True)
            return
        if not osd.visible:
            if self.performance_mode:
                self.set_performance_mode(False)
            osd.suppressed = False
            osd.enabled = True
            osd.post(f"OSD {osd.position}")
            return
        osd.position = "top" if osd.position == "bottom" else "bottom"
        osd.post(f"OSD {osd.position}")

    def toggle_effect_layer(self, slot: int) -> bool | None:
        """Flip the bypass (`enabled`) of effect-chain layer `slot` on the current
        scene, returning the new state (or None for an out-of-range slot / a scene
        with no chain). A GIL-atomic bool write the render loop reads next frame
        (Live DJ/VJ Phase 3), so it's safe to call from any control thread — the
        MIDI reader (`fx_toggle`), the web console, and the vision controller
        (Phase 6) all land here. No OSD (the C64 stays audience-facing)."""
        scene = self.current
        if scene is None:
            return None
        effects = getattr(scene, "effects", None)
        if not effects or not 0 <= slot < len(effects):
            return None
        eff = effects[slot]
        new_state = not getattr(eff, "enabled", True)
        eff.enabled = new_state
        return new_state

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
            self.safe_teardown(self.current)
            self.current = None
        self.scenes = new_scenes
        self.single_scene = len(new_scenes) == 1
        if new_interstitial is not None:
            self.interstitial_factory = new_interstitial
        self.index = 0
        self.transitioning = False

    def perf_swap_scene(self, new_scene: Scene) -> bool:
        """Single-scene hot-swap for the clip-launch engine (Phase 2): tear down
        the current scene and set up `new_scene` in its place, returning True on
        success. Generalizes `_apply_reload`'s teardown+setup seam to one scene
        with no scene-list/index mutation. Honors the ensemble audio claim like
        single-scene looping does (a no-op returning True in single-system mode);
        a lost claim / stop leaves `current` torn down and returns False. Runs on
        the playlist thread only (from PerformanceSession.service)."""
        if self.current is not None:
            self.safe_teardown(self.current)
            self.current = None
        if not self.ensemble_coord.wait_for_audio_claim(new_scene):
            return False
        self.safe_setup(new_scene)
        self.current = new_scene
        return True

    def _default_interstitial_factory(self) -> InterstitialFactory:
        # Late, so a test supplying its own factory need not pull in backgrounds.
        from c64cast.scenes.interstitial import InterstitialScene

        api = self.api
        return lambda name: InterstitialScene(api, name)

    def frame_time_for(self, scene: Scene) -> float:
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

    def _advance(self) -> None:
        # First refusal each frame: it drains pad events, services background clip
        # builds, performs the quantized swap, and manages clip looping. A True
        # means an active clip owns the program and the playlist advance is
        # suspended for this iteration.
        if self.performance.service(self):
            return
        if self.single_scene:
            self._advance_single_scene()
            return
        if self.current is None:
            resolved = self.ensemble_coord.resolve_next_index()
            if resolved is None:
                return  # stop_event fired during the gate wait
            self.index = resolved
            self._enter_interstitial()
        elif self.transitioning and self.current.is_done:
            self.fades.fade_out(self.current)
            self.safe_teardown(self.current)
            self.current = self.scenes[self.index]
            self.log.info("scene %d/%d → %r", self.index + 1, len(self.scenes), self.current.name)
            self.safe_setup(self.current)
            self.transitioning = False
        elif not self.transitioning and self.current.is_done:
            self._advance_after_scene()

    def _advance_single_scene(self) -> None:
        """Single-scene mode: first setup, then loop the one scene back-to-back
        via teardown + setup on is_done (works for every scene type — webcam
        re-reads source, video re-opens the file, waveform restarts the SID) —
        or stop cleanly when loop=False."""
        if self.current is None:
            scene = self.scenes[0]
            if not self.ensemble_coord.wait_for_audio_claim(scene):
                return
            self.current = scene
            self.log.info(
                "scene %r (single-scene mode, %s)",
                self.current.name,
                "looping" if self.loop else "once-through",
            )
            self.safe_setup(self.current)
        elif self.current.is_done:
            if not self.loop:
                self.log.info("scene %r finished and loop=False — stopping", self.current.name)
                self.fades.fade_out(self.current)
                self.safe_teardown(self.current)
                self.current = None
                self.stop_event.set()
                return
            scene = self.current
            self.fades.fade_out(scene)
            self.safe_teardown(scene)
            if not self.ensemble_coord.wait_for_audio_claim(scene):
                self.current = None
                return
            self.safe_setup(scene)
            scene.is_done = False

    def _advance_after_scene(self) -> None:
        """A non-transition scene finished: fade + tear it down, then honor a
        pending jump request (hard cut or via the interstitial), or walk to
        the next index — stopping at end-of-list when loop=False."""
        assert self.current is not None
        self.fades.fade_out(self.current)
        self.safe_teardown(self.current)
        with self._jump_lock:
            jump_target = self._jump_target
            jump_skip_interstitial = self._jump_skip_interstitial
            self._jump_target = None
        if jump_target is not None:
            self.index = jump_target
            if jump_skip_interstitial:
                scene = self.scenes[self.index]
                if not self.ensemble_coord.wait_for_audio_claim(scene):
                    self.current = None
                    return
                self.log.info(
                    "scene %d/%d → %r (jump)", self.index + 1, len(self.scenes), scene.name
                )
                self.current = scene
                self.safe_setup(self.current)
                self.transitioning = False
                return
            resolved = self.ensemble_coord.resolve_next_index()
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
        resolved = self.ensemble_coord.resolve_next_index()
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
        # Before `nxt.name` is read, so a randomized scene has picked its file and
        # the "UP NEXT" card names the real upcoming content.
        self._safe_prepare_next(nxt)
        self.log.info("interstitial → %r (scene %d/%d)", nxt.name, self.index + 1, len(self.scenes))
        self.current = self.interstitial_factory(nxt.name)
        self.safe_setup(self.current)
        self.transitioning = True

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

    def safe_setup(self, scene: Scene) -> None:
        self.ensemble_coord.maybe_install_conductor(scene)
        # Before the scene renders a frame, for any `mod_source = "clock"` layer.
        scene.clock_modulation = self._clock_modulation
        scene.setup()
        # Mode instances are per-scene, so a dim set on the previous scene's mode
        # would not otherwise carry.
        if self.user_dim < 1.0:
            dm = getattr(scene, "display_mode", None)
            if dm is not None:
                dm.user_dim = self.user_dim
        # Same re-stamp, same reason. Written every lap and not only when the mode
        # is on: `suppressed` is the run's gate (the config owns `enabled`), so a
        # scene that was live while the mode was on has to have it *cleared* when
        # it comes round again. Turning the mode off reaches only the current scene.
        scene.osd.suppressed = self.performance_mode
        # The display mode starts black and ramps up over the opening live frames,
        # driven by `_advance_fade_in`.
        self.fades.begin_fade_in(scene)
        # Restores pitch under the host-DMA servo by boosting the NMI consumer rate
        # back toward 8000 Hz after video DMA bus-halts throttled it.
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
                self.log.exception("overlay %r setup failed on %r — disabling", ov.name, scene.name)
                ov.disabled = True  # checked in process_frame loop
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

    def safe_teardown(self, scene: Scene) -> None:
        for ov in getattr(scene, "overlays", ()):
            if getattr(ov, "disabled", False):
                continue
            try:
                ov.teardown(self.api, scene)
            except Exception:
                self.log.exception("overlay %r teardown failed", ov.name)
        try:
            scene.teardown()
        except Exception:
            self.log.exception("teardown of %r failed", scene.name)
        # Runs even when teardown raised, so a crashing scene cannot strand the
        # conductor slot or the ensemble audio lock.
        self.ensemble_coord.release_scene(scene)

    def _maybe_heartbeat(self, now: float) -> None:
        if self.heartbeat_interval <= 0:
            return
        if now - self._last_heartbeat < self.heartbeat_interval:
            return
        # The first call only establishes the baseline; a 0-second window is not
        # worth emitting.
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
        # WARNING while errors are flowing, so a failing link stands out from
        # the routine heartbeat.
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
        frame_time = self.frame_time_for(scene)
        now = time.time()
        if now < next_deadline:
            self.stop_event.wait(timeout=next_deadline - now)
        return max(next_deadline + frame_time, time.time())

    def run_one_frame(self, scene: Scene, next_deadline: float) -> float:
        """Render one frame of `scene`. Returns the new next_deadline.

        Extracted from the inner loop of run() so the broadcast-interrupt
        path can drive a follower scene through the same render +
        overlay + heartbeat + frame-drop machinery without duplicating
        any of it. Skip/cycle events are honored against this scene
        (consistent with `self.current` being the active scene)."""
        frame_time = self.frame_time_for(scene)
        with self.profiler.frame(scene.name):
            t0 = time.time()
            # A slow DMA push can put t0 past the deadline; natural pacing absorbs
            # that, and the catch-up below absorbs the rest.
            if t0 < next_deadline:
                with self.profiler.stage("wait"):
                    self.stop_event.wait(timeout=next_deadline - t0)
                t0 = time.time()

            stats_before = self.api.stats

            # Before the scene composes, so the opening frames render
            # progressively brighter over live playback.
            self.fades.advance_fade_in(scene)
            # Before the scene renders, so a seek issued this tick is reflected in
            # the frame about to be composed.
            self.transport.tick(self, t0)
            if self._tempo_audio_drive:
                self._drive_tempo_from_audio(scene, t0)

            still_active = self._render_scene_frame(scene, t0)

            stats_after = self.api.stats
            self.profiler.record_counts(
                writes=stats_after["writes"] - stats_before["writes"],
                bytes_=stats_after["bytes"] - stats_before["bytes"],
            )

            self._apply_frame_events(scene, still_active)

            self._maybe_heartbeat(t0)
            if self.profiler.emit_if_due(t0, self.log):
                # Same cadence as the profiler, so per-DMA-write latency says
                # whether `cpu_render` is CPU work or a producer blocked on the
                # network.
                latency_line = self.api.format_write_latency()
                if latency_line is not None:
                    self.log.info(latency_line)

        return self._advance_deadline(scene, next_deadline, frame_time)

    def _render_scene_frame(self, scene: Scene, t0: float) -> bool:
        """Render one frame of `scene` plus its direct-write overlays, under
        the cpu_render profiler stage. Returns the scene's still-active flag
        (False also when process_frame raised — a crashing scene advances).

        Overlays with PAINTS_INTO_BUFFERS are skipped here: they were already
        composed into the scene's screen+color buffers during
        scene.process_frame — calling process_frame again would race the
        scene write."""
        with self.profiler.stage("cpu_render"):
            try:
                still_active = scene.process_frame(t0)
            except Exception:
                self.log.exception("scene %r raised; advancing", scene.name)
                still_active = False
            for ov in getattr(scene, "overlays", ()):
                if getattr(ov, "disabled", False):
                    continue
                if getattr(ov, "PAINTS_INTO_BUFFERS", False):
                    continue
                try:
                    ov.process_frame(self.api, scene, t0)
                except Exception:
                    self.log.exception("overlay %r raised on %r — disabling", ov.name, scene.name)
                    ov.disabled = True
        return still_active

    def _apply_frame_events(self, scene: Scene, still_active: bool) -> None:
        """Resolve the scene's is_done for this frame, then honor the skip and
        cycle events against it.

        is_done defers while any overlay reports busy (e.g. BigText with an
        unfinished scroll-off) — but a CTRL skip still wins, forcing is_done
        regardless. The skip apply is race-free because it runs *after* the
        is_done = not still_active assignment. Cycle is ignored during an
        interstitial transition — cycling the interstitial mid-flight would be
        confusing and it doesn't implement cycle_style anyway."""
        scene.is_done = not still_active
        if scene.is_done and any(
            not getattr(ov, "disabled", False) and ov.is_busy()
            for ov in getattr(scene, "overlays", ())
        ):
            scene.is_done = False
        if self.skip_event.is_set():
            if self.single_scene:
                self.log.debug("skip ignored — single-scene mode")
            else:
                self.log.info("skip requested — advancing past %r", scene.name)
                scene.is_done = True
                # A skip means "next scene now": abort the fade-in, suppress the
                # fade-out.
                self.fades.cancel_fade_in(scene)
                self.fades.ended_via_skip = True
            self.skip_event.clear()
        if self.cycle_event.is_set():
            if not self.transitioning:
                self._handle_cycle()
            self.cycle_event.clear()

    def _advance_deadline(self, scene: Scene, next_deadline: float, frame_time: float) -> float:
        """Advance the pace deadline one frame; if we fell more than 2 frames
        behind, snap it forward (drop frames) so we don't burst to catch up.
        A large snap (seek catch-up / stream rebuffer) abnormally loads the
        bus, so the audio loop is told to hold its NMI rate steady through it
        instead of chasing the transient and gliding the pitch."""
        next_deadline += frame_time
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
                if self.audio is not None and dropped * frame_time >= _AUDIO_DISTURBANCE_DROP_S:
                    self.audio.note_playback_disturbance()
        return next_deadline

    def _drive_tempo_from_audio(self, scene: Scene, now: float) -> None:
        """Forward the current scene's analyzer BPM into the process-wide beat
        grid (`tempo_source = "audio"`). The live-input analyzer already runs a
        full TempoEstimator over its onsets, so we mirror that BPM into
        `self.tempo`; the grid integrates its own phase from it (see
        TempoClock.audio_drive). A scene with no feature stream (or an unlocked
        analyzer, bpm == 0) freezes the grid — no phantom tempo on a non-audio
        scene. Called every frame on the playlist thread; cheap in-memory work."""
        feats = scene.features()
        self.tempo.audio_drive(feats.bpm if feats is not None else 0.0, now)
        # Lock/loss transitions at INFO, the live BPM at DEBUG every ~2 s while
        # locked, so -v shows the grid tracking the beat.
        locked = self.tempo.running
        if locked != self._tempo_audio_locked:
            self._tempo_audio_locked = locked
            if locked:
                self.log.info("tempo: audio beat locked — %.1f BPM", self.tempo.bpm)
            else:
                self.log.info("tempo: audio beat lost — grid idle")
            self._tempo_audio_log_t = now
        elif locked and now - self._tempo_audio_log_t >= 2.0:
            self.log.debug(
                "tempo: audio beat %.1f BPM, phase %.2f", self.tempo.bpm, self.tempo.beat_phase
            )
            self._tempo_audio_log_t = now

    def _active_features(self) -> MusicModulation | None:
        """The currently-playing scene's live music features (None when there's
        no scene, or the scene has no music source). The WLED broadcaster polls
        this from its own thread — Scene.features() reads are self-synchronized
        (SID scenes take their own lock), so no extra locking is needed here.

        With `[wled].broadcast_tempo_fallback` on, a scene that reports no
        features (video/webcam/slideshow) falls back to the beat grid's
        `ClockModulationSource` while the tempo clock is running, so WLED keeps
        pulsing to the MIDI/tap tempo on non-SID scenes (Live DJ/VJ Phase 6). A
        SID-driven scene always wins — the fallback only fills a `None`."""
        scene = self.current
        feats = scene.features() if scene is not None else None
        if feats is None and self._wled_tempo_fallback and self.tempo.running:
            return self._clock_modulation.features()
        return feats

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
                    # The extra buffer read only happens when the menu is enabled.
                    menu_event=self.menu_event if menu_enabled else None,
                    menu_active=self.menu_active if menu_enabled else None,
                    menu_eligible=self.menu_eligible if menu_enabled else None,
                    nav_queue=self.nav_queue if menu_enabled else None,
                )
        # Deadline-based pacing: each frame advances the deadline by one
        # `frame_time`, and a wall clock far behind it snaps the deadline forward,
        # dropping the missed frames rather than compounding lag.
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
                if self.broadcast_interrupt is not None and self.broadcast_interrupt.is_set():
                    self.ensemble_coord.handle_broadcast_interrupt()
                    next_deadline = time.time()
                    if self.stop_event.is_set():
                        break

                try:
                    self._advance()
                except Exception:
                    self.log.exception("playlist advance failed; aborting")
                    break
                # loop=False end-of-playlist: `_advance` has torn down the last
                # scene, cleared `current` and set `stop_event`.
                if self.current is None:
                    break

                self.menu.service()
                # Holding the last frame stops the post-render panel flickering
                # against a scene that redraws every tick; a menu interaction sets
                # `menu.repaint`, so the live preview still updates.
                if self.menu_active.is_set() and not self.menu.repaint:
                    next_deadline = self._idle_pace(self.current, next_deadline)
                    continue
                self.menu.repaint = False
                next_deadline = self.run_one_frame(self.current, next_deadline)
        except KeyboardInterrupt:
            self.log.info("interrupted")
        finally:
            if self._wled is not None:
                self._wled.stop()
            for controller in (self.key_poller, self.vision_controller):
                if controller is not None:
                    controller.stop()
            if self.current is not None:
                self.safe_teardown(self.current)

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
            if getattr(ov, "disabled", False):
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
            self.safe_teardown(self.current)
            self.current = None
        # Before idling, not after: the poller can set `resume_event` the moment
        # it sees a 3 s C= hold, which can land *during* a slow `pause_idle`, and
        # clearing afterwards would wipe a legitimate resume and strand the pause.
        self.resume_event.clear()
        try:
            # Leaves the kernal keyboard scan alive so $028D keeps updating for the
            # resume-hold detection. Backend-specific: the Ultimate resets to the
            # BASIC READY banner; the TeensyROM clears the screen but keeps the
            # display ON, since a bare reset lands at the TR menu and freezes $028D,
            # and blanking would remove the VIC badlines its cycle-clean DMA needs.
            self.api.pause_idle()
        except Exception:
            self.log.exception("pause_idle failed")

        # `stop_event.wait`, so SIGTERM can shortcut the pause.
        while not self.stop_event.is_set() and not self.resume_event.is_set():
            self.stop_event.wait(timeout=0.1)
        if self.stop_event.is_set():
            return

        self.log.info("resuming — reset + run clear loop")
        try:
            self.api.reset()
            self.stop_event.wait(1.0)
            self.api.run_basic_clear_loop()
            self.api.disable_case_switch()
        except Exception:
            self.log.exception("reset/clear during resume failed")
        self.pause_event.clear()
        self.resume_event.clear()
