"""Playlist collaborators: scene fades, the on-C64 menu driver, and ensemble
coordination.

Each class holds a back-reference to its Playlist — they are extensions of
the playlist state machine, split out (2026-08) so `playlist.py` keeps one
job: the scene walk + frame loop. The split moved method bodies verbatim;
behavior, log lines and event semantics are unchanged.

* ``SceneFades`` — the fade-in ramp / fade-out dim between scenes.
* ``PlaylistMenu`` — SPACE-key on-C64 menu: open/close, nav forwarding, the
  config save-back flow.
* ``EnsembleCoordinator`` — everything multi-system: audio-slot gating,
  conductor install/release, and the broadcast-follower interlude.
* ``MachineRestartWatch`` — tells a machine that restarted under a running
  scene from a link that only dropped out.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from c64cast.hw.backend import C64Backend, LinkError
from c64cast.hw.delivery import write_confirmed

if TYPE_CHECKING:
    from c64cast.control.keyboard import CommodoreKeyPoller
    from c64cast.scenes.scenes import Scene
    from c64cast.video.modes import DisplayMode

    from .playlist import Playlist


def _preserve_original(config_path: str, backup: str) -> str:
    """Copy `config_path` to `backup` unless `backup` already exists, and
    describe what happened for the log line.

    The one-shot semantics are the point — see `PlaylistMenu.save_config`.

    The "already there" wording is deliberately about the *file*, not its
    provenance: this cannot know whether an existing `.bak` is the pristine
    original, an earlier version's save output, or something the operator put
    there. Claiming "the original is preserved" would be the same unearned
    reassurance the old unconditional-copy log line gave. Saying what it did —
    left the existing file alone — is checkable and true either way.

    Imports locally to keep this module's import cost where the callers put
    it."""
    import os  # noqa: PLC0415  (lazy; matches save_config's own imports)
    import shutil  # noqa: PLC0415

    if not os.path.exists(config_path):
        return "no original to preserve (new file)"
    if os.path.exists(backup):
        return f"kept the existing {backup} — not overwritten"
    shutil.copy2(config_path, backup)
    return f"original preserved at {backup}"


class SceneFades:
    """Scene fade transitions. duration_s <= 0 disables (hard cuts).

    Fade-in overlaps the opening live frames (the display mode's fade_alpha
    ramps 0→1 as frames render); fade-out freezes the last composed frame
    and dims it to black before teardown on a NORMAL end. A CTRL skip
    cancels both (see the skip branch in Playlist.run_one_frame and the
    ended_via_skip guard in fade_out)."""

    def __init__(self, playlist: Playlist, *, duration_s: float) -> None:
        self._pl = playlist
        self.duration_s = duration_s
        self.fade_in_remaining = 0
        self.fade_in_total = 0
        self.ended_via_skip = False

    def fade_frames(self, scene: Scene) -> int:
        """How many frames a fade spans for `scene` at its current frame rate.
        0 when fades are disabled (duration_s <= 0)."""
        if self.duration_s <= 0:
            return 0
        return max(1, round(self.duration_s / self._pl.frame_time_for(scene)))

    def fade_mode(self, scene: Scene) -> DisplayMode | None:
        """The compose-based display mode the fade can drive, or None. Non-compose
        scenes (waveform/midi oscilloscope, native launcher) and scenes without a
        display mode are left untouched."""
        dm: DisplayMode | None = getattr(scene, "display_mode", None)
        if dm is not None and getattr(dm, "supports_compose", False):
            return dm
        return None

    def begin_fade_in(self, scene: Scene) -> None:
        """Arm a fade-in for `scene`: start its display mode fully black and let
        Playlist.run_one_frame ramp fade_alpha 0→1 over the opening live frames.
        No-op (and clears any stale fade) when fades are off or unsupported."""
        self.ended_via_skip = False
        self.fade_in_remaining = 0
        dm = self.fade_mode(scene)
        if dm is None:
            return
        n = self.fade_frames(scene)
        if n <= 0:
            dm.fade_alpha = 1.0
            return
        dm.fade_alpha = 0.0
        self.fade_in_remaining = n
        self.fade_in_total = n

    def advance_fade_in(self, scene: Scene) -> None:
        """Step the fade-in ramp one frame, before the scene composes. Called at
        the top of each rendered frame so the dimming overlaps live playback."""
        if self.fade_in_remaining <= 0:
            return
        dm = getattr(scene, "display_mode", None)
        if dm is None:
            self.fade_in_remaining = 0
            return
        done = self.fade_in_total - self.fade_in_remaining + 1
        dm.fade_alpha = min(1.0, done / self.fade_in_total)
        self.fade_in_remaining -= 1

    def cancel_fade_in(self, scene: Scene) -> None:
        """Snap to full brightness and stop the fade-in ramp (CTRL skip)."""
        self.fade_in_remaining = 0
        dm = getattr(scene, "display_mode", None)
        if dm is not None:
            dm.fade_alpha = 1.0

    def fade_out(self, scene: Scene) -> None:
        """Freeze the scene's last composed frame and dim it to black over the
        fade window, then leave the mode at full brightness for the next scene.
        Aborts immediately on a CTRL skip (consuming the event so it doesn't
        also skip the next scene), a stop request, or a dead link (a
        `LinkError`, which goes to the playlist's outage log). No-op when
        fades are off, the scene ended via skip, the mode can't compose, or
        nothing was rendered yet."""
        pl = self._pl
        if self.ended_via_skip:
            return
        dm = self.fade_mode(scene)
        if dm is None or dm.last_buffers is None:
            return
        n = self.fade_frames(scene)
        if n <= 0:
            return
        frame_time = pl.frame_time_for(scene)
        for i in range(1, n + 1):
            if pl.stop_event.is_set():
                break
            if pl.skip_event.is_set():
                pl.skip_event.clear()  # satisfied by ending the fade early
                break
            push_start = pl.link_outage.now()
            try:
                dm.repush_faded(pl.api, 1.0 - i / n)
            except LinkError as e:
                # A dead link, not a defect: reported through the playlist's
                # throttled outage log rather than as a traceback per scene end.
                pl.link_outage.failed(
                    f"fade-out of {scene.name!r}",
                    e,
                    pl.api.stats["writes"],
                    started=push_start,
                    frame_time=frame_time,
                )
                break
            except Exception:
                pl.log.exception("fade-out push failed on %r — ending fade", scene.name)
                break
            pl.stop_event.wait(timeout=frame_time)
        dm.fade_alpha = 1.0


class PlaylistMenu:
    """The SPACE-key on-C64 menu: open/close + nav forwarding + save-back.

    The menu Events (menu_event / menu_active / menu_eligible / nav_queue)
    stay on the Playlist — they are the poller's contract — while the
    overlay lifecycle and the save flow live here."""

    def __init__(self, playlist: Playlist) -> None:
        self._pl = playlist
        self.overlay: object | None = None
        # The background is frozen while the menu is open, so the panel cannot
        # flicker against a per-frame redraw; this requests the one-shot
        # re-render that keeps the live preview current. See `service()` and the
        # freeze gate in `Playlist.run()`.
        self.repaint = False

    def service(self) -> None:
        """Open/close the on-C64 menu on SPACE (menu_event) and forward nav
        keys to an open menu. Called each loop iteration before the frame
        renders, so a value change previews on the same frame."""
        pl = self._pl
        scene = pl.current
        if scene is None:
            pl.menu_eligible.clear()
            return
        if pl.menu_cfg is None or not getattr(pl.menu_cfg, "enabled", False):
            return
        from c64cast.scenes.overlays.menu import can_show_menu

        # Only an eligible scene lets the poller drain the keyboard buffer, so
        # SPACE is inert and $00C6 untouched on launcher/waveform/midi scenes.
        if can_show_menu(scene):
            pl.menu_eligible.set()
        else:
            pl.menu_eligible.clear()
        # The scene can change out from under an open menu (reload, broadcast).
        if self.overlay is not None and self.overlay not in getattr(scene, "overlays", ()):
            self.overlay = None
            pl.menu_active.clear()
        if pl.menu_event.is_set():
            pl.menu_event.clear()
            if self.overlay is None:
                self.open()
            elif self.overlay.on_toggle():  # type: ignore[attr-defined]
                self.close()
            self.repaint = True  # open / close / confirm changed the view
        if self.overlay is not None:
            while pl.nav_queue:
                try:
                    code = pl.nav_queue.popleft()
                except IndexError:
                    break
                self.overlay.on_key(code)  # type: ignore[attr-defined]
                self.repaint = True  # nav / value change → preview update
            if self.overlay.closed:  # type: ignore[attr-defined]
                self.close()
                self.repaint = True

    def can_save(self) -> bool:
        """Save-back is available only when we know the source TOML path and
        have the in-memory Config (single-system or a per-system ensemble
        config; the serializer rejects an ensemble master)."""
        pl = self._pl
        return pl.config is not None and bool(pl.config_path)

    def open(self) -> None:
        from c64cast.scenes.overlays.menu import MenuOverlay, can_show_menu

        pl = self._pl
        scene = pl.current
        if scene is None or not can_show_menu(scene):
            pl.log.info("menu: not available for this scene")
            return
        overlay = MenuOverlay(
            scene,
            pl.api,
            can_save=self.can_save(),
            prompt_to_save=bool(getattr(pl.menu_cfg, "prompt_to_save", True)),
            save_fn=self.save_config,
            logger=pl.log,
        )
        scene.overlays = list(getattr(scene, "overlays", [])) + [overlay]
        self.overlay = overlay
        pl.menu_active.set()
        pl.nav_queue.clear()  # drop any keys queued before the menu opened
        pl.api.invalidate_cache()  # full repaint so the panel composites cleanly
        pl.log.info("menu: opened (%d options)", len(overlay.items))

    def close(self) -> None:
        pl = self._pl
        scene = pl.current
        if scene is not None and self.overlay is not None:
            with contextlib.suppress(ValueError, AttributeError):
                scene.overlays.remove(self.overlay)  # type: ignore[arg-type]
        self.overlay = None
        pl.menu_active.clear()
        # The scene's delta cache is unaware the menu overwrote these cells.
        pl.api.invalidate_cache()
        pl.log.info("menu: closed")

    def save_config(self) -> bool:
        """Write the (menu-mutated) Config back to its source path, preserving
        the hand-written original as a one-time .bak. Returns True on success.

        The .bak is written **only when it does not already exist**, because
        "the original" is what it is for and a second save would otherwise
        overwrite it with the first save's output. That is not a hypothetical:
        `session.save_live_tune_changes` calls this on every normal exit and
        Ctrl+C when live-tune changes exist — automatically under
        `--overwrite` — so two runs of a tuned show used to leave no pristine
        copy at all, while the log line said "(backup .bak)" and read as
        reassurance. Losing the ability to undo just the *last* save is the
        cheaper loss: the file worth keeping is the one nothing generated."""
        from . import config as cfgmod
        from . import config_serialize

        pl = self._pl
        if pl.config is None or not pl.config_path:
            return False
        backup = pl.config_path + ".bak"
        try:
            note = _preserve_original(pl.config_path, backup)
            # The running Config was built on the machine-settings layer, so that
            # is what "unset" means for it; dumping against the dataclass defaults
            # would write this machine's settings into the show file.
            config_serialize.dump(pl.config, pl.config_path, baseline=cfgmod.machine_baseline())
            pl.log.info("menu: saved config → %s (%s)", pl.config_path, note)
            return True
        except Exception:
            pl.log.exception("menu: failed to save config")
            return False


class EnsembleCoordinator:
    """Everything multi-system: the ensemble audio-slot gate, conductor
    install/release, and the broadcast-follower interlude. Every method is a
    fast no-op / pass-through in single-system mode (playlist.ensemble is
    None), so the Playlist calls in unguarded."""

    def __init__(self, playlist: Playlist) -> None:
        self._pl = playlist

    def wait_for_audio_claim(self, scene: Scene) -> bool:
        """If the playlist is part of an ensemble and `scene` actually
        contends for audio (`competes_for_audio_lock()`), block until we
        hold the ensemble's audio slot — or return False if stop_event
        fires first. Stamps the scene with `_audio_lock_held = True` on
        success so the matching release_scene() releases. Always
        returns True for non-ensemble runs or scenes that don't
        compete for audio (including a muted video).

        Used by single-scene mode (which can't skip itself, so the
        only sensible option is to wait). Multi-scene playlists use
        `resolve_next_index` instead — that one skips past gated
        scenes to a runnable one before falling back to wait."""
        pl = self._pl
        if pl.ensemble is None or not scene.competes_for_audio_lock():
            return True
        poll_interval = 0.1
        first_wait = True
        while not pl.stop_event.is_set():
            if pl.ensemble.try_claim_audio(pl.name):
                scene.__dict__["_audio_lock_held"] = True
                return True
            if first_wait:
                pl.log.info(
                    "audio-bearing scene %r waiting — slot held by %s",
                    scene.name,
                    pl.ensemble.audio_holder,
                )
                first_wait = False
            pl.stop_event.wait(timeout=poll_interval)
        return False

    def resolve_next_index(self) -> int | None:
        """Walk forward from playlist.index in ensemble mode to find the
        next scene we can actually run. Scenes that actually contend for
        audio (`competes_for_audio_lock()`) whose lock is held by another
        system are skipped; a muted video passes through like any
        non-audio scene. If every scene is gated,
        blocks (stop_event-aware) until the lock frees and a candidate
        becomes claimable. Returns the resolved index, or None only if
        stop_event fires while waiting.

        Side effect: on a successful audio-bearing claim, marks the
        chosen scene so its eventual release releases the slot.

        In single-system mode (ensemble is None) returns playlist.index
        directly — no gating possible."""
        pl = self._pl
        if pl.ensemble is None:
            return pl.index
        n = len(pl.scenes)
        poll_interval = 0.1
        first_full_wait = True
        while not pl.stop_event.is_set():
            first_pass_log = first_full_wait
            for offset in range(n):
                idx = (pl.index + offset) % n
                scene = pl.scenes[idx]
                if not scene.competes_for_audio_lock():
                    return idx
                if pl.ensemble.try_claim_audio(pl.name):
                    scene.__dict__["_audio_lock_held"] = True
                    return idx
                if first_pass_log:
                    pl.log.info(
                        "skipping audio-bearing %r — slot held by %s",
                        scene.name,
                        pl.ensemble.audio_holder,
                    )
            if first_full_wait:
                pl.log.info("all scenes audio-gated; waiting for ensemble audio slot to free")
                first_full_wait = False
            pl.stop_event.wait(timeout=poll_interval)
        return None

    def maybe_install_conductor(self, scene: Scene) -> None:
        """If this scene's SceneCfg has `orchestrate = true` AND we're
        running in ensemble mode, resolve the right Orchestrator
        subclass, instantiate it, and stamp the scene so overlays can
        find it. The overlay (e.g. big_text) is what actually calls
        orch.begin() to fire the follower interrupts — we just put the
        orchestrator in place + set the ensemble's active slot."""
        pl = self._pl
        if pl.ensemble is None:
            return
        # `handle_broadcast_interrupt` stamps follower scenes before calling
        # here, and a follower's fallback cfg can be the conductor's own
        # orchestrate=true cfg — so an already-wired scene keeps its role.
        if scene.__dict__.get("_orchestrator") is not None:
            return
        cfg = scene.__dict__.get("_cfg")
        if cfg is None or not getattr(cfg, "orchestrate", False):
            return
        try:
            from .orchestrator import resolve_orchestrator

            orch_cls = resolve_orchestrator(cfg)
        except Exception:
            pl.log.exception(
                "orchestrate=true on scene %r: could not "
                "resolve orchestrator subclass; running "
                "scene as local-only",
                scene.name,
            )
            return
        orch = orch_cls(pl.ensemble, pl.name)
        pl.ensemble.active_orchestrator = orch
        scene.bind_orchestrator(
            orch, conductor=True, index=pl.ensemble.system_names().index(pl.name)
        )

    def release_scene(self, scene: Scene) -> None:
        """The teardown-side counterpart: clear the ensemble's active-
        orchestrator slot if this was a conductor scene, and release the
        ensemble audio lock if the scene held it. Runs even when the scene's
        own teardown raised — a crashing VideoScene must not strand the slot.

        The per-scene conductor stamps are cleared too: the same Scene
        instance is reused across loop iterations, and a stale _orchestrator
        would make maybe_install_conductor short-circuit on the next setup,
        leaving ensemble.active_orchestrator unset — followers would then
        drop the broadcast interrupt as "no active orch". The _audio_lock_held
        flag is reset so a subsequent re-setup (single-scene loop) re-resolves
        the claim rather than thinking it still holds the previous one."""
        pl = self._pl
        if pl.ensemble is not None and scene.__dict__.get("_is_conductor", False):
            pl.ensemble.active_orchestrator = None
            scene.clear_orchestrator()
        self.release_audio_claim(scene)

    def audio_claimant(self, scene: Scene, announcing: Scene | None) -> Scene | None:
        """The scene this playlist holds the ensemble audio slot for while
        `scene` is set up: `scene` itself, or, when `scene` is the "UP NEXT"
        card, the upcoming scene it is `announcing`, which
        `resolve_next_index` claimed the slot for. None when it holds no
        slot. A slot some other scene still holds is not `scene`'s to
        release: a broadcast follower or a launched clip that replaced a
        card would otherwise wait to claim it back for a scene it is not."""
        if self._pl.ensemble is None:
            return None
        for candidate in (scene, announcing):
            if candidate is not None and candidate.__dict__.get("_audio_lock_held", False):
                return candidate
        return None

    def release_audio_claim(self, scene: Scene) -> bool:
        """Release the ensemble audio slot if `scene` holds it, and say
        whether it did. `wait_for_audio_claim` takes it back."""
        pl = self._pl
        if pl.ensemble is None or not scene.__dict__.get("_audio_lock_held", False):
            return False
        pl.ensemble.release_audio(pl.name)
        scene.__dict__["_audio_lock_held"] = False
        return True

    def handle_broadcast_interrupt(self) -> None:
        """Save current scene state, swap in a follower scene driven by
        the ensemble's active orchestrator, run frames until the
        orchestrator releases us, then restore the saved scene index.

        Called from the run loop when `_broadcast_interrupt` is set
        (only happens in ensemble mode where the orchestrator wired the
        events). The actual orchestrator subclass + its protocol live
        in c64cast/app/orchestrator.py + subclasses."""
        pl = self._pl
        assert pl.broadcast_interrupt is not None
        assert pl.broadcast_resume is not None
        pl.broadcast_interrupt.clear()
        if pl.ensemble is None or pl.ensemble.active_orchestrator is None:
            # Stale event: the orchestrator ended between set and observation.
            return
        if pl.build_follower_scene is None:
            pl.log.error(
                "broadcast interrupt arrived but no follower scene factory wired; ignoring"
            )
            return
        orch = pl.ensemble.active_orchestrator

        # A broadcast overrides a pause, and leaves the system un-paused after:
        # clearing pause_event and setting resume_event lets any concurrent
        # `_handle_pause` loop exit cleanly.
        if pl.pause_event.is_set():
            pl.log.info("broadcast: force-resuming paused playlist")
            pl.pause_event.clear()
            pl.resume_event.set()

        # Torn down cleanly so its overlays release threads and network state;
        # the follower scene runs in its place until the orchestrator releases us.
        saved_idx = pl.index
        if pl.current is not None:
            pl.safe_teardown(pl.current)
            pl.current = None

        follower_cfg = orch.follower_scene_cfg_for(pl.name)
        try:
            follower_scene = pl.build_follower_scene(follower_cfg)
        except Exception:
            pl.log.exception("broadcast: follower scene build failed; skipping interrupt")
            return
        # Overlays that participate in the broadcast (big_text) read the
        # orchestrator, role and left-to-right ensemble index in their setup();
        # span-mode orchestrators use the index to pick each follower's slice.
        follower_scene.bind_orchestrator(
            orch, conductor=False, index=pl.ensemble.system_names().index(pl.name)
        )
        pl.safe_setup(follower_scene)
        pl.current = follower_scene

        pl.log.info("broadcast: follower scene %r running until resume", follower_scene.name)

        next_deadline = time.time()
        while not pl.broadcast_resume.is_set() and not pl.stop_event.is_set():
            next_deadline = pl.run_one_frame(follower_scene, next_deadline)
        pl.broadcast_resume.clear()

        pl.log.info(
            "broadcast: resume — tearing down follower, restoring scene index %d", saved_idx
        )
        pl.safe_teardown(follower_scene)
        pl.current = None
        # `_advance()` re-sets-up the scene at `playlist.index` on the next
        # iteration, so the broadcast's exit pins it back.
        pl.index = saved_idx


# Eight bytes the KERNAL leaves unused and its RAMTAS zeroes on every reset,
# power-on included. The rest of page 3 holds the vectors a restart rewrites
# anyway, and RAM from $0400 up survives a reset's memory test.
RESTART_SENTINEL_ADDR = 0x0334
RESTART_SENTINEL_LEN = 8
# The fewest seconds between two sentinel reads while the link keeps changing.
RESTART_CHECK_MIN_S = 2.0
# Restarts in one scene with the nonce never read back in between, after
# which the watch stands down: a reset the machine really took re-arms to a
# readable nonce, so zeros every time are something storing zeros there.
RESTART_UNCONFIRMED_LIMIT = 2


class MachineRestartWatch:
    """Tells a machine that restarted under a running scene (a power blip, a
    firmware crash) from a link that only dropped out. At the socket the two
    look the same, but a restart loses everything the scene's setup put on
    the machine, and the live+volatile configuration `hw_provision` set.

    `arm()` writes a per-run nonce where a C64 reset zeroes it. After a
    frame whose writes landed, `after_frame()` reads it back over REST,
    but only once the backend's `delivery_epoch` or `link_generation` has
    moved since the last look, and at most every `RESTART_CHECK_MIN_S`: a
    machine reached on the same connection, losing nothing, has not
    restarted. A periodic read was rejected because REST polling during
    playback is what wedges the Ultimate. A read that fails is tried again
    later rather than taken as a restart.

    A C64 reset leaves the link alone (the front-panel button, a REST
    `machine:reset` from anywhere else), so the link marks never move for
    it. With `attach_poller()`, the Commodore-key poller's 10 Hz read of
    `$028D` is widened to cover the nonce, and `after_frame()` judges each
    fresh sample as well: it costs no REST request of its own, and a reset
    is seen within a poll. Only a sample from a read issued after the
    nonce was written counts, so one that predates the write is not taken
    for a cleared nonce.

    Something on the machine that stores zeros over the nonce (a tune
    clearing page 3) reads exactly like a reset, and since the scene sets up
    again after one, it would restart the scene over and over. So a restart
    counts as unconfirmed until a read finds the nonce re-written, and after
    `RESTART_UNCONFIRMED_LIMIT` unconfirmed ones in a row in the same
    scene the watch stands down until a different scene arms it.

    A restart a scene outlives, with the link down until the next setup,
    leaves no landed frame to look after, so `restarted_before_setup()`
    takes the same look before each setup attempt, unthrottled.

    A reset c64cast issues itself (a SID scene's `run_prg`) zeroes the
    nonce too, so the backend's reset listener re-arms it after the next
    frame, and after each later landed one until that write lands; a
    restart that comes after such a reset and before the re-arm leaves
    nothing to tell it from that reset. `suspend()` stands the watch down
    while a launched program owns the machine, whose RAM the nonce must
    not touch. Only a backend that reads memory and reports its
    own resets (`add_reset_listener`) is watched."""

    def __init__(
        self,
        api: C64Backend,
        log: logging.Logger,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._log = log
        self._clock = clock
        add_listener = getattr(api, "add_reset_listener", None)
        profile = getattr(api, "profile", None)
        self.enabled = getattr(profile, "supports_read", False) is True and callable(add_listener)
        self._nonce = bytes(b | 0x01 for b in os.urandom(RESTART_SENTINEL_LEN))
        self._armed = False
        self._rearm = False
        # The last re-arm the link lost, so the next waits for a landed frame.
        self._rearm_lost = False
        self._suspended = False
        self._marks = (0, 0)
        self._next_check = 0.0
        self._poller: CommodoreKeyPoller | None = None
        # The poller's read count once the nonce landed: later reads see it.
        self._poller_mark = 0
        # Restarts since a read last found the nonce in place.
        self._unconfirmed_restarts = 0
        self._stood_down = False
        # The scene the last arm was for; the unconfirmed count is per scene.
        self._scene: object | None = None
        if self.enabled:
            assert add_listener is not None
            add_listener(self._after_reset)

    def _after_reset(self) -> None:
        if not self._suspended and not self._stood_down:
            self._rearm = True

    def attach_poller(self, poller: CommodoreKeyPoller) -> None:
        """Judge the nonce from `poller`'s own reads as well."""
        if not self.enabled:
            return
        poller.watch_bytes(RESTART_SENTINEL_ADDR, RESTART_SENTINEL_LEN)
        self._poller = poller
        self._poller_mark = poller.reads_started

    def _current_marks(self) -> tuple[int, int]:
        return self._api.delivery_epoch, self._api.link_generation

    def arm(self, scene: object | None = None) -> None:
        """Write the nonce, after a setup or a reset c64cast issued. One the
        link loses leaves the watch disarmed, so a lost write is never read
        back as a restart. `scene` is the scene that set up; a different one
        from the last clears a stand-down."""
        if not self.enabled:
            return
        self._rearm = self._rearm_lost = False
        self._suspended = False
        if scene is not None and scene is not self._scene:
            self._scene = scene
            self._unconfirmed_restarts = 0
            self._stood_down = False
        if self._stood_down:
            self._armed = False
            return
        self._armed = write_confirmed(
            self._api,
            lambda: self._api.write_memory_file(f"{RESTART_SENTINEL_ADDR:04X}", self._nonce),
        )
        self._marks = self._current_marks()
        if self._poller is not None:
            self._poller_mark = self._poller.reads_started

    def after_frame(self, landed: bool) -> bool:
        """True when the machine restarted since the nonce was written.
        `landed`: the frame raised no link error and the backend's write
        count moved, so the link reaches the machine now."""
        if not self.enabled:
            return False
        if self._rearm:
            # A retry waits for a landed frame: on a link that is down, a
            # confirmed write every frame spends up to three flushes a frame.
            if self._rearm_lost and not landed:
                return False
            self.arm()
            # A re-arm the link lost is tried again after a later frame:
            # leaving it off would stop watching for the rest of the scene.
            self._rearm = self._rearm_lost = not self._armed
            return False
        if self._armed and self._poller is not None:
            sample = self._poller.watched_since(self._poller_mark)
            if sample is not None:
                self._poller_mark = sample[0]
                if self._judge(sample[1]):
                    return True
        if not self._armed or not landed:
            return False
        marks = self._current_marks()
        if marks == self._marks:
            return False
        now = self._clock()
        if now < self._next_check:
            return False
        self._next_check = now + RESTART_CHECK_MIN_S
        return self._look(marks)

    def restarted_before_setup(self) -> bool:
        """True when the machine restarted since the nonce was written,
        asked before a scene sets up, without the frame path's spacing.
        Looks only while armed, with no reset of c64cast's own pending
        re-arm, and once the link has changed since the last look."""
        if not self.enabled or not self._armed or self._rearm:
            return False
        marks = self._current_marks()
        if marks == self._marks:
            return False
        return self._look(marks)

    def suspend(self) -> None:
        """Stand the watch down until the next `arm()`: a launched program
        owns the machine, so the nonce is neither written nor read, and its
        resets are not re-armed."""
        self._armed = False
        self._rearm = self._rearm_lost = False
        self._suspended = True

    def _look(self, marks: tuple[int, int]) -> bool:
        """Read the nonce back. True, and disarmed, only when a reset
        cleared it to zeros. A read that fails or comes back the wrong
        length leaves the watch armed and `marks` unrecorded, so the next
        look tries again. Other bytes there mean something on the machine
        wrote over it, and taking that for a restart would reset the
        machine under the writer at every link change, so the watch stands
        down until the next scene arms it."""
        seen = self._api.read_memory(RESTART_SENTINEL_ADDR, RESTART_SENTINEL_LEN)
        if seen is None or len(seen) != RESTART_SENTINEL_LEN:
            return False
        self._marks = marks
        return self._judge(seen)

    def _judge(self, seen: bytes) -> bool:
        """The verdict on bytes read back from where the nonce was written,
        as `_look` describes it."""
        if len(seen) != RESTART_SENTINEL_LEN:
            return False
        if seen == self._nonce:
            self._unconfirmed_restarts = 0
            return False
        self._armed = False
        if any(seen):
            self._log.warning(
                "the restart check found $%04X-$%04X overwritten rather than cleared; "
                "not watching for a machine restart until the next scene sets up",
                RESTART_SENTINEL_ADDR,
                RESTART_SENTINEL_ADDR + RESTART_SENTINEL_LEN - 1,
            )
            return False
        self._unconfirmed_restarts += 1
        if self._unconfirmed_restarts >= RESTART_UNCONFIRMED_LIMIT:
            self._stood_down = True
            self._log.warning(
                "the restart check found $%04X-$%04X zeroed %d times with the nonce "
                "never read back in between; something on the machine stores zeros "
                "there, so not watching for a machine restart until another scene "
                "sets up",
                RESTART_SENTINEL_ADDR,
                RESTART_SENTINEL_ADDR + RESTART_SENTINEL_LEN - 1,
                self._unconfirmed_restarts,
            )
            return False
        return True
