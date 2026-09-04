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
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
        also skip the next scene) or a stop request. No-op when fades are off,
        the scene ended via skip, the mode can't compose, or nothing was
        rendered yet."""
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
            try:
                dm.repush_faded(pl.api, 1.0 - i / n)
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
        # While the menu is open the background is frozen (not re-rendered
        # every frame) so the post-render panel can't flicker against a
        # per-frame scene redraw. This flag requests a one-shot re-render on
        # open / nav / value-change so the live preview still updates. See
        # service() + the freeze gate in Playlist.run().
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

        # Publish eligibility to the poller every frame: only an eligible scene
        # lets it drain/clear the keyboard buffer (so SPACE-to-open is inert,
        # and $00C6 untouched, on launcher/waveform/midi scenes).
        if can_show_menu(scene):
            pl.menu_eligible.set()
        else:
            pl.menu_eligible.clear()
        # Defensive: if the scene changed out from under an open menu (reload,
        # broadcast), drop the menu state cleanly.
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
        # Reclaim the panel cells: the scene's delta cache is unaware the menu
        # overwrote them, so force a full repaint on the next frame.
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
            # The running Config was built on the machine-settings layer, so
            # that is what "unset" means for it — dumping against the dataclass
            # defaults would write this machine's settings into the show file.
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
        # Skip if the scene is already wired with an orchestrator —
        # handle_broadcast_interrupt stamps follower scenes before
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
        if pl.ensemble is not None and scene.__dict__.get("_audio_lock_held", False):
            pl.ensemble.release_audio(pl.name)
            scene.__dict__["_audio_lock_held"] = False

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
            # Stale event (orchestrator ended between set and our
            # observation). Drop the interrupt and let the run loop
            # continue normally.
            return
        if pl.build_follower_scene is None:
            pl.log.error(
                "broadcast interrupt arrived but no follower scene factory wired; ignoring"
            )
            return
        orch = pl.ensemble.active_orchestrator

        # Force-resume if paused. The pause_event was set by the keyboard
        # poller; we clear it + set resume_event so any concurrent
        # _handle_pause loop exits cleanly. Per the design, paused
        # systems get woken by a broadcast and are left un-paused after
        # (matches user expectation: emergency broadcast overrides pause).
        if pl.pause_event.is_set():
            pl.log.info("broadcast: force-resuming paused playlist")
            pl.pause_event.clear()
            pl.resume_event.set()

        # Save scene index; tear down the current scene cleanly so its
        # overlays release threads/network state. The follower scene
        # runs in its place until the orchestrator releases us.
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
        # Stamp orchestrator + role + this system's index in the
        # ensemble (left-to-right) onto the scene so overlays that
        # participate in the broadcast (e.g. big_text) can find them in
        # their setup(). Followers are not conductors; the index is
        # used by span-mode orchestrators to compute each follower's
        # slice of the global content.
        follower_scene.bind_orchestrator(
            orch, conductor=False, index=pl.ensemble.system_names().index(pl.name)
        )
        pl.safe_setup(follower_scene)
        pl.current = follower_scene

        pl.log.info("broadcast: follower scene %r running until resume", follower_scene.name)

        # Spin frames until the orchestrator releases us or stop fires.
        next_deadline = time.time()
        while not pl.broadcast_resume.is_set() and not pl.stop_event.is_set():
            next_deadline = pl.run_one_frame(follower_scene, next_deadline)
        pl.broadcast_resume.clear()

        pl.log.info(
            "broadcast: resume — tearing down follower, restoring scene index %d", saved_idx
        )
        pl.safe_teardown(follower_scene)
        pl.current = None
        # Defensive: _advance() reads playlist.index on the next iteration
        # and re-sets-up the scene at that index from scratch. We didn't
        # touch the index during the broadcast, but pin it anyway in
        # case some future code path mutates it mid-flight.
        pl.index = saved_idx
